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
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from walkie_dokie.agents.claude_agent import ClaudeAgentSDKBackend
from walkie_dokie.artifacts import (
    display_name,
    resolve_artifact_reference,
    store_incoming_file,
)
from walkie_dokie.logging_config import setup_logging
from walkie_dokie.main_agent import (
    DeepSeekMainAgent,
    JsonMemoryRepository,
    LONG_TERM_MEMORY_COMMAND,
    render_memory_snapshot,
)
from walkie_dokie.main_agent.summarizer import ClaudeAgentSummarizer
from walkie_dokie.orchestrator import build_graph
from walkie_dokie.orchestrator.debounce import Debouncer
from walkie_dokie.orchestrator.graph import COMPACTION_BATCH_SIZE
from walkie_dokie.orchestrator.locks import UserLocks
from walkie_dokie.orchestrator.outbox import Outbox
from walkie_dokie.platforms.base import IncomingFile, InboundEvent, OutboundMessage
from walkie_dokie.platforms.feishu import FeishuAdapter
from walkie_dokie.turn_log import TurnRecord, log_turn

logger = logging.getLogger(__name__)

DEBOUNCE_WINDOW_SECONDS = 10.0
# 单条 send 的上限。平台 SDK 自己的超时不可靠（连上了但不返回），worker 是单
# 协程串行发件，一条卡住就等于整个投递停摆——宁可当失败进退避表重发一次。
_SEND_TIMEOUT_SECONDS = 30
# 空批时最多睡这么久；入队方 wakeup.set() 会提前叫醒，所以这只是兜底轮询周期。
_IDLE_POLL_SECONDS = 1.0
# inbox 去重表的 TTL 清理节流：删的是 7 天前的行，一小时一次远远够用。
_SEEN_PURGE_INTERVAL_SECONDS = 3600.0
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
    files: tuple[IncomingFile, ...] = (),
    trace_id: str,
):
    """Re-check durable state at dispatch time, then resume or start atomically.

    ``files`` is the whole debounced batch (possibly several files). If the graph
    raced ahead to ``ask_confirm``/``ask_memory`` while this batch was in flight,
    every file in the batch must still reach the graph via the resume payload's
    plural ``files`` key — dropping any of them here is exactly the silent-loss
    failure class this multi-file design exists to eliminate.

    ``trace_id`` is the id this *new* debounce batch was minted with. On the
    confirm-race resume path that id is discarded in favour of the trace_id
    already persisted on the in-flight task's snapshot, so the whole
    propose->confirm->execute->deliver lifecycle keeps sharing one id instead
    of the race splitting it into two. Returns ``(state, effective_trace_id)``
    so the caller logs/turn-records the id that actually applies.
    """
    snapshot = await graph.aget_state(config=config)
    file_references = tuple(
        store_incoming_file(platform_name, user_id, item) for item in files
    )
    if _waiting_for_confirmation(snapshot):
        effective_trace_id = snapshot.values.get("trace_id", trace_id)
        state = await graph.ainvoke(
            Command(resume={"text": text, "files": file_references}),
            config=config,
            durability="sync",
        )
        return state, effective_trace_id
    if snapshot.interrupts:
        raise RuntimeError(f"未知 interrupt 状态 next={snapshot.next!r}")
    if snapshot.next:
        raise RuntimeError(f"会话存在非 interrupt 的未完成任务 next={snapshot.next!r}")
    state = await graph.ainvoke(
        {
            "platform": platform_name,
            "user_id": user_id,
            "new_text": text or None,
            "new_files": file_references,
            "trace_id": trace_id,
        },
        config=config,
        durability="sync",
    )
    return state, trace_id


