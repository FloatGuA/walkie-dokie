import json
import sys

from scripts.run_golden_eval import _run_calibration, main, run_suite
from walkie_dokie.evals.cases import GoldenCase, Turn, TurnExpect
from walkie_dokie.evals.driver import CaseResult
from walkie_dokie.evals.judge import JudgeVerdict
from walkie_dokie.evals.recording_main_agent import RecordingMainAgent
from walkie_dokie.main_agent.base import MainAgent


def _factory(main_agent_recorder=None):
    """graph_factory 的假替身：只有主 Agent recorder 会被 run_suite 读。"""

    recorder = main_agent_recorder or RecordingMainAgent(_SilentMainAgent())
    return lambda: (object(), object(), object(), recorder)


class _SilentMainAgent(MainAgent):
    async def decide(self, context):
        raise AssertionError("测试不该真的调主 Agent")

    async def finalize(self, context):
        raise AssertionError("测试不该真的调主 Agent")

    async def judge_confirmation(self, context):
        raise AssertionError("本测试不应触发确认判定")


class _BoomMainAgent(MainAgent):
    async def decide(self, context):
        raise RuntimeError("DeepSeek API 报错")

    async def finalize(self, context):
        raise RuntimeError("DeepSeek API 报错")

    async def judge_confirmation(self, context):
        raise AssertionError("本测试不应触发确认判定")


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
        graph_factory=_factory(),
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
        graph_factory=_factory(),
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
        graph_factory=_factory(),
        judge_fn=fake_judge,
        report_dir=tmp_path,
    )
    assert report.status == "FAILED_INFRA"
    assert "超时" in report.error
    assert len(report.case_results) == 1  # 只保留 a
    assert list(tmp_path.glob("*.json"))  # 崩溃也要落盘报告


async def test_judge_failure_keeps_the_case_it_crashed_on(tmp_path, monkeypatch):
    """judge 挂掉不该连累刚跑完的确定性断言结果——它们是这次运行仅有的产出。"""

    async def fake_run_case(case, **kwargs):
        return _result(case.id, True)

    async def fake_judge(case_result, case):
        if case.id == "b":
            raise RuntimeError("judge 没有返回结构化结果")
        return {"clarity": 5, "misleading": False, "comment": "ok"}

    monkeypatch.setattr("scripts.run_golden_eval.run_case", fake_run_case)
    report = await run_suite(
        [_case("a"), _case("b"), _case("c")],
        graph_factory=_factory(),
        judge_fn=fake_judge,
        report_dir=tmp_path,
    )
    assert report.status == "FAILED_INFRA"
    assert [c["case_id"] for c in report.case_results] == ["a", "b"]
    assert report.case_results[1]["passed"] is True  # b 的断言结果没丢
    assert report.case_results[1]["judge"] is None  # 只是没判到分


async def test_main_agent_failure_is_infra_not_a_green_reply(tmp_path, monkeypatch):
    """graph 会把主 Agent 异常吞成确定性 reply——recorder 记下的错误必须扳回 FAILED_INFRA。"""

    recorder = RecordingMainAgent(_BoomMainAgent())

    async def fake_run_case(case, **kwargs):
        if case.id == "b":
            # 模拟 graph 的降级：decide 抛异常被吞掉，样本照样「跑完且通过」
            try:
                await recorder.decide(object())
            except RuntimeError:
                pass
        return _result(case.id, True)

    async def fake_judge(case_result, case):
        return None

    monkeypatch.setattr("scripts.run_golden_eval.run_case", fake_run_case)
    report = await run_suite(
        [_case("a"), _case("b"), _case("c")],
        graph_factory=_factory(recorder),
        judge_fn=fake_judge,
        report_dir=tmp_path,
    )
    assert report.status == "FAILED_INFRA"
    assert "DeepSeek API 报错" in report.error
    # a 和触发故障的 b 都保留，c 没跑
    assert [c["case_id"] for c in report.case_results] == ["a", "b"]


async def test_mode_is_recorded_in_report(tmp_path, monkeypatch):
    async def fake_run_case(case, **kwargs):
        return _result(case.id, True)

    async def fake_judge(case_result, case):
        return None

    monkeypatch.setattr("scripts.run_golden_eval.run_case", fake_run_case)
    default_report = await run_suite(
        [_case("a")],
        graph_factory=_factory(),
        judge_fn=fake_judge,
        report_dir=tmp_path,
    )
    assert default_report.mode == "regression"

    real_report = await run_suite(
        [_case("a")],
        graph_factory=_factory(),
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


async def test_calibration_prompt_carries_scene_context(tmp_path, monkeypatch):
    """校准 prompt 必须带场景，否则靠上下文才成立的坏话术 judge 根本看不出来。"""

    path = tmp_path / "cal.yaml"
    path.write_text(
        "- id: cal-bad-2\n"
        "  reply: 文件已经处理完成。\n"
        "  context: 用户刚发来文件，还没确认任何任务\n"
        "  expected: bad\n",
        encoding="utf-8",
    )
    prompts = []

    async def fake_judge_replies(prompt, **kwargs):
        prompts.append(prompt)
        return JudgeVerdict(clarity=5, misleading=False, comment="ok")

    monkeypatch.setattr("scripts.run_golden_eval.CALIBRATION_PATH", path)
    monkeypatch.setattr("scripts.run_golden_eval.judge_replies", fake_judge_replies)

    assert await _run_calibration() == 1  # judge 判 good、期望 bad，一致率 0
    assert "用户刚发来文件，还没确认任何任务" in prompts[0]
    assert "助手：文件已经处理完成。" in prompts[0]


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
