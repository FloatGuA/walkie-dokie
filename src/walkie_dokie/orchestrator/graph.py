"""可恢复的会话工作流。

LangGraph 在这里是控制平面，不是主 Agent：它只负责累积跨消息输入、暂停等待
确认、路由和调用两个明确边界的 Agent。MainAgent 负责面向用户的语义与长期
记忆候选；ExecutionAgent 只执行已经确认的文档任务契约。

    collect -> main_agent --+-- reply ---------------------------> END
                             +-- ask_memory --+-- save/discard ---> END
                             |                +-- collect --------...
                             +-- ask_confirm -+-- prepare -> execute -> END
                                              +-- collect --------...

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

from walkie_dokie.agents.base import ExecutionAgent, ExecutionReport, resolve_output_file
from walkie_dokie.artifacts import (
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
    MemoryRepository,
    render_memory_notice,
    render_memory_proposal,
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


def _is_confirmation(reply: str) -> bool:
    """只接受一条完整、无附加条件的肯定回复。

    旧实现用正向前缀匹配，会把“好像不对”“可以先别做”和“是，不过先改……”
    都误判为确认。这里宁可多澄清一轮，也不带着未处理的否定/补充直接执行。
    """

    return bool(_CONFIRM_RE.fullmatch(reply.strip()))


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
        "result_filename": report.result_filename,
        "warnings": list(report.warnings),
    }
    _atomic_write_json(marker, payload)


def _load_execution_marker(workdir: Path) -> ExecutionReport | None:
    marker = _execution_metadata_dir(workdir) / _EXECUTION_MARKER
    if not marker.is_file():
        return None
    payload = json.loads(marker.read_text(encoding="utf-8"))
    filename = payload.get("result_filename")
    artifact_path = resolve_output_file(workdir, filename) if filename else None
    return ExecutionReport(
        summary=payload["summary"],
        artifact_path=artifact_path,
        result_filename=filename,
        warnings=tuple(payload.get("warnings", ())),
    )


def _validate_execution_report(
    workdir: Path, report: ExecutionReport
) -> ExecutionReport:
    """Re-establish trust at the plugin boundary for this exact execution cwd."""
    if report.artifact_path is None:
        return report
    assert report.result_filename is not None  # enforced by ExecutionReport
    expected = resolve_output_file(workdir, report.result_filename)
    if report.artifact_path.resolve() != expected:
        raise RuntimeError(
            "执行 Agent 返回了其他工作目录的 artifact："
            f"{report.artifact_path}（本轮期望 {expected}）"
        )
    # Keep only the path reconstructed from trusted workdir + validated basename.
    return ExecutionReport(
        summary=report.summary,
        artifact_path=expected,
        result_filename=report.result_filename,
        warnings=report.warnings,
    )


async def _collect(state: SessionState) -> dict:
    existing = state.get("pending_instruction")
    new = state.get("new_text")
    combined = f"{existing}\n{new}" if existing and new else (new or existing)
    file = state.get("new_file") or state.get("pending_file")
    if file is not None:
        resolve_artifact_reference(file)
    return {
        "pending_instruction": combined,
        "pending_file": file,
        "new_text": None,
        "new_file": None,
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
    if state["decision"]["action"] == "propose_task":
        return "ask_confirm"
    if state["decision"].get("memory_operations"):
        return "ask_memory"
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
        "pending_file": None,
        "current_user_text": None,
        "decision": None,
        "result": {
            "reply_text": decision["user_message"],
            "artifact": None,
            "success": True,
        },
        "recent_messages": _completed_turn_history(
            state, state["pending_instruction"], decision["user_message"]
        ),
    }


async def _ask_confirm(state: SessionState) -> dict:
    decision = state["decision"]
    reply = interrupt(
        {
            "user_message": decision["user_message"],
            "task": decision["task"],
        }
    )
    if isinstance(reply, dict):
        text = reply.get("text")
        file = reply.get("file")
        if text is not None and not isinstance(text, str):
            raise RuntimeError("确认 resume.text 必须是字符串")
        if file is not None and not isinstance(file, dict):
            raise RuntimeError("确认 resume.file 必须是 artifact reference")
        return {"new_text": text or None, "new_file": file}
    if not isinstance(reply, str):
        raise RuntimeError("确认 resume 必须是字符串或 {text,file} object")
    return {"new_text": reply or None, "new_file": None}


async def _ask_memory(state: SessionState) -> dict:
    reply = interrupt(
        {
            "user_message": state["decision"]["user_message"],
            "memory_operations": state["decision"]["memory_operations"],
        }
    )
    if isinstance(reply, dict):
        text = reply.get("text")
        file = reply.get("file")
        if text is not None and not isinstance(text, str):
            raise RuntimeError("memory resume.text 必须是字符串")
        if file is not None and not isinstance(file, dict):
            raise RuntimeError("memory resume.file 必须是 artifact reference")
        return {"new_text": text or None, "new_file": file}
    if not isinstance(reply, str):
        raise RuntimeError("memory resume 必须是字符串或 {text,file} object")
    return {"new_text": reply or None, "new_file": None}


async def _route_confirm(state: SessionState) -> str:
    # 确认阶段又收到附件时，把它当任务补充重新交给主 Agent；不能像旧 runner
    # 那样丢掉文件，也不能一边换附件一边按“是”执行旧任务。
    if state.get("new_file") is not None:
        return "collect"
    reply = state.get("new_text") or ""
    if state["decision"].get("memory_operations") and _TASK_AND_MEMORY_CONFIRM_RE.fullmatch(
        reply.strip()
    ):
        return "save_memory_task"
    return "execute" if _is_confirmation(reply) else "collect"


async def _route_memory_confirmation(state: SessionState) -> str:
    if state.get("new_file") is not None:
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
        file = state.get("pending_file")
        active_artifact = state.get("active_artifact")
        try:
            known_facts = memory_repository.load(platform, user_id)
            async with asyncio.timeout(60):
                decision = await main_agent.decide(
                    DialogueContext(
                        user_text=state["pending_instruction"],
                        input_filename=file["filename"] if file else None,
                        known_facts=known_facts,
                        recent_messages=tuple(state.get("recent_messages") or ()),
                        active_artifact_filename=(
                            active_artifact["filename"] if active_artifact else None
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
                    "action": "reply",
                    "user_message": "我这次没能理解你的请求，请稍后再发一次。",
                    "task": None,
                    "memory_operations": [],
                },
                "memory_changes": None,
            }

        validated_operations = memory_repository.validate(
            decision.memory_operations,
            source_text=state.get("current_user_text") or "",
        )
        decision = replace(decision, memory_operations=validated_operations)
        if validated_operations:
            proposal = render_memory_proposal(validated_operations)
            instruction = (
                "回复“是”只执行任务；回复“是并记住”执行任务并保存上述资料。"
                if decision.action == "propose_task"
                else "回复“记住”才会保存；回复“不用记”不会保存。"
            )
            decision = replace(
                decision,
                user_message=f"{decision.user_message}\n\n{proposal}\n{instruction}",
            )
        return {
            "decision": decision_to_dict(decision),
            "memory_changes": None,
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
            "pending_file": None,
            "current_user_text": None,
            "new_text": None,
            "decision": None,
            "memory_changes": changes or None,
            "memory_feedback": reply,
            "result": {"reply_text": reply, "artifact": None, "success": True},
            "recent_messages": _completed_turn_history(
                state, state["pending_instruction"], assistant_history
            ),
        }

    async def _discard_memory_reply(state: SessionState) -> dict:
        reply = "好的，这些资料不会保存。"
        assistant_history = f'{state["decision"]["user_message"]}\n{reply}'
        return {
            "pending_instruction": None,
            "pending_file": None,
            "current_user_text": None,
            "new_text": None,
            "decision": None,
            "memory_changes": None,
            "memory_feedback": reply,
            "result": {"reply_text": reply, "artifact": None, "success": True},
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
        current_file = state.get("pending_file")
        previous_file = state.get("active_artifact")
        selection_error = None
        if task.use_previous_artifact:
            if current_file is not None:
                selection_error = (
                    "任务同时包含新附件并要求上一份 artifact，来源不明确，拒绝执行"
                )
                file = None
            else:
                file = previous_file
        else:
            file = current_file

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
        artifact = None
        user_message: str | None = None
        try:
            if execution.get("error"):
                raise RuntimeError("无法创建执行工作目录")
            if selection_error:
                raise RuntimeError(selection_error)
            if not workdir.is_relative_to(WORKSPACES_ROOT.resolve()):
                raise RuntimeError("执行工作目录越过 workspace 根目录")
            if task.use_previous_artifact and file is None:
                raise RuntimeError("任务要求使用上一份文件，但会话中没有可用产物")
            input_path = resolve_artifact_reference(file) if file else None

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
                        input_path=input_path,
                        workdir=workdir,
                        input_filename=file["filename"] if file else None,
                    )
                report = _validate_execution_report(workdir, report)
                if report.artifact_path and report.result_filename:
                    artifact = output_artifact_reference(
                        report.artifact_path, report.result_filename
                    )
                _write_execution_marker(workdir, report)
            else:
                report = _validate_execution_report(workdir, report)
                if report.artifact_path and report.result_filename:
                    artifact = output_artifact_reference(
                        report.artifact_path, report.result_filename
                    )
                logger.warning(
                    "检测到 execution report marker，跳过重复执行 execution_id=%s",
                    execution.get("execution_id"),
                )
            try:
                async with asyncio.timeout(60):
                    user_message = await main_agent.finalize(
                        FinalizeContext(
                            task=task,
                            report=report,
                        )
                    )
            except Exception:
                # 执行已经产生副作用/文件，不能因为最后一次措辞调用失败就让整个
                # 节点失败并在重试时重复执行。这里用确定性降级文案完成投递。
                logger.exception("主 Agent 整理执行结果失败，使用降级回复")
                user_message = (
                    f"已经处理完成，文件「{report.result_filename}」已生成。"
                    if report.result_filename
                    else "已经处理完成。"
                )
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
                        input_filename=file["filename"] if file else None,
                        backend=type(execution_agent).__name__,
                        output_text=user_message,
                        output_filename=report.result_filename if report else None,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        success=error is None,
                        error=error,
                    )
                )
            except Exception:
                # 诊断日志不能把已经成功的外部执行变成 pending execute 并触发重跑。
                logger.exception("写 turn log 失败，但不改变本轮业务结果")

        # 失败结果绝不能同时发布/激活不确定的产物。后端已写文件但 marker 失败
        # 时属于 outcome unknown，保留 workdir 供人工复盘，不交给用户继续使用。
        if error is not None:
            artifact = None

        update = {
            "pending_instruction": None,
            "pending_file": None,
            "current_user_text": None,
            "decision": None,
            "execution": None,
            "memory_feedback": None,
            "result": {
                "reply_text": user_message,
                "artifact": artifact,
                "success": error is None,
            },
            "recent_messages": _completed_turn_history(
                state, state["pending_instruction"], user_message
            ),
        }
        if artifact is not None:
            update["active_artifact"] = artifact
        elif error is None and file is not None:
            # 读取/总结任务可能只返回文字而不生成新文件；此时“刚才的文件”应当
            # 继续指向本轮实际使用的输入，而不是更早的一份产物。
            update["active_artifact"] = file
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
            "ask_memory": "ask_memory",
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
