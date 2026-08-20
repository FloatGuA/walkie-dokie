"""按用户防抖攒消息：10 秒内的连续消息拼成一轮，而不是逐条各当一次请求。

只在"用户发起一轮新请求"时用——如果这个用户正在等确认（图已经 interrupt
暂停），调用方应该跳过防抖，直接把回复喂给 Command(resume=...)，见
scripts/run_mvp.py 里对 snapshot.interrupts 的判断。

文字和文件都会被这个窗口攒住：用户可能先发文件再说要干什么，也可能反过来，
窗口到期时把攒到的文字拼成一段、文件按到达顺序全部交给 on_ready（不是只留
最后一个——早前实现只留最后收到的文件，窗口内连发多个文件时前面的会被静默
覆盖丢弃，是已知修过的缺口，见 DECISION.md 2026-08-18）。
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from walkie_dokie.platforms.base import IncomingFile

logger = logging.getLogger(__name__)


class Debouncer:
    def __init__(
        self,
        window_seconds: float,
        on_ready: Callable[[str, str, str, tuple[IncomingFile, ...], str], Awaitable[None]],
    ):
        self._window = window_seconds
        self._on_ready = on_ready
        self._buffers: dict[tuple[str, str], list[str]] = {}
        self._files: dict[tuple[str, str], list[IncomingFile]] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    def add(
        self,
        platform: str,
        user_id: str,
        text: str | None,
        file: IncomingFile | None = None,
    ) -> None:
        key = (platform, user_id)
        if text:
            self._buffers.setdefault(key, []).append(text)
        if file is not None:
            self._files.setdefault(key, []).append(file)
        if key in self._tasks:
            self._tasks[key].cancel()
        self._tasks[key] = asyncio.create_task(self._fire_after_delay(key))
        logger.info(
            "防抖窗口重置 platform=%s user_id=%s，累计 %d 条文字 + %d 个文件待处理",
            platform,
            user_id,
            len(self._buffers.get(key, [])),
            len(self._files.get(key, [])),
        )

    async def _fire_after_delay(self, key: tuple[str, str]) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            return
        platform, user_id = key
        messages = self._buffers.pop(key, [])
        files = tuple(self._files.pop(key, []))
        self._tasks.pop(key, None)
        if not messages and not files:
            return
        combined = "\n".join(messages)
        trace_id = uuid.uuid4().hex[:8]
        logger.info(
            "防抖窗口到期 user_id=%s trace_id=%s，%d 条文字 + %d 个文件合并派发",
            user_id,
            trace_id,
            len(messages),
            len(files),
        )
        await self._on_ready(platform, user_id, combined, files, trace_id)

    async def close(self) -> None:
        """Cancel pending windows so application shutdown can drain cleanly."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._buffers.clear()
        self._files.clear()
