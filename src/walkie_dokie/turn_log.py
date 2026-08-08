"""结构化留痕：每一轮"用户输入 -> 系统输出"落一行 JSONL，供复盘用。

跟 logging_config 的人读日志是两回事——那个是给人看的过程叙述，这个是给
以后写脚本查询/统计用的结构化记录（多少轮成功、平均耗时、哪个用户问得最多）。
"""

import asyncio
import dataclasses
import json
from datetime import datetime
from pathlib import Path

_VAR_ROOT = Path(__file__).parent.parent.parent / "var"
TURN_LOG_PATH = _VAR_ROOT / "logs" / "turns.jsonl"

_write_lock = asyncio.Lock()


@dataclasses.dataclass
class TurnRecord:
    platform: str
    user_id: str
    run_id: str | None
    input_text: str | None
    input_filename: str | None
    backend: str | None
    output_text: str | None
    output_filename: str | None
    duration_ms: int | None
    success: bool
    error: str | None = None


async def log_turn(record: TurnRecord) -> None:
    line = {"timestamp": datetime.now().isoformat(), **dataclasses.asdict(record)}
    async with _write_lock:
        TURN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TURN_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
