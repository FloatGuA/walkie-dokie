"""可恢复的会话工作流。

LangGraph 在这里是控制平面，不是主 Agent：它只负责累积跨消息输入、暂停等待
确认、路由和调用两个明确边界的 Agent。MainAgent 负责面向用户的语义与长期
记忆候选；ExecutionAgent 只执行已经确认的文档任务契约。

    collect -+-- compact ------------------------------------------> END
             +-- main_agent -+-- reply -----------------------------> END
                             +-- ask_confirm -+-- prepare -> execute -> END
                                              +-- cancel_task ---------> END
                                              +-- collect --------...
                                              +-- judge_confirm -+-- prepare -> ...
                                                                 +-- cancel_task -> END
                                                                 +-- collect ----...

``compact`` 是投递之后由触发方单独发起的后台回合（``new_compaction_request``
旗标），把被挤出窗口的历史压成带逐字 evidence 的摘要条目，没有用户输出。

确认回复分四层判定：零歧义白名单直接执行、整句放弃直接收尾、否定词硬否决直接
重新理解，剩下的灰区才进 ``judge_confirm`` 调主 Agent。模型调用只在节点里，
路由函数保持纯函数。

``ask_memory`` 相关节点仅保留用于恢复升级前已经暂停的 checkpoint；新回合的
长期记忆在确定性证据校验通过后隐式写入，不再要求用户二次确认。

所有注册给 LangGraph 的节点和条件路由都使用 ``async def``。当前受管 Linux
环境禁止工作线程通过 asyncio 的 socketpair 唤醒事件循环；同步节点在 ainvoke
中会走线程池，函数完成后 Future 无法通知 event loop，表现为图永久卡住。全异步
节点既符合这条调用链，也避免依赖该环境有问题的跨线程唤醒路径。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from walkie_dokie.agents.base import (
    ExecutionAgent,
    ExecutionArtifact,
    ExecutionReport,
    resolve_output_file,
)
from walkie_dokie.agents.security import validate_office_artifact, validate_report_text
from walkie_dokie.artifacts import (
    display_name,
    output_artifact_reference,
    resolve_artifact_reference,
)
from walkie_dokie.main_agent.base import (
    ConfirmationContext,
    DialogueContext,
    FinalizeContext,
    MainAgent,
    MemoryOperation,
    decision_to_dict,
    task_from_dict,
)
from walkie_dokie.main_agent.memory import (
    LONG_TERM_MEMORY_COMMAND,
    MemoryRepository,
    render_memory_notice,
    render_memory_snapshot,
)
from walkie_dokie.main_agent.summarizer import Summarizer, validate_entries
from walkie_dokie.orchestrator.state import SessionState
from walkie_dokie.turn_log import TurnRecord, log_turn
from walkie_dokie.workspace import WORKSPACES_ROOT, create_workspace_dir

logger = logging.getLogger(__name__)
_MAX_RECENT_MESSAGES = 12
_MAX_RECENT_MESSAGE_CHARS = 2_000
_MAX_RECENT_TOTAL_CHARS = 12_000
_VAR_ROOT = Path(__file__).parent.parent.parent.parent / "var"
EXECUTION_METADATA_ROOT = _VAR_ROOT / "execution-metadata"
_EXECUTION_MARKER = "execution-report.json"
_EXECUTION_STARTED_MARKER = "execution-started.json"
# 放弃任务的回复是确定性系统话术，不走模型：这一步没有任何需要判断的东西，
# 而用户已经表达了“别做了”，再多调一次模型只会增加延迟和跑偏的机会。
_CANCEL_REPLY = "好的，这个任务不做了。有需要随时再发我。"
_JUDGE_TIMEOUT_SECONDS = 30
# 攒够多少条被挤出的消息才压一次（≈3 轮对话）。触发方在图外读 pending_compaction
# 做判断，所以这个常量是公开的。
COMPACTION_BATCH_SIZE = 6
# 摘要条目超过这条线就在同一节点内做二级合并，目标压回 _SUMMARY_MERGE_TARGET；
# 合并后仍超线不递归，留待下次触发。
_SUMMARY_MERGE_THRESHOLD = 20
_SUMMARY_MERGE_TARGET = 10
# 同一批 pending 连续失败到这个次数就丢弃该批：这些内容在压缩上线前本来就被
# 硬截断静默丢掉，丢批不劣于现状，但无限重试会一直烧调用。
_MAX_COMPACTION_FAILURES = 3
_COMPACT_TIMEOUT_SECONDS = 120

_CONFIRM_RE = re.compile(
    # 收紧版白名单：只收零歧义确认词，语气词（嗯/好/行/可以/对/ok）一律进灰区
    # 交 judge_confirmation 判（spec 决策 3）。
    r"^(?:是(?:的)?|确认|没错|yes|y)[\s!！。．.]*$",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    # 否定词硬否决层：命中即不得进 execute，模型无权推翻（spec 决策 2）。
    # 宁宽勿漏——“不错”被误伤只是多澄清一轮，安全方向。
    r"不|别|先|等|算了|慢|暂|停|取消|改|换|回头|以后|no|wait|cancel|hold",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(
    # 确定性放弃层：整句就是“别做了”的常见说法，直接走 cancel_task 收尾
    # （spec 决策 4）。必须排在否定层之前，否则这些词全被否定层拦去 collect，
    # 用户说“算了”反而被反问一轮。
    r"^(?:算了(?:吧)?|不做了|不用了|不弄了|取消(?:吧)?|别弄了|不要了)[\s，,。．.！!]*$"
)
_MEMORY_CONFIRM_RE = re.compile(
    r"^(?:记住|保存|确认保存|可以记住|是|yes|ok)[\s!！。．.]*$", re.IGNORECASE
)
_MEMORY_REJECT_RE = re.compile(
    r"^(?:不用记|不要记|不保存|别记|否|不用|no)[\s!！。．.]*$", re.IGNORECASE
)
_TASK_AND_MEMORY_CONFIRM_RE = re.compile(
    r"^(?:是并记住|是，?并记住|确认并记住|执行并记住)[\s!！。．.]*$"
)
_MEMORY_PERSISTENCE_CLAIM_RE = re.compile(
    r"(?:记住|记下|保存|记录)(?:了|好|下来|成功|到(?:档案|资料)里)?",
    re.IGNORECASE,
)


def _is_confirmation(reply: str) -> bool:
    """四层确认判定的第一层：零歧义白名单快路径（零延迟、不调模型）。

    只认完整、无附加条件、语义无歧义的肯定回复。语气歧义词（嗯/好的/行/
    可以/对/ok）一律不在这层放行，留给灰区的模型判定去理解上下文。
    """

    return bool(_CONFIRM_RE.fullmatch(reply.strip()))


def _is_negation(reply: str) -> bool:
    """确定性否定信号：搜索命中即硬否决，绝不进 execute。"""
    return bool(_NEGATION_RE.search(reply.strip()))


def _is_cancellation(reply: str) -> bool:
    """确定性放弃信号：整句就是“别做了”，直接收尾，不再反问。

    刻意用 fullmatch：“算了，先改个标题”带着后续诉求，不是放弃，会落到否定层
    走重新理解——判错方向也只是多问一轮，安全。
    """

    return bool(_CANCEL_RE.fullmatch(reply.strip()))


def _remove_unverified_memory_claim(
    decision, *, had_memory_candidates: bool
):
    """Never let model prose claim a memory write before control-plane commit.

    For direct identity turns with candidates, use a deterministic neutral reply;
    the validated proposal and confirmation instructions are appended later.  This
    also covers candidates rejected by the repository, which was the path that once
    told a user "我记住了" while persisting nothing.
    """

    has_claim = bool(_MEMORY_PERSISTENCE_CLAIM_RE.search(decision.user_message))
    if decision.action == "reply" and (had_memory_candidates or has_claim):
        if has_claim:
            logger.warning("移除未经落盘验证的记忆成功话术")
        return replace(decision, user_message="谢谢你告诉我。")
    if decision.action == "propose_task" and has_claim:
        logger.warning("移除任务确认话术中未经落盘验证的记忆成功声明")
        return replace(
            decision,
            user_message=f"我理解为：{decision.task.instruction}\n请回复“是”确认是否执行。",
        )
    return decision


def _history_and_pending(
    state: SessionState, user_text: str, assistant_text: str
) -> dict:
    """收束 recent_messages 窗口，并把被挤出的整条消息交给 pending_compaction。

    只有整条被移出窗口的消息才算“挤出”——窗口内消息为省 checkpoint 做的字符
    截断不算，那部分原文仍在 recent_messages 里可见。挤出的消息保留 role/content
    原样不截断：压缩节点需要完整原文才能抽出可验证的事实。
    """

    history = list(state.get("recent_messages") or [])
    history.extend(
        [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
    )
    bounded: list[dict[str, str]] = []
    total = 0
    for message in reversed(history[-_MAX_RECENT_MESSAGES:]):
        content = str(message.get("content", ""))[:_MAX_RECENT_MESSAGE_CHARS]
        remaining = _MAX_RECENT_TOTAL_CHARS - total
        if remaining <= 0:
            break
        content = content[:remaining]
        bounded.append({"role": str(message.get("role", "user")), "content": content})
        total += len(content)
    bounded.reverse()
    evicted = history[: len(history) - len(bounded)]
    return {
        "recent_messages": bounded,
        "pending_compaction": list(state.get("pending_compaction") or []) + evicted,
    }


def _execution_metadata_dir(workdir: Path) -> Path:
    """Place orchestration metadata outside the execution Agent's writable cwd."""
    resolved = workdir.resolve()
    if not resolved.is_relative_to(WORKSPACES_ROOT.resolve()):
        raise RuntimeError("execution metadata 对应的 workdir 越过 workspace 根目录")
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    return EXECUTION_METADATA_ROOT / digest


