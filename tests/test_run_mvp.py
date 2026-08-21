import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import walkie_dokie.artifacts as artifact_store
from scripts.run_mvp import (
    _invoke_from_event,
    _waiting_for_confirmation,
    build_outbound_messages,
    deliver_due_once,
    delivery_worker,
    dispatch_fresh,
    handle_event,
)
from walkie_dokie.main_agent.base import MemoryOperation
from walkie_dokie.main_agent.memory import JsonMemoryRepository
from walkie_dokie.orchestrator.locks import UserLocks
from walkie_dokie.orchestrator.outbox import Outbox
from walkie_dokie.platforms.base import IncomingFile, InboundEvent


class FakePlatform:
    """回合终点改为入队之后，``dispatch_fresh``/``handle_event`` 不再自己发送。

    这个 fake 留着是因为两个函数仍然收 ``platform``（投递 worker 用），它的
    ``sent`` 现在是一条负向断言：回合期间必须一条都没发。
    """

    def __init__(self):
        self.sent = []

    async def send(self, user_id, message):
        self.sent.append((user_id, message))


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "outbox.db"


@pytest.fixture
def outbox(db_path):
    return Outbox(db_path)


def _outbox_rows(db_path):
    """直读 outbox 表：``due_batch`` 每 session 只给队头，断言"整批入队"要看全部行。"""

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in connection.execute("SELECT * FROM outbox ORDER BY id")
        ]
    finally:
        connection.close()
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return rows


class _StateGraph:
    """fresh 回合的 fake graph：会话空闲，``ainvoke`` 直接返回预置的图输出状态。"""

    def __init__(self, state):
        self._state = state

    async def aget_state(self, config):
        return SimpleNamespace(next=(), interrupts=(), values={})

    async def ainvoke(self, value, config, durability=None):
        return self._state


def test_only_real_ask_confirm_interrupt_is_resumable():
    waiting = SimpleNamespace(
        next=("ask_confirm",), interrupts=(object(),)
    )
    failed = SimpleNamespace(next=("main_agent",), interrupts=())
    wrong_interrupt = SimpleNamespace(next=("other",), interrupts=(object(),))
    assert _waiting_for_confirmation(waiting) is True
    assert _waiting_for_confirmation(failed) is False
    assert _waiting_for_confirmation(wrong_interrupt) is False
    memory_waiting = SimpleNamespace(next=("ask_memory",), interrupts=(object(),))
    assert _waiting_for_confirmation(memory_waiting) is True


async def test_fresh_invoke_carries_caller_trace_id_into_initial_state():
    class Graph:
        def __init__(self):
            self.input = None

        async def aget_state(self, config):
            return SimpleNamespace(next=(), interrupts=(), values={})

        async def ainvoke(self, value, config, durability=None):
            self.input = value
            return {"result": None}

    graph = Graph()
    _state, effective_trace_id = await _invoke_from_event(
        graph,
        config={"configurable": {"thread_id": "test:u1"}},
        platform_name="test",
        user_id="u1",
        text="帮我写份文档",
        trace_id="new-batch-id",
    )
    assert graph.input["trace_id"] == "new-batch-id"
    assert effective_trace_id == "new-batch-id"


async def test_confirm_race_resume_reuses_snapshots_trace_id_not_callers():
    """回合正在等确认时，一个新 debounce 批次带着自己新生成的 trace_id 赶到——
    这时候应该沿用原任务在 snapshot 里已经落盘的 trace_id，而不是让新批次的
    id 覆盖这一整轮任务的追踪标识。"""

    class Graph:
        def __init__(self):
            self.input = None

        async def aget_state(self, config):
            return SimpleNamespace(
                next=("ask_confirm",),
                interrupts=(object(),),
                values={"trace_id": "original-propose-id"},
            )

        async def ainvoke(self, value, config, durability=None):
            self.input = value
            return {"result": None}

    graph = Graph()
    _state, effective_trace_id = await _invoke_from_event(
        graph,
        config={"configurable": {"thread_id": "test:u1"}},
        platform_name="test",
        user_id="u1",
        text="是",
        trace_id="new-batch-id",
    )
    assert effective_trace_id == "original-propose-id"


async def test_debounced_dispatch_rechecks_and_resumes_new_interrupt():
    class Graph:
        def __init__(self):
            self.input = None
            self.durability = None

        async def aget_state(self, config):
            return SimpleNamespace(
                next=("ask_confirm",), interrupts=(object(),), values={"trace_id": "t1"}
            )

        async def ainvoke(self, value, config, durability=None):
            self.input = value
            self.durability = durability
            return {"result": None}

    graph = Graph()
    await _invoke_from_event(
        graph,
        config={"configurable": {"thread_id": "test:u1"}},
        platform_name="test",
        user_id="u1",
        text="是",
        trace_id="new-batch-id",
    )
    assert graph.input.resume == {"text": "是", "files": ()}
    assert graph.durability == "sync"


