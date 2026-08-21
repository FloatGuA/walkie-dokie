"""观测台的数据读取层：把 var/ 下的 JSONL、记忆文件与 eval 报告变成面板要的形状。

只读，不写任何文件，也不 import fastapi——HTTP 层（Task 4）依赖这里，反过来不行，
这样这些函数在没装 admin extra 的环境里照样能跑、能测。

对外部数据宽容是刻意的：日志文件可能正被 bot 追加、可能上次进程被 kill 留了半行。
坏行跳过并计数（``skipped_lines`` 交给页面显示），文件缺失当空态——观测台自己因为
被观测对象还没产出数据而崩掉，是最没用的失败方式。
"""

import json
import logging
import re
import sqlite3
from pathlib import Path

from scripts.report_costs import aggregate
from walkie_dokie.main_agent.memory import JsonMemoryRepository

logger = logging.getLogger(__name__)

COST_DISCLAIMER = "金额为保守上界估算，对账以控制台账单为准"

# eval 报告的文件名格式，对应 ``evals.report.write_report`` 写的 UTC 时间戳
# （``%Y%m%dT%H%M%SZ.json``）。这条正则同时是白名单：``name`` 来自 URL，只有
# fullmatch 通过的才允许拼进路径。
_EVAL_NAME_RE = re.compile(r"^\d{8}T\d{6}Z\.json$")


def _read_jsonl(path: Path) -> tuple[list[dict], int]:
    """读 JSONL，返回（成功解析的 JSON 对象, 跳过的坏行数）。文件不存在 → ([], 0)。

    "解析成功"不等于"能用"：``null``、数组、裸字符串都是合法 JSON，但下游一律
    按 dict 用（``item.get(...)``、``item["timestamp"]``）。不在这里挡住的话，
    半行被截断成 ``"abc`` 之外的坏数据会一路飘到调用方炸成 500，整张表消失。
    """
    if not path.exists():
        return [], 0

    records: list[dict] = []
    skipped = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(record, dict):
                skipped += 1
                continue
            records.append(record)
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

    # 延迟到这里再 import 纯粹是省开销：langgraph 是本项目的核心依赖，必然装着，
    # 但只看成本页的请求没必要为此付它那串 import 的时间。
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


def list_sessions(
    turns_path: Path,
    memory_dir: Path,
    checkpoint_db: Path,
    model_calls_path: Path,
) -> dict:
    """一行一个用户的 session 索引：回合数、失败数、成本、记忆状态。

    三个源（回合日志 / 记忆 / 成本）按 ``(platform, user_id)`` 取**并集**，理由同
    ``read_memory``：只显示交集会让"只落了日志还没写记忆"和"记忆在但日志被轮转过"
    这两种最该被看见的状态从列表上消失。

    每个源都复用既有读取函数，绝不在这里重新解析 JSONL 或重算成本——抄一份聚合，
    列表页和成本页迟早会给出两个对不上的数字。

    成本沿用 ``read_costs`` 的默认 7 天窗口：侧栏的金额是"最近一周"的量级参考，
    不是这个用户的历史总额（历史总额没有任何一个源存着，现算要全量扫）。

    排序：按 ``last_active`` 倒序，从没落过回合的排最后（不是排最前，也不是按
    名字插进中间）——运维台第一眼要看的是"刚刚谁在说话"。
    """
    turn_records, turn_skipped = _read_jsonl(turns_path)
    memory = read_memory(memory_dir, checkpoint_db)
    costs = read_costs(model_calls_path)

    sessions: dict[tuple[str, str], dict] = {}

    def bucket(platform: str, user_id: str) -> dict:
        key = (platform, user_id)
        if key not in sessions:
            sessions[key] = {
                "platform": platform,
                "user_id": user_id,
                "last_active": None,
                "turn_count": 0,
                "failed_count": 0,
                "cost_usd": 0.0,
                "cost_calls": 0,
                "has_profile": False,
                "summary_count": 0,
                "pending_compaction": 0,
            }
        return sessions[key]

    for record in turn_records:
        user_id = record.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            # 归不到任何用户的回合没法在这个视图里显示。它仍然出现在回合详情里，
            # 这里不编一个空用户出来占一行。
            continue
        platform = record.get("platform")
        entry = bucket(platform if isinstance(platform, str) else "", user_id)
        entry["turn_count"] += 1
        # 缺 success 字段的按成功算：把"字段没写"渲染成红点会让看板天天飘红。
        if not record.get("success", True):
            entry["failed_count"] += 1
        stamp = record.get("timestamp")
        if isinstance(stamp, str) and (
            entry["last_active"] is None or stamp > entry["last_active"]
        ):
            entry["last_active"] = stamp

    for user in memory["users"]:
        entry = bucket(user["platform"], user["user_id"])
        entry["has_profile"] = bool(user["profile"])
        entry["summary_count"] = len(user["summary"])
        entry["pending_compaction"] = user["pending_compaction"]

    for row in costs["aggregate"]["by_user"]:
        platform, separator, user_id = str(row["owner"]).partition(":")
        if not separator:
            # aggregate 把没有 platform/user_id 的调用归到 "unknown"，它不是某个
            # 用户，拆不回二元组，也不该在 session 列表里冒充一行。
            continue
        entry = bucket(platform, user_id)
        entry["cost_usd"] = row["cost_usd"]
        entry["cost_calls"] = row["calls"]

    rows = list(sessions.values())
    active = sorted(
        [row for row in rows if row["last_active"]],
        key=lambda row: row["last_active"],
        reverse=True,
    )
    silent = sorted(
        [row for row in rows if not row["last_active"]],
        key=lambda row: (row["platform"], row["user_id"]),
    )
    return {
        "sessions": active + silent,
        # 两个 JSONL 都在这个视图里出数据，坏行合并计数：页面只有一句"跳过 N 行"，
        # 分开报会让人以为另一个文件是干净的。
        "skipped_lines": turn_skipped + costs["skipped_lines"],
        "checkpoint_error": memory["checkpoint_error"],
    }


