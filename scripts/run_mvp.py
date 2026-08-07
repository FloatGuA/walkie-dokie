"""MVP 端到端胶水脚本：飞书收消息 -> Claude Agent SDK 处理 -> 飞书发结果回去。

不经过 orchestrator，先跑通最糙的路径。用法：python scripts/run_mvp.py，
跑起来之后在飞书里找 Wokie-Dokie智能助手 发一句话（比如"帮我写一份请假条"）。
Ctrl+C 停止。
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv

from walkie_dokie.agents.claude_agent import ClaudeAgentSDKBackend
from walkie_dokie.logging_config import setup_logging
from walkie_dokie.platforms.base import IncomingFile, OutboundMessage
from walkie_dokie.platforms.feishu import FeishuAdapter

load_dotenv()
setup_logging()
logger = logging.getLogger(__name__)


async def main():
    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]

    platform = FeishuAdapter(app_id, app_secret)
    backend = ClaudeAgentSDKBackend()

    platform.start()
    logger.info("MVP 胶水循环已启动，等待飞书消息……（Ctrl+C 停止）")

    while True:
        event = await platform.receive()

        if not event.text:
            continue

        try:
            result = await backend.run(instruction=event.text, input_file=None)
        except Exception as e:
            logger.exception("执行失败")
            await platform.send(event.user_id, OutboundMessage(text=f"处理失败：{e}"))
            continue

        if result.result_file is not None:
            await platform.send(
                event.user_id,
                OutboundMessage(
                    file=IncomingFile(
                        filename=result.result_filename,
                        content=result.result_file,
                        mime_type="application/octet-stream",
                    )
                ),
            )
        await platform.send(event.user_id, OutboundMessage(text=result.reply_text))


asyncio.run(main())