async def test_debounced_batch_survives_confirm_race_with_multiple_files(
    monkeypatch, tmp_path
):
    """回归 Finding 1：确认竞态发生时，整批文件必须全部随 resume 送达图，
    而不是被静默丢弃（只有 text 被当成确认回复送过去）。"""

    root = tmp_path / "inputs"
    monkeypatch.setattr(artifact_store, "INPUT_ARTIFACTS_ROOT", root)

    class Graph:
        def __init__(self):
            self.input = None
            self.durability = None

        async def aget_state(self, config):
            return SimpleNamespace(
                next=("ask_confirm",), interrupts=(object(),), values={"trace_id": "t1"}
            )

        async def ainvoke(self, value, config, durability=None):
            self.input = value
            self.durability = durability
            return {"result": None}

    graph = Graph()
    file_a = IncomingFile("a.docx", b"a", "application/octet-stream")
    file_b = IncomingFile("b.docx", b"b", "application/octet-stream")
    await _invoke_from_event(
        graph,
        config={"configurable": {"thread_id": "test:u1"}},
        platform_name="test",
        user_id="u1",
        text="是",
        files=(file_a, file_b),
        trace_id="new-batch-id",
    )
    assert graph.input.resume["text"] == "是"
    resumed_files = graph.input.resume["files"]
    assert [ref["filename"] for ref in resumed_files] == ["a.docx", "b.docx"]
    assert graph.durability == "sync"


async def test_artifact_is_enqueued_before_main_agent_text(
    monkeypatch, tmp_path, outbox, db_path
):
    """"文件先于文字"这条投递承诺现在由入队顺序承担：seq 决定投递序，file 行
    只带 reference，bytes 留给投递 worker 发送时再读。"""

    root = tmp_path / "workspaces"
    root.mkdir()
    artifact = root / "result.docx"
    artifact.write_bytes(b"document")
    monkeypatch.setattr(artifact_store, "WORKSPACES_ROOT", root)
    reference = artifact_store.output_artifact_reference(artifact, artifact.name)

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    platform = FakePlatform()

    await dispatch_fresh(
        _StateGraph(
            {
                "result": {
                    "artifacts": [reference],
                    "reply_text": "已经处理好了。",
                    "success": True,
                }
            }
        ),
        platform,
        "test",
        "u1",
        "帮我改一下",
        (),
        UserLocks(),
        trace_id="t1",
        outbox=outbox,
    )

    rows = _outbox_rows(db_path)
    assert [(row["seq"], row["kind"]) for row in rows] == [(0, "file"), (1, "text")]
    assert rows[0]["payload"] == reference
    assert rows[0]["payload"]["filename"] == "result.docx"
    assert "content" not in rows[0]["payload"]
    assert Path(rows[0]["payload"]["path"]).read_bytes() == b"document"
    assert rows[1]["payload"] == {"text": "已经处理好了。"}
    assert platform.sent == []


async def test_fresh_direct_reply_is_written_to_conversation_turn_log(
    monkeypatch, outbox
):
    class Graph:
        async def aget_state(self, config):
            return SimpleNamespace(next=(), interrupts=())

        async def ainvoke(self, value, config, durability=None):
            return {
                "result": {
                    "artifacts": [],
                    "reply_text": "我是小帮。",
                    "success": True,
                }
            }

    records = []

    async def fake_log_turn(record):
        records.append(record)

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    platform = FakePlatform()
    await dispatch_fresh(
        Graph(),
        platform,
        "test",
        "u1",
        "你是谁？",
        (),
        UserLocks(),
        trace_id="t1",
        outbox=outbox,
    )

    assert len(records) == 1
    assert records[0].record_type == "conversation"
    assert records[0].input_text == "你是谁？"
    assert records[0].output_text == "我是小帮。"
    assert records[0].success is True


async def test_dispatch_fresh_trace_id_is_recorded_as_conversation_run_id(
    monkeypatch, outbox, db_path
):
    class Graph:
        async def aget_state(self, config):
            return SimpleNamespace(next=(), interrupts=(), values={})

        async def ainvoke(self, value, config, durability=None):
            return {
                "result": {
                    "artifacts": [],
                    "reply_text": "我是小帮。",
                    "success": True,
                }
            }

    records = []

    async def fake_log_turn(record):
        records.append(record)

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    platform = FakePlatform()
    await dispatch_fresh(
        Graph(),
        platform,
        "test",
        "u1",
        "你是谁？",
        (),
        UserLocks(),
        trace_id="batch-42",
        outbox=outbox,
    )

    assert records[0].run_id == "batch-42"
    # 同一个 trace_id 也要写进 outbox 行：投递侧日志与 turn log 才能对得上。
    assert [row["trace_id"] for row in _outbox_rows(db_path)] == ["batch-42"]


async def test_handle_event_confirm_resume_reuses_snapshots_trace_id(
    monkeypatch, outbox, db_path
):
    class Graph:
        async def aget_state(self, config):
            return SimpleNamespace(
                next=("ask_confirm",),
                interrupts=(object(),),
                values={"trace_id": "original-propose-id"},
            )

        async def ainvoke(self, value, config, durability=None):
            return {
                "result": {
                    "artifacts": [],
                    "reply_text": "好的，已经处理。",
                    "success": True,
                }
            }

    records = []

    async def fake_log_turn(record):
        records.append(record)

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    platform = FakePlatform()
    await handle_event(
        Graph(),
        platform,
        object(),  # debouncer.add is unreachable once the resume branch handled it
        UserLocks(),
        object(),  # memory_repository is unreachable outside the /long-term-memory branch
        InboundEvent("test", "u1", "是", None),
        outbox=outbox,
    )

    assert records[0].run_id == "original-propose-id"
    assert [row["trace_id"] for row in _outbox_rows(db_path)] == [
        "original-propose-id"
    ]


