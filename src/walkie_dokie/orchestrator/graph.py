"""会话状态图：管"这个用户这次对话走到哪一步了"。

调用执行 agent 内部具体怎么把代码写出来跑通，这张图不关心——那是
agents/ 下各执行后端自己的 agentic loop，图里只把它当一个黑盒节点。

流程（见 DECISION.md「orchestrator 加回一道确认环节」）：

    collect --> draft --> ask_confirm --+-- execute --> END
                  ^                     |
                  +---------------------+  （用户没确认，当补充信息，回去重新生成草稿）

collect     把这条新消息（new_text/new_file）并进 pending_*
draft       轻量 LLM 调用，把 pending_instruction 提炼成一句 task prompt 草稿
ask_confirm 用 interrupt() 暂停，把草稿发给用户，等下一条消息当回复
execute     确认通过了才跑，拿 draft_task_prompt（不是原始 pending_instruction）喂给执行 agent

10 秒防抖攒消息不归这张图管，是调用方（scripts/run_mvp.py 的 Debouncer）的事——
图只在"这一轮真的要处理了"时才被调用一次。

每个 user_id 是独立的 LangGraph thread，靠 checkpointer 隔离，互不干扰。
"""

import logging
import time

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from walkie_dokie.agents.base import ExecutionAgent
from walkie_dokie.orchestrator import memory
from walkie_dokie.orchestrator.draft import generate_draft_task_prompt
from walkie_dokie.orchestrator.state import SessionState
from walkie_dokie.turn_log import TurnRecord, log_turn
from walkie_dokie.workspace import create_workspace_dir

logger = logging.getLogger(__name__)

_CONFIRM_PREFIXES = (
    "是", "对", "确认", "没错", "可以", "行", "嗯", "好", "ok", "okay", "yes", "y",
)


def _is_confirmation(reply: str) -> bool:
    """机械判断，不是 NLU 意图分类——前缀匹配这几个词（"是的""好的呢"这类都算），
    别的一律当"还在补充信息"。"""
    return reply.strip().lower().startswith(_CONFIRM_PREFIXES)


def _collect(state: SessionState) -> dict:
    existing = state.get("pending_instruction")
    new = state.get("new_text")
    combined = f"{existing}\n{new}" if existing and new else (new or existing)
    file = state.get("new_file") or state.get("pending_file")
    return {
        "pending_instruction": combined,
        "pending_file": file,
        "new_text": None,
        "new_file": None,
    }


def _has_instruction(state: SessionState) -> str:
    return "draft" if state.get("pending_instruction") else END


async def _draft(state: SessionState) -> dict:
    file = state.get("pending_file")
    known_facts = memory.load_facts(state["platform"], state["user_id"])
    draft = await generate_draft_task_prompt(
        state["pending_instruction"], input_filename=file.filename if file else None, known_facts=known_facts
    )
    return {"draft_task_prompt": draft}


def _ask_confirm(state: SessionState) -> dict:
    reply = interrupt({"draft_task_prompt": state["draft_task_prompt"]})
    return {"new_text": reply}


def _route_confirm(state: SessionState) -> str:
    reply = state.get("new_text") or ""
    return "execute" if _is_confirmation(reply) else "collect"


def build_graph(execution_agent: ExecutionAgent, checkpointer=None) -> CompiledStateGraph:
    async def _execute(state: SessionState) -> dict:
        platform = state["platform"]
        user_id = state["user_id"]
        draft = state["draft_task_prompt"]
        task_prompt = draft["task_summary"]
        known_facts = memory.load_facts(platform, user_id)
        if known_facts:
            # 有存下来的用户信息，优先用真实值，不要为已知字段编占位符。
            facts_str = "、".join(f"{k}：{v}" for k, v in known_facts.items())
            task_prompt += f"\n\n（已知这个用户的信息——{facts_str}。涉及这些字段时用真实值，不要用占位符。）"
        if draft["missing_info"]:
            # 用户已经确认"照这个理解直接做"，缺的信息不能再让执行 agent 反过来追问，
            # 交代它自己用合理的通用占位符/默认值把任务完成。
            task_prompt += (
                "\n\n（以上是用户已确认的任务，原本缺少："
                + "、".join(draft["missing_info"])
                + "——这些信息用合理的通用占位符/默认值直接完成任务，不要再向用户提问。）"
            )
        file = state.get("pending_file")

        workdir = create_workspace_dir(platform, user_id)
        logger.info("orchestrator 派发执行 user_id=%s workdir=%s", user_id, workdir)

        started = time.monotonic()
        error: str | None = None
        result_dict: dict | None = None
        new_facts: dict = {}
        try:
            result = await execution_agent.run(
                instruction=task_prompt,
                input_file=file.content if file else None,
                workdir=workdir,
                input_filename=file.filename if file else None,
            )
            result_dict = {
                "reply_text": result.reply_text,
                "result_file": result.result_file,
                "result_filename": result.result_filename,
            }
            new_facts = await memory.extract_facts(state["pending_instruction"])
            memory.save_facts(platform, user_id, new_facts)
        except Exception as e:
            error = str(e)
            raise
        finally:
            await log_turn(
                TurnRecord(
                    platform=platform,
                    user_id=user_id,
                    run_id=workdir.name,
                    input_text=task_prompt,
                    input_filename=file.filename if file else None,
                    backend=type(execution_agent).__name__,
                    output_text=result_dict["reply_text"] if result_dict else None,
                    output_filename=result_dict["result_filename"] if result_dict else None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    success=error is None,
                    error=error,
                )
            )

        return {
            "pending_instruction": None,
            "pending_file": None,
            "draft_task_prompt": None,
            "result": result_dict,
            "new_facts": new_facts or None,
        }

    graph = StateGraph(SessionState)
    graph.add_node("collect", _collect)
    graph.add_node("draft", _draft)
    graph.add_node("ask_confirm", _ask_confirm)
    graph.add_node("execute", _execute)

    graph.set_entry_point("collect")
    graph.add_conditional_edges("collect", _has_instruction, {"draft": "draft", END: END})
    graph.add_edge("draft", "ask_confirm")
    graph.add_conditional_edges("ask_confirm", _route_confirm, {"execute": "execute", "collect": "collect"})
    graph.add_edge("execute", END)

    return graph.compile(checkpointer=checkpointer)
