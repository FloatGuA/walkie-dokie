import asyncio
import logging
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


async def test_concurrent_dispatch_fresh_and_handle_event_resume_do_not_interleave(
    monkeypatch,
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
        ),
        handle_event(
            graph,
            platform,
            object(),
            locks,
            object(),
            InboundEvent("test", "u1", "是", None),
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


async def test_compaction_triggers_after_delivery_when_batch_full(monkeypatch):
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
    )

    assert len(graph.invocations) == 2
    assert graph.invocations[1] == ({"new_compaction_request": True}, "sync")
    # compaction 回合没有用户输出：deliver_graph_output 只能为正常回合跑一次。
    assert [message.text for _user, message in platform.sent] == ["好的。"]
    assert records[0].success is True


async def test_compaction_skipped_when_pending_below_threshold(monkeypatch):
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
    )

    assert len(graph.invocations) == 1
    assert _compaction_invocations(graph) == []


async def test_compaction_skipped_while_waiting_for_confirmation(monkeypatch):
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
    )

    assert _compaction_invocations(graph) == []


async def test_compaction_skipped_when_summarizer_none(monkeypatch):
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
    )

    # 没有 summarizer 时连状态都不该查：aget_state 只有回合本身调用了一次。
    assert graph.state_calls == 1
    assert _compaction_invocations(graph) == []


async def test_compaction_invoke_failure_does_not_fail_turn(monkeypatch, caplog):
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
        )

    assert records[0].success is True
    assert [message.text for _user, message in platform.sent] == ["好的。"]
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


async def test_turn_duration_excludes_compaction_time(monkeypatch):
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
    )

    assert _compaction_invocations(graph) != []  # 压缩确实跑了
    assert clock.now == 5.0  # 且确实吃掉了 5 秒
    assert records[0].duration_ms == 0


async def test_confirm_turn_duration_excludes_compaction_time(monkeypatch):
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
    )

    assert clock.now == 5.0
    assert records[0].duration_ms == 0


async def test_compaction_triggers_after_confirm_resume_delivery(monkeypatch):
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
        object(),  # debouncer.add is unreachable once resumed_state is set
        UserLocks(),
        object(),  # memory_repository is unreachable outside /long-term-memory
        InboundEvent("test", "u1", "是", None),
        summarizer=object(),
    )

    assert graph.invocations[1] == ({"new_compaction_request": True}, "sync")
