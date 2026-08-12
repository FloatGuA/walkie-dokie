"""按用户防抖攒消息：10 秒内的连续消息拼成一轮，而不是逐条各当一次请求。

只在"用户发起一轮新请求"时用——如果这个用户正在等确认（图已经 interrupt
暂停），调用方应该跳过防抖，直接把回复喂给 Command(resume=...)，见
scripts/run_mvp.py 里对 snapshot.interrupts 的判断。

文字和文件都会被这个窗口攒住：用户可能先发文件再说要干什么，也可能反过来，
窗口到期时把攒到的文字拼成一段、文件取最后收到的那个一起交给 on_ready。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from walkie_dokie.platforms.base import IncomingFile

logger = logging.getLogger(__name__)


class Debouncer:
    def __init__(
        self,
        window_seconds: float,
        on_ready: Callable[[str, str, str, IncomingFile | None], Awaitable[None]],
    ):
        self._window = window_seconds
        self._on_ready = on_ready
        self._buffers: dict[tuple[str, str], list[str]] = {}
        self._files: dict[tuple[str, str], IncomingFile] = {}
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
            self._files[key] = file
        if key in self._tasks:
            self._tasks[key].cancel()
        self._tasks[key] = asyncio.create_task(self._fire_after_delay(key))
        logger.info(
            "防抖窗口重置 platform=%s user_id=%s，累计 %d 条文字待处理，file=%r",
            platform,
            user_id,
            len(self._buffers.get(key, [])),
            self._files.get(key, None) and self._files[key].filename,
        )

    async def _fire_after_delay(self, key: tuple[str, str]) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            return
        platform, user_id = key
        messages = self._buffers.pop(key, [])
        file = self._files.pop(key, None)
        self._tasks.pop(key, None)
        if not messages and file is None:
            return
        combined = "\n".join(messages)
        logger.info(
            "防抖窗口到期 user_id=%s，%d 条文字 + file=%r 合并派发", user_id, len(messages), file and file.filename
        )
        await self._on_ready(platform, user_id, combined, file)

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
