import json
import sys

from scripts.run_golden_eval import main, run_suite
from walkie_dokie.evals.cases import GoldenCase, Turn, TurnExpect
from walkie_dokie.evals.driver import CaseResult


def _case(case_id):
    return GoldenCase(
        id=case_id,
        category="intent_routing",
        description="x",
        turns=(Turn(user="hi", expect=TurnExpect(action="reply")),),
    )


def _result(case_id, passed):
    return CaseResult(
        case_id=case_id,
        category="intent_routing",
        passed=passed,
        failures=() if passed else ("boom",),
        turns=(),
        aborted_at_turn=None,
        duration_ms=1,
    )


async def test_all_pass_writes_passed_report(tmp_path, monkeypatch):
    async def fake_run_case(case, **kwargs):
        return _result(case.id, True)

    async def fake_judge(case_result, case):
        return {"clarity": 5, "misleading": False, "comment": "ok"}

    monkeypatch.setattr("scripts.run_golden_eval.run_case", fake_run_case)
    report = await run_suite(
        [_case("a"), _case("b")],
        graph_factory=lambda: (object(), object(), object()),
        judge_fn=fake_judge,
        report_dir=tmp_path,
    )
    assert report.status == "PASSED"
    assert report.summary == {
        "total": 2,
        "passed": 2,
        "failed": 0,
        "judge_clarity_avg": 5.0,
    }
    assert list(tmp_path.glob("*.json"))


async def test_assertion_failure_marks_failed_but_runs_all(tmp_path, monkeypatch):
    seen = []

    async def fake_run_case(case, **kwargs):
        seen.append(case.id)
        return _result(case.id, case.id != "a")

    async def fake_judge(case_result, case):
        return None

    monkeypatch.setattr("scripts.run_golden_eval.run_case", fake_run_case)
    report = await run_suite(
        [_case("a"), _case("b")],
        graph_factory=lambda: (object(), object(), object()),
        judge_fn=fake_judge,
        report_dir=tmp_path,
    )
    assert report.status == "FAILED"
    assert seen == ["a", "b"]  # 断言失败不终止运行


async def test_infra_exception_aborts_and_keeps_partial(tmp_path, monkeypatch):
    async def fake_run_case(case, **kwargs):
        if case.id == "b":
            raise RuntimeError("DeepSeek API 超时")
        return _result(case.id, True)

    async def fake_judge(case_result, case):
        return None

    monkeypatch.setattr("scripts.run_golden_eval.run_case", fake_run_case)
    report = await run_suite(
        [_case("a"), _case("b"), _case("c")],
        graph_factory=lambda: (object(), object(), object()),
        judge_fn=fake_judge,
        report_dir=tmp_path,
    )
    assert report.status == "FAILED_INFRA"
    assert "超时" in report.error
    assert len(report.case_results) == 1  # 只保留 a
    assert list(tmp_path.glob("*.json"))  # 崩溃也要落盘报告


async def test_mode_is_recorded_in_report(tmp_path, monkeypatch):
    async def fake_run_case(case, **kwargs):
        return _result(case.id, True)

    async def fake_judge(case_result, case):
        return None

    monkeypatch.setattr("scripts.run_golden_eval.run_case", fake_run_case)
    default_report = await run_suite(
        [_case("a")],
        graph_factory=lambda: (object(), object(), object()),
        judge_fn=fake_judge,
        report_dir=tmp_path,
    )
    assert default_report.mode == "regression"

    real_report = await run_suite(
        [_case("a")],
        graph_factory=lambda: (object(), object(), object()),
        judge_fn=fake_judge,
        report_dir=tmp_path,
        mode="real-execution",
    )
    assert real_report.mode == "real-execution"


def _silence_logging(monkeypatch):
    # setup_logging 每次调用都往 root 挂新 handler，测试里调真身会污染整个 session。
    monkeypatch.setattr("scripts.run_golden_eval.setup_logging", lambda: None)


def test_loader_failure_exits_2_and_writes_infra_report(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("evals/cases 目录不存在")

    _silence_logging(monkeypatch)
    monkeypatch.setattr("scripts.run_golden_eval.load_cases", boom)
    monkeypatch.setattr("scripts.run_golden_eval.REPORT_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_golden_eval"])

    assert main() == 2

    reports = list(tmp_path.glob("*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["status"] == "FAILED_INFRA"
    assert "evals/cases 目录不存在" in payload["error"]
    assert payload["case_results"] == []


def test_calibration_failure_exits_2(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("校准样本 cal-bad-1 的 expected 非法")

    _silence_logging(monkeypatch)
    monkeypatch.setattr("scripts.run_golden_eval.load_calibration", boom)
    monkeypatch.setattr("scripts.run_golden_eval.REPORT_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_golden_eval", "--calibrate"])

    assert main() == 2
    # 校准不是 golden 运行，不产出 RunReport。
    assert list(tmp_path.glob("*.json")) == []
