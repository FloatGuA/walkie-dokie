"""模型调用成本埋点：每次真实模型调用落一行 JSONL，供 scripts/report_costs.py 聚合。

跟 turn_log 的分工：turn_log 记"一轮用户交互发生了什么"，这里记"为此打了几次
模型、烧了多少 token"。一轮可能对应多次模型调用（decide + finalize + 压缩），
所以不能塞进 TurnRecord，只能单独一张表。

形状和写入方式刻意照抄 turn_log：同一个 asyncio.Lock + append JSONL，磁盘出问题
就该炸（fail fast），不额外兜底。
"""

import asyncio
import dataclasses
import json
from datetime import datetime
from pathlib import Path

_VAR_ROOT = Path(__file__).parent.parent.parent / "var"
MODEL_CALL_LOG_PATH = _VAR_ROOT / "logs" / "model_calls.jsonl"

_write_lock = asyncio.Lock()


@dataclasses.dataclass
class ModelCallRecord:
    # provider/model 分开存：同一个 provider 以后可能换模型，报表要能按模型分档计价。
    provider: str
    model: str
    # decide|finalize|judge_confirmation|summarize|merge —— 报表的主分组维度。
    purpose: str
    platform: str | None
    user_id: str | None
    # token 数来自 provider 返回的 usage；拿不到就是 None，绝不填 0——
    # 0 在报表里等于"这次真的没烧 token"，是另一个意思。
    prompt_tokens: int | None
    completion_tokens: int | None
    duration_ms: int | None


async def log_model_call(record: ModelCallRecord) -> None:
    line = {"timestamp": datetime.now().isoformat(), **dataclasses.asdict(record)}
    async with _write_lock:
        MODEL_CALL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MODEL_CALL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