async def test_concurrent_handle_event_calls_do_not_interleave_graph_access(
    monkeypatch, outbox
):
    """两个几乎同时到达的 handle_event 调用（同一 session，都命中确认分支）必须
    被 UserLocks 完全序列化——graph.aget_state/ainvoke 不能交错执行。这是
    handle_event 里"查询状态和 resume 决策必须和 ainvoke 使用同一把锁"那条注释
    背后的真实承诺；此前只有顺序模拟测过结果形状，从没用真并发（asyncio.gather）
    验证过锁真的挡住了交错。"""

    order = []

    class Graph:
        async def aget_state(self, config):
            order.append("aget_state-start")
            await asyncio.sleep(0.02)
            order.append("aget_state-end")
            return SimpleNamespace(
                next=("ask_confirm",), interrupts=(object(),), values={"trace_id": "t0"}
            )

        async def ainvoke(self, value, config, durability=None):
            order.append("ainvoke-start")
            await asyncio.sleep(0.02)
            order.append("ainvoke-end")
            return {
                "result": {"artifacts": [], "reply_text": "ok", "success": True}
            }

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    graph = Graph()
    platform = FakePlatform()
    locks = UserLocks()

    await asyncio.gather(
        handle_event(
            graph,
            platform,
            object(),  # debouncer.add is unreachable once the resume branch handled it
            locks,
            object(),  # memory_repository is unreachable outside /long-term-memory
            InboundEvent("test", "u1", "是", None),
            outbox=outbox,
        ),
        handle_event(
            graph,
            platform,
            object(),
            locks,
            object(),
            InboundEvent("test", "u1", "是，确认", None),
            outbox=outbox,
        ),
    )

    for i in range(0, len(order), 2):
        assert order[i].endswith("-start")
        assert order[i + 1].endswith("-end")
        assert order[i].split("-")[0] == order[i + 1].split("-")[0], (
            f"interleaved graph access detected: {order}"
        )


async def test_concurrent_dispatch_fresh_and_handle_event_resume_do_not_interleave(
    monkeypatch, outbox
):
    """一次由 debounce 触发的 dispatch_fresh，和一条几乎同时到达、直接命中确认
    分支的 handle_event，必须被同一把 UserLocks 完全序列化——这是上次真实
    confirm-race bug（commit 1201650）的场景，但这次用 asyncio.gather 真并发
    压出来，而不是手工摆好 resume payload 形状去验证结果。"""

    order = []

    class Graph:
        async def aget_state(self, config):
            order.append("aget_state-start")
            await asyncio.sleep(0.02)
            order.append("aget_state-end")
            return SimpleNamespace(
                next=("ask_confirm",), interrupts=(object(),), values={"trace_id": "t0"}
            )

        async def ainvoke(self, value, config, durability=None):
            order.append("ainvoke-start")
            await asyncio.sleep(0.02)
            order.append("ainvoke-end")
            return {
                "result": {"artifacts": [], "reply_text": "ok", "success": True}
            }

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    graph = Graph()
    platform = FakePlatform()
    locks = UserLocks()

    await asyncio.gather(
        dispatch_fresh(
            graph,
            platform,
            "test",
            "u1",
            "新一批消息",
            (),
            locks,
            trace_id="new-batch",
            outbox=outbox,
        ),
        handle_event(
            graph,
            platform,
            object(),
            locks,
            object(),
            InboundEvent("test", "u1", "是", None),
            outbox=outbox,
        ),
    )

    for i in range(0, len(order), 2):
        assert order[i].endswith("-start")
        assert order[i + 1].endswith("-end")
        assert order[i].split("-")[0] == order[i + 1].split("-")[0], (
            f"interleaved graph access detected: {order}"
        )


async def test_long_term_memory_command_bypasses_graph_and_debounce(
    monkeypatch, tmp_path, outbox, db_path
):
    class MustNotRun:
        def __getattr__(self, name):
            raise AssertionError(f"{name} should not be called")

    records = []

    async def fake_log_turn(record):
        records.append(record)

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    memory = JsonMemoryRepository(tmp_path / "memory")
    memory.apply(
        "test",
        "u1",
        (MemoryOperation("set", "name", "浮瓜", "我是浮瓜"),),
        source_text="我是浮瓜",
    )
    platform = FakePlatform()

    await handle_event(
        MustNotRun(),
        platform,
        MustNotRun(),
        UserLocks(),
        memory,
        InboundEvent("test", "u1", " /long-term-memory ", None),
        outbox=outbox,
    )

    rows = _outbox_rows(db_path)
    assert rows[0]["payload"]["text"] == "当前保存的长期记忆：\n姓名：浮瓜"
    assert rows[0]["session_key"] == "test:u1"
    assert platform.sent == []
    assert records[0].record_type == "conversation"
    assert records[0].success is True


async def test_multiple_artifacts_are_enqueued_before_text(
    monkeypatch, tmp_path, outbox, db_path
):
    root = tmp_path / "workspaces"
    root.mkdir()
    a = root / "a.docx"
    a.write_bytes(b"doc-a")
    b = root / "b.docx"
    b.write_bytes(b"doc-b")
    monkeypatch.setattr(artifact_store, "WORKSPACES_ROOT", root)
    ref_a = artifact_store.output_artifact_reference(a, a.name)
    ref_b = artifact_store.output_artifact_reference(b, b.name)

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    platform = FakePlatform()

    await dispatch_fresh(
        _StateGraph(
            {
                "result": {
                    "artifacts": [ref_a, ref_b],
                    "reply_text": "两份都处理好了。",
                    "success": True,
                }
            }
        ),
        platform,
        "test",
        "u1",
        "帮我改一下",
        (),
        UserLocks(),
        trace_id="t1",
        outbox=outbox,
    )

    rows = _outbox_rows(db_path)
    assert [row["kind"] for row in rows] == ["file", "file", "text"]
    assert rows[0]["payload"]["filename"] == "a.docx"
    assert rows[1]["payload"]["filename"] == "b.docx"
    assert rows[2]["payload"] == {"text": "两份都处理好了。"}
    assert platform.sent == []