def build_outbound_messages(state: dict) -> tuple[list[tuple[str, dict]], dict]:
    """把一轮图输出组装成有序出站清单 + 这一轮写 turn log 用的小结。

    纯函数：不发送、不碰文件系统。``file`` 消息只带 ArtifactReference dict，
    bytes 留到真正发送的那一刻再读——和旧的“发送时才 read_bytes”语义一致，
    也让清单可以整条落盘进 outbox。
    """

    if "__interrupt__" in state:
        # 给用户的话来自 MainAgent；task contract 只给 ExecutionAgent，不能混用。
        user_message = state["__interrupt__"][0].value["user_message"]
        return (
            [("text", {"text": user_message})],
            {"output_text": user_message, "output_filename": None, "success": True},
        )

    result = state.get("result")
    if result is None:
        pending_files = state.get("pending_files") or ()
        if pending_files:
            # 飞书发文件时不能带文字，只能分开发——收到文件但还没指令是正常情况，
            # 得主动回一句，不能沉默，不然用户不知道文件收到没有。
            names = "、".join(display_name(ref) for ref in pending_files)
            text = f"收到文件「{names}」了，请告诉我需要我做什么。"
            return (
                [("text", {"text": text})],
                {"output_text": text, "output_filename": None, "success": True},
            )
        return [], {"output_text": None, "output_filename": None, "success": True}

    artifacts = result.get("artifacts") or []
    messages: list[tuple[str, dict]] = [
        ("file", reference) for reference in artifacts
    ]
    messages.append(("text", {"text": result["reply_text"]}))
    return messages, {
        "output_text": result["reply_text"],
        "output_filename": ", ".join(item["filename"] for item in artifacts) or None,
        "success": bool(result.get("success")),
    }


def _log_graph_output(
    user_id: str, trace_id: str | None, state: dict, messages: list
) -> None:
    """一行日志说清这一轮走的是哪个出口：等确认 / 空输出 / 有结果（带产物数）。

    投递搬去 worker 之后这几行跟着回合终点走，不跟着发送走：它记的是"图给了
    什么"，不是"平台收到了什么"。
    """

    if "__interrupt__" in state:
        logger.info("图输出等待用户确认 user_id=%s trace_id=%s", user_id, trace_id)
    elif state.get("result") is None:
        if not messages:
            logger.info("图输出为空 user_id=%s trace_id=%s", user_id, trace_id)
    else:
        logger.info(
            "图输出完成 user_id=%s trace_id=%s success=%s artifact_count=%d",
            user_id,
            trace_id,
            state["result"].get("success"),
            len(messages) - 1,
        )


def _wake_delivery_worker(wakeup: asyncio.Event | None) -> None:
    """入队之后叫醒常驻投递 worker，省掉最多一轮（1 秒）轮询等待。

    调用点一律在放开 session 锁之后。没有 worker 的调用方（测试、eval）传 None。
    """

    if wakeup is not None:
        wakeup.set()


def _outbound_from_row(row: dict) -> OutboundMessage:
    """outbox 行 -> 平台出站消息。file 行的 bytes 到这一刻才从磁盘读出来。

    引用解析（越界/不存在/文件名不一致）失败会抛，调用方按一次投递失败处理：
    产物没了是这条消息的问题，不该让整个 worker 陪葬。
    """

    payload = row["payload"]
    if row["kind"] == "file":
        path = resolve_artifact_reference(payload)
        return OutboundMessage(
            file=IncomingFile(
                filename=payload["filename"],
                content=path.read_bytes(),
                mime_type=payload["mime_type"],
            )
        )
    return OutboundMessage(text=payload["text"])


