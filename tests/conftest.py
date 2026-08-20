import pytest

from walkie_dokie import model_call_log, turn_log


@pytest.fixture(autouse=True)
def _isolate_model_call_log(tmp_path, monkeypatch):
    """把成本埋点重定向到 tmp，不让 pytest 污染真实的 var/logs/model_calls.jsonl。

    这条日志是成本报表的唯一数据源：测试里的假调用写进去就是假账（38 条
    tokens=null 的记录），会把报表的调用数直接算错。单测自己要断言写入内容时
    仍可以再 monkeypatch 一次，覆盖这里的路径。
    """

    monkeypatch.setattr(
        model_call_log, "MODEL_CALL_LOG_PATH", tmp_path / "model_calls.jsonl"
    )


@pytest.fixture(autouse=True)
def _isolate_turn_log(tmp_path, monkeypatch):
    """同上：turn log 是复盘统计的数据源，测试假回合写进真实 turns.jsonl 就是假账。

    多数测试本来就 monkeypatch 掉 log_turn 函数；这里兜住的是漏网的真实写入
    路径（既有污染问题，2026-08-21 随成本埋点隔离一并治理）。
    """

    monkeypatch.setattr(turn_log, "TURN_LOG_PATH", tmp_path / "turns.jsonl")