async def test_pending_files_notice_lists_all_filenames(
    monkeypatch, tmp_path, outbox, db_path
):
    root = tmp_path / "inputs"
    monkeypatch.setattr(artifact_store, "INPUT_ARTIFACTS_ROOT", root)
    ref_a = artifact_store.store_incoming_file(
        "test", "u1", IncomingFile("a.docx", b"a", "application/octet-stream")
    )
    ref_b = artifact_store.store_incoming_file(
        "test", "u1", IncomingFile("b.docx", b"b", "application/octet-stream")
    )

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)

    await dispatch_fresh(
        _StateGraph({"result": None, "pending_files": (ref_a, ref_b)}),
        FakePlatform(),
        "test",
        "u1",
        "",
        (),
        UserLocks(),
        trace_id="t1",
        outbox=outbox,
    )

    rows = _outbox_rows(db_path)
    assert len(rows) == 1
    assert "a.docx" in rows[0]["payload"]["text"]
    assert "b.docx" in rows[0]["payload"]["text"]


async def test_pending_files_notice_uses_deduped_display_name(
    monkeypatch, outbox, db_path
):
    """回归 Finding 5：碰撞文件名要展示去重后的 display_filename，不能重复展示
    原始 filename（“收到文件「报价单.xlsx、报价单.xlsx」了”这种误导性文案）。"""

    reference_1 = {
        "kind": "input",
        "path": "/tmp/does-not-matter-1",
        "filename": "报价单.xlsx",
        "display_filename": None,
        "mime_type": "application/octet-stream",
    }
    reference_2 = {
        "kind": "input",
        "path": "/tmp/does-not-matter-2",
        "filename": "报价单.xlsx",
        "display_filename": "报价单-2.xlsx",
        "mime_type": "application/octet-stream",
    }

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)

    await dispatch_fresh(
        _StateGraph({"result": None, "pending_files": (reference_1, reference_2)}),
        FakePlatform(),
        "test",
        "u1",
        "",
        (),
        UserLocks(),
        trace_id="t1",
        outbox=outbox,
    )

    text = _outbox_rows(db_path)[0]["payload"]["text"]
    assert text.count("报价单.xlsx") == 1
    assert "报价单-2.xlsx" in text


async def test_build_outbound_messages_interrupt_asks_with_main_agent_text():
    """确认话术来自 MainAgent 的 user_message，且这一轮算成功（等待用户输入）。"""

    messages, summary = build_outbound_messages(
        {
            "__interrupt__": (
                SimpleNamespace(value={"user_message": "要转成表格吗？回复「是」。"}),
            )
        }
    )
    assert messages == [("text", {"text": "要转成表格吗？回复「是」。"})]
    assert summary == {
        "output_text": "要转成表格吗？回复「是」。",
        "output_filename": None,
        "success": True,
    }


async def test_build_outbound_messages_pending_files_notice_uses_display_names():
    """收到文件还没指令：一条主动话术，文件名用去重后的 display_filename。"""

    reference_1 = {
        "kind": "input",
        "path": "/tmp/does-not-matter-1",
        "filename": "报价单.xlsx",
        "display_filename": None,
        "mime_type": "application/octet-stream",
    }
    reference_2 = {
        "kind": "input",
        "path": "/tmp/does-not-matter-2",
        "filename": "报价单.xlsx",
        "display_filename": "报价单-2.xlsx",
        "mime_type": "application/octet-stream",
    }
    messages, summary = build_outbound_messages(
        {"pending_files": (reference_1, reference_2)}
    )
    assert len(messages) == 1
    kind, payload = messages[0]
    assert kind == "text"
    assert payload["text"].count("报价单.xlsx") == 1
    assert "报价单-2.xlsx" in payload["text"]
    assert summary == {
        "output_text": payload["text"],
        "output_filename": None,
        "success": True,
    }


async def test_build_outbound_messages_empty_state_produces_nothing():
    """图没产出也没待处理文件：清单为空，这一轮仍算成功（不是失败）。"""

    assert build_outbound_messages({"result": None, "pending_files": ()}) == (
        [],
        {"output_text": None, "output_filename": None, "success": True},
    )


async def test_build_outbound_messages_puts_files_before_text_as_references(
    monkeypatch, tmp_path
):
    """文件在前文字在后；file 消息只带 reference dict，纯函数绝不读 bytes。"""

    root = tmp_path / "workspaces"
    root.mkdir()
    a = root / "a.docx"
    a.write_bytes(b"doc-a")
    b = root / "b.docx"
    b.write_bytes(b"doc-b")
    monkeypatch.setattr(artifact_store, "WORKSPACES_ROOT", root)
    ref_a = artifact_store.output_artifact_reference(a, a.name)
    ref_b = artifact_store.output_artifact_reference(b, b.name)

    messages, summary = build_outbound_messages(
        {
            "result": {
                "artifacts": [ref_a, ref_b],
                "reply_text": "两份都处理好了。",
                "success": True,
            }
        }
    )
    assert messages == [
        ("file", ref_a),
        ("file", ref_b),
        ("text", {"text": "两份都处理好了。"}),
    ]
    assert all("content" not in payload for _kind, payload in messages)
    assert summary == {
        "output_text": "两份都处理好了。",
        "output_filename": "a.docx, b.docx",
        "success": True,
    }


async def test_build_outbound_messages_keeps_failed_result_text_and_success_flag():
    """执行失败也要把话术发出去；success 照抄 result，不做兜底改写。"""

    messages, summary = build_outbound_messages(
        {"result": {"artifacts": [], "reply_text": "这次没做成。", "success": False}}
    )
    assert messages == [("text", {"text": "这次没做成。"})]
    assert summary == {
        "output_text": "这次没做成。",
        "output_filename": None,
        "success": False,
    }


