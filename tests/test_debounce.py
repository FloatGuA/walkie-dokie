import asyncio

import pytest

from walkie_dokie.orchestrator.debounce import Debouncer
from walkie_dokie.platforms.base import IncomingFile


@pytest.fixture
def collected():
    return []


def _recorder(collected):
    async def on_ready(platform, user_id, text, files, trace_id):
        assert trace_id
        collected.append((platform, user_id, text, files))

    return on_ready


async def test_single_message_fires_after_window(collected):
    d = Debouncer(0.05, _recorder(collected))
    d.add("test", "u1", "帮我写份文档")
    await asyncio.sleep(0.15)
    assert collected == [("test", "u1", "帮我写份文档", ())]


async def test_multiple_messages_in_window_merge_and_reset_timer(collected):
    d = Debouncer(0.08, _recorder(collected))
    d.add("test", "u1", "第一句")
    await asyncio.sleep(0.03)
    d.add("test", "u1", "第二句")  # 应该重置计时器，不是各自独立触发一次
    await asyncio.sleep(0.03)
    assert collected == []  # 这时候还没到 0.08s（从第二句算），不该触发
    await asyncio.sleep(0.1)
    assert collected == [("test", "u1", "第一句\n第二句", ())]


async def test_file_and_text_arriving_separately_are_combined(collected):
    file = IncomingFile(filename="a.docx", content=b"x", mime_type="application/octet-stream")
    d = Debouncer(0.08, _recorder(collected))
    d.add("test", "u1", None, file)  # 飞书发文件不能带文字，先单独收到文件
    await asyncio.sleep(0.03)
    d.add("test", "u1", "总结一下")  # 再补一句指令
    await asyncio.sleep(0.15)
    assert collected == [("test", "u1", "总结一下", (file,))]


async def test_different_users_fire_independently(collected):
    d = Debouncer(0.05, _recorder(collected))
    d.add("test", "u1", "来自 u1")
    d.add("test", "u2", "来自 u2")
    await asyncio.sleep(0.15)
    assert set(collected) == {
        ("test", "u1", "来自 u1", ()),
        ("test", "u2", "来自 u2", ()),
    }


async def test_same_user_id_on_different_platforms_is_not_merged(collected):
    d = Debouncer(0.05, _recorder(collected))
    d.add("feishu", "same", "飞书消息")
    d.add("wecom", "same", "企微消息")
    await asyncio.sleep(0.15)
    assert set(collected) == {
        ("feishu", "same", "飞书消息", ()),
        ("wecom", "same", "企微消息", ()),
    }


async def test_close_cancels_pending_windows(collected):
    d = Debouncer(1.0, _recorder(collected))
    d.add("test", "u1", "不会派发")
    await d.close()
    await asyncio.sleep(0)
    assert collected == []


async def test_fired_batch_includes_a_nonempty_trace_id():
    collected = []

    async def on_ready(platform, user_id, text, files, trace_id):
        collected.append(trace_id)

    d = Debouncer(0.05, on_ready)
    d.add("test", "u1", "帮我写份文档")
    await asyncio.sleep(0.15)
    assert collected and collected[0]


async def test_multiple_files_in_same_window_are_accumulated_not_overwritten(collected):
    file_a = IncomingFile(filename="a.docx", content=b"a", mime_type="application/octet-stream")
    file_b = IncomingFile(filename="b.docx", content=b"b", mime_type="application/octet-stream")
    d = Debouncer(0.08, _recorder(collected))
    d.add("test", "u1", None, file_a)
    await asyncio.sleep(0.03)
    d.add("test", "u1", None, file_b)
    await asyncio.sleep(0.15)
    assert collected == [("test", "u1", "", (file_a, file_b))]