async def deliver_due_once(outbox: Outbox, platform, *, now: datetime) -> int:
    """把当前到期的一批（每 session 至多一条队头）寄出去，返回处理条数。

    一条 send 抛错绝不中断本批其余条目：这一批里每个会话互不相干，u1 的平台
    500 不该让 u2 的文件也压到下一轮。失败一律走 ``mark_failed``（退避/死信由
    它决定），成功走 ``mark_delivered``。
    """

    rows = outbox.due_batch(now)
    for row in rows:
        session_key = row["session_key"]
        # 队列里存的是 session_key（platform:user_id），平台要的是 user_id。
        user_id = session_key.split(":", 1)[1]
        outbox.mark_sending(row["id"])
        try:
            message = _outbound_from_row(row)
            async with asyncio.timeout(_SEND_TIMEOUT_SECONDS):
                await platform.send(user_id, message)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "消息投递失败 session=%s trace_id=%s id=%d kind=%s error=%s",
                session_key,
                row["trace_id"],
                row["id"],
                row["kind"],
                error,
            )
            # 重试排期的 INFO 和转死信的 WARNING 都由 mark_failed 自己记，这里
            # 不重复——死信告警重复两条会让"一条告警=一次人工介入"失效。
            outbox.mark_failed(row["id"], error, now=now)
        else:
            outbox.mark_delivered(row["id"], now=now)
            logger.info(
                "消息已送达 session=%s trace_id=%s id=%d kind=%s attempts=%d",
                session_key,
                row["trace_id"],
                row["id"],
                row["kind"],
                row["attempts"] + 1,
            )
    return len(rows)


async def delivery_worker(outbox: Outbox, platform, wakeup: asyncio.Event) -> None:
    """常驻投递协程：整个进程里唯一会调 ``platform.send`` 的地方。

    启动先 ``reset_sending``——上次进程崩在 sending 上时没人知道平台收到没有，
    一律重寄（at-least-once：宁可重发一次，不可静默丢件）。

    然后永循环取件。有件就立刻取下一批（连发同一会话的后续消息不必等一轮
    轮询），空批则等 ``wakeup`` 或 1 秒超时——入队方 set 之后这里立刻醒。

    外层兜住所有异常并继续：投递系统存活优先于任何单轮的正确性，worker 死了
    等于所有用户的消息全部停发。
    """

    reset = outbox.reset_sending()
    if reset:
        logger.info("投递 worker 启动：%d 条卡在 sending 的消息将重寄", reset)
    last_purge: float | None = None
    while True:
        try:
            now = datetime.now()
            handled = await deliver_due_once(outbox, platform, now=now)
            if (
                last_purge is None
                or time.monotonic() - last_purge >= _SEEN_PURGE_INTERVAL_SECONDS
            ):
                last_purge = time.monotonic()
                purged = outbox.purge_expired_seen(now=now)
                if purged:
                    logger.info("清理过期的事件去重记录 %d 条", purged)
            if handled == 0:
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=_IDLE_POLL_SECONDS)
                except TimeoutError:
                    pass  # 没人叫醒就是没新件，正常路径
                wakeup.clear()
        except Exception:
            logger.exception("投递 worker 本轮异常，继续下一轮")
            # 故障持续时（比如库文件锁死）不空转刷屏，退到轮询周期上重试。
            await asyncio.sleep(_IDLE_POLL_SECONDS)


async def maybe_run_compaction(graph, config: dict, summarizer) -> None:
    """投递完成后、仍在 session 锁内发起的后台压缩回合。"""

    if summarizer is None:
        return
    snapshot = await graph.aget_state(config=config)
    if snapshot.next or snapshot.interrupts:
        return  # interrupt 等待中不触发；对该状态做非 resume invoke 是未定义行为
    pending = snapshot.values.get("pending_compaction") or []
    if len(pending) < COMPACTION_BATCH_SIZE:
        return
    await graph.ainvoke(
        {"new_compaction_request": True}, config=config, durability="sync"
    )
    # 刻意不组装/入队这一轮的输出：compaction 无用户输出，且 build_outbound_messages
    # 对 pending_files 非空的状态会重发“收到文件”提示。


