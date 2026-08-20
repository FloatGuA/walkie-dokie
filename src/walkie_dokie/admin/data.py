"""观测台的数据读取层：把 var/logs 下的 JSONL 变成面板要的形状。

只读，不写任何文件，也不 import fastapi——HTTP 层（Task 4）依赖这里，反过来不行，
这样这些函数在没装 admin extra 的环境里照样能跑、能测。

对外部数据宽容是刻意的：日志文件可能正被 bot 追加、可能上次进程被 kill 留了半行。
坏行跳过并计数（``skipped_lines`` 交给页面显示），文件缺失当空态——观测台自己因为
被观测对象还没产出数据而崩掉，是最没用的失败方式。
"""

import json
import logging
import sqlite3
from pathlib import Path

from scripts.report_costs import aggregate
from walkie_dokie.main_agent.memory import JsonMemoryRepository

logger = logging.getLogger(__name__)

COST_DISCLAIMER = "金额为保守上界估算，对账以控制台账单为准"


def _read_jsonl(path: Path) -> tuple[list[dict], int]:
    """读 JSONL，返回（成功解析的行, 跳过的坏行数）。文件不存在 → ([], 0)。"""
    if not path.exists():
        return [], 0

    records: list[dict] = []
    skipped = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1
    return records, skipped


def read_turns(path: Path, *, limit: int = 50, user: str | None = None) -> dict:
    """最近的回合记录，最新在前。

    ``user`` 非空时先按 ``user_id`` 精确匹配过滤再取 ``limit``——顺序反过来会让
    筛选某个用户时只在"全局最近 limit 条"里找，翻不到更早的记录。
    """
    records, skipped = _read_jsonl(path)
    if user:
        records = [item for item in records if item.get("user_id") == user]
    return {"turns": records[::-1][:limit], "skipped_lines": skipped}


def read_costs(path: Path, *, days: int = 7) -> dict:
    """窗口内的成本聚合。聚合逻辑一律复用报表脚本，绝不在这里重算一份。"""
    records, skipped = _read_jsonl(path)
    return {
        "aggregate": aggregate(records, days),
        "skipped_lines": skipped,
        "disclaimer": COST_DISCLAIMER,
    }


