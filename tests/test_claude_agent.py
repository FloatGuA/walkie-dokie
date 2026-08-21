"""ClaudeAgentSDKBackend 的配置行为（安全边界的测试在 test_execution_security.py）。"""

import pytest

from walkie_dokie.agents.claude_agent import ClaudeAgentSDKBackend, _execution_options


def test_execution_options_carry_the_configured_model(tmp_path):
    options = _execution_options(tmp_path, model="sonnet")
    assert options.model == "sonnet"


@pytest.mark.parametrize(
    "difficulty,expected",
    [("simple", "haiku"), ("standard", "sonnet"), ("complex", "opus")],
)
def test_backend_routes_difficulty_to_model(difficulty, expected):
    assert ClaudeAgentSDKBackend().model_for(difficulty) == expected


def test_unknown_difficulty_falls_back_to_sonnet():
    # MainAgent 侧已经把非法值兜成 standard，这里是第二道边界：
    # 老 checkpoint 或直接调用者传了怪值时不炸、走中档。
    assert ClaudeAgentSDKBackend().model_for("weird") == "sonnet"


def test_env_locked_model_bypasses_routing():
    backend = ClaudeAgentSDKBackend(model="opus")
    assert backend.model_for("simple") == "opus"
    assert backend.model_for("complex") == "opus"
