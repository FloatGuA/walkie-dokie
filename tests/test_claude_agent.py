"""ClaudeAgentSDKBackend 的配置行为（安全边界的测试在 test_execution_security.py）。"""

from walkie_dokie.agents.claude_agent import ClaudeAgentSDKBackend, _execution_options


def test_execution_options_carry_the_configured_model(tmp_path):
    options = _execution_options(tmp_path, model="sonnet")
    assert options.model == "sonnet"


def test_backend_defaults_to_sonnet():
    assert ClaudeAgentSDKBackend().model == "sonnet"


def test_backend_accepts_a_model_override():
    assert ClaudeAgentSDKBackend(model="opus").model == "opus"
