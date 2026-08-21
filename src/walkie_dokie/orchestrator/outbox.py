"""投递账本：回合的输出先落库，再由 worker 异步寄出。

回合终点从"直接调 platform.send"改成"写这张表"，是为了让用户的等待时长只包含
图 + 本地入队；网络重试、退避、死信全在用户走开之后发生。表本身就是账本——
每条消息的状态/尝试次数/最后错误都能事后查（死信区、admin 控制台）。

四条核心语义，都在 tests/test_outbox.py 里钉死：

1. 保序：同 session 只取最早那条未终态的行。前一条 pending/sending 时后一条绝
   不发（否则"文件还没到，'都改好了'先到了"），delivered/dead 则放行。
2. 退避与死信：失败按 (30s, 2m, 10m) 三档退避，第 4 次失败置 dead + WARNING。
3. at-least-once：进程崩在 sending 上时没人知道平台收到没有，启动 reset_sending
   一律复位重寄——宁可重发一次，不可静默丢件。
4. 去重：inbox_seen 挡住平台的重复推送（飞书会重投），7 天 TTL。

时间一律由调用方以 ``now: datetime`` 注入，模块内不读时钟：退避表是分钟级的，
测试要能瞬间把时钟推到 10 分钟后。
"""

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_VAR_ROOT = Path(__file__).parent.parent.parent.parent / "var"
OUTBOX_DB_PATH = _VAR_ROOT / "outbox.db"

_BACKOFF_SECONDS = (30, 120, 600)
# 4 而不是 3：退避表有三档，第 1/2/3 次失败各排一档（30s/2m/10m），第 4 次失败才转死信。
# 写 3 的话 600 这一档永远排不上——三元组会变成两级退避加一个死代码常量。
_MAX_ATTEMPTS = 4
_SEEN_TTL_DAYS = 7

