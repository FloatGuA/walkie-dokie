"""可恢复的会话工作流。

LangGraph 在这里是控制平面，不是主 Agent：它只负责累积跨消息输入、暂停等待
确认、路由和调用两个明确边界的 Agent。MainAgent 负责面向用户的语义与长期
记忆候选；ExecutionAgent 只执行已经确认的文档任务契约。

    collect -> main_agent --+-- reply ---------------------------> END
                             +-- ask_confirm -+-- prepare -> execute -> END
                                              +-- collect --------...

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

_CONFIRM_RE = re.compile(
    r"^(?:是(?:的)?(?:呀|啊|呢)?|对(?:的)?(?:呀|啊|呢)?|确认|没错|可以|行|"
    r"嗯+|好(?:的)?(?:呀|啊|呢)?|ok(?:ay)?|yes|y)[\s!！。．.]*$",
    re.IGNORECASE,
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
    """只接受一条完整、无附加条件的肯定回复。

    旧实现用正向前缀匹配，会把“好像不对”“可以先别做”和“是，不过先改……”
    都误判为确认。这里宁可多澄清一轮，也不带着未处理的否定/补充直接执行。
    """

    return bool(_CONFIRM_RE.fullmatch(reply.strip()))


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


def _completed_turn_history(
    state: SessionState, user_text: str, assistant_text: str
) -> list[dict[str, str]]:
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
    return list(reversed(bounded))


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
    return {
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
    }


async def _has_instruction(state: SessionState) -> str:
    return "main_agent" if state.get("pending_instruction") else END


async def _route_after_decision(state: SessionState) -> str:
    logger.info(
        "意图分流 platform=%s user_id=%s intent=%s action=%s route=%s",
        state["platform"],
        state["user_id"],
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
        "recent_messages": _completed_turn_history(
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
    # 确认阶段又收到附件时（可能是一整批），把它当任务补充重新交给主 Agent；
    # 不能像旧 runner 那样丢掉文件，也不能一边换附件一边按“是”执行旧任务。
    if state.get("new_files"):
        return "collect"
    reply = state.get("new_text") or ""
    if state["decision"].get("memory_operations") and _TASK_AND_MEMORY_CONFIRM_RE.fullmatch(
        reply.strip()
    ):
        return "save_memory_task"
    return "execute" if _is_confirmation(reply) else "collect"


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
            "recent_messages": _completed_turn_history(
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
            "recent_messages": _completed_turn_history(
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
        logger.info("orchestrator 派发执行 user_id=%s workdir=%s", user_id, workdir)

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
                    "检测到 execution report marker，跳过重复执行 execution_id=%s",
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
            logger.exception("执行 Agent 处理失败 execution_id=%s", execution.get("execution_id"))
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
            "recent_messages": _completed_turn_history(
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
    graph.add_node("save_memory_task", _save_memory_task)
    graph.add_node("save_memory_reply", _save_memory_reply)
    graph.add_node("discard_memory_reply", _discard_memory_reply)
    graph.add_node("prepare_execution", _prepare_execution)
    graph.add_node("execute", _execute)

    graph.set_entry_point("collect")
    graph.add_conditional_edges(
        "collect", _has_instruction, {"main_agent": "main_agent", END: END}
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
            "collect": "collect",
        },
    )
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

    return graph.compile(checkpointer=checkpointer)