def _read_profiles(memory_dir: Path) -> dict[str, dict]:
    """扫 ``v2_*.json``，返回 {文件名: 档案内容}。

    文件名里的两段是 sha256，不可逆，所以这里只能以文件名为键；真实的
    platform/user_id 要靠 checkpoint 的 thread 反查（见 ``read_memory``）。
    这里刻意读原始 JSON 而不复用 ``JsonMemoryRepository.load``：后者要 platform/
    user_id 才能定位文件，而这里恰恰是拿不到它们的那条路径；观测台要显示的也是
    盘上真实存着什么，不是归一化之后的样子。
    """
    if not memory_dir.is_dir():
        return {}

    profiles: dict[str, dict] = {}
    for path in sorted(memory_dir.glob("v2_*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 档案坏了也要让这个用户出现在列表里：静默少一行，看板上就是"这个人
            # 没档案"，比显示空档案更容易误判。
            logger.exception("用户档案不可读，按空档案展示 path=%s", path)
            raw = None
        profiles[path.name] = raw if isinstance(raw, dict) else {}
    return profiles


def _thread_identity(thread_id: str, channel_values: dict) -> tuple[str, str]:
    """还原 thread 背后的 platform/user_id。

    优先用 checkpoint 里存的真实字段；thread_id 是 ``platform:user_id`` 拼出来的，
    user_id 本身含冒号时切不回来，只能当兜底。
    """
    platform = channel_values.get("platform")
    user_id = channel_values.get("user_id")
    if isinstance(platform, str) and isinstance(user_id, str) and platform and user_id:
        return platform, user_id
    head, _, tail = thread_id.partition(":")
    return (head, tail) if tail else ("", thread_id)


def _read_checkpoint_users(checkpoint_db: Path) -> tuple[list[dict], str | None]:
    """只读读出每个 thread 最新 checkpoint 的摘要与待压缩条数。

    反序列化交给 langgraph 自己的 ``SqliteSaver``——checkpoint 的 BLOB 编码是它的
    内部实现，手撸 msgpack 会在它换格式的那天悄悄读出错的数据。
    """
    if not checkpoint_db.exists():
        return [], None

    # 延迟到这里再 import：没跑过图的环境（比如只看成本页）不该因为缺 langgraph
    # 就连 data.py 都导不进来。
    from langgraph.checkpoint.sqlite import SqliteSaver

    users: list[dict] = []
    try:
        connection = sqlite3.connect(f"file:{checkpoint_db}?mode=ro", uri=True)
        try:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'"
            ).fetchone()
            if not table_exists:
                # db 刚建好还没跑过图。这是空态不是故障，别在看板上飘红。
                return [], None

            saver = SqliteSaver(connection)
            # SqliteSaver.setup() 会跑 PRAGMA journal_mode=WAL + CREATE TABLE，在
            # 只读连接上直接抛 "attempt to write a readonly database"（非 WAL 的
            # db 必炸，WAL 的碰巧不炸）。观测台永远不写被观测的库，所以直接声明
            # 建表已完成，跳过整个 setup。
            saver.is_setup = True

            thread_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints"
                )
            ]
            for thread_id in thread_ids:
                checkpoint_tuple = saver.get_tuple(
                    {"configurable": {"thread_id": thread_id}}
                )
                if checkpoint_tuple is None:
                    continue
                channel_values = checkpoint_tuple.checkpoint.get("channel_values") or {}
                platform, user_id = _thread_identity(thread_id, channel_values)
                users.append(
                    {
                        "platform": platform,
                        "user_id": user_id,
                        "summary": list(
                            channel_values.get("conversation_summary") or []
                        ),
                        "pending_compaction": len(
                            channel_values.get("pending_compaction") or []
                        ),
                    }
                )
        finally:
            connection.close()
    # 这里的宽 except 是刻意的系统边界：db 由 bot 进程并发写，观测台读到半个
    # 事务、坏文件、换了格式的 BLOB 都有可能，但都不该让整个看板 500。
    except Exception as exc:
        logger.exception("读取 checkpoint 失败 path=%s", checkpoint_db)
        return [], str(exc)
    return users, None


def read_memory(memory_dir: Path, checkpoint_db: Path) -> dict:
    """长期档案 + 会话摘要的并集视图。

    有档案没摘要、有摘要没档案都要出现——只显示交集会让"记忆写进去了但会话没
    了"和"聊过但什么都没记住"这两种最该被看见的状态直接从看板上消失。

    checkpoint 那一半是尽力而为：库缺失或还没建表算空态，真读挂了把异常放进
    ``checkpoint_error`` 交给页面显示，档案部分照常返回。
    """
    profiles = _read_profiles(memory_dir)
    checkpoint_users, checkpoint_error = _read_checkpoint_users(checkpoint_db)

    # 用真正的写入方来算文件名，绝不在这里抄一份 sha256 拼接：算法哪天改了
    # （v2 -> v3），抄的那份会静默对不上，看板上每个用户裂成两行还不报错。
    repository = JsonMemoryRepository(memory_dir)

    users: list[dict] = []
    for entry in checkpoint_users:
        file_key = repository._path(entry["platform"], entry["user_id"]).name
        users.append(
            {
                "platform": entry["platform"],
                "user_id": entry["user_id"],
                "profile": profiles.pop(file_key, {}),
                "summary": entry["summary"],
                "pending_compaction": entry["pending_compaction"],
            }
        )

    # 剩下的档案没有任何 thread 对得上（换过 checkpoint 库、或会话被清过）。
    # 文件名的 sha256 逆不回来，就原样展示文件名，不编造 ID。
    for file_name, profile in profiles.items():
        users.append(
            {
                "platform": "",
                "user_id": file_name,
                "profile": profile,
                "summary": [],
                "pending_compaction": 0,
            }
        )

    users.sort(key=lambda user: (user["platform"], user["user_id"]))
    return {"users": users, "checkpoint_error": checkpoint_error}
