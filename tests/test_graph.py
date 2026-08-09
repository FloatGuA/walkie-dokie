import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from walkie_dokie.agents.base import ExecutionAgent, ExecutionResult
from walkie_dokie.orchestrator import build_graph
from walkie_dokie.orchestrator.graph import _is_confirmation
from walkie_dokie.platforms.base import IncomingFile

FAKE_DRAFT = {
    "is_task": True,
    "task_summary": "写一份测试文档",
    "missing_info": [],
    "user_message": "你的意思是不是要写一份测试文档？回复'是'确认",
}
FAKE_CHITCHAT_DRAFT = {
    "is_task": False,
    "task_summary": "",
    "missing_info": [],
    "user_message": "你好，需要我帮你处理什么文档？",
}


class FakeAgent(ExecutionAgent):
    def __init__(self):
        self.calls: list[str] = []

    async def run(self, instruction, input_file, workdir, input_filename=None):
        self.calls.append(instruction)
        return ExecutionResult(reply_text=f"done: {instruction}", result_file=None, result_filename=None)


@pytest.fixture(autouse=True)
def _no_real_io(monkeypatch, tmp_path):
    """graph.py 会碰真实文件系统（工作目录、用户 memory）和真实外部 API
    （日志留痕、DeepSeek 事实提取），测试里都换成不落地/不联网的假实现——
    不能依赖"测试环境恰好没配 DEEPSEEK_API_KEY"这种隐式安全网。"""
    monkeypatch.setattr(
        "walkie_dokie.orchestrator.graph.create_workspace_dir", lambda platform, user_id: tmp_path
    )

    async def _fake_log_turn(record):
        pass

    monkeypatch.setattr("walkie_dokie.orchestrator.graph.log_turn", _fake_log_turn)
    monkeypatch.setattr("walkie_dokie.orchestrator.graph.memory.load_facts", lambda platform, user_id: {})
    monkeypatch.setattr("walkie_dokie.orchestrator.graph.memory.save_facts", lambda platform, user_id, facts: None)

    async def _fake_extract_facts(text):
        return {}

    monkeypatch.setattr("walkie_dokie.orchestrator.graph.memory.extract_facts", _fake_extract_facts)


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
def graph(agent, monkeypatch):
    async def _fake_draft(text, input_filename=None, known_facts=None):
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


async def test_newly_extracted_facts_surface_in_final_state(graph, agent, monkeypatch):
    """被动记忆不能悄悄发生——execute 提取到新事实时，必须体现在返回给
    调用方（run_mvp.py 用来回显给用户）的 state 里，不能只是存了文件不吭声。"""
    extracted = {"姓名": "李四"}

    async def _fake_extract(text):
        return extracted

    monkeypatch.setattr("walkie_dokie.orchestrator.graph.memory.extract_facts", _fake_extract)
    saved = {}
    monkeypatch.setattr(
        "walkie_dokie.orchestrator.graph.memory.save_facts",
        lambda platform, user_id, facts: saved.update(facts),
    )

    config = _config()
    await graph.ainvoke(
        {"platform": "test", "user_id": "u1", "new_text": "我叫李四，帮我写份文档", "new_file": None},
        config=config,
    )
    state = await graph.ainvoke(Command(resume="是"), config=config)
    assert state["new_facts"] == extracted
    assert saved == extracted


async def test_no_new_facts_leaves_new_facts_none(graph, agent):
    """没提取到新东西时 new_facts 该是 None，不是空字典——调用方靠这个判断
    要不要发"顺便记住了"这条消息。"""
    config = _config()
    await graph.ainvoke(
        {"platform": "test", "user_id": "u1", "new_text": "帮我写份文档", "new_file": None}, config=config
    )
    state = await graph.ainvoke(Command(resume="是"), config=config)
    assert state["new_facts"] is None


async def test_known_facts_flow_into_draft_and_execute(graph, agent, monkeypatch):
    """已存的用户 memory（姓名/部门这类）要传给 draft 生成，也要拼进最终喂给
    执行 agent 的 task_prompt——不能只是存了没用上。"""
    known = {"姓名": "张三", "部门": "人事部"}
    monkeypatch.setattr("walkie_dokie.orchestrator.graph.memory.load_facts", lambda platform, user_id: known)

    draft_calls = []

    async def _capturing_draft(text, input_filename=None, known_facts=None):
        draft_calls.append(known_facts)
        return FAKE_DRAFT

    monkeypatch.setattr("walkie_dokie.orchestrator.graph.generate_draft_task_prompt", _capturing_draft)

    config = _config()
    await graph.ainvoke(
        {"platform": "test", "user_id": "u1", "new_text": "帮我写份文档", "new_file": None}, config=config
    )
    assert draft_calls == [known]

    await graph.ainvoke(Command(resume="是"), config=config)
    assert "张三" in agent.calls[0]
    assert "人事部" in agent.calls[0]


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


async def test_chitchat_replies_directly_without_confirm_loop(graph, agent, monkeypatch):
    """回归测试：闲聊/寒暄（is_task=false）不该被拖进 confirm 循环，得直接回话
    并清空 pending_instruction——这是 2026-08-10 修的那个死循环的出口。"""

    async def _fake_draft(text, input_filename=None, known_facts=None):
        return FAKE_CHITCHAT_DRAFT

    monkeypatch.setattr("walkie_dokie.orchestrator.graph.generate_draft_task_prompt", _fake_draft)

    config = _config()
    state = await graph.ainvoke(
        {"platform": "test", "user_id": "u1", "new_text": "你好呀", "new_file": None}, config=config
    )
    assert "__interrupt__" not in state
    assert state["result"]["reply_text"] == FAKE_CHITCHAT_DRAFT["user_message"]
    assert state["result"]["result_file"] is None
    assert state["pending_instruction"] is None  # 清空了，不会跟下一轮消息拼在一起
    assert agent.calls == []  # 没有被当成任务执行


async def test_stuck_confirm_loop_exits_once_reclassified_as_chitchat(graph, agent, monkeypatch):
    """模拟真实撞见的场景：用户在 ask_confirm 里回了不相关的话，累积文字被
    draft 重新判成非任务后，应该跳出循环直接回话，而不是无限攒文字重新问。"""
    calls = []

    async def _fake_draft(text, input_filename=None, known_facts=None):
        calls.append(text)
        if len(calls) == 1:
            return FAKE_DRAFT  # 第一次：正常任务，进 confirm
        return FAKE_CHITCHAT_DRAFT  # 第二次：累积文字被重新判成闲聊

    monkeypatch.setattr("walkie_dokie.orchestrator.graph.generate_draft_task_prompt", _fake_draft)

    config = _config()
    await graph.ainvoke(
        {"platform": "test", "user_id": "u1", "new_text": "帮我写份文档", "new_file": None}, config=config
    )
    state = await graph.ainvoke(Command(resume="我是谁？你是谁？"), config=config)
    assert "__interrupt__" not in state  # 跳出了循环，不是又生成一次草稿等确认
    assert state["result"]["reply_text"] == FAKE_CHITCHAT_DRAFT["user_message"]
    assert state["pending_instruction"] is None
    assert agent.calls == []


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