async def _log_conversation_turn(
    *,
    platform_name: str,
    user_id: str,
    trace_id: str | None,
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
                run_id=trace_id,
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
    trace_id: str,
    summarizer=None,
    *,
    outbox: Outbox,
    wakeup: asyncio.Event | None = None,
) -> None:
    """一个防抖回合：图 -> 组装 -> 入队 -> turn log -> 压缩 -> 叫醒 worker。

    ``platform`` 参数保留但本函数不再使用：发送整体搬去了投递 worker，回合终点
    只负责把这一轮的输出写进 outbox。
    """

    session_key = _session_key(platform_name, user_id)
    started = time.monotonic()
    logger.info(
        "开始处理防抖回合 session=%s trace_id=%s text_chars=%d files=%r",
        session_key,
        trace_id,
        len(combined_text),
        [item.filename for item in files],
    )
    async with locks.get(session_key):
        fallback_text = "处理失败，稍后再试一下"
        try:
            state, trace_id = await _invoke_from_event(
                graph,
                config={"configurable": {"thread_id": session_key}},
                platform_name=platform_name,
                user_id=user_id,
                text=combined_text,
                files=files,
                trace_id=trace_id,
            )
        except Exception as exc:
            logger.exception(
                "orchestrator 处理失败 user_id=%s trace_id=%s", user_id, trace_id
            )
            # 兜底话术也走 outbox：直接 send 会让它跟着一次网络抖动一起消失。
            outbox.enqueue(
                session_key,
                trace_id,
                [("text", {"text": fallback_text})],
                now=datetime.now(),
            )
            await _log_conversation_turn(
                platform_name=platform_name,
                user_id=user_id,
                trace_id=trace_id,
                input_text=combined_text or None,
                input_filename=", ".join(item.filename for item in files) or None,
                output_text=fallback_text,
                output_filename=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                success=False,
                error=str(exc),
            )
        else:
            messages, summary = build_outbound_messages(state)
            _log_graph_output(user_id, trace_id, state, messages)
            if messages:
                outbox.enqueue(session_key, trace_id, messages, now=datetime.now())
            # 本轮时长在入队完成的这一刻定格：用户等待 = 图 + 本地入队。后面的
            # compaction 是后台回合，投递也已经交给 worker，都不属于“用户等待”。
            duration_ms = int((time.monotonic() - started) * 1000)
            try:
                # 压缩失败不改变本轮业务结果：compact 节点内部已有重试语义，这层
                # 兜的是 invoke 本身的意外（如 checkpoint IO 错）。
                await maybe_run_compaction(
                    graph, {"configurable": {"thread_id": session_key}}, summarizer
                )
            except Exception:
                logger.exception(
                    "压缩回合失败 session=%s trace_id=%s", session_key, trace_id
                )
            # success 直接照抄图小结：投递成败已经不在这一层，没有 delivery_error。
            await _log_conversation_turn(
                platform_name=platform_name,
                user_id=user_id,
                trace_id=trace_id,
                input_text=combined_text or None,
                input_filename=", ".join(item.filename for item in files) or None,
                output_text=summary["output_text"],
                output_filename=summary["output_filename"],
                duration_ms=duration_ms,
                success=summary["success"],
            )
            logger.info(
                "防抖回合处理结束 session=%s trace_id=%s duration_ms=%d",
                session_key,
                trace_id,
                duration_ms,
            )
    _wake_delivery_worker(wakeup)