class CompactionGraph:
    """fake Graph：``aget_state`` 按调用次序返回不同快照（第一次给回合本身的
    resume 判定，第二次给投递后的 compaction 判定），并记录每次 ainvoke 的输入
    和 durability。"""

    def __init__(self, snapshots, *, compaction_error=None, on_compaction=None):
        self._snapshots = list(snapshots)
        self._compaction_error = compaction_error
        # 压缩回合开始时的回调：给时长类测试一个推进假时钟的挂点。
        self._on_compaction = on_compaction
        self.state_calls = 0
        self.invocations = []

    async def aget_state(self, config):
        snapshot = self._snapshots[min(self.state_calls, len(self._snapshots) - 1)]
        self.state_calls += 1
        return snapshot

    async def ainvoke(self, value, config, durability=None):
        self.invocations.append((value, durability))
        if isinstance(value, dict) and value.get("new_compaction_request"):
            if self._on_compaction is not None:
                self._on_compaction()
            if self._compaction_error is not None:
                raise self._compaction_error
            return {}
        return {
            "result": {"artifacts": [], "reply_text": "好的。", "success": True}
        }


def _idle_snapshot(pending_count):
    return SimpleNamespace(
        next=(),
        interrupts=(),
        values={
            "pending_compaction": [
                {"role": "user", "content": f"m{i}"} for i in range(pending_count)
            ]
        },
    )


def _compaction_invocations(graph):
    return [
        (value, durability)
        for value, durability in graph.invocations
        if isinstance(value, dict) and "new_compaction_request" in value
    ]


async def test_compaction_triggers_after_delivery_when_batch_full(
    monkeypatch, outbox, db_path
):
    records = []

    async def fake_log_turn(record):
        records.append(record)

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    graph = CompactionGraph([_idle_snapshot(0), _idle_snapshot(6)])
    platform = FakePlatform()

    await dispatch_fresh(
        graph,
        platform,
        "test",
        "u1",
        "你是谁？",
        (),
        UserLocks(),
        trace_id="t1",
        summarizer=object(),
        outbox=outbox,
    )

    assert len(graph.invocations) == 2
    assert graph.invocations[1] == ({"new_compaction_request": True}, "sync")
    # compaction 回合没有用户输出：只有正常回合那一条进了 outbox。
    assert [row["payload"]["text"] for row in _outbox_rows(db_path)] == ["好的。"]
    assert platform.sent == []
    assert records[0].success is True


async def test_compaction_skipped_when_pending_below_threshold(monkeypatch, outbox):
    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    graph = CompactionGraph([_idle_snapshot(0), _idle_snapshot(5)])

    await dispatch_fresh(
        graph,
        FakePlatform(),
        "test",
        "u1",
        "你是谁？",
        (),
        UserLocks(),
        trace_id="t1",
        summarizer=object(),
        outbox=outbox,
    )

    assert len(graph.invocations) == 1
    assert _compaction_invocations(graph) == []


async def test_compaction_skipped_while_waiting_for_confirmation(monkeypatch, outbox):
    """对 interrupt 等待态做非 resume 的 compaction invoke 是未定义行为：带旗标的
    checkpoint 会落盘、interrupt 重抛、compact 根本不跑，旗标就此粘滞。"""

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    waiting = SimpleNamespace(
        next=("ask_confirm",),
        interrupts=(object(),),
        values={
            "pending_compaction": [
                {"role": "user", "content": f"m{i}"} for i in range(6)
            ]
        },
    )
    graph = CompactionGraph([_idle_snapshot(0), waiting])

    await dispatch_fresh(
        graph,
        FakePlatform(),
        "test",
        "u1",
        "你是谁？",
        (),
        UserLocks(),
        trace_id="t1",
        summarizer=object(),
        outbox=outbox,
    )

    assert _compaction_invocations(graph) == []


async def test_compaction_skipped_when_summarizer_none(monkeypatch, outbox):
    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    graph = CompactionGraph([_idle_snapshot(0), _idle_snapshot(6)])

    await dispatch_fresh(
        graph,
        FakePlatform(),
        "test",
        "u1",
        "你是谁？",
        (),
        UserLocks(),
        trace_id="t1",
        outbox=outbox,
    )

    # 没有 summarizer 时连状态都不该查：aget_state 只有回合本身调用了一次。
    assert graph.state_calls == 1
    assert _compaction_invocations(graph) == []


async def test_compaction_invoke_failure_does_not_fail_turn(
    monkeypatch, caplog, outbox, db_path
):
    records = []

    async def fake_log_turn(record):
        records.append(record)

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    graph = CompactionGraph(
        [_idle_snapshot(0), _idle_snapshot(6)],
        compaction_error=RuntimeError("checkpoint 写失败"),
    )
    platform = FakePlatform()

    with caplog.at_level(logging.ERROR, logger="scripts.run_mvp"):
        await dispatch_fresh(
            graph,
            platform,
            "test",
            "u1",
            "你是谁？",
            (),
            UserLocks(),
            trace_id="t1",
            summarizer=object(),
            outbox=outbox,
        )

    assert records[0].success is True
    assert [row["payload"]["text"] for row in _outbox_rows(db_path)] == ["好的。"]
    assert any(record.exc_info for record in caplog.records)


