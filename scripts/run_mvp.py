"""MVP 端到端胶水脚本：飞书收消息 -> orchestrator（LangGraph 会话状态机）-> Claude Agent SDK -> 飞书发结果回去。

用法：python scripts/run_mvp.py，跑起来之后在飞书里找 Wokie-Dokie智能助手
发一句话（比如"帮我写一份请假条"）。Ctrl+C 停止。

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

from walkie_dokie.agents.claude_agent import ClaudeAgentSDKBackend
from walkie_dokie.logging_config import setup_logging
from walkie_dokie.orchestrator import build_graph
from walkie_dokie.platforms.base import IncomingFile, InboundEvent, OutboundMessage
from walkie_dokie.platforms.feishu import FeishuAdapter

load_dotenv()
setup_logging()
logger = logging.getLogger(__name__)


async def handle_event(graph, platform: FeishuAdapter, event: InboundEvent) -> None:
    try:
        state = await graph.ainvoke(
            {
                "platform": event.platform,
                "user_id": event.user_id,
                "new_text": event.text,
                "new_file": event.file,
            },
            config={"configurable": {"thread_id": event.user_id}},
        )
    except Exception:
        logger.exception("orchestrator 处理失败 user_id=%s", event.user_id)
        await platform.send(event.user_id, OutboundMessage(text="处理失败，稍后再试一下"))
        return

    result = state.get("result")
    if result is None:
        # 信息还没收齐（比如只发了文件没说要干什么），等下一条消息
        return

    if result["result_file"] is not None:
        await platform.send(
            event.user_id,
            OutboundMessage(
                file=IncomingFile(
                    filename=result["result_filename"],
                    content=result["result_file"],
                    mime_type="application/octet-stream",
                )
            ),
        )
    await platform.send(event.user_id, OutboundMessage(text=result["reply_text"]))


async def main():
    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]

    platform = FeishuAdapter(app_id, app_secret)
    backend = ClaudeAgentSDKBackend()
    graph = build_graph(backend, checkpointer=InMemorySaver())

    platform.start()
    logger.info("MVP 胶水循环已启动，等待飞书消息……（Ctrl+C 停止）")

    while True:
        event = await platform.receive()
        asyncio.create_task(handle_event(graph, platform, event))


asyncio.run(main())
