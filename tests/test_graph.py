import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from walkie_dokie.agents.base import ExecutionAgent, ExecutionResult
from walkie_dokie.orchestrator import build_graph
from walkie_dokie.orchestrator.graph import _is_confirmation
from walkie_dokie.platforms.base import IncomingFile

FAKE_DRAFT = {"task_summary": "写一份测试文档", "missing_info": []}


class FakeAgent(ExecutionAgent):
    def __init__(self):
        self.calls: list[str] = []

    async def run(self, instruction, input_file, workdir, input_filename=None):
        self.calls.append(instruction)
        return ExecutionResult(reply_text=f"done: {instruction}", result_file=None, result_filename=None)


@pytest.fixture(autouse=True)
def _no_real_io(monkeypatch, tmp_path):
    """graph.py 会碰真实文件系统（工作目录）和真实日志文件，测试里都换成不落地的假实现。"""
    monkeypatch.setattr(
        "walkie_dokie.orchestrator.graph.create_workspace_dir", lambda platform, user_id: tmp_path
    )

    async def _fake_log_turn(record):
        pass

    monkeypatch.setattr("walkie_dokie.orchestrator.graph.log_turn", _fake_log_turn)


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
def graph(agent, monkeypatch):
    async def _fake_draft(text, input_filename=None):
        return FAKE_DRAFT

    monkeypatch.setattr("walkie_dokie.orchestrator.graph.generate_draft_task_prompt", _fake_draft)
    return build_graph(agent, checkpointer=InMemorySaver())


def _config(user_id="u1"):
    return {"configurable": {"thread_id": user_id}}


async def test_text_only_reaches_confirm_then_executes(graph, agent):
    config = _config()
    state = await graph.ainvoke(
        {"platform": "test", "user_id": "u1", "new_text": "帮我写份文档", "new_file": None}, config=config
    )
    assert "__interrupt__" in state
    assert state["__interrupt__"][0].value["draft_task_prompt"] == FAKE_DRAFT

    state = await graph.ainvoke(Command(resume="是"), config=config)
    assert state["result"]["reply_text"] == "done: 写一份测试文档"
    assert state["pending_instruction"] is None
    assert agent.calls == ["写一份测试文档"]


async def test_file_only_message_does_not_trigger_draft(graph, agent):
    """回归测试：飞书发文件不能带文字，只收到文件时不该沉默地当成"没收到东西"，
    但也不该立刻派发执行——要等指令。"""
    config = _config()
    file = IncomingFile(filename="a.docx", content=b"x", mime_type="application/octet-stream")
    state = await graph.ainvoke(
        {"platform": "test", "user_id": "u1", "new_text": None, "new_file": file}, config=config
    )
    assert "__interrupt__" not in state
    assert state.get("result") is None
    assert state["pending_file"] == file
    assert agent.calls == []

    # 后续补一句指令，应该带着之前存的文件一起进入确认环节
    state = await graph.ainvoke(
        {"platform": "test", "user_id": "u1", "new_text": "总结一下", "new_file": None}, config=config
    )
    assert "__interrupt__" in state


async def test_non_confirmation_reply_loops_back_to_draft_without_executing(graph, agent):
    config = _config()
    await graph.ainvoke(
        {"platform": "test", "user_id": "u1", "new_text": "帮我写份文档", "new_file": None}, config=config
    )
    state = await graph.ainvoke(Command(resume="不对，我想要别的"), config=config)
    assert "__interrupt__" in state  # 又生成了一次草稿，还在等确认
    assert agent.calls == []  # 没有被执行


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("是", True),
        ("是的", True),  # 2026-08-09 修的那个 bug：精确匹配漏掉了这个
        ("是的呀", True),
        ("好的呢", True),
        ("确认", True),
        ("Yes", True),
        ("ok", True),
        ("不是", False),
        ("不对，我想要别的", False),
        ("换个格式", False),
        ("", False),
    ],
)
def test_is_confirmation_prefix_matching(reply, expected):
    assert _is_confirmation(reply) is expected
