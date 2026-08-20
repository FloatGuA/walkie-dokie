from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from walkie_dokie.evals.cases import FinalExpect, GoldenCase, Turn, TurnExpect
from walkie_dokie.evals.driver import run_case
from walkie_dokie.evals.fake_execution import (
    FakeExecutionAgent,
    RecordingExecutionAgent,
)
from walkie_dokie.main_agent.base import (
    DialogueContext,
    FinalizeContext,
    MainAgent,
    MainAgentDecision,
    MemoryOperation,
    TaskContract,
)
from walkie_dokie.main_agent.memory import JsonMemoryRepository
from walkie_dokie.orchestrator import build_graph

FIXTURES = Path(__file__).resolve().parents[1] / "evals" / "fixtures"


class ScriptedMainAgent(MainAgent):
    """按预置队列出牌的 MainAgent，让 driver 测试不依赖真实 DeepSeek。"""

    def __init__(self, decisions):
        self._decisions = list(decisions)

    async def decide(self, context: DialogueContext) -> MainAgentDecision:
        return self._decisions.pop(0)

    async def finalize(self, context: FinalizeContext) -> str:
        return "任务完成，文件已发给你。"

    async def judge_confirmation(self, context):
        raise AssertionError("本测试不应触发确认判定")


def _graph(decisions, tmp_path):
    memory = JsonMemoryRepository(tmp_path / "memory")
    recorder = RecordingExecutionAgent(
        FakeExecutionAgent(output_fixture=FIXTURES / "fake_output.docx")
    )
    graph = build_graph(
        ScriptedMainAgent(decisions),
        recorder,
        memory,
        checkpointer=InMemorySaver(),
    )
    return graph, recorder, memory


async def test_reply_turn_passes_and_records_observation(tmp_path):
    graph, recorder, memory = _graph(
        [
            MainAgentDecision(
                intent="chat", action="reply", user_message="行距在段落设置里调。"
            )
        ],
        tmp_path,
    )
    case = GoldenCase(
        id="t-reply",
        category="intent_routing",
        description="方法咨询直接回复",
        turns=(Turn(user="Word里怎么调行距？", expect=TurnExpect(action="reply")),),
    )
    result = await run_case(
        case,
        graph=graph,
        recorder=recorder,
        memory_repository=memory,
        fixtures_dir=FIXTURES,
    )
    assert result.passed
    assert result.case_id == "t-reply"
    assert result.category == "intent_routing"
    assert result.aborted_at_turn is None
    assert result.turns[0].action == "reply"
    assert result.turns[0].intent is None
    assert result.turns[0].executed is False
    assert "行距" in result.turns[0].replies[0]


async def test_propose_then_confirm_executes(tmp_path):
    graph, recorder, memory = _graph(
        [
            MainAgentDecision(
                intent="document_task",
                action="propose_task",
                user_message="要把文档转成表格吗？回复「是」开始。",
                task=TaskContract(instruction="把输入文档转成表格"),
            )
        ],
        tmp_path,
    )
    case = GoldenCase(
        id="t-exec",
        category="intent_routing",
        description="确认后执行",
        turns=(
            Turn(
                user="转成表格",
                files=("simple.docx",),
                expect=TurnExpect(action="propose_task", intent="document_task"),
            ),
            Turn(user="是", expect=TurnExpect(executed=True)),
        ),
    )
    result = await run_case(
        case,
        graph=graph,
        recorder=recorder,
        memory_repository=memory,
        fixtures_dir=FIXTURES,
    )
    assert result.passed, result.failures
    assert result.turns[0].action == "propose_task"
    assert result.turns[0].intent == "document_task"
    assert result.turns[0].executed is False
    assert result.turns[1].executed is True
    assert recorder.calls[0]["input_filenames"] == ("simple.docx",)


async def test_turn_failure_aborts_remaining_turns(tmp_path):
    graph, recorder, memory = _graph(
        [
            MainAgentDecision(
                intent="chat", action="reply", user_message="直接回复了"
            )
        ],
        tmp_path,
    )
    case = GoldenCase(
        id="t-abort",
        category="intent_routing",
        description="第一轮就断言失败，第二轮不应驱动",
        turns=(
            Turn(user="转成表格", expect=TurnExpect(action="propose_task")),
            Turn(user="是", expect=TurnExpect(executed=True)),
        ),
    )
    result = await run_case(
        case,
        graph=graph,
        recorder=recorder,
        memory_repository=memory,
        fixtures_dir=FIXTURES,
    )
    assert not result.passed
    assert result.aborted_at_turn == 0
    assert len(result.turns) == 1  # 第二轮没驱动
    assert "turn[0]" in result.failures[0]


async def test_final_memory_check_reads_eval_platform_profile(tmp_path):
    """final 断言必须读到 driver 实际写入的 ("eval", case.id) 档案。"""
    graph, recorder, memory = _graph(
        [
            MainAgentDecision(
                intent="chat",
                action="reply",
                user_message="好的。",
                memory_operations=(
                    MemoryOperation(
                        action="set", field="name", value="浮瓜", evidence="我叫浮瓜"
                    ),
                ),
            )
        ],
        tmp_path,
    )
    case = GoldenCase(
        id="t-memory",
        category="memory",
        description="姓名写入长期档案",
        turns=(Turn(user="我叫浮瓜", expect=TurnExpect(action="reply")),),
        final=FinalExpect(memory_must_contain={"name": "浮瓜"}),
    )
    result = await run_case(
        case,
        graph=graph,
        recorder=recorder,
        memory_repository=memory,
        fixtures_dir=FIXTURES,
    )
    assert result.passed, result.failures
    assert memory.load("eval", "t-memory") == {"name": "浮瓜"}
