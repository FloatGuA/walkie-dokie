"""持久 outbox 的存储语义：保序取件、退避死信、at-least-once、inbox 去重。

时间一律用固定时钟变量往前推（``now=`` 参数注入），不 monkeypatch time——
退避表是 30s/2m/10m，真等一遍要 11 分钟，而假时钟让"到期没到期"变成纯断言。
"""

import logging
import sqlite3
from datetime import datetime, timedelta

import pytest

from walkie_dokie.orchestrator.outbox import Outbox

T0 = datetime(2026, 8, 21, 10, 0, 0)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "outbox.db"


@pytest.fixture
def outbox(db_path):
    return Outbox(db_path)


def _all_rows(db_path):
    """绕过 Outbox 直读表，用来断言"库里到底剩什么"（零插入、只有一条 delivered）。"""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM outbox ORDER BY id")]
    finally:
        connection.close()


def test_enqueue_then_due_batch_returns_head_message(outbox):
    outbox.enqueue(
        "feishu:u1",
        "trace-1",
        [
            ("file", {"path": "a.docx"}),
            ("file", {"path": "b.docx"}),
            ("text", {"text": "都改好了"}),
        ],
        now=T0,
    )

    batch = outbox.due_batch(T0)

    assert len(batch) == 1
    row = batch[0]
    assert isinstance(row["id"], int)
    assert row["session_key"] == "feishu:u1"
    assert row["trace_id"] == "trace-1"
    assert row["seq"] == 0
    assert row["kind"] == "file"
    assert row["payload"] == {"path": "a.docx"}
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["next_attempt_at"] == T0.isoformat()
    assert row["created_at"] == T0.isoformat()
    assert row["delivered_at"] is None
    assert row["last_error"] is None


def test_ordering_head_blocks_tail(outbox):
    outbox.enqueue(
        "feishu:u1",
        "trace-1",
        [("file", {"path": "a.docx"}), ("text", {"text": "都改好了"})],
        now=T0,
    )
    head = outbox.due_batch(T0)[0]

    outbox.mark_sending(head["id"])
    assert outbox.due_batch(T0) == []

    outbox.mark_delivered(head["id"], now=T0)
    batch = outbox.due_batch(T0)
    assert len(batch) == 1
    assert batch[0]["seq"] == 1
    assert batch[0]["kind"] == "text"
    assert batch[0]["payload"] == {"text": "都改好了"}


def test_dead_head_releases_tail(outbox):
    outbox.enqueue(
        "feishu:u1",
        "trace-1",
        [("file", {"path": "a.docx"}), ("text", {"text": "都改好了"})],
        now=T0,
    )
    head = outbox.due_batch(T0)[0]

    for attempt in range(3):
        outbox.mark_failed(head["id"], f"网络炸了 {attempt}", now=T0 + timedelta(hours=attempt))

    batch = outbox.due_batch(T0 + timedelta(days=1))
    assert len(batch) == 1
    assert batch[0]["seq"] == 1

    dead = outbox.dead_letters()
    assert len(dead) == 1
    assert dead[0]["id"] == head["id"]
    assert dead[0]["status"] == "dead"
    assert dead[0]["attempts"] == 3
    assert dead[0]["last_error"] == "网络炸了 2"
    assert dead[0]["payload"] == {"path": "a.docx"}
    assert outbox.dead_letters("feishu:u2") == []
    assert outbox.dead_letters("feishu:u1")[0]["id"] == head["id"]


def test_backoff_schedule(outbox, caplog):
    outbox.enqueue("feishu:u1", "trace-1", [("text", {"text": "hi"})], now=T0)
    message_id = outbox.due_batch(T0)[0]["id"]

    outbox.mark_failed(message_id, "第一次失败", now=T0)
    assert outbox.due_batch(T0) == []
    assert outbox.due_batch(T0 + timedelta(seconds=29)) == []
    first_retry = outbox.due_batch(T0 + timedelta(seconds=31))
    assert len(first_retry) == 1
    assert first_retry[0]["attempts"] == 1
    assert first_retry[0]["status"] == "pending"
    assert first_retry[0]["next_attempt_at"] == (T0 + timedelta(seconds=30)).isoformat()
    assert first_retry[0]["last_error"] == "第一次失败"

    t1 = T0 + timedelta(seconds=31)
    outbox.mark_failed(message_id, "第二次失败", now=t1)
    assert outbox.due_batch(t1 + timedelta(seconds=119)) == []
    second_retry = outbox.due_batch(t1 + timedelta(seconds=121))
    assert len(second_retry) == 1
    assert second_retry[0]["attempts"] == 2
    assert second_retry[0]["next_attempt_at"] == (t1 + timedelta(seconds=120)).isoformat()

    t2 = t1 + timedelta(seconds=121)
    with caplog.at_level(logging.WARNING, logger="walkie_dokie.orchestrator.outbox"):
        outbox.mark_failed(message_id, "第三次失败", now=t2)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    warning_text = warnings[0].getMessage()
    assert "feishu:u1" in warning_text
    assert "trace-1" in warning_text
    assert "第三次失败" in warning_text

    assert outbox.due_batch(t2 + timedelta(days=1)) == []
    dead = outbox.dead_letters()
    assert len(dead) == 1
    assert dead[0]["id"] == message_id
    assert dead[0]["attempts"] == 3