_VALID_KINDS = ("file", "text")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL,
  trace_id TEXT,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS inbox_seen (
  event_id TEXT PRIMARY KEY,
  seen_at TEXT NOT NULL
);
"""


class Outbox:
    """``var/outbox.db`` 的读写口。全同步 sqlite3——本地库毫秒级，与 turn_log 同取舍。

    连接策略：每次调用短开短关（``_connect()``），不持有长连接。理由是调用方分布在
    入队侧和 worker 侧两个 asyncio task，长连接得配 ``check_same_thread=False`` 并
    保证永不跨线程，还要多一个 ``close()`` 生命周期给调用方和测试维护；而每次
    ``connect`` 打开一个本地文件的成本对 1 秒一轮的 worker 完全可忽略。WAL 模式写在
    库文件里，新连接自动继承。
    """

    def __init__(self, db_path: Path = OUTBOX_DB_PATH):
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            # WAL：worker 在读的同时入队方要能写，默认的 rollback journal 会互相挡。
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """开一个连接，块内是一个事务（正常 commit / 抛异常 rollback），出块必关。"""
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def enqueue(
        self,
        session_key: str,
        trace_id: str | None,
        messages: list[tuple[str, dict]],
        *,
        now: datetime,
    ) -> None:
        """一个回合的输出整批入队，``messages`` 的列表序就是投递序（seq 从 0 递增）。

        kind 非法整批拒绝：先全量校验再插入，一条脏数据不会带着半个回合进库——
        半个回合比没有回合更糟（用户只收到文件，永远等不到那句"都改好了"）。
        """
        for kind, _payload in messages:
            if kind not in _VALID_KINDS:
                raise ValueError(f"非法的消息 kind：{kind!r}，只接受 {_VALID_KINDS}")

        timestamp = now.isoformat()
        rows = [
            (
                session_key,
                trace_id,
                seq,
                kind,
                json.dumps(payload, ensure_ascii=False),
                "pending",
                0,
                timestamp,
                timestamp,
            )
            for seq, (kind, payload) in enumerate(messages)
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO outbox (
                  session_key, trace_id, seq, kind, payload,
                  status, attempts, next_attempt_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        logger.info(
            "消息入队 session=%s trace_id=%s 条数=%d",
            session_key,
            trace_id,
            len(rows),
        )

    def due_batch(self, now: datetime) -> list[dict]:
        """每个 session 至多一条：该 session 最早的未终态行，且它已 pending 且到期。

        保序全靠这条 SQL 的形状：子查询先按 session 取 MIN(id)（未终态的队头），外层
        再要求队头自己是 pending 且 due。队头在 sending 就没有任何行满足条件——后面
        的消息自然被挡住；队头 delivered/dead 之后它不再进子查询，下一条自动成为队头。
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.* FROM outbox o JOIN (
                  SELECT session_key, MIN(id) AS head_id FROM outbox
                  WHERE status IN ('pending','sending') GROUP BY session_key
                ) h ON o.id = h.head_id
                WHERE o.status='pending' AND o.next_attempt_at <= ?
                ORDER BY o.id
                """,
                (now.isoformat(),),
            ).fetchall()
        return [_to_dict(row) for row in rows]

    def mark_sending(self, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE outbox SET status='sending' WHERE id = ?", (message_id,)
            )

    def mark_delivered(self, message_id: int, *, now: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE outbox SET status='delivered', delivered_at = ? WHERE id = ?",
                (now.isoformat(), message_id),
            )

    def mark_failed(self, message_id: int, error: str, *, now: datetime) -> None:
        """尝试次数 +1，按退避表安排重试（第 n 次失败排 _BACKOFF_SECONDS[n-1]）；
        到 _MAX_ATTEMPTS 转死信并 WARNING。

        死信这行日志是唯一的人工介入信号：消息不会再被取件了，只剩死信区能看到。
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT session_key, trace_id, attempts FROM outbox WHERE id = ?",
                (message_id,),
            ).fetchone()
            attempts = row["attempts"] + 1

            if attempts >= _MAX_ATTEMPTS:
                connection.execute(
                    "UPDATE outbox SET status='dead', attempts = ?, last_error = ? WHERE id = ?",
                    (attempts, error, message_id),
                )
                logger.warning(
                    "消息投递失败 %d 次转死信 session=%s trace_id=%s id=%d error=%s",
                    attempts,
                    row["session_key"],
                    row["trace_id"],
                    message_id,
                    error,
                )
                return

            next_attempt_at = now + timedelta(seconds=_BACKOFF_SECONDS[attempts - 1])
            connection.execute(
                """
                UPDATE outbox
                SET status='pending', attempts = ?, next_attempt_at = ?, last_error = ?
                WHERE id = ?
                """,
                (attempts, next_attempt_at.isoformat(), error, message_id),
            )
            logger.info(
                "消息投递失败第 %d 次，%s 后重试 session=%s trace_id=%s id=%d error=%s",
                attempts,
                next_attempt_at.isoformat(),
                row["session_key"],
                row["trace_id"],
                message_id,
                error,
            )

    def reset_sending(self) -> int:
        """启动恢复：所有卡在 sending 的复位成 pending，返回复位条数。

        attempts 不加——上次崩溃不是平台的错，不该消耗这条消息的重试预算。
        """
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET status='pending' WHERE status='sending'"
            )
            count = cursor.rowcount
        if count:
            logger.info("启动恢复：%d 条卡在 sending 的消息已复位重寄", count)
        return count

    def seen_event(self, event_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM inbox_seen WHERE event_id = ?", (event_id,)
            ).fetchone()
        return row is not None

    def record_event(self, event_id: str, *, now: datetime) -> None:
        """记下这条事件已经见过。重复落记是静默 no-op。

        OR IGNORE 不是防御性兜底：两条重复推送并发到达时都会先 seen_event 得到
        False 再各自 record，普通 INSERT 在这里必然撞 PRIMARY KEY。忽略而不是
        覆盖，是因为 seen_at 撑着 7 天 TTL——每重投一次就刷新时间戳的话，去重记录
        会被平台的重试永远续命。
        """
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO inbox_seen (event_id, seen_at) VALUES (?, ?)",
                (event_id, now.isoformat()),
            )

    def purge_expired_seen(self, *, now: datetime) -> int:
        """删掉 7 天前的去重记录，返回删除条数。平台的重投窗口远短于 7 天。"""
        cutoff = (now - timedelta(days=_SEEN_TTL_DAYS)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM inbox_seen WHERE seen_at < ?", (cutoff,)
            )
            return cursor.rowcount

    def dead_letters(self, session_key: str | None = None) -> list[dict]:
        """死信查询：投递彻底失败、需要人看一眼的消息。不传 session_key 就是全部。"""
        sql = "SELECT * FROM outbox WHERE status='dead'"
        params: tuple = ()
        if session_key is not None:
            sql += " AND session_key = ?"
            params = (session_key,)
        sql += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_to_dict(row) for row in rows]


def _to_dict(row: sqlite3.Row) -> dict:
    """行 -> plain dict（键=列名）。payload 顺手解回 dict，与 enqueue 的入参对称。"""
    record = dict(row)
    record["payload"] = json.loads(record["payload"])
    return record
