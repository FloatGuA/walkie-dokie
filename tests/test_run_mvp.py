import asyncio
from types import SimpleNamespace

import walkie_dokie.artifacts as artifact_store
from scripts.run_mvp import (
    _invoke_from_event,
    _waiting_for_confirmation,
    deliver_graph_output,
    dispatch_fresh,
    handle_event,
)
from walkie_dokie.main_agent.base import MemoryOperation
from walkie_dokie.main_agent.memory import JsonMemoryRepository
from walkie_dokie.orchestrator.locks import UserLocks
from walkie_dokie.platforms.base import IncomingFile, InboundEvent


class FakePlatform:
    def __init__(self):
        self.sent = []

    async def send(self, user_id, message):
        self.sent.append((user_id, message))


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


async def test_artifact_is_delivered_before_main_agent_text(monkeypatch, tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    artifact = root / "result.docx"
    artifact.write_bytes(b"document")
    monkeypatch.setattr(artifact_store, "WORKSPACES_ROOT", root)
    reference = artifact_store.output_artifact_reference(artifact, artifact.name)
    platform = FakePlatform()

    await deliver_graph_output(
        platform,
        "u1",
        {
            "result": {
                "artifacts": [reference],
                "reply_text": "已经处理好了。",
                "success": True,
            }
        },
    )
    assert len(platform.sent) == 2
    assert platform.sent[0][1].file.filename == "result.docx"
    assert platform.sent[0][1].file.content == b"document"
    assert platform.sent[1][1].text == "已经处理好了。"


async def test_fresh_direct_reply_is_written_to_conversation_turn_log(monkeypatch):
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
    )

    assert len(records) == 1
    assert records[0].record_type == "conversation"
    assert records[0].input_text == "你是谁？"
    assert records[0].output_text == "我是小帮。"
    assert records[0].success is True


async def test_dispatch_fresh_trace_id_is_recorded_as_conversation_run_id(monkeypatch):
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
    )

    assert records[0].run_id == "batch-42"


async def test_handle_event_confirm_resume_reuses_snapshots_trace_id(monkeypatch):
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
        object(),  # debouncer.add is unreachable once resumed_state is set
        UserLocks(),
        object(),  # memory_repository is unreachable outside the /long-term-memory branch
        InboundEvent("test", "u1", "是", None),
    )

    assert records[0].run_id == "original-propose-id"


async def test_concurrent_handle_event_calls_do_not_interleave_graph_access(
    monkeypatch,
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
            object(),  # debouncer.add is unreachable once resumed_state is set
            locks,
            object(),  # memory_repository is unreachable outside /long-term-memory
            InboundEvent("test", "u1", "是", None),
        ),
        handle_event(
            graph,
            platform,
            object(),
            locks,
            object(),
            InboundEvent("test", "u1", "是，确认", None),
        ),
    )

    for i in range(0, len(order), 2):
        assert order[i].endswith("-start")
        assert order[i + 1].endswith("-end")
        assert order[i].split("-")[0] == order[i + 1].split("-")[0], (
            f"interleaved graph access detected: {order}"
        )


async def test_long_term_memory_command_bypasses_graph_and_debounce(
    monkeypatch, tmp_path
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
    )

    assert platform.sent[0][1].text == "当前保存的长期记忆：\n姓名：浮瓜"
    assert records[0].record_type == "conversation"
    assert records[0].success is True


async def test_multiple_artifacts_are_delivered_before_text(monkeypatch, tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    a = root / "a.docx"
    a.write_bytes(b"doc-a")
    b = root / "b.docx"
    b.write_bytes(b"doc-b")
    monkeypatch.setattr(artifact_store, "WORKSPACES_ROOT", root)
    ref_a = artifact_store.output_artifact_reference(a, a.name)
    ref_b = artifact_store.output_artifact_reference(b, b.name)
    platform = FakePlatform()

    await deliver_graph_output(
        platform,
        "u1",
        {
            "result": {
                "artifacts": [ref_a, ref_b],
                "reply_text": "两份都处理好了。",
                "success": True,
            }
        },
    )
    assert len(platform.sent) == 3
    assert platform.sent[0][1].file.filename == "a.docx"
    assert platform.sent[1][1].file.filename == "b.docx"
    assert platform.sent[2][1].text == "两份都处理好了。"


async def test_pending_files_notice_lists_all_filenames(monkeypatch, tmp_path):
    root = tmp_path / "inputs"
    monkeypatch.setattr(artifact_store, "INPUT_ARTIFACTS_ROOT", root)
    ref_a = artifact_store.store_incoming_file(
        "test", "u1", IncomingFile("a.docx", b"a", "application/octet-stream")
    )
    ref_b = artifact_store.store_incoming_file(
        "test", "u1", IncomingFile("b.docx", b"b", "application/octet-stream")
    )
    platform = FakePlatform()
    await deliver_graph_output(
        platform, "u1", {"pending_files": (ref_a, ref_b)}
    )
    assert "a.docx" in platform.sent[0][1].text
    assert "b.docx" in platform.sent[0][1].text


async def test_pending_files_notice_uses_deduped_display_name():
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
    platform = FakePlatform()
    await deliver_graph_output(
        platform, "u1", {"pending_files": (reference_1, reference_2)}
    )
    text = platform.sent[0][1].text
    assert text.count("报价单.xlsx") == 1
    assert "报价单-2.xlsx" in text
