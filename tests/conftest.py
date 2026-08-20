import pytest

from walkie_dokie import model_call_log


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