class _FakeClock:
    """可控假时钟：压缩回合里显式推进，不用 sleep，断言不会因机器快慢而抖。"""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _install_fake_clock(monkeypatch):
    clock = _FakeClock()
    # run_mvp 只用 time.monotonic，整体换掉模块引用即可。
    monkeypatch.setattr(
        "scripts.run_mvp.time", SimpleNamespace(monotonic=clock.monotonic)
    )
    return clock


async def test_turn_duration_excludes_compaction_time(monkeypatch, outbox):
    """turn log 的 duration_ms 是用户回合时长（“零感知延迟”的验收指标）：
    投递之后才发起的后台压缩绝不能计进去。"""

    records = []

    async def fake_log_turn(record):
        records.append(record)

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    clock = _install_fake_clock(monkeypatch)
    graph = CompactionGraph(
        [_idle_snapshot(0), _idle_snapshot(6)],
        on_compaction=lambda: clock.advance(5.0),
    )

    await dispatch_fresh(
        graph,
        FakePlatform(),
        "test",
        "u1",
        "你是谁？",
        (),
        UserLocks(),
        trace_id="t1",
        summarizer=object(),
        outbox=outbox,
    )

    assert _compaction_invocations(graph) != []  # 压缩确实跑了
    assert clock.now == 5.0  # 且确实吃掉了 5 秒
    assert records[0].duration_ms == 0


async def test_confirm_turn_duration_excludes_compaction_time(monkeypatch, outbox):
    """第二个触发点（确认恢复分支）同样不得把压缩耗时算进本轮时长。"""

    records = []

    async def fake_log_turn(record):
        records.append(record)

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    clock = _install_fake_clock(monkeypatch)
    waiting = SimpleNamespace(
        next=("ask_confirm",), interrupts=(object(),), values={"trace_id": "t0"}
    )
    graph = CompactionGraph(
        [waiting, _idle_snapshot(6)], on_compaction=lambda: clock.advance(5.0)
    )

    await handle_event(
        graph,
        FakePlatform(),
        object(),
        UserLocks(),
        object(),
        InboundEvent("test", "u1", "是", None),
        summarizer=object(),
        outbox=outbox,
    )

    assert clock.now == 5.0
    assert records[0].duration_ms == 0


async def test_compaction_triggers_after_confirm_resume_delivery(monkeypatch, outbox):
    """第二个触发点：handle_event 的确认恢复分支投递完成后同样要触发压缩。"""

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    waiting = SimpleNamespace(
        next=("ask_confirm",), interrupts=(object(),), values={"trace_id": "t0"}
    )
    graph = CompactionGraph([waiting, _idle_snapshot(6)])

    await handle_event(
        graph,
        FakePlatform(),
        object(),  # debouncer.add is unreachable once the resume branch handled it
        UserLocks(),
        object(),  # memory_repository is unreachable outside /long-term-memory
        InboundEvent("test", "u1", "是", None),
        summarizer=object(),
        outbox=outbox,
    )

    assert graph.invocations[1] == ({"new_compaction_request": True}, "sync")


async def test_empty_graph_output_is_not_enqueued(monkeypatch, outbox, db_path):
    """图既没产出也没待处理文件：一行都不能进 outbox（空批入队等于制造空投递）。"""

    records = []

    async def fake_log_turn(record):
        records.append(record)

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)

    await dispatch_fresh(
        _StateGraph({"result": None, "pending_files": ()}),
        FakePlatform(),
        "test",
        "u1",
        "嗯",
        (),
        UserLocks(),
        trace_id="t1",
        outbox=outbox,
    )

    assert _outbox_rows(db_path) == []
    assert records[0].output_text is None
    assert records[0].success is True


async def test_interrupt_question_is_enqueued_as_a_text_message(
    monkeypatch, outbox, db_path
):
    """等确认这一轮的输出（MainAgent 的确认话术）同样走 outbox，不再直接发。"""

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    platform = FakePlatform()

    await dispatch_fresh(
        _StateGraph(
            {
                "__interrupt__": (
                    SimpleNamespace(value={"user_message": "要转成表格吗？"}),
                )
            }
        ),
        platform,
        "test",
        "u1",
        "把这个表整理一下",
        (),
        UserLocks(),
        trace_id="t1",
        outbox=outbox,
    )

    rows = _outbox_rows(db_path)
    assert [(row["kind"], row["payload"]) for row in rows] == [
        ("text", {"text": "要转成表格吗？"})
    ]
    assert rows[0]["session_key"] == "test:u1"
    assert rows[0]["status"] == "pending"
    assert platform.sent == []


async def test_orchestrator_failure_enqueues_the_fallback_text(
    monkeypatch, outbox, db_path
):
    """异常兜底话术也必须落 outbox：直接 send 会在网络抖动时把它一起丢掉。"""

    class Graph:
        async def aget_state(self, config):
            return SimpleNamespace(next=(), interrupts=(), values={})

        async def ainvoke(self, value, config, durability=None):
            raise RuntimeError("图炸了")

    records = []

    async def fake_log_turn(record):
        records.append(record)

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    platform = FakePlatform()
    wakeup = asyncio.Event()

    await dispatch_fresh(
        Graph(),
        platform,
        "test",
        "u1",
        "你是谁？",
        (),
        UserLocks(),
        trace_id="t1",
        outbox=outbox,
        wakeup=wakeup,
    )

    rows = _outbox_rows(db_path)
    assert [(row["kind"], row["payload"]) for row in rows] == [
        ("text", {"text": "处理失败，稍后再试一下"})
    ]
    assert rows[0]["trace_id"] == "t1"
    assert records[0].success is False
    assert records[0].error == "图炸了"
    assert platform.sent == []
    assert wakeup.is_set()


