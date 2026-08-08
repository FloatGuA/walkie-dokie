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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from walkie_dokie.agents.claude_agent import ClaudeAgentSDKBackend
from walkie_dokie.logging_config import setup_logging
from walkie_dokie.orchestrator import build_graph
from walkie_dokie.orchestrator.debounce import Debouncer
from walkie_dokie.orchestrator.locks import UserLocks
from walkie_dokie.platforms.base import IncomingFile, InboundEvent, OutboundMessage
from walkie_dokie.platforms.feishu import FeishuAdapter

load_dotenv()
setup_logging()
logger = logging.getLogger(__name__)

DEBOUNCE_WINDOW_SECONDS = 10.0


async def deliver_graph_output(platform: FeishuAdapter, user_id: str, state: dict) -> None:
    if "__interrupt__" in state:
        draft = state["__interrupt__"][0].value["draft_task_prompt"]
        if draft["missing_info"]:
            missing = "、".join(draft["missing_info"])
            text = (
                f"我理解你想要：{draft['task_summary']}\n\n"
                f"还缺这些信息：{missing}\n\n"
                "可以直接告诉我，或者回'是'我就用通用内容直接生成。"
            )
        else:
            text = f"你的意思是不是——{draft['task_summary']}\n\n回复'是'确认，或者继续补充说明"
        await platform.send(user_id, OutboundMessage(text=text))
        return

    result = state.get("result")
    if result is None:
        pending_file = state.get("pending_file")
        if pending_file is not None:
            # 飞书发文件时不能带文字，只能分开发——收到文件但还没指令是正常情况，
            # 得主动回一句，不能沉默，不然用户不知道文件收到没有。
            await platform.send(
                user_id, OutboundMessage(text=f"收到文件「{pending_file.filename}」了，请告诉我需要我做什么。")
            )
        return

    if result["result_file"] is not None:
        await platform.send(
            user_id,
            OutboundMessage(
                file=IncomingFile(
                    filename=result["result_filename"],
                    content=result["result_file"],
                    mime_type="application/octet-stream",
                )
            ),
        )
    await platform.send(user_id, OutboundMessage(text=result["reply_text"]))


async def dispatch_fresh(
    graph,
    platform: FeishuAdapter,
    user_id: str,
    combined_text: str,
    file: IncomingFile | None,
    locks: UserLocks,
) -> None:
    async with locks.get(user_id):
        try:
            state = await graph.ainvoke(
                {
                    "platform": "feishu",
                    "user_id": user_id,
                    "new_text": combined_text or None,
                    "new_file": file,
                },
                config={"configurable": {"thread_id": user_id}},
            )
        except Exception:
            logger.exception("orchestrator 处理失败 user_id=%s", user_id)
            await platform.send(user_id, OutboundMessage(text="处理失败，稍后再试一下"))
            return
    await deliver_graph_output(platform, user_id, state)


async def resume_pending(
    graph, platform: FeishuAdapter, user_id: str, reply_text: str, locks: UserLocks
) -> None:
    async with locks.get(user_id):
        try:
            state = await graph.ainvoke(
                Command(resume=reply_text), config={"configurable": {"thread_id": user_id}}
            )
        except Exception:
            logger.exception("orchestrator 恢复失败 user_id=%s", user_id)
            await platform.send(user_id, OutboundMessage(text="处理失败，稍后再试一下"))
            return
    await deliver_graph_output(platform, user_id, state)


async def handle_event(
    graph, platform: FeishuAdapter, debouncer: Debouncer, locks: UserLocks, event: InboundEvent
) -> None:
    if not event.text and event.file is None:
        return

    snapshot = await graph.aget_state(config={"configurable": {"thread_id": event.user_id}})
    if snapshot.next:
        # 这个用户正卡在 ask_confirm 等回复——直接当确认/补充处理，不走防抖。
        # 确认回复目前只看文字；如果用户这时候发的是文件，忽略文字部分为空的情况。
        await resume_pending(graph, platform, event.user_id, event.text or "", locks)
    else:
        debouncer.add(event.user_id, event.text, event.file)


async def main():
    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]

    platform = FeishuAdapter(app_id, app_secret)
    backend = ClaudeAgentSDKBackend()
    graph = build_graph(backend, checkpointer=InMemorySaver())
    locks = UserLocks()
    debouncer = Debouncer(
        DEBOUNCE_WINDOW_SECONDS,
        on_ready=lambda user_id, text, file: dispatch_fresh(graph, platform, user_id, text, file, locks),
    )

    platform.start()
    logger.info("MVP 胶水循环已启动，等待飞书消息……（Ctrl+C 停止）")

    while True:
        event = await platform.receive()
        asyncio.create_task(handle_event(graph, platform, debouncer, locks, event))


asyncio.run(main())
