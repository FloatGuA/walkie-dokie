import json
from types import SimpleNamespace

import pytest

from walkie_dokie.agents.base import ExecutionReport
from walkie_dokie.main_agent.base import DialogueContext, FinalizeContext, TaskContract
from walkie_dokie.main_agent.deepseek import DeepSeekMainAgent


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = json.dumps(self.responses.pop(0), ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


def fake_client(responses):
    completions = FakeCompletions(responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


async def test_decide_uses_toolless_main_agent_prompt_and_builds_task_contract():
    client, completions = fake_client(
        [
            {
                "action": "propose_task",
                "user_message": "我会为你写请假条，回复“是”确认。",
                "task": {
                    "instruction": "为张三生成请假条",
                    "missing_info": ["请假日期"],
                },
                "memory_operations": [
                    {
                        "action": "set",
                        "field": "name",
                        "value": "张三",
                        "evidence": "我叫张三",
                    }
                ],
            }
        ]
    )
    agent = DeepSeekMainAgent(client=client)
    decision = await agent.decide(
        DialogueContext(
            "我叫张三，帮我写请假条",
            None,
            {},
            current_user_text="我叫张三，帮我写请假条",
        )
    )
    assert decision.task == TaskContract("为张三生成请假条", ("请假日期",))
    assert decision.memory_operations[0].field == "name"
    call = completions.calls[0]
    assert "tools" not in call
    system_prompt = call["messages"][0]["content"]
    assert "唯一负责理解用户" in system_prompt
    assert "你是小帮" in system_prompt
    assert "绝不能写入用户记忆" in system_prompt


async def test_invalid_memory_operation_from_model_is_discarded():
    client, _ = fake_client(
        [
            {
                "action": "reply",
                "user_message": "你好。",
                "task": None,
                "memory_operations": [
                    {
                        "action": "set",
                        "field": "robot_name",
                        "value": "小帮",
                        "evidence": "你叫小帮",
                    }
                ],
            }
        ]
    )
    decision = await DeepSeekMainAgent(client=client).decide(
        DialogueContext("你叫小帮", None, {})
    )
    assert decision.memory_operations == ()


async def test_finalize_turns_internal_report_into_user_message(tmp_path):
    client, completions = fake_client([{"user_message": "请假条已经写好并发给你了。"}])
    agent = DeepSeekMainAgent(client=client)
    artifact = tmp_path / "请假条.docx"
    artifact.write_bytes(b"x")
    message = await agent.finalize(
        FinalizeContext(
            task=TaskContract("生成请假条"),
            report=ExecutionReport("已生成 docx", artifact, "请假条.docx"),
        )
    )
    assert message == "请假条已经写好并发给你了。"
    payload = json.loads(completions.calls[0]["messages"][1]["content"])
    assert payload["execution_report"]["summary"] == "已生成 docx"


async def test_propose_task_requires_valid_task_contract():
    client, _ = fake_client(
        [
            {
                "action": "propose_task",
                "user_message": "确认吗？",
                "task": None,
                "memory_operations": [],
            }
        ]
    )
    with pytest.raises(RuntimeError, match="task.instruction"):
        await DeepSeekMainAgent(client=client).decide(
            DialogueContext("写文档", None, {})
        )


async def test_memory_operation_without_verbatim_evidence_is_discarded():
    client, _ = fake_client(
        [
            {
                "action": "reply",
                "user_message": "你好。",
                "task": None,
                "memory_operations": [
                    {"action": "set", "field": "name", "value": "张三"}
                ],
            }
        ]
    )
    decision = await DeepSeekMainAgent(client=client).decide(
        DialogueContext("我叫张三", None, {}, current_user_text="我叫张三")
    )
    assert decision.memory_operations == ()


async def test_previous_artifact_selection_is_part_of_task_contract():
    client, _ = fake_client(
        [
            {
                "action": "propose_task",
                "user_message": "我会继续修改上一份文件，回复“是”确认。",
                "task": {
                    "instruction": "修改上一份文件",
                    "missing_info": [],
                    "use_previous_artifact": True,
                },
                "memory_operations": [],
            }
        ]
    )
    decision = await DeepSeekMainAgent(client=client).decide(
        DialogueContext(
            "继续修改刚才的文件",
            None,
            {},
            active_artifact_filename="result.docx",
            current_user_text="继续修改刚才的文件",
        )
    )
    assert decision.task is not None
    assert decision.task.use_previous_artifact is True


async def test_previous_artifact_flag_must_be_real_json_boolean():
    client, _ = fake_client(
        [
            {
                "action": "propose_task",
                "user_message": "确认吗？",
                "task": {
                    "instruction": "修改上一份文件",
                    "missing_info": [],
                    "use_previous_artifact": "false",
                },
                "memory_operations": [],
            }
        ]
    )
    with pytest.raises(RuntimeError, match="JSON boolean"):
        await DeepSeekMainAgent(client=client).decide(
            DialogueContext("修改文件", None, {})
        )
