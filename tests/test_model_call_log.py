import json

from walkie_dokie import model_call_log
from walkie_dokie.model_call_log import ModelCallRecord, log_model_call


def _record(**overrides) -> ModelCallRecord:
    fields = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "purpose": "decide",
        "platform": "feishu",
        "user_id": "u1",
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "duration_ms": 900,
    }
    fields.update(overrides)
    return ModelCallRecord(**fields)


async def test_log_model_call_appends_jsonl_line_with_timestamp(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "model_calls.jsonl"
    monkeypatch.setattr(model_call_log, "MODEL_CALL_LOG_PATH", path)

    await log_model_call(_record())

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["provider"] == "deepseek"
    assert entry["model"] == "deepseek-chat"
    assert entry["purpose"] == "decide"
    assert entry["platform"] == "feishu"
    assert entry["user_id"] == "u1"
    assert entry["prompt_tokens"] == 100
    assert entry["completion_tokens"] == 20
    assert entry["duration_ms"] == 900
    assert entry["timestamp"]


async def test_log_model_call_appends_instead_of_overwriting(tmp_path, monkeypatch):
    path = tmp_path / "model_calls.jsonl"
    monkeypatch.setattr(model_call_log, "MODEL_CALL_LOG_PATH", path)

    await log_model_call(_record(purpose="decide"))
    await log_model_call(_record(purpose="finalize"))

    purposes = [json.loads(line)["purpose"] for line in path.read_text().splitlines()]
    assert purposes == ["decide", "finalize"]


async def test_unknown_usage_is_recorded_as_none_not_zero(tmp_path, monkeypatch):
    """usage 缺失时记 None：0 会被报表当成"这次真的没花 token"。"""

    path = tmp_path / "model_calls.jsonl"
    monkeypatch.setattr(model_call_log, "MODEL_CALL_LOG_PATH", path)

    await log_model_call(
        _record(prompt_tokens=None, completion_tokens=None, platform=None, user_id=None)
    )

    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["prompt_tokens"] is None
    assert entry["completion_tokens"] is None
    assert entry["platform"] is None
    assert entry["user_id"] is None