def list_eval_reports(evals_dir: Path) -> dict:
    """eval 报告索引，最新在前。

    只返回列表页要的摘要字段——逐 case 的 ``case_results`` 动辄几十 KB，全塞进
    索引会让"看一眼跑没跑过"这个动作把所有报告都读一遍。明细走
    ``read_eval_report``。

    文件名就是 UTC 时间戳，字典序倒排即时间倒序，不必解析时间也不必读文件内容
    排序——报告被拷贝/触碰过 mtime 就不可信了。

    坏 JSON 跳过并计入 ``skipped_files``（同 ``skipped_lines`` 的哲学：一份写了
    一半的报告不该让另外二十份从看板上消失）。名字不合规的文件根本不算报告，
    不计数。目录不存在 → 空态。
    """
    if not evals_dir.is_dir():
        return {"reports": [], "skipped_files": 0}

    reports: list[dict] = []
    skipped = 0
    for path in sorted(evals_dir.iterdir(), key=lambda p: p.name, reverse=True):
        if not _EVAL_NAME_RE.fullmatch(path.name):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("eval 报告不可读，已跳过 path=%s", path)
            skipped += 1
            continue
        if not isinstance(raw, dict):
            logger.warning("eval 报告不是 JSON 对象，已跳过 path=%s", path)
            skipped += 1
            continue
        reports.append(
            {
                "name": path.name,
                "status": raw.get("status"),
                "mode": raw.get("mode"),
                "summary": raw.get("summary") or {},
                "git_commit": raw.get("git_commit"),
            }
        )
    return {"reports": reports, "skipped_files": skipped}


def read_eval_report(evals_dir: Path, name: str) -> dict:
    """按文件名读整份报告（含 ``case_results``）。

    ``name`` 直接来自 URL，所以先过白名单正则再拼路径：正则里没有 ``/`` 也没有
    ``.``（除了固定的 ``.json``），``../`` 这类穿越根本拼不出来。绝不改成"拼完
    再检查是否落在目录内"——那种写法在符号链接上会漏。

    这里的坏 JSON 不像 ``list_eval_reports`` 那样吞掉：用户点开的就是这一份，
    静默返回空报告等于骗人，让 ``JSONDecodeError`` 抛给上层。
    """
    if not _EVAL_NAME_RE.fullmatch(name):
        raise ValueError(f"非法的 eval 报告名: {name!r}")
    path = evals_dir / name
    if not path.is_file():
        raise FileNotFoundError(f"eval 报告不存在: {name}")
    return json.loads(path.read_text(encoding="utf-8"))
