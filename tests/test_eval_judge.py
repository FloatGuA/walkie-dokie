from pathlib import Path
from types import SimpleNamespace

import pytest

from walkie_dokie.agents.security import sensitive_environment_overrides
from walkie_dokie.evals.judge import (
    JudgeVerdict,
    _judge_options,
    agreement_rate,
    build_judge_prompt,
    judge_replies,
    load_calibration,
    verdict_matches,
)

CALIBRATION = Path(__file__).resolve().parents[1] / "evals" / "judge_calibration.yaml"


def _fake_query(payload: dict):
    """伪 SDK：只产出一条形如 ResultMessage 的消息（结构化结果走 structured_output）。"""

    async def query_fn(*, prompt, options):
        yield SimpleNamespace(
            structured_output=payload,
            result=None,
            is_error=False,
            subtype="success",
        )

    return query_fn


async def test_judge_parses_structured_verdict():
    verdict = await judge_replies(
        build_judge_prompt("方法咨询", "用户：怎么调行距\n助手：在段落设置里调。"),
        query_fn=_fake_query({"clarity": 5, "misleading": False, "comment": "清晰"}),
    )
    assert verdict == JudgeVerdict(clarity=5, misleading=False, comment="清晰")


async def test_judge_error_result_raises():
    async def query_fn(*, prompt, options):
        yield SimpleNamespace(
            structured_output=None, result=None, is_error=True, subtype="error_max_turns"
        )

    with pytest.raises(RuntimeError, match="error_max_turns"):
        await judge_replies("p", query_fn=query_fn)


async def test_judge_without_structured_output_raises():
    async def query_fn(*, prompt, options):
        yield SimpleNamespace(text="思考中")

    with pytest.raises(RuntimeError, match="没有返回结构化结果"):
        await judge_replies("p", query_fn=query_fn)


def test_judge_options_are_isolated_like_execution_options():
    """真实 SDK 路径离线测不到，至少把隔离字段锁住（对齐 claude_agent._execution_options）。"""

    options = _judge_options()
    assert options.model == "opus"
    assert options.allowed_tools == []
    assert options.setting_sources == []
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.skills == []
    assert options.env == sensitive_environment_overrides()


def test_build_judge_prompt_contains_case_and_transcript():
    prompt = build_judge_prompt("方法咨询", "用户：怎么调行距")
    assert "方法咨询" in prompt
    assert "用户：怎么调行距" in prompt


def test_calibration_verdict_matching():
    assert verdict_matches("good", JudgeVerdict(4, False, ""))
    assert not verdict_matches("good", JudgeVerdict(4, True, ""))
    assert verdict_matches("bad", JudgeVerdict(2, False, ""))
    assert verdict_matches("bad", JudgeVerdict(5, True, ""))
    assert not verdict_matches("bad", JudgeVerdict(3, False, ""))  # 中间地带不算 bad 命中


def test_load_calibration_and_agreement():
    entries = load_calibration(CALIBRATION)
    assert {e["expected"] for e in entries} == {"good", "bad"}
    # 每条都要带场景：golden 判分喂「场景 + 转写」，校准喂孤立单句会让分布错配，
    # 靠上下文才成立的坏话术（如未确认就谎报完成）在 judge 眼里必然是 good。
    assert all(e["context"].strip() for e in entries)
    verdicts = [
        JudgeVerdict(5, False, "") if e["expected"] == "good" else JudgeVerdict(1, True, "")
        for e in entries
    ]
    assert agreement_rate([e["expected"] for e in entries], verdicts) == 1.0


def test_load_calibration_rejects_illegal_expected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "- id: x\n  reply: hi\n  context: 用户刚打招呼\n  expected: maybe\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="expected 非法"):
        load_calibration(path)


@pytest.mark.parametrize("missing", ["reply", "context", "expected"])
def test_load_calibration_requires_all_fields(tmp_path, missing):
    entry = {"reply": "hi", "context": "用户刚打招呼", "expected": "good"}
    del entry[missing]
    lines = "\n".join(f"  {k}: {v}" for k, v in entry.items())
    path = tmp_path / "bad.yaml"
    path.write_text(f"- id: x\n{lines}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=missing):
        load_calibration(path)


def test_agreement_rate_rejects_length_mismatch():
    with pytest.raises(ValueError):
        agreement_rate(["good", "bad"], [JudgeVerdict(5, False, "")])
