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
from walkie_dokie.platforms.base import InboundEvent


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


async def test_debounced_dispatch_rechecks_and_resumes_new_interrupt():
    class Graph:
        def __init__(self):
            self.input = None
            self.durability = None

        async def aget_state(self, config):
            return SimpleNamespace(next=("ask_confirm",), interrupts=(object(),))

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
        file=None,
    )
    assert graph.input.resume == {"text": "是", "file": None}
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
                "artifact": reference,
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
                    "artifact": None,
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
        None,
        UserLocks(),
    )

    assert len(records) == 1
    assert records[0].record_type == "conversation"
    assert records[0].input_text == "你是谁？"
    assert records[0].output_text == "我是小帮。"
    assert records[0].success is True


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
