"""会话状态图：管"这个用户这次对话走到哪一步了"。

调用执行 agent 内部具体怎么把代码写出来跑通，这张图不关心——那是
agents/ 下各执行后端自己的 agentic loop，图里只把它当一个黑盒节点。

两个节点：
  collect  把这条新消息（new_text/new_file）并进 pending_*，判断够不够跑
  execute  调用执行 agent，产出 result，清空 pending_*，记一条结构化留痕

每个 user_id 是独立的 LangGraph thread，靠 checkpointer 隔离，互不干扰。
"""

import logging
import time

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from walkie_dokie.agents.base import ExecutionAgent
from walkie_dokie.orchestrator.state import SessionState
from walkie_dokie.turn_log import TurnRecord, log_turn
from walkie_dokie.workspace import create_workspace_dir

logger = logging.getLogger(__name__)


def _collect(state: SessionState) -> dict:
    instruction = state.get("new_text") or state.get("pending_instruction")
    file = state.get("new_file") or state.get("pending_file")
    return {
        "pending_instruction": instruction,
        "pending_file": file,
        "new_text": None,
        "new_file": None,
    }


def _ready(state: SessionState) -> str:
    return "execute" if state.get("pending_instruction") else END


def build_graph(execution_agent: ExecutionAgent, checkpointer=None) -> CompiledStateGraph:
    async def _execute(state: SessionState) -> dict:
        platform = state["platform"]
        user_id = state["user_id"]
        instruction = state["pending_instruction"]
        file = state.get("pending_file")

        workdir = create_workspace_dir(platform, user_id)
        logger.info("orchestrator 派发执行 user_id=%s workdir=%s", user_id, workdir)

        started = time.monotonic()
        error: str | None = None
        result_dict: dict | None = None
        try:
            result = await execution_agent.run(
                instruction=instruction,
                input_file=file.content if file else None,
                workdir=workdir,
                input_filename=file.filename if file else None,
            )
            result_dict = {
                "reply_text": result.reply_text,
                "result_file": result.result_file,
                "result_filename": result.result_filename,
            }
        except Exception as e:
            error = str(e)
            raise
        finally:
            await log_turn(
                TurnRecord(
                    platform=platform,
                    user_id=user_id,
                    run_id=workdir.name,
                    input_text=instruction,
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
            "result": result_dict,
        }

    graph = StateGraph(SessionState)
    graph.add_node("collect", _collect)
    graph.add_node("execute", _execute)
    graph.set_entry_point("collect")
    graph.add_conditional_edges("collect", _ready, {"execute": "execute", END: END})
    graph.add_edge("execute", END)
    return graph.compile(checkpointer=checkpointer)