async def test_turn_log_success_follows_the_graph_summary_not_delivery(
    monkeypatch, outbox, db_path
):
    """回合终点是入队，投递成败已不在这一层：turn log 的 success 直接照抄图小结，
    error 不再承载 delivery_error。"""

    records = []

    async def fake_log_turn(record):
        records.append(record)

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)

    await dispatch_fresh(
        _StateGraph(
            {"result": {"artifacts": [], "reply_text": "这次没做成。", "success": False}}
        ),
        FakePlatform(),
        "test",
        "u1",
        "帮我改一下",
        (),
        UserLocks(),
        trace_id="t1",
        outbox=outbox,
    )

    assert records[0].success is False
    assert records[0].error is None
    assert records[0].output_text == "这次没做成。"
    assert [row["payload"]["text"] for row in _outbox_rows(db_path)] == ["这次没做成。"]


async def test_dispatch_fresh_wakes_the_delivery_worker_after_enqueue(
    monkeypatch, outbox, db_path
):
    """入队之后要叫醒常驻 worker，否则最坏要等它下一次 1 秒轮询才发件。"""

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    wakeup = asyncio.Event()

    await dispatch_fresh(
        _StateGraph(
            {"result": {"artifacts": [], "reply_text": "好的。", "success": True}}
        ),
        FakePlatform(),
        "test",
        "u1",
        "你是谁？",
        (),
        UserLocks(),
        trace_id="t1",
        outbox=outbox,
        wakeup=wakeup,
    )

    assert len(_outbox_rows(db_path)) == 1
    assert wakeup.is_set()


async def test_confirm_resume_output_is_enqueued_and_wakes_worker(
    monkeypatch, outbox, db_path
):
    """第二个回合终点（handle_event 的确认恢复分支）语义与 dispatch_fresh 一致。"""

    class Graph:
        async def aget_state(self, config):
            return SimpleNamespace(
                next=("ask_confirm",),
                interrupts=(object(),),
                values={"trace_id": "t0"},
            )

        async def ainvoke(self, value, config, durability=None):
            return {
                "result": {
                    "artifacts": [],
                    "reply_text": "好的，已经处理。",
                    "success": True,
                }
            }

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    platform = FakePlatform()
    wakeup = asyncio.Event()

    await handle_event(
        Graph(),
        platform,
        object(),  # debouncer.add is unreachable once the resume branch handled it
        UserLocks(),
        object(),  # memory_repository is unreachable outside /long-term-memory
        InboundEvent("test", "u1", "是", None),
        outbox=outbox,
        wakeup=wakeup,
    )

    rows = _outbox_rows(db_path)
    assert [(row["session_key"], row["trace_id"], row["payload"]) for row in rows] == [
        ("test:u1", "t0", {"text": "好的，已经处理。"})
    ]
    assert platform.sent == []
    assert wakeup.is_set()


# --- 投递 worker -------------------------------------------------------------
# 时间一律用固定时钟往前推（``now=`` 注入），不 monkeypatch time：退避表是
# 30s/2m/10m，真等一遍要 11 分钟，假时钟让"到期没到期"变成纯断言。约定同
# tests/test_outbox.py。

T0 = datetime(2026, 8, 21, 10, 0, 0)


class DeliveryPlatform:
    """投递侧的 fake 平台：记录每次成功 send，可编程抛错 / 长睡（测超时）。"""

    def __init__(self, *, error=None, error_users=(), delay=None):
        self.sent = []
        self._error = error
        self._error_users = set(error_users)
        self._delay = delay

    async def send(self, user_id, message):
        if self._delay is not None:
            await asyncio.sleep(self._delay)
        if self._error is not None and (
            not self._error_users or user_id in self._error_users
        ):
            raise RuntimeError(self._error)
        self.sent.append((user_id, message))


def _workspace_reference(monkeypatch, tmp_path, filename, content):
    root = tmp_path / "workspaces"
    root.mkdir(exist_ok=True)
    artifact = root / filename
    artifact.write_bytes(content)
    monkeypatch.setattr(artifact_store, "WORKSPACES_ROOT", root)
    return artifact_store.output_artifact_reference(
        artifact, filename, "application/octet-stream"
    )


async def test_deliver_due_once_sends_in_order_and_marks_delivered(
    monkeypatch, tmp_path, outbox, db_path, caplog
):
    """一个回合的两条消息必须按 seq 先后寄出：文件先到，"都改好了"后到。

    ``due_batch`` 每 session 只给队头，所以"发完一条才轮到下一条"这件事由连续
    两次 ``deliver_due_once`` 体现——中间那条 delivered 之前，第二条取不出来。
    """

    reference = _workspace_reference(monkeypatch, tmp_path, "result.docx", b"document")
    outbox.enqueue(
        "test:u1",
        "t1",
        [("file", reference), ("text", {"text": "都改好了"})],
        now=T0,
    )
    platform = DeliveryPlatform()

    caplog.set_level(logging.INFO, logger="scripts.run_mvp")
    assert await deliver_due_once(outbox, platform, now=T0) == 1
    assert len(platform.sent) == 1
    assert await deliver_due_once(outbox, platform, now=T0) == 1

    assert [user_id for user_id, _message in platform.sent] == ["u1", "u1"]
    first, second = platform.sent[0][1], platform.sent[1][1]
    assert first.text is None
    assert first.file.filename == "result.docx"
    assert first.file.content == b"document"
    assert second.file is None
    assert second.text == "都改好了"

    rows = _outbox_rows(db_path)
    assert [row["status"] for row in rows] == ["delivered", "delivered"]
    assert all(row["delivered_at"] == T0.isoformat() for row in rows)
    # 送达日志带 trace_id/session，死信排查时靠它把一条消息和回合串起来。
    delivered_logs = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "送达" in record.getMessage()
    ]
    assert len(delivered_logs) == 2
    assert "t1" in delivered_logs[0] and "test:u1" in delivered_logs[0]


