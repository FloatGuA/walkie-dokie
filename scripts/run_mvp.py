"""MVP 端到端胶水脚本：飞书收消息 -> orchestrator（LangGraph 状态机，含防抖+确认）
-> Claude Agent SDK -> 飞书发结果回去。

用法：python scripts/run_mvp.py，跑起来之后在飞书里找 Wokie-Dokie智能助手
发一句话（比如"帮我写一份请假条"），10 秒内没有新消息后会收到一句确认草稿，
回"是"才会真的执行。Ctrl+C 停止。

每条消息 asyncio.create_task 并发处理，不同用户互不阻塞。
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from walkie_dokie.agents.claude_agent import ClaudeAgentSDKBackend
from walkie_dokie.artifacts import resolve_artifact_reference, store_incoming_file
from walkie_dokie.logging_config import setup_logging
from walkie_dokie.main_agent import (
    DeepSeekMainAgent,
    JsonMemoryRepository,
    LONG_TERM_MEMORY_COMMAND,
    render_memory_snapshot,
)
from walkie_dokie.orchestrator import build_graph
from walkie_dokie.orchestrator.debounce import Debouncer
from walkie_dokie.orchestrator.locks import UserLocks
from walkie_dokie.platforms.base import IncomingFile, InboundEvent, OutboundMessage
from walkie_dokie.platforms.feishu import FeishuAdapter
from walkie_dokie.turn_log import TurnRecord, log_turn

logger = logging.getLogger(__name__)

DEBOUNCE_WINDOW_SECONDS = 10.0
# v2 状态 schema 引入 MainAgent decision，不能拿旧 draft_task_prompt checkpoint
# 直接恢复；开发阶段明确换一份数据库，旧库保留供复盘，不做破坏性删除。
CHECKPOINT_DB_PATH = Path(__file__).parent.parent / "var" / "checkpoints-v2.db"


def _session_key(platform: str, user_id: str) -> str:
    """LangGraph thread 和互斥锁都用平台+用户复合键，避免跨平台 ID 碰撞。"""
    return f"{platform}:{user_id}"


def _waiting_for_confirmation(snapshot) -> bool:
    """`next` is scheduling state; only known user-input interrupts are resumable."""
    return bool(snapshot.interrupts) and snapshot.next in {
        ("ask_confirm",),
        ("ask_memory",),
    }


async def _invoke_from_event(
    graph,
    *,
    config: dict,
    platform_name: str,
    user_id: str,
    text: str,
    file: IncomingFile | None = None,
    files: tuple[IncomingFile, ...] = (),
):
    """Re-check durable state at dispatch time, then resume or start atomically."""
    snapshot = await graph.aget_state(config=config)
    if _waiting_for_confirmation(snapshot):
        file_reference = (
            store_incoming_file(platform_name, user_id, file) if file else None
        )
        return await graph.ainvoke(
            Command(resume={"text": text, "file": file_reference}),
            config=config,
            durability="sync",
        )
    if snapshot.interrupts:
        raise RuntimeError(f"未知 interrupt 状态 next={snapshot.next!r}")
    if snapshot.next:
        raise RuntimeError(f"会话存在非 interrupt 的未完成任务 next={snapshot.next!r}")
    file_references = tuple(
        store_incoming_file(platform_name, user_id, item) for item in files
    )
    return await graph.ainvoke(
        {
            "platform": platform_name,
            "user_id": user_id,
            "new_text": text or None,
            "new_files": file_references,
        },
        config=config,
        durability="sync",
    )


async def deliver_graph_output(
    platform: FeishuAdapter, user_id: str, state: dict
) -> tuple[str | None, str | None, bool]:
    if "__interrupt__" in state:
        # 给用户的话来自 MainAgent；task contract 只给 ExecutionAgent，不能混用。
        payload = state["__interrupt__"][0].value
        logger.info("图输出等待用户确认 user_id=%s", user_id)
        await platform.send(user_id, OutboundMessage(text=payload["user_message"]))
        return payload["user_message"], None, True

    result = state.get("result")
    if result is None:
        pending_files = state.get("pending_files") or ()
        if pending_files:
            # 飞书发文件时不能带文字，只能分开发——收到文件但还没指令是正常情况，
            # 得主动回一句，不能沉默，不然用户不知道文件收到没有。
            names = "、".join(ref["filename"] for ref in pending_files)
            text = f"收到文件「{names}」了，请告诉我需要我做什么。"
            await platform.send(user_id, OutboundMessage(text=text))
            return text, None, True
        else:
            logger.info("图输出为空 user_id=%s", user_id)
        return None, None, True

    artifacts = result.get("artifacts") or []
    logger.info(
        "图输出完成 user_id=%s success=%s artifact_count=%d",
        user_id,
        result.get("success"),
        len(artifacts),
    )
    for reference in artifacts:
        artifact = resolve_artifact_reference(reference)
        await platform.send(
            user_id,
            OutboundMessage(
                file=IncomingFile(
                    filename=reference["filename"],
                    content=artifact.read_bytes(),
                    mime_type=reference["mime_type"],
                )
            ),
        )
    await platform.send(user_id, OutboundMessage(text=result["reply_text"]))
    return (
        result["reply_text"],
        ", ".join(item["filename"] for item in artifacts) or None,
        bool(result.get("success")),
    )


async def _log_conversation_turn(
    *,
    platform_name: str,
    user_id: str,
    input_text: str | None,
    input_filename: str | None,
    output_text: str | None,
    output_filename: str | None,
    duration_ms: int,
    success: bool,
    error: str | None = None,
) -> None:
    """Best-effort platform input/output evidence; never changes business outcome."""

    try:
        await log_turn(
            TurnRecord(
                platform=platform_name,
                user_id=user_id,
                run_id=None,
                input_text=input_text,
                input_filename=input_filename,
                backend=None,
                output_text=output_text,
                output_filename=output_filename,
                duration_ms=duration_ms,
                success=success,
                record_type="conversation",
                error=error,
            )
        )
    except Exception:
        logger.exception("写 conversation turn log 失败，但不改变本轮业务结果")


async def dispatch_fresh(
    graph,
    platform: FeishuAdapter,
    platform_name: str,
    user_id: str,
    combined_text: str,
    files: tuple[IncomingFile, ...],
    locks: UserLocks,
) -> None:
    session_key = _session_key(platform_name, user_id)
    started = time.monotonic()
    logger.info(
        "开始处理防抖回合 session=%s text_chars=%d files=%r",
        session_key,
        len(combined_text),
        [item.filename for item in files],
    )
    async with locks.get(session_key):
        fallback_text = "处理失败，稍后再试一下"
        try:
            state = await _invoke_from_event(
                graph,
                config={"configurable": {"thread_id": session_key}},
                platform_name=platform_name,
                user_id=user_id,
                text=combined_text,
                files=files,
            )
        except Exception as exc:
            logger.exception("orchestrator 处理失败 user_id=%s", user_id)
            await platform.send(user_id, OutboundMessage(text=fallback_text))
            await _log_conversation_turn(
                platform_name=platform_name,
                user_id=user_id,
                input_text=combined_text or None,
                input_filename=", ".join(item.filename for item in files) or None,
                output_text=fallback_text,
                output_filename=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                success=False,
                error=str(exc),
            )
            return
        output_text = None
        output_filename = None
        output_success = False
        delivery_error = None
        try:
            # MVP 先把同 session 的状态推进与对应网络投递放在同一顺序域；正式版
            # 应改为 durable outbox，而不是长期持锁等待平台网络。
            output_text, output_filename, output_success = await deliver_graph_output(
                platform, user_id, state
            )
        except Exception as exc:
            delivery_error = str(exc)
            # 文件可能已经成功而文字失败；不能再追加一条“处理失败”制造更多
            # 不确定投递。持久 outbox 实现前只记录并保留 workspace 供人工恢复。
            logger.exception(
                "投递图输出失败 platform=%s user_id=%s", platform_name, user_id
            )
        finally:
            await _log_conversation_turn(
                platform_name=platform_name,
                user_id=user_id,
                input_text=combined_text or None,
                input_filename=", ".join(item.filename for item in files) or None,
                output_text=output_text,
                output_filename=output_filename,
                duration_ms=int((time.monotonic() - started) * 1000),
                success=output_success and delivery_error is None,
                error=delivery_error,
            )
            logger.info(
                "防抖回合处理结束 session=%s duration_ms=%d",
                session_key,
                int((time.monotonic() - started) * 1000),
            )


async def handle_event(
    graph,
    platform: FeishuAdapter,
    debouncer: Debouncer,
    locks: UserLocks,
    memory_repository: JsonMemoryRepository,
    event: InboundEvent,
) -> None:
    if not event.text and event.file is None:
        return

    session_key = _session_key(event.platform, event.user_id)
    if event.file is None and (event.text or "").strip() == LONG_TERM_MEMORY_COMMAND:
        started = time.monotonic()
        async with locks.get(session_key):
            try:
                output_text = render_memory_snapshot(
                    memory_repository.load(event.platform, event.user_id)
                )
                await platform.send(event.user_id, OutboundMessage(text=output_text))
                error = None
            except Exception as exc:
                logger.exception("查询长期记忆失败 user_id=%s", event.user_id)
                output_text = "长期记忆这次没有读取成功，请稍后再试。"
                error = str(exc)
                await platform.send(event.user_id, OutboundMessage(text=output_text))
            await _log_conversation_turn(
                platform_name=event.platform,
                user_id=event.user_id,
                input_text=event.text,
                input_filename=None,
                output_text=output_text,
                output_filename=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                success=error is None,
                error=error,
            )
        return

    resumed_state = None
    # 查询状态和决定“resume 还是防抖新回合”也必须和 ainvoke 使用同一把锁。
    # 否则 execute 正好结束/进入 interrupt 的边界上仍有 TOCTOU 窗口。
    try:
        async with locks.get(session_key):
            snapshot = await graph.aget_state(
                config={"configurable": {"thread_id": session_key}}
            )
            logger.info(
                "事件路由 session=%s waiting_confirmation=%s next=%r",
                session_key,
                _waiting_for_confirmation(snapshot),
                snapshot.next,
            )
            if _waiting_for_confirmation(snapshot):
                started = time.monotonic()
                logger.info("恢复确认中的回合 session=%s", session_key)
                config = {"configurable": {"thread_id": session_key}}
                file_reference = (
                    store_incoming_file(event.platform, event.user_id, event.file)
                    if event.file
                    else None
                )
                resumed_state = await graph.ainvoke(
                    Command(
                        resume={"text": event.text or "", "file": file_reference}
                    ),
                    config=config,
                    durability="sync",
                )
                output_text = None
                output_filename = None
                output_success = False
                delivery_error = None
                try:
                    output_text, output_filename, output_success = await deliver_graph_output(
                        platform, event.user_id, resumed_state
                    )
                except Exception as exc:
                    delivery_error = str(exc)
                    logger.exception("恢复后投递失败 user_id=%s", event.user_id)
                finally:
                    await _log_conversation_turn(
                        platform_name=event.platform,
                        user_id=event.user_id,
                        input_text=event.text,
                        input_filename=event.file.filename if event.file else None,
                        output_text=output_text,
                        output_filename=output_filename,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        success=output_success and delivery_error is None,
                        error=delivery_error,
                    )
                    logger.info(
                        "确认回合处理结束 session=%s duration_ms=%d",
                        session_key,
                        int((time.monotonic() - started) * 1000),
                    )
            elif snapshot.interrupts:
                raise RuntimeError(f"未知 interrupt 状态 next={snapshot.next!r}")
            elif snapshot.next:
                # next 也会出现在 failed/pending task 上，它并不是 interrupt 标志。
                # 绝不能把当前用户消息作为 resume 吞掉或自动重放有副作用的 execute。
                logger.error(
                    "会话存在非 interrupt 的未完成任务 session=%s next=%r",
                    session_key,
                    snapshot.next,
                )
                await platform.send(
                    event.user_id,
                    OutboundMessage(
                        text="上一次处理留下了异常状态，我没有执行你这条新消息，请联系维护者恢复会话。"
                    ),
                )
                return
    except Exception:
        logger.exception("orchestrator 查询/恢复失败 user_id=%s", event.user_id)
        await platform.send(
            event.user_id, OutboundMessage(text="处理失败，稍后再试一下")
        )
        return

    if resumed_state is not None:
        return
    debouncer.add(event.platform, event.user_id, event.text, event.file)


async def main():
    load_dotenv()
    setup_logging()
    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]

    CHECKPOINT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB_PATH)) as checkpointer:
        await checkpointer.setup()

        platform = FeishuAdapter(app_id, app_secret)
        main_agent = DeepSeekMainAgent()
        backend = ClaudeAgentSDKBackend()
        memory_repository = JsonMemoryRepository()
        graph = build_graph(
            main_agent,
            backend,
            memory_repository,
            checkpointer=checkpointer,
        )
        locks = UserLocks()
        debouncer = Debouncer(
            DEBOUNCE_WINDOW_SECONDS,
            on_ready=lambda platform_name, user_id, text, files: dispatch_fresh(
                graph, platform, platform_name, user_id, text, files, locks
            ),
        )

        platform.start()
        logger.info("MVP 胶水循环已启动，会话状态落盘到 %s，等待飞书消息……（Ctrl+C 停止）", CHECKPOINT_DB_PATH)

        in_flight: set[asyncio.Task] = set()

        async def _handle_safely(event: InboundEvent) -> None:
            try:
                await handle_event(
                    graph,
                    platform,
                    debouncer,
                    locks,
                    memory_repository,
                    event,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "未捕获的事件处理异常 platform=%s user_id=%s",
                    event.platform,
                    event.user_id,
                )

        try:
            while True:
                event = await platform.receive()
                task = asyncio.create_task(_handle_safely(event))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        finally:
            await debouncer.close()
            for task in in_flight:
                task.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
