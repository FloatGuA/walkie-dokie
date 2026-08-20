import json

import pytest

from walkie_dokie.evals.checks import TurnObservation
from walkie_dokie.evals.driver import CaseResult
from walkie_dokie.evals.report import build_report, write_report


def _case(passed, judge=None):
    return CaseResult(
        case_id="c1",
        category="intent_routing",
        passed=passed,
        failures=() if passed else ("turn[0] action 期望 x，实际 y",),
        turns=(TurnObservation("reply", None, False, ("好",)),),
        aborted_at_turn=None,
        duration_ms=12,
        judge=judge,
    )


def test_build_report_summarizes_and_serializes(tmp_path):
    report = build_report(
        "regression",
        "FAILED",
        [_case(True, judge={"clarity": 4, "misleading": False}), _case(False)],
        deepseek_model="deepseek-chat",
        judge_model="opus",
    )
    assert report.summary == {
        "total": 2,
        "passed": 1,
        "failed": 1,
        "judge_clarity_avg": 4.0,
    }
    path = write_report(report, out_dir=tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "FAILED"
    assert data["case_results"][1]["failures"]
    assert path.name.endswith("Z.json")


def test_judge_dict_without_clarity_fails_fast():
    """judge 契约固化后缺 clarity 属于 harness 坏了，不能悄悄从均值里漏掉。"""

    with pytest.raises(KeyError):
        build_report(
            "regression",
            "PASSED",
            [_case(True, judge={"misleading": False})],
            deepseek_model="deepseek-chat",
            judge_model="opus",
        )


def test_infra_failure_keeps_error_and_partial_results(tmp_path):
    report = build_report(
        "regression",
        "FAILED_INFRA",
        [_case(True)],
        deepseek_model="deepseek-chat",
        judge_model=None,
        error="DeepSeek API 超时",
    )
    assert report.error == "DeepSeek API 超时"
    assert report.summary["total"] == 1