def test_multi_session_isolation(outbox):
    outbox.enqueue(
        "feishu:u1",
        "trace-a",
        [("file", {"path": "a.docx"}), ("text", {"text": "a"})],
        now=T0,
    )
    outbox.enqueue(
        "feishu:u2",
        "trace-b",
        [("file", {"path": "b.docx"}), ("text", {"text": "b"})],
        now=T0,
    )

    batch = outbox.due_batch(T0)
    assert len(batch) == 2
    assert [row["session_key"] for row in batch] == ["feishu:u1", "feishu:u2"]
    assert [row["seq"] for row in batch] == [0, 0]

    a_head = next(row for row in batch if row["session_key"] == "feishu:u1")
    outbox.mark_sending(a_head["id"])

    batch = outbox.due_batch(T0)
    assert len(batch) == 1
    assert batch[0]["session_key"] == "feishu:u2"
    assert batch[0]["seq"] == 0


def test_reset_sending_restores_pending_without_touching_attempts(outbox):
    outbox.enqueue("feishu:u1", "trace-1", [("text", {"text": "hi"})], now=T0)
    message_id = outbox.due_batch(T0)[0]["id"]
    outbox.mark_failed(message_id, "第一次失败", now=T0)

    t1 = T0 + timedelta(seconds=31)
    outbox.mark_sending(outbox.due_batch(t1)[0]["id"])
    assert outbox.due_batch(t1) == []

    assert outbox.reset_sending() == 1

    restored = outbox.due_batch(t1)
    assert len(restored) == 1
    assert restored[0]["id"] == message_id
    assert restored[0]["status"] == "pending"
    assert restored[0]["attempts"] == 1
    assert restored[0]["last_error"] == "第一次失败"

    assert outbox.reset_sending() == 0


def test_at_least_once_replay(outbox, db_path):
    outbox.enqueue("feishu:u1", "trace-1", [("text", {"text": "hi"})], now=T0)
    message_id = outbox.due_batch(T0)[0]["id"]

    outbox.mark_sending(message_id)
    # 进程在这里崩了：这条已经 sending，但没人知道平台到底收到没有。
    assert outbox.reset_sending() == 1

    t1 = T0 + timedelta(minutes=5)
    replayed = outbox.due_batch(t1)
    assert len(replayed) == 1
    assert replayed[0]["id"] == message_id
    outbox.mark_delivered(message_id, now=t1)

    assert outbox.due_batch(t1) == []
    rows = _all_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "delivered"
    assert rows[0]["delivered_at"] == t1.isoformat()
    assert rows[0]["attempts"] == 0


def test_inbox_seen_roundtrip_and_ttl(outbox):
    assert outbox.seen_event("evt-1") is False

    outbox.record_event("evt-1", now=T0)
    assert outbox.seen_event("evt-1") is True

    assert outbox.purge_expired_seen(now=T0 + timedelta(days=6)) == 0
    assert outbox.seen_event("evt-1") is True

    assert outbox.purge_expired_seen(now=T0 + timedelta(days=8)) == 1
    assert outbox.seen_event("evt-1") is False


def test_enqueue_is_transactional(outbox, db_path):
    with pytest.raises(ValueError) as excinfo:
        outbox.enqueue(
            "feishu:u1",
            "trace-1",
            [
                ("file", {"path": "a.docx"}),
                ("image", {"path": "b.png"}),
                ("text", {"text": "都改好了"}),
            ],
            now=T0,
        )

    assert "image" in str(excinfo.value)
    assert _all_rows(db_path) == []
    assert outbox.due_batch(T0) == []