async def handle_event(
    graph,
    platform: FeishuAdapter,
    debouncer: Debouncer,
    locks: UserLocks,
    memory_repository: JsonMemoryRepository,
    event: InboundEvent,
    summarizer=None,
    *,
    outbox: Outbox,
    wakeup: asyncio.Event | None = None,
) -> None:
    """路由一条平台事件：长期记忆命令 / 确认恢复回合 / 交给防抖攒批。

    ``platform`` 参数保留但本函数不再使用，理由同 ``dispatch_fresh``：所有出站
    消息一律先入 outbox，发送由投递 worker 独占。
    """

    if not event.text and event.file is None:
        return

    session_key = _session_key(event.platform, event.user_id)
    if event.file is None and (event.text or "").strip() == LONG_TERM_MEMORY_COMMAND:
        started = time.monotonic()
        trace_id = uuid.uuid4().hex[:8]
        async with locks.get(session_key):
            try:
                output_text = render_memory_snapshot(
                    memory_repository.load(event.platform, event.user_id)
                )
                error = None
            except Exception as exc:
                logger.exception("查询长期记忆失败 user_id=%s", event.user_id)
                output_text = "长期记忆这次没有读取成功，请稍后再试。"
                error = str(exc)
            outbox.enqueue(
                session_key,
                trace_id,
                [("text", {"text": output_text})],
                now=datetime.now(),
            )
            await _log_conversation_turn(
                platform_name=event.platform,
                user_id=event.user_id,
                trace_id=trace_id,
                input_text=event.text,
                input_filename=None,
                output_text=output_text,
                output_filename=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                success=error is None,
                error=error,
            )
        _wake_delivery_worker(wakeup)
        return

    # 这一轮是否已经在锁内处理完（确认恢复 / 异常状态拒绝）；否则交给防抖攒批。
    handled = False
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
                trace_id = snapshot.values.get("trace_id")
                logger.info(
                    "恢复确认中的回合 session=%s trace_id=%s", session_key, trace_id
                )
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
                handled = True
                messages, summary = build_outbound_messages(resumed_state)
                _log_graph_output(event.user_id, trace_id, resumed_state, messages)
                if messages:
                    outbox.enqueue(session_key, trace_id, messages, now=datetime.now())
                # 同 dispatch_fresh：时长在入队完成时定格，不含后台压缩。
                duration_ms = int((time.monotonic() - started) * 1000)
                try:
                    # 同 dispatch_fresh：压缩失败只记录，不改变本轮业务结果。
                    await maybe_run_compaction(graph, config, summarizer)
                except Exception:
                    logger.exception(
                        "压缩回合失败 session=%s trace_id=%s", session_key, trace_id
                    )
                await _log_conversation_turn(
                    platform_name=event.platform,
                    user_id=event.user_id,
                    trace_id=trace_id,
                    input_text=event.text,
                    input_filename=event.file.filename if event.file else None,
                    output_text=summary["output_text"],
                    output_filename=summary["output_filename"],
                    duration_ms=duration_ms,
                    success=summary["success"],
                )
                logger.info(
                    "确认回合处理结束 session=%s trace_id=%s duration_ms=%d",
                    session_key,
                    trace_id,
                    duration_ms,
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
                handled = True
                outbox.enqueue(
                    session_key,
                    None,
                    [
                        (
                            "text",
                            {
                                "text": "上一次处理留下了异常状态，我没有执行你这条新消息，请联系维护者恢复会话。"
                            },
                        )
                    ],
                    now=datetime.now(),
                )
    except Exception:
        logger.exception("orchestrator 查询/恢复失败 user_id=%s", event.user_id)
        outbox.enqueue(
            session_key,
            None,
            [("text", {"text": "处理失败，稍后再试一下"})],
            now=datetime.now(),
        )
        _wake_delivery_worker(wakeup)
        return

    if handled:
        _wake_delivery_worker(wakeup)
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
        summarizer = ClaudeAgentSummarizer()
        graph = build_graph(
            main_agent,
            backend,
            memory_repository,
            checkpointer=checkpointer,
            summarizer=summarizer,
        )
        locks = UserLocks()
        outbox = Outbox()
        # 入队方叫醒投递 worker 的信号，省掉最多一轮轮询等待。
        wakeup = asyncio.Event()
        debouncer = Debouncer(
            DEBOUNCE_WINDOW_SECONDS,
            on_ready=lambda platform_name, user_id, text, files, trace_id: dispatch_fresh(
                graph,
                platform,
                platform_name,
                user_id,
                text,
                files,
                locks,
                trace_id,
                summarizer=summarizer,
                outbox=outbox,
                wakeup=wakeup,
            ),
        )

        platform.start()
        # 不做优雅停机：进程退出就地停发，未送达的消息下次启动由 reset_sending +
        # due_batch 接着寄——这正是 at-least-once 想要的语义。局部变量持有引用，
        # 免得 task 被 GC 掉（asyncio 只持弱引用）。
        delivery_task = asyncio.create_task(delivery_worker(outbox, platform, wakeup))
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
                    summarizer=summarizer,
                    outbox=outbox,
                    wakeup=wakeup,
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
