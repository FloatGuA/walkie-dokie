import asyncio

import pytest

from walkie_dokie.orchestrator.debounce import Debouncer
from walkie_dokie.platforms.base import IncomingFile


@pytest.fixture
def collected():
    return []


def _recorder(collected):
    async def on_ready(user_id, text, file):
        collected.append((user_id, text, file))

    return on_ready


async def test_single_message_fires_after_window(collected):
    d = Debouncer(0.05, _recorder(collected))
    d.add("u1", "帮我写份文档")
    await asyncio.sleep(0.15)
    assert collected == [("u1", "帮我写份文档", None)]


async def test_multiple_messages_in_window_merge_and_reset_timer(collected):
    d = Debouncer(0.08, _recorder(collected))
    d.add("u1", "第一句")
    await asyncio.sleep(0.03)
    d.add("u1", "第二句")  # 应该重置计时器，不是各自独立触发一次
    await asyncio.sleep(0.03)
    assert collected == []  # 这时候还没到 0.08s（从第二句算），不该触发
    await asyncio.sleep(0.1)
    assert collected == [("u1", "第一句\n第二句", None)]


async def test_file_and_text_arriving_separately_are_combined(collected):
    file = IncomingFile(filename="a.docx", content=b"x", mime_type="application/octet-stream")
    d = Debouncer(0.08, _recorder(collected))
    d.add("u1", None, file)  # 飞书发文件不能带文字，先单独收到文件
    await asyncio.sleep(0.03)
    d.add("u1", "总结一下")  # 再补一句指令
    await asyncio.sleep(0.15)
    assert collected == [("u1", "总结一下", file)]


async def test_different_users_fire_independently(collected):
    d = Debouncer(0.05, _recorder(collected))
    d.add("u1", "来自 u1")
    d.add("u2", "来自 u2")
    await asyncio.sleep(0.15)
    assert set(collected) == {("u1", "来自 u1", None), ("u2", "来自 u2", None)}
