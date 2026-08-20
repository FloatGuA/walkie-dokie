"""Golden set 回归评估入口。手动运行，标准 pytest 不收集（联网、花钱）。

用法（必须从仓库根运行——driver 依赖 ``scripts.run_mvp``，需要仓库根在 sys.path）：
    python3 -m scripts.run_golden_eval                    # 回归：真实 DeepSeek + fake 执行
    python3 -m scripts.run_golden_eval --real-execution   # 冒烟：真实执行后端
    python3 -m scripts.run_golden_eval --calibrate        # 只跑 judge 校准集

退出码：0=全过 / 1=断言失败或校准不达标 / 2=基础设施异常。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from walkie_dokie.evals.cases import load_cases
from walkie_dokie.evals.driver import run_case
from walkie_dokie.evals.fake_execution import (
    FakeExecutionAgent,
    RecordingExecutionAgent,
)
from walkie_dokie.evals.judge import (
    agreement_rate,
    build_judge_prompt,
    judge_replies,
    load_calibration,
)
from walkie_dokie.evals.recording_main_agent import RecordingMainAgent
from walkie_dokie.evals.report import build_report, write_report
from walkie_dokie.logging_config import setup_logging

logger = logging.getLogger(__name__)

CASES_DIR = Path("evals/cases")
FIXTURES_DIR = Path("evals/fixtures")
CALIBRATION_PATH = Path("evals/judge_calibration.yaml")
REPORT_DIR = Path("var/evals")
DEEPSEEK_MODEL = "deepseek-chat"
JUDGE_MODEL = "opus"


def _blacklist() -> tuple[str, ...]:
    raw = os.environ.get("EVAL_REPLY_BLACKLIST", "")
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not items:
        logger.warning("EVAL_REPLY_BLACKLIST 未设置——开发者邮箱等敏感词不会被断言拦截")
    return items


def _transcript(case, result) -> str:
    lines = []
    for turn, obs in zip(case.turns, result.turns):
        lines.append(f"用户：{turn.user}")
        for reply in obs.replies:
            lines.append(f"助手：{reply}")
    return "\n".join(lines)


async def _judge_case(case_result, case) -> dict | None:
    verdict = await judge_replies(
        build_judge_prompt(case.description, _transcript(case, case_result))
    )
    return {
        "clarity": verdict.clarity,
        "misleading": verdict.misleading,
        "comment": verdict.comment,
    }


async def run_suite(
    cases,
    *,
    graph_factory,
    judge_fn,
    report_dir: Path,
    mode: str = "regression",
):
    """顺序跑完全部样本并落盘报告。

    fail-fast 只针对基础设施异常：样本断言失败照常跑完剩下的样本（一次运行看到
    全部 badcase 才有诊断价值），而 ``run_case``/judge/graph 构造抛出的异常说明
    harness 本身坏了，继续跑只会产出一堆无意义的假失败，立即终止并保留已完成结果。
    """
    results = []
    try:
        # graph/recorder/memory 必须成套构造：recorder 没接进 graph 时 executed 恒 False。
        # 每次运行都是全新 checkpointer + 全新 tmp memory 目录，见 _graph_factory。
        graph, recorder, memory_repository, main_agent_recorder = graph_factory()
        for case in cases:
            result = await run_case(
                case,
                graph=graph,
                recorder=recorder,
                memory_repository=memory_repository,
                fixtures_dir=FIXTURES_DIR,
            )
            # 先入列再判分：judge 抛异常时，刚跑完的确定性断言结果照样进报告
            # （CaseResult 可变，append 之后再填 judge 字段）。
            results.append(result)
            if main_agent_recorder.errors:
                # graph 把主 Agent 异常降级成了确定性 reply，样本表面正常但语义全假。
                raise RuntimeError(
                    f"主 Agent 调用失败 {len(main_agent_recorder.errors)} 次"
                    f"（graph 已降级成确定性回复，样本结果不可信）："
                    f"{main_agent_recorder.errors[0]!r}"
                )
            result.judge = await judge_fn(result, case)
    except Exception as exc:  # 基础设施异常：fail-fast，保留已完成结果
        report = build_report(
            mode,
            "FAILED_INFRA",
            results,
            deepseek_model=DEEPSEEK_MODEL,
            judge_model=JUDGE_MODEL,
            error=str(exc),
        )
        path = write_report(report, out_dir=report_dir)
        logger.exception("基础设施异常，已跑 %d 个样本，报告写入 %s", len(results), path)
        return report
    status = "PASSED" if all(r.passed for r in results) else "FAILED"
    report = build_report(
        mode,
        status,
        results,
        deepseek_model=DEEPSEEK_MODEL,
        judge_model=JUDGE_MODEL,
    )
    path = write_report(report, out_dir=report_dir)
    logger.info("报告已写入 %s summary=%s", path, report.summary)
    return report


def _graph_factory(real_execution: bool):
    def factory():
        from walkie_dokie.main_agent.deepseek import DeepSeekMainAgent
        from walkie_dokie.main_agent.memory import JsonMemoryRepository
        from walkie_dokie.orchestrator import build_graph

        if real_execution:
            from walkie_dokie.agents.claude_agent import ClaudeAgentSDKBackend

            inner = ClaudeAgentSDKBackend()
        else:
            inner = FakeExecutionAgent(output_fixture=FIXTURES_DIR / "fake_output.docx")
        recorder = RecordingExecutionAgent(inner)
        memory_repository = JsonMemoryRepository(
            Path(tempfile.mkdtemp(prefix="eval-memory-"))
        )
        # 主 Agent 也要包一层：graph 会吞掉它的异常，不记录就看不见 DeepSeek 挂了。
        main_agent_recorder = RecordingMainAgent(DeepSeekMainAgent())
        graph = build_graph(
            main_agent_recorder,
            recorder,
            memory_repository,
            checkpointer=InMemorySaver(),
        )
        return graph, recorder, memory_repository, main_agent_recorder

    return factory


def _print_summary(report) -> None:
    print(f"状态：{report.status} 模式：{report.mode} summary={report.summary}")
    if report.error:
        print(f"基础设施异常：{report.error}")
    for case in report.case_results:
        if case["passed"]:
            continue
        where = (
            f"第 {case['aborted_at_turn'] + 1} 轮中止"
            if case["aborted_at_turn"] is not None
            else "跑完全部轮次"
        )
        print(f"  FAIL {case['case_id']}（{where}）")
        for failure in case["failures"]:
            print(f"    - {failure}")


async def _run_calibration() -> int:
    entries = load_calibration(CALIBRATION_PATH)
    verdicts = []
    for entry in entries:
        # 场景走 case_description 位、话术走 transcript 位，和 golden 判分同一个形状。
        verdicts.append(
            await judge_replies(
                build_judge_prompt(entry["context"], f"助手：{entry['reply']}")
            )
        )
    rate = agreement_rate([e["expected"] for e in entries], verdicts)
    print(f"judge 校准一致率：{rate:.0%}（{len(entries)} 条，建议线 >=90%）")
    for entry, verdict in zip(entries, verdicts):
        print(f"  {entry['id']}: expected={entry['expected']} got={verdict}")
    return 0 if rate >= 0.9 else 1


def _run_regression(real_execution: bool, report_dir: Path):
    """常规分支：样本加载失败与 run_case/judge 失败同属基础设施异常。

    loader 抛异常时如果让它冒泡，用户拿到的是裸 traceback 和 Python 默认退出码 1
    ——和「样本断言失败」撞码，且一份报告都不写。这里把它收敛成和 ``run_suite``
    内部一致的处理：FAILED_INFRA 报告 + 退出码 2。
    """
    mode = "real-execution" if real_execution else "regression"
    try:
        cases = load_cases(CASES_DIR, FIXTURES_DIR, _blacklist())
    except Exception as exc:
        report = build_report(
            mode,
            "FAILED_INFRA",
            [],
            deepseek_model=DEEPSEEK_MODEL,
            judge_model=JUDGE_MODEL,
            error=str(exc),
        )
        path = write_report(report, out_dir=report_dir)
        logger.exception("样本加载失败，一个样本都没跑，报告写入 %s", path)
        return report
    return asyncio.run(
        run_suite(
            cases,
            graph_factory=_graph_factory(real_execution),
            judge_fn=_judge_case,
            report_dir=report_dir,
            mode=mode,
        )
    )


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-execution", action="store_true")
    parser.add_argument("--calibrate", action="store_true")
    args = parser.parse_args()

    if args.calibrate:
        try:
            return asyncio.run(_run_calibration())
        except Exception as exc:
            # 校准集读不出来 / judge 调不通同样是基础设施异常，退出码同为 2。
            # 这一支不写 RunReport：RunReport 的字段（case_results、summary、
            # deepseek_model）全是 golden 运行的语义，塞一份空壳只会污染报告目录。
            logger.exception("judge 校准失败")
            print(f"judge 校准失败：{exc}")
            return 2

    report = _run_regression(args.real_execution, REPORT_DIR)
    _print_summary(report)
    if report.status == "FAILED_INFRA":
        return 2
    return 0 if report.status == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
