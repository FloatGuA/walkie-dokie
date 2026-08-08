"""按用户防抖攒消息：10 秒内的连续消息拼成一轮，而不是逐条各当一次请求。

只在"用户发起一轮新请求"时用——如果这个用户正在等确认（图已经 interrupt
暂停），调用方应该跳过防抖，直接把回复喂给 Command(resume=...)，见
scripts/run_mvp.py 里对 aget_state().next 的判断。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class Debouncer:
    def __init__(self, window_seconds: float, on_ready: Callable[[str, str], Awaitable[None]]):
        self._window = window_seconds
        self._on_ready = on_ready
        self._buffers: dict[str, list[str]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def add(self, user_id: str, text: str) -> None:
        self._buffers.setdefault(user_id, []).append(text)
        if user_id in self._tasks:
            self._tasks[user_id].cancel()
        self._tasks[user_id] = asyncio.create_task(self._fire_after_delay(user_id))
        logger.info("防抖窗口重置 user_id=%s，累计 %d 条消息待处理", user_id, len(self._buffers[user_id]))

    async def _fire_after_delay(self, user_id: str) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            return
        messages = self._buffers.pop(user_id, [])
        self._tasks.pop(user_id, None)
        if not messages:
            return
        combined = "\n".join(messages)
        logger.info("防抖窗口到期 user_id=%s，%d 条消息合并派发", user_id, len(messages))
        await self._on_ready(user_id, combined)
