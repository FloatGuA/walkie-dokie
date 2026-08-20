"""观测台的数据读取层：把 var/logs 下的 JSONL 变成面板要的形状。

只读，不写任何文件，也不 import fastapi——HTTP 层（Task 4）依赖这里，反过来不行，
这样这些函数在没装 admin extra 的环境里照样能跑、能测。

对外部数据宽容是刻意的：日志文件可能正被 bot 追加、可能上次进程被 kill 留了半行。
坏行跳过并计数（``skipped_lines`` 交给页面显示），文件缺失当空态——观测台自己因为
被观测对象还没产出数据而崩掉，是最没用的失败方式。
"""

import json
from pathlib import Path

from scripts.report_costs import aggregate

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