async def test_send_failure_backs_off_then_dead(outbox, db_path, caplog):
    """三档退避（30s/2m/10m）全部用完之后的第 4 次失败才转死信；期间不到点不取件。"""

    outbox.enqueue("test:u1", "t1", [("text", {"text": "都改好了"})], now=T0)
    platform = DeliveryPlatform(error="平台 500")
    caplog.set_level(logging.INFO, logger="scripts.run_mvp")

    assert await deliver_due_once(outbox, platform, now=T0) == 1
    row = _outbox_rows(db_path)[0]
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["next_attempt_at"] == (T0 + timedelta(seconds=30)).isoformat()
    assert "平台 500" in row["last_error"]

    # 退避窗口内不取件：差 1 秒也不发。
    assert await deliver_due_once(outbox, platform, now=T0 + timedelta(seconds=29)) == 0

    assert await deliver_due_once(outbox, platform, now=T0 + timedelta(seconds=30)) == 1
    row = _outbox_rows(db_path)[0]
    assert (row["status"], row["attempts"]) == ("pending", 2)
    assert row["next_attempt_at"] == (T0 + timedelta(seconds=150)).isoformat()

    assert await deliver_due_once(outbox, platform, now=T0 + timedelta(seconds=150)) == 1
    row = _outbox_rows(db_path)[0]
    assert (row["status"], row["attempts"]) == ("pending", 3)
    assert row["next_attempt_at"] == (T0 + timedelta(seconds=750)).isoformat()

    assert await deliver_due_once(outbox, platform, now=T0 + timedelta(seconds=750)) == 1
    row = _outbox_rows(db_path)[0]
    assert (row["status"], row["attempts"]) == ("dead", 4)
    assert platform.sent == []
    assert [item["id"] for item in outbox.dead_letters("test:u1")] == [row["id"]]
    # 死信 WARNING 是 mark_failed 自带的，worker 不再补一条——一次死信一条告警。
    dead_warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "死信" in record.getMessage()
    ]
    assert len(dead_warnings) == 1


async def test_single_failure_does_not_block_other_sessions(outbox, db_path):
    """同一批里一条 send 抛错，绝不能带走本批其余会话的投递。"""

    outbox.enqueue("test:u1", "t1", [("text", {"text": "给 u1 的"})], now=T0)
    outbox.enqueue("test:u2", "t2", [("text", {"text": "给 u2 的"})], now=T0)
    platform = DeliveryPlatform(error="只有 u1 失败", error_users={"u1"})

    assert await deliver_due_once(outbox, platform, now=T0) == 2

    assert [user_id for user_id, _message in platform.sent] == ["u2"]
    rows = {row["session_key"]: row for row in _outbox_rows(db_path)}
    assert rows["test:u1"]["status"] == "pending"
    assert rows["test:u1"]["attempts"] == 1
    assert rows["test:u2"]["status"] == "delivered"


async def test_worker_startup_resets_sending_and_redelivers(outbox, db_path, caplog):
    """进程崩在 sending 上：启动 reset 之后必须重寄，且只留下一条 delivered。"""

    outbox.enqueue("test:u1", "t1", [("text", {"text": "都改好了"})], now=datetime.now())
    stuck_id = _outbox_rows(db_path)[0]["id"]
    outbox.mark_sending(stuck_id)
    platform = DeliveryPlatform()
    caplog.set_level(logging.INFO, logger="scripts.run_mvp")

    task = asyncio.create_task(delivery_worker(outbox, platform, asyncio.Event()))
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if platform.sent:
                break
        # 再给它几轮空转的机会，确认不会重复寄第二次。
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert len(platform.sent) == 1
    assert platform.sent[0][1].text == "都改好了"
    rows = _outbox_rows(db_path)
    assert [row["status"] for row in rows] == ["delivered"]
    assert any(
        "重" in record.getMessage() and record.levelno == logging.INFO
        for record in caplog.records
    )


async def test_send_timeout_counts_as_failure(monkeypatch, outbox, db_path):
    """卡住的 send 不能把 worker 一起卡死：超时按一次投递失败算，进退避表。"""

    monkeypatch.setattr("scripts.run_mvp._SEND_TIMEOUT_SECONDS", 0.01)
    outbox.enqueue("test:u1", "t1", [("text", {"text": "都改好了"})], now=T0)
    platform = DeliveryPlatform(delay=5)

    assert await deliver_due_once(outbox, platform, now=T0) == 1

    row = _outbox_rows(db_path)[0]
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert "TimeoutError" in row["last_error"]
    assert platform.sent == []


async def test_file_payload_resolves_reference(monkeypatch, tmp_path, outbox, db_path):
    """file 行只存路径引用，bytes 在发送这一刻才从磁盘读出来交给平台。"""

    reference = _workspace_reference(monkeypatch, tmp_path, "报表.xlsx", b"\x50\x4b\x03\x04")
    outbox.enqueue("test:u1", "t1", [("file", reference)], now=T0)
    platform = DeliveryPlatform()

    assert await deliver_due_once(outbox, platform, now=T0) == 1

    _user_id, message = platform.sent[0]
    assert message.file.filename == "报表.xlsx"
    assert message.file.content == b"\x50\x4b\x03\x04"
    assert message.file.mime_type == reference["mime_type"]
    assert _outbox_rows(db_path)[0]["status"] == "delivered"