def _atomic_write_json(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary:
            json.dump(payload, temporary, ensure_ascii=False)
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _mark_execution_started(workdir: Path) -> None:
    _atomic_write_json(
        _execution_metadata_dir(workdir) / _EXECUTION_STARTED_MARKER,
        {"workdir": str(workdir.resolve())},
    )


def _execution_was_started(workdir: Path) -> bool:
    return (_execution_metadata_dir(workdir) / _EXECUTION_STARTED_MARKER).is_file()


def _write_execution_marker(workdir: Path, report: ExecutionReport) -> None:
    """Persist a completed backend report before the LangGraph step is committed."""

    marker = _execution_metadata_dir(workdir) / _EXECUTION_MARKER
    payload = {
        "summary": report.summary,
        "filenames": [item.filename for item in report.artifacts],
        "warnings": list(report.warnings),
    }
    _atomic_write_json(marker, payload)


def _load_execution_marker(workdir: Path) -> ExecutionReport | None:
    marker = _execution_metadata_dir(workdir) / _EXECUTION_MARKER
    if not marker.is_file():
        return None
    payload = json.loads(marker.read_text(encoding="utf-8"))
    artifacts = tuple(
        ExecutionArtifact(resolve_output_file(workdir, filename), filename)
        for filename in payload.get("filenames", ())
    )
    return ExecutionReport(
        summary=payload["summary"],
        artifacts=artifacts,
        warnings=tuple(payload.get("warnings", ())),
    )


def _validate_execution_report(
    workdir: Path, report: ExecutionReport
) -> ExecutionReport:
    """Re-establish trust at the plugin boundary for this exact execution cwd."""
    summary = validate_report_text(report.summary, field="summary")
    warnings = tuple(
        validate_report_text(item, field="warnings") for item in report.warnings
    )
    validated_artifacts = []
    for item in report.artifacts:
        expected = resolve_output_file(workdir, item.filename)
        if item.path.resolve() != expected:
            raise RuntimeError(
                "执行 Agent 返回了其他工作目录的 artifact："
                f"{item.path}（本轮期望 {expected}）"
            )
        validate_office_artifact(expected, role="执行产物")
        validated_artifacts.append(ExecutionArtifact(expected, item.filename))
    return ExecutionReport(summary=summary, artifacts=tuple(validated_artifacts), warnings=warnings)


def _dedupe_display_filename(existing_names: set[str], filename: str) -> str | None:
    """existing_names 已经包含所有已用过的有效文件名（filename 或 display_filename）。
    不碰撞返回 None（沿用原 filename）；碰撞则返回按到达顺序递增的去重名。
    """
    if filename not in existing_names:
        return None
    stem, dot, ext = filename.rpartition(".")
    base, suffix = (stem, f".{ext}") if dot else (filename, "")
    n = 2
    candidate = f"{base}-{n}{suffix}"
    while candidate in existing_names:
        n += 1
        candidate = f"{base}-{n}{suffix}"
    return candidate


def _merge_pending_files(existing: tuple[dict, ...], incoming: tuple[dict, ...]) -> tuple[dict, ...]:
    used = {display_name(ref) for ref in existing}
    merged = list(existing)
    for ref in incoming:
        resolve_artifact_reference(ref)
        display = _dedupe_display_filename(used, ref["filename"])
        used.add(display or ref["filename"])
        merged.append({**ref, "display_filename": display})
    return tuple(merged)


async def _collect(state: SessionState) -> dict:
    existing = state.get("pending_instruction")
    new = state.get("new_text")
    combined = f"{existing}\n{new}" if existing and new else (new or existing)
    resume_file = state.get("new_file")
    resume_files = (resume_file,) if resume_file is not None else ()
    incoming_files = state.get("new_files") or resume_files
    merged_files = _merge_pending_files(state.get("pending_files") or (), incoming_files)
    update = {
        "pending_instruction": combined,
        "pending_files": merged_files,
        "new_text": None,
        "new_file": None,
        "new_files": (),
        "current_user_text": new,
        # result/memory_changes 是单回合输出，不得泄漏到下一轮。旧代码未清理，
        # 下一轮只发文件时会再次发送上一轮结果。
        "decision": None,
        "result": None,
        "memory_changes": None,
        "memory_feedback": None,
        "execution": None,
        # 上一轮灰区判定的结论不得残留到新回合的路由里。
        "confirmation_verdict": None,
    }
    if new or incoming_files:
        # 有新用户输入的回合必须清掉可能粘滞的压缩旗标。旗标随 checkpoint 落盘，
        # 而只有 _compact 正常返回才会清它：compact 抛异常、或压缩 invoke 撞上
        # ask_confirm 的 interrupt 等待期，都会让旗标一直为 True，此后用户的真实
        # 消息会被路由劫持进 compact -> END，result 为 None——用户收到一轮空回复。
        # 压缩 invoke 本身不带新输入，走不到这里，路由优先级不受影响。
        update["new_compaction_request"] = False
    return update


async def _has_instruction(state: SessionState) -> str:
    # 压缩旗标优先于 pending_instruction：压缩 invoke 是投递之后的后台回合，不带
    # 新用户输入，绝不能顺带把残留指令再送一次主 Agent。
    if state.get("new_compaction_request"):
        return "compact"
    return "main_agent" if state.get("pending_instruction") else END


async def _route_after_decision(state: SessionState) -> str:
    logger.info(
        "意图分流 platform=%s user_id=%s trace_id=%s intent=%s action=%s route=%s",
        state["platform"],
        state["user_id"],
        state.get("trace_id"),
        state["decision"].get("intent"),
        state["decision"]["action"],
        "execution_confirmation"
        if state["decision"]["action"] == "propose_task"
        else "main_agent_reply",
    )
    if state["decision"]["action"] == "propose_task":
        return "ask_confirm"
    return "reply"


def _memory_operations_from_state(state: SessionState) -> tuple[MemoryOperation, ...]:
    return tuple(
        MemoryOperation(
            action=value["action"],
            field=value["field"],
            value=value.get("value"),
            evidence=value.get("evidence"),
        )
        for value in state["decision"].get("memory_operations", ())
    )


async def _reply(state: SessionState) -> dict:
    decision = state["decision"]
    return {
        "pending_instruction": None,
        "pending_files": (),
        "current_user_text": None,
        "decision": None,
        "result": {
            "reply_text": decision["user_message"],
            "artifacts": [],
            "success": True,
        },
        **_history_and_pending(
            state, state["pending_instruction"], decision["user_message"]
        ),
    }


def _parse_resume_reply(reply, *, field: str) -> tuple[str | None, tuple[dict, ...]]:
    """归一化 ask_confirm/ask_memory 中断恢复时的 resume 载荷。

    resume 有两种来源：``handle_event`` 直接恢复用户在确认阶段的即时回复（一条
    平台消息最多带一个附件，用单数 ``file`` 键）；``_invoke_from_event`` 的竞态
    恢复分支（防抖窗口累积的一整批文件，在派发前发现图已经进入确认态，用复数
    ``files`` 键携带全部文件）。这里把两种形状统一归并成 ``new_files`` 元组，
    下游节点不再区分来源。
    """
    if isinstance(reply, dict):
        text = reply.get("text")
        if text is not None and not isinstance(text, str):
            raise RuntimeError(f"{field} resume.text 必须是字符串")
        files = reply.get("files")
        if files is not None:
            if not isinstance(files, (list, tuple)) or not all(
                isinstance(item, dict) for item in files
            ):
                raise RuntimeError(f"{field} resume.files 必须是 artifact reference 列表")
            new_files = tuple(files)
        else:
            file = reply.get("file")
            if file is not None and not isinstance(file, dict):
                raise RuntimeError(f"{field} resume.file 必须是 artifact reference")
            new_files = (file,) if file is not None else ()
        return text or None, new_files
    if not isinstance(reply, str):
        raise RuntimeError(f"{field} resume 必须是字符串或 {{text,file/files}} object")
    return reply or None, ()


async def _ask_confirm(state: SessionState) -> dict:
    decision = state["decision"]
    reply = interrupt(
        {
            "user_message": decision["user_message"],
            "task": decision["task"],
        }
    )
    text, new_files = _parse_resume_reply(reply, field="确认")
    return {"new_text": text, "new_files": new_files}


async def _ask_memory(state: SessionState) -> dict:
    reply = interrupt(
        {
            "user_message": state["decision"]["user_message"],
            "memory_operations": state["decision"]["memory_operations"],
        }
    )
    text, new_files = _parse_resume_reply(reply, field="memory")
    return {"new_text": text, "new_files": new_files}


async def _route_confirm(state: SessionState) -> str:
    """确认回复的分流：确定性放行 -> 确定性放弃 -> 确定性否决 -> 灰区交模型。

    放弃层必须排在否定层前面：常见放弃说法都含否定词表里的字符，顺序反过来就
    全被拦去 collect 反问一轮，正是 spec 决策 4 要修的体验。

    路由本身保持纯函数（不调模型），灰区只是把控制权交给 ``judge_confirm``
    节点，模型调用集中在那里。
    """

    # 确认阶段又收到附件时（可能是一整批），把它当任务补充重新交给主 Agent；
    # 不能像旧 runner 那样丢掉文件，也不能一边换附件一边按“是”执行旧任务。
    if state.get("new_files"):
        return "collect"
    reply = (state.get("new_text") or "").strip()
    if state["decision"].get("memory_operations") and _TASK_AND_MEMORY_CONFIRM_RE.fullmatch(
        reply
    ):
        return "save_memory_task"
    if _is_confirmation(reply):
        logger.info(
            "确认回复命中白名单快路径放行，直接执行 trace_id=%s", state.get("trace_id")
        )
        return "execute"
    if _is_cancellation(reply):
        logger.info(
            "确认回复命中确定性放弃层，直接收尾 trace_id=%s", state.get("trace_id")
        )
        return "cancel_task"
    if _is_negation(reply):
        logger.info("确认回复命中否定词，硬否决进入重新理解 trace_id=%s", state.get("trace_id"))
        return "collect"
    if not reply:
        return "collect"
    return "judge_confirm"


async def _route_after_judge(state: SessionState) -> str:
    verdict = state.get("confirmation_verdict") or {}
    decision = verdict.get("decision")
    if decision == "confirm":
        return "execute"
    if decision == "cancel":
        return "cancel_task"
    # revise 与任何意外取值都走最保守的一条：回到主 Agent 重新理解。
    return "collect"


async def _cancel_task(state: SessionState) -> dict:
    """用户放弃这次任务：清空待执行内容，用确定性话术收尾。

    两条入口都会到这里：``_route_confirm`` 的确定性放弃层（“算了”“不做了”这类
    整句放弃，零延迟不调模型），以及 ``judge_confirm`` 判 cancel 的灰区说法
    （“撤回这个请求吧”这种不在词表里的表达）。
    """

    reply = (state.get("new_text") or "").strip()
    instruction = state.get("pending_instruction") or ""
    # 历史里同时留下原始请求和放弃原话，下一轮主 Agent 才看得懂“刚才那个没做”。
    user_history = f"{instruction}\n{reply}" if instruction and reply else (instruction or reply)
    logger.info(
        "确认判定为放弃，清空待执行任务 trace_id=%s reason=%s",
        state.get("trace_id"),
        (state.get("confirmation_verdict") or {}).get("reason"),
    )
    return {
        "pending_instruction": None,
        "pending_files": (),
        "current_user_text": None,
        "decision": None,
        "new_text": None,
        "new_file": None,
        "confirmation_verdict": None,
        # active_artifacts 故意保留：放弃一次任务不该让“继续改刚才那份文件”
        # 的引用失效。
        "result": {"reply_text": _CANCEL_REPLY, "artifacts": [], "success": True},
        **_history_and_pending(state, user_history, _CANCEL_REPLY),
    }


async def _route_memory_confirmation(state: SessionState) -> str:
    if state.get("new_files"):
        return "collect"
    reply = (state.get("new_text") or "").strip()
    if _MEMORY_CONFIRM_RE.fullmatch(reply):
        return "save_memory_reply"
    if _MEMORY_REJECT_RE.fullmatch(reply):
        return "discard_memory_reply"
    return "collect"


def build_graph(
    main_agent: MainAgent,
    execution_agent: ExecutionAgent,
    memory_repository: MemoryRepository,
    checkpointer=None,
    summarizer: Summarizer | None = None,
) -> CompiledStateGraph:
    async def _main_agent(state: SessionState) -> dict:
        platform = state["platform"]
        user_id = state["user_id"]
        files = state.get("pending_files") or ()
        active_artifacts = state.get("active_artifacts") or ()
        try:
            known_facts = memory_repository.load(platform, user_id)
            if (
                state["pending_instruction"].strip() == LONG_TERM_MEMORY_COMMAND
                and not files
            ):
                return {
                    "decision": {
                        "intent": "chat",
                        "action": "reply",
                        "user_message": render_memory_snapshot(known_facts),
                        "task": None,
                        "memory_operations": [],
                    },
                    "memory_changes": None,
                }
            async with asyncio.timeout(60):
                decision = await main_agent.decide(
                    DialogueContext(
                        user_text=state["pending_instruction"],
                        input_filenames=tuple(display_name(ref) for ref in files),
                        known_facts=known_facts,
                        recent_messages=tuple(state.get("recent_messages") or ()),
                        conversation_summary=tuple(
                            entry["fact"]
                            for entry in state.get("conversation_summary") or ()
                        ),
                        active_artifact_filenames=tuple(
                            display_name(ref) for ref in active_artifacts
                        ),
                        current_user_text=state.get("current_user_text"),
                    )
                )
        except Exception:
            # 主 Agent 不可用时结束这一轮，而不是留下 failed task，让用户下一条
            # 消息误触发旧节点重跑。错误话术是确定性系统降级，不交给执行 Agent。
            logger.exception("主 Agent 决策失败")
            return {
                "decision": {
                    "intent": "chat",
                    "action": "reply",
                    "user_message": "我这次没能理解你的请求，请稍后再发一次。",
                    "task": None,
                    "memory_operations": [],
                },
                "memory_changes": None,
            }

        had_memory_candidates = bool(decision.memory_operations)
        validated_operations = memory_repository.validate(
            decision.memory_operations,
            source_text=state.get("current_user_text") or "",
        )
        decision = replace(decision, memory_operations=validated_operations)
        decision = _remove_unverified_memory_claim(
            decision, had_memory_candidates=had_memory_candidates
        )
        changes: list[dict] = []
        if validated_operations:
            try:
                changes = memory_repository.apply(
                    platform,
                    user_id,
                    validated_operations,
                    source_text=state.get("current_user_text") or "",
                )
                memory_notice = (
                    render_memory_notice(changes)
                    or "这些长期资料已经是最新的。"
                )
            except Exception:
                logger.exception("长期记忆隐式写入失败")
                memory_notice = "长期资料这次没有保存成功，请稍后再告诉我一次。"
            decision = replace(
                decision,
                user_message=f"{decision.user_message}\n\n{memory_notice}",
            )
        return {
            "decision": decision_to_dict(decision),
            "memory_changes": changes or None,
        }

    async def _judge_confirm(state: SessionState) -> dict:
        """灰区确认回复交主 Agent 判定：模型这一步的异常吞在节点内，用户无感知。

        try 只包模型调用本身。上下文组装留在外面——decision 形状坏掉是内部
        bug，被吞成 revise 只会把它伪装成“模型判不出来”，让真正的问题查不到。
        """

        decision = state["decision"]
        reply = (state.get("new_text") or "").strip()
        context = ConfirmationContext(
            task_instruction=(decision.get("task") or {}).get("instruction", ""),
            proposal_message=decision["user_message"],
            user_reply=reply,
        )
        started = time.monotonic()
        try:
            async with asyncio.timeout(_JUDGE_TIMEOUT_SECONDS):
                verdict = await main_agent.judge_confirmation(context)
            verdict_dict = {"decision": verdict.decision, "reason": verdict.reason}
        except Exception:
            # 降级只许落 revise（多问一轮），绝不落 confirm——判不出来时执行是
            # 不可逆的，重新理解只是多一次对话。
            logger.warning(
                "确认判定失败，降级为 revise trace_id=%s", state.get("trace_id"), exc_info=True
            )
            verdict_dict = {"decision": "revise", "reason": "判定调用失败，安全降级"}
        logger.info(
            "确认判定 trace_id=%s verdict=%s reason=%s elapsed_ms=%d",
            state.get("trace_id"),
            verdict_dict["decision"],
            verdict_dict["reason"],
            int((time.monotonic() - started) * 1000),
        )
        return {"confirmation_verdict": verdict_dict}

    async def _compact(state: SessionState) -> dict:
        """把攒够的一批被挤出消息压成带逐字 evidence 的摘要条目。

        这是投递之后的后台回合，没有用户输出：失败只影响记忆沉淀，绝不能把已经
        发出去的回复变成一次报错。压缩后端是外部 API（真正的系统边界），所以这里
        有重试语义——同一批连续失败 ``_MAX_COMPACTION_FAILURES`` 次才丢弃。
        """

        if summarizer is None:
            # 路由已经把回合送到这里却没有后端，是装配错误，不是运行期状况：
            # 吞掉只会让压缩永远静默不工作。
            raise RuntimeError("compact 节点被触发，但 build_graph 没有注入 summarizer")

        trace_id = state.get("trace_id")
        pending = list(state.get("pending_compaction") or [])
        summary = list(state.get("conversation_summary") or [])
        failures = state.get("compaction_failures") or 0
        started = time.monotonic()
        # 一级校验的源文本就是本批消息原文：模型只许从这里逐字摘。
        source_texts = tuple(str(message.get("content", "")) for message in pending)

        try:
            async with asyncio.timeout(_COMPACT_TIMEOUT_SECONDS):
                candidates = await summarizer.summarize(tuple(pending))
            accepted, rejected = validate_entries(candidates, source_texts=source_texts)
            if not accepted:
                # 整批被拒和后端报错同一性质：这批消息这次没压出任何可信内容。
                raise RuntimeError(f"本批候选全部未通过校验：{list(rejected)!r}")
        except Exception:
            failures += 1
            logger.warning(
                "历史压缩失败 trace_id=%s batch=%d failures=%d",
                trace_id,
                len(pending),
                failures,
                exc_info=True,
            )
            if failures >= _MAX_COMPACTION_FAILURES:
                logger.warning(
                    "历史压缩连续失败 %d 次，丢弃本批 %d 条消息 trace_id=%s",
                    failures,
                    len(pending),
                    trace_id,
                )
                return {
                    "pending_compaction": [],
                    "compaction_failures": 0,
                    "new_compaction_request": False,
                }
            return {
                "compaction_failures": failures,
                "new_compaction_request": False,
            }

        summary.extend(accepted)
        merged = False
        if len(summary) > _SUMMARY_MERGE_THRESHOLD:
            # 二级只许合并与精简：evidence 必须还是旧条目 evidence 里的逐字片段，
            # 合并这一步不得引入任何新证据。
            merge_sources = tuple(
                item for entry in summary for item in entry["evidence"]
            )
            try:
                async with asyncio.timeout(_COMPACT_TIMEOUT_SECONDS):
                    merge_candidates = await summarizer.merge(tuple(summary))
                merged_entries, merge_rejected = validate_entries(
                    merge_candidates,
                    source_texts=merge_sources,
                    max_entries=_SUMMARY_MERGE_TARGET,
                )
                # 下界守卫：合并是全特性唯一会整表替换的写路径，模型只回一两条就
                # 会永久抹掉几十条已验证事实。低于下限一律当合并失败处理，宁可留着
                # 没精简的表，也不接受一次无声的记忆销毁。
                merge_floor = min(_SUMMARY_MERGE_TARGET // 2, len(summary))
                if len(merged_entries) < merge_floor:
                    raise RuntimeError(
                        f"合并结果只有 {len(merged_entries)} 条，低于下限 {merge_floor}："
                        f"{list(merge_rejected)!r}"
                    )
                summary = list(merged_entries)
                merged = True
            except Exception:
                # 合并失败没有任何数据丢失，未合并的摘要本身完全可用；不计入批次
                # 失败计数，下次再超阈值时自然重试。
                logger.warning(
                    "摘要二级合并失败，保留未合并摘要 trace_id=%s entries=%d",
                    trace_id,
                    len(summary),
                    exc_info=True,
                )
                # 上界守卫：合并稳定失败时摘要会随每批继续变长，而它每轮都进
                # decide prompt。丢最旧的方向与压缩上线前的硬截断一致，安全。
                if len(summary) > _SUMMARY_MERGE_THRESHOLD * 2:
                    dropped = len(summary) - _SUMMARY_MERGE_THRESHOLD * 2
                    summary = summary[-_SUMMARY_MERGE_THRESHOLD * 2 :]
                    logger.warning(
                        "摘要超过硬顶 %d 条，丢弃最旧的 %d 条 trace_id=%s",
                        _SUMMARY_MERGE_THRESHOLD * 2,
                        dropped,
                        trace_id,
                    )

        logger.info(
            "历史压缩完成 trace_id=%s batch=%d accepted=%d rejected=%d "
            "merged=%s entries=%d elapsed_ms=%d",
            trace_id,
            len(pending),
            len(accepted),
            len(rejected),
            merged,
            len(summary),
            int((time.monotonic() - started) * 1000),
        )
        return {
            "conversation_summary": summary,
            "pending_compaction": [],
            "compaction_failures": 0,
            "new_compaction_request": False,
        }

    def _apply_confirmed_memory(state: SessionState) -> list[dict]:
        return memory_repository.apply(
            state["platform"],
            state["user_id"],
            _memory_operations_from_state(state),
            source_text=state.get("current_user_text") or "",
        )

    async def _save_memory_task(state: SessionState) -> dict:
        try:
            changes = _apply_confirmed_memory(state)
            feedback = render_memory_notice(changes) or "这些资料已经是最新的，不需要重复保存。"
        except Exception:
            logger.exception("用户确认后长期记忆写入失败，任务继续执行")
            changes = []
            feedback = "长期资料这次没有保存成功，但文档任务会继续执行。"
        return {
            "memory_changes": changes or None,
            "memory_feedback": feedback,
            "new_text": None,
        }

    async def _save_memory_reply(state: SessionState) -> dict:
        try:
            changes = _apply_confirmed_memory(state)
            reply = render_memory_notice(changes) or "这些资料已经是最新的，不需要重复保存。"
        except Exception:
            logger.exception("用户确认后长期记忆写入失败")
            changes = []
            reply = "这次没有保存成功，请稍后再告诉我一次。"
        assistant_history = f'{state["decision"]["user_message"]}\n{reply}'
        return {
            "pending_instruction": None,
            "pending_files": (),
            "current_user_text": None,
            "new_text": None,
            "decision": None,
            "memory_changes": changes or None,
            "memory_feedback": reply,
            "result": {"reply_text": reply, "artifacts": [], "success": True},
            **_history_and_pending(
                state, state["pending_instruction"], assistant_history
            ),
        }

    async def _discard_memory_reply(state: SessionState) -> dict:
        reply = "好的，这些资料不会保存。"
        assistant_history = f'{state["decision"]["user_message"]}\n{reply}'
        return {
            "pending_instruction": None,
            "pending_files": (),
            "current_user_text": None,
            "new_text": None,
            "decision": None,
            "memory_changes": None,
            "memory_feedback": reply,
            "result": {"reply_text": reply, "artifacts": [], "success": True},
            **_history_and_pending(
                state, state["pending_instruction"], assistant_history
            ),
        }

    async def _prepare_execution(state: SessionState) -> dict:
        try:
            workdir = create_workspace_dir(state["platform"], state["user_id"])
            return {
                "execution": {
                    "execution_id": workdir.name,
                    "workdir": str(workdir.resolve()),
                }
            }
        except Exception as exc:
            logger.exception("创建执行工作目录失败")
            return {"execution": {"error": str(exc)}}

    async def _execute(state: SessionState) -> dict:
        platform = state["platform"]
        user_id = state["user_id"]
        task = task_from_dict(state["decision"]["task"])
        execution_instruction = task.instruction
        current_files = state.get("pending_files") or ()
        previous_files = state.get("active_artifacts") or ()
        selection_error = None
        if task.use_previous_artifact:
            if current_files:
                selection_error = (
                    "任务同时包含新附件并要求上一份 artifact，来源不明确，拒绝执行"
                )
                files = ()
            else:
                files = previous_files
        else:
            files = current_files

        execution = state.get("execution") or {}
        workdir_value = execution.get("workdir")
        if workdir_value:
            workdir = Path(workdir_value).resolve()
        else:
            # 仅为失败日志提供一个稳定路径；不会调用执行后端。
            workdir = WORKSPACES_ROOT.resolve()
        logger.info(
            "orchestrator 派发执行 user_id=%s trace_id=%s workdir=%s",
            user_id,
            state.get("trace_id"),
            workdir,
        )

        started = time.monotonic()
        error: str | None = None
        report = None
        artifacts: list[dict] = []
        user_message: str | None = None
        input_filenames_for_log = ", ".join(display_name(ref) for ref in files) or None
        try:
            if execution.get("error"):
                raise RuntimeError("无法创建执行工作目录")
            if selection_error:
                raise RuntimeError(selection_error)
            if not workdir.is_relative_to(WORKSPACES_ROOT.resolve()):
                raise RuntimeError("执行工作目录越过 workspace 根目录")
            if task.use_previous_artifact and not files:
                raise RuntimeError("任务要求使用上一份文件，但会话中没有可用产物")
            input_paths = tuple(resolve_artifact_reference(ref) for ref in files)
            input_filenames = tuple(display_name(ref) for ref in files)

            report = _load_execution_marker(workdir)
            if report is None:
                if _execution_was_started(workdir):
                    raise RuntimeError(
                        "上一次执行已经开始但没有可信完成报告，结果状态未知；"
                        "为避免重复副作用，本次不会自动重跑"
                    )
                # started marker 必须在任何 backend 副作用之前落盘。恢复时若只有
                # started、没有 report，宁可报告 outcome unknown，也不自动二次执行。
                _mark_execution_started(workdir)
                async with asyncio.timeout(900):
                    report = await execution_agent.run(
                        instruction=execution_instruction,
                        input_paths=input_paths,
                        input_filenames=input_filenames,
                        workdir=workdir,
                    )
                report = _validate_execution_report(workdir, report)
                artifacts = [
                    output_artifact_reference(item.path, item.filename)
                    for item in report.artifacts
                ]
                _write_execution_marker(workdir, report)
            else:
                report = _validate_execution_report(workdir, report)
                artifacts = [
                    output_artifact_reference(item.path, item.filename)
                    for item in report.artifacts
                ]
                logger.warning(
                    "检测到 execution report marker，跳过重复执行 trace_id=%s execution_id=%s",
                    state.get("trace_id"),
                    execution.get("execution_id"),
                )
            try:
                async with asyncio.timeout(60):
                    user_message = await main_agent.finalize(
                        FinalizeContext(task=task, report=report)
                    )
            except Exception:
                # 执行已经产生副作用/文件，不能因为最后一次措辞调用失败就让整个
                # 节点失败并在重试时重复执行。这里用确定性降级文案完成投递。
                logger.exception("主 Agent 整理执行结果失败，使用降级回复")
                if report.artifacts:
                    names = "、".join(item.filename for item in report.artifacts)
                    user_message = f"已经处理完成，文件「{names}」已生成。"
                else:
                    user_message = "已经处理完成。"
                if report.warnings:
                    user_message += "\n\n注意：" + "；".join(report.warnings)
            memory_feedback = state.get("memory_feedback")
            if memory_feedback:
                user_message = f"{user_message}\n\n{memory_feedback}"
        except Exception as exc:
            error = str(exc)
            logger.exception(
                "执行 Agent 处理失败 trace_id=%s execution_id=%s",
                state.get("trace_id"),
                execution.get("execution_id"),
            )
            # 不把 failed execute 留在 checkpoint 中等待下一条用户消息重跑。真正的
            # 显式重试以后应由 execution_id/idempotency policy 驱动。
            user_message = "这次文档处理没有完成，请稍后重新发起任务。"
        finally:
            try:
                await log_turn(
                    TurnRecord(
                        platform=platform,
                        user_id=user_id,
                        run_id=execution.get("execution_id") or "prepare-failed",
                        input_text=execution_instruction,
                        input_filename=input_filenames_for_log,
                        backend=type(execution_agent).__name__,
                        output_text=user_message,
                        output_filename=(
                            ", ".join(item["filename"] for item in artifacts) or None
                        ),
                        duration_ms=int((time.monotonic() - started) * 1000),
                        success=error is None,
                        record_type="execution",
                        error=error,
                    )
                )
            except Exception:
                # 诊断日志不能把已经成功的外部执行变成 pending execute 并触发重跑。
                logger.exception("写 turn log 失败，但不改变本轮业务结果")

        # 失败结果绝不能同时发布/激活不确定的产物。后端已写文件但 marker 失败
        # 时属于 outcome unknown，保留 workdir 供人工复盘，不交给用户继续使用。
        if error is not None:
            artifacts = []

        update = {
            "pending_instruction": None,
            "pending_files": (),
            "current_user_text": None,
            "decision": None,
            "execution": None,
            "memory_feedback": None,
            "result": {
                "reply_text": user_message,
                "artifacts": artifacts,
                "success": error is None,
            },
            **_history_and_pending(
                state, state["pending_instruction"], user_message
            ),
        }
        if artifacts:
            update["active_artifacts"] = tuple(artifacts)
        elif error is None and files:
            # 读取/总结任务可能只返回文字而不生成新文件；此时“刚才的文件”应当
            # 继续指向本轮实际使用的输入，而不是更早的一份产物。
            update["active_artifacts"] = tuple(files)
        return update

    graph = StateGraph(SessionState)
    graph.add_node("collect", _collect)
    graph.add_node("main_agent", _main_agent)
    graph.add_node("reply", _reply)
    graph.add_node("ask_confirm", _ask_confirm)
    graph.add_node("ask_memory", _ask_memory)
    graph.add_node("judge_confirm", _judge_confirm)
    graph.add_node("cancel_task", _cancel_task)
    graph.add_node("save_memory_task", _save_memory_task)
    graph.add_node("save_memory_reply", _save_memory_reply)
    graph.add_node("discard_memory_reply", _discard_memory_reply)
    graph.add_node("prepare_execution", _prepare_execution)
    graph.add_node("execute", _execute)
    graph.add_node("compact", _compact)

    graph.set_entry_point("collect")
    graph.add_conditional_edges(
        "collect",
        _has_instruction,
        {"main_agent": "main_agent", "compact": "compact", END: END},
    )
    graph.add_conditional_edges(
        "main_agent",
        _route_after_decision,
        {
            "ask_confirm": "ask_confirm",
            "reply": "reply",
        },
    )
    graph.add_edge("reply", END)
    graph.add_conditional_edges(
        "ask_confirm",
        _route_confirm,
        {
            "execute": "prepare_execution",
            "save_memory_task": "save_memory_task",
            "cancel_task": "cancel_task",
            "judge_confirm": "judge_confirm",
            "collect": "collect",
        },
    )
    graph.add_conditional_edges(
        "judge_confirm",
        _route_after_judge,
        {
            "execute": "prepare_execution",
            "cancel_task": "cancel_task",
            "collect": "collect",
        },
    )
    graph.add_edge("cancel_task", END)
    graph.add_conditional_edges(
        "ask_memory",
        _route_memory_confirmation,
        {
            "save_memory_reply": "save_memory_reply",
            "discard_memory_reply": "discard_memory_reply",
            "collect": "collect",
        },
    )
    graph.add_edge("save_memory_reply", END)
    graph.add_edge("discard_memory_reply", END)
    graph.add_edge("save_memory_task", "prepare_execution")
    graph.add_edge("prepare_execution", "execute")
    graph.add_edge("execute", END)
    graph.add_edge("compact", END)

    return graph.compile(checkpointer=checkpointer)
