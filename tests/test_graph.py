import itertools
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from docx import Document

import walkie_dokie.artifacts as artifact_store
import walkie_dokie.orchestrator.graph as graph_module
from walkie_dokie.agents.base import ExecutionAgent, ExecutionArtifact, ExecutionReport
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
from walkie_dokie.orchestrator.graph import _is_confirmation, _is_negation
from walkie_dokie.platforms.base import IncomingFile


def task_decision(
    instruction="写一份测试文档",
    *,
    user_message="我理解为写一份测试文档，回复“是”确认。",
    missing_info=(),
    memory_operations=(),
    use_previous_artifact=False,
):
    return MainAgentDecision(
        intent="document_task",
        action="propose_task",
        user_message=user_message,
        task=TaskContract(
            instruction,
            tuple(missing_info),
            use_previous_artifact=use_previous_artifact,
        ),
        memory_operations=tuple(memory_operations),
    )


def reply_decision(message="你好，需要我帮你处理什么文档？", *, memory_operations=()):
    return MainAgentDecision(
        intent="chat",
        action="reply",
        user_message=message,
        memory_operations=tuple(memory_operations),
    )


class FakeMainAgent(MainAgent):
    def __init__(
        self,
        decisions=None,
        final_message="文档已经处理好了。",
        finalize_error: Exception | None = None,
    ):
        self.decisions = list(decisions or [task_decision()])
        self.final_message = final_message
        self.finalize_error = finalize_error
        self.decide_calls: list[DialogueContext] = []
        self.finalize_calls: list[FinalizeContext] = []

    async def decide(self, context: DialogueContext) -> MainAgentDecision:
        self.decide_calls.append(context)
        if not self.decisions:
            raise AssertionError("FakeMainAgent 没有可返回的 decision")
        decision = self.decisions.pop(0)
        if isinstance(decision, Exception):
            raise decision
        return decision

    async def finalize(self, context: FinalizeContext) -> str:
        self.finalize_calls.append(context)
        if self.finalize_error:
            raise self.finalize_error
        return self.final_message

    async def judge_confirmation(self, context):
        raise AssertionError("本测试不应触发确认判定")


class FakeExecutionAgent(ExecutionAgent):
    def __init__(
        self,
        *,
        produce_artifact=False,
        artifact_names: tuple[str, ...] | None = None,
        error: Exception | None = None,
    ):
        self.calls = []
        self.produce_artifact = produce_artifact
        # artifact_names 允许测试要求生成多份产物；不传时沿用 produce_artifact
        # 单文件语义，向后兼容既有测试。
        self.artifact_names = artifact_names
        self.error = error

    async def run(self, instruction, input_paths, input_filenames, workdir):
        self.calls.append(
            {
                "instruction": instruction,
                "input_paths": input_paths,
                "input_filenames": input_filenames,
                "workdir": workdir,
            }
        )
        if self.error:
            raise self.error
        names = self.artifact_names or (("result.docx",) if self.produce_artifact else ())
        artifacts = []
        for name in names:
            path = workdir / name
            document = Document()
            document.add_paragraph("generated")
            document.save(path)
            artifacts.append(ExecutionArtifact(path, name))
        return ExecutionReport(summary="测试文档已生成", artifacts=tuple(artifacts))


@pytest.fixture(autouse=True)
def _isolated_io(monkeypatch, tmp_path):
    workspace_root = tmp_path / "workspaces"
    input_root = tmp_path / "inputs"
    workspace_root.mkdir()
    input_root.mkdir()
    monkeypatch.setattr(graph_module, "WORKSPACES_ROOT", workspace_root)
    monkeypatch.setattr(
        graph_module, "EXECUTION_METADATA_ROOT", tmp_path / "execution-metadata"
    )
    monkeypatch.setattr(artifact_store, "WORKSPACES_ROOT", workspace_root)
    monkeypatch.setattr(artifact_store, "INPUT_ARTIFACTS_ROOT", input_root)

    sequence = itertools.count()

    def _create_workspace(platform, user_id):
        workdir = workspace_root / f"run-{next(sequence)}"
        workdir.mkdir()
        return workdir

    monkeypatch.setattr(graph_module, "create_workspace_dir", _create_workspace)

    async def _fake_log_turn(record):
        return None

    monkeypatch.setattr(graph_module, "log_turn", _fake_log_turn)


@pytest.fixture
def execution_agent():
    return FakeExecutionAgent()


def make_graph(tmp_path, main_agent, execution_agent):
    memory = JsonMemoryRepository(tmp_path / "memory")
    graph = build_graph(
        main_agent,
        execution_agent,
        memory,
        checkpointer=InMemorySaver(),
    )
    return graph, memory


def config(user_id="u1"):
    return {"configurable": {"thread_id": f"test:{user_id}"}}


def input_reference(filename="input.docx", content=b"input"):
    return artifact_store.store_incoming_file(
        "test",
        "u1",
        IncomingFile(filename, content, "application/octet-stream"),
    )


async def test_task_reaches_confirm_then_execution_and_main_agent_finalizes(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent()
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)

    proposal = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "帮我写份文档",
            "new_file": None,
        },
        config=config(),
    )
    assert proposal["__interrupt__"][0].value == {
        "user_message": "我理解为写一份测试文档，回复“是”确认。",
        "task": {
            "instruction": "写一份测试文档",
            "missing_info": [],
            "use_previous_artifact": False,
        },
    }

    state = await graph.ainvoke(Command(resume="是"), config=config())
    assert state["result"]["reply_text"] == "文档已经处理好了。"
    assert state["result"]["success"] is True
    assert state["pending_instruction"] is None
    assert execution_agent.calls[0]["instruction"] == "写一份测试文档"
    assert main_agent.finalize_calls[0].report.summary == "测试文档已生成"


async def test_direct_reply_never_calls_execution_agent(tmp_path, execution_agent):
    main_agent = FakeMainAgent([reply_decision()])
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)

    state = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "你好呀",
            "new_file": None,
        },
        config=config(),
    )
    assert "__interrupt__" not in state
    assert state["result"]["reply_text"] == "你好，需要我帮你处理什么文档？"
    assert execution_agent.calls == []


async def test_memory_is_owned_by_main_agent_and_applies_on_direct_conversation(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent(
        [
            reply_decision(
                "很高兴认识你。",
                memory_operations=(
                    MemoryOperation("set", "name", "张三", "我叫张三"),
                ),
            )
        ]
    )
    graph, memory = make_graph(tmp_path, main_agent, execution_agent)

    state = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "我叫张三",
            "new_file": None,
        },
        config=config(),
    )
    assert memory.load("test", "u1") == {"name": "张三"}
    assert "姓名：张三" in state["result"]["reply_text"]
    assert state["memory_changes"][0]["field"] == "name"
    assert execution_agent.calls == []


async def test_implicit_memory_save_replaces_model_claim_with_verified_notice(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent(
        [
            reply_decision(
                "好的，浮瓜，我已经记住了。",
                memory_operations=(
                    MemoryOperation("set", "name", "浮瓜", "我是浮瓜"),
                ),
            )
        ]
    )
    graph, memory = make_graph(tmp_path, main_agent, execution_agent)

    state = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "我是浮瓜",
            "new_file": None,
        },
        config=config(),
    )

    message = state["result"]["reply_text"]
    assert "我已经记住" not in message
    assert "我记住了" in message
    assert "姓名：浮瓜" in message
    assert memory.load("test", "u1") == {"name": "浮瓜"}


async def test_rejected_memory_candidate_cannot_leave_false_saved_reply(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent(
        [
            reply_decision(
                "好的，我已经记住你叫小帮。",
                memory_operations=(
                    MemoryOperation("set", "name", "小帮", "你是小帮"),
                ),
            )
        ]
    )
    graph, memory = make_graph(tmp_path, main_agent, execution_agent)

    state = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "你是小帮",
            "new_file": None,
        },
        config=config(),
    )

    assert state["result"]["reply_text"] == "谢谢你告诉我。"
    assert memory.load("test", "u1") == {}


async def test_saved_claim_without_memory_operation_is_also_removed(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent([reply_decision("好的，我记住了。")])
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)
    state = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "我是浮瓜",
            "new_file": None,
        },
        config=config(),
    )
    assert state["result"]["reply_text"] == "谢谢你告诉我。"


async def test_implicit_memory_write_failure_never_claims_success(
    tmp_path, execution_agent, monkeypatch
):
    main_agent = FakeMainAgent(
        [
            reply_decision(
                "好的，我已经记住了。",
                memory_operations=(
                    MemoryOperation("set", "name", "浮瓜", "我是浮瓜"),
                ),
            )
        ]
    )
    graph, memory = make_graph(tmp_path, main_agent, execution_agent)

    def fail_apply(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(memory, "apply", fail_apply)
    state = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "我是浮瓜",
            "new_file": None,
        },
        config=config(),
    )

    assert "没有保存成功" in state["result"]["reply_text"]
    assert "我已经记住" not in state["result"]["reply_text"]
    assert memory.load("test", "u1") == {}


async def test_task_confirmation_is_separate_from_implicit_memory_save(tmp_path):
    operation = MemoryOperation("set", "name", "张三", "我叫张三")
    main_agent = FakeMainAgent(
        [task_decision("为张三写文档", memory_operations=(operation,))]
    )
    execution_agent = FakeExecutionAgent()
    graph, memory = make_graph(tmp_path, main_agent, execution_agent)
    proposal = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "我叫张三，帮我写文档",
            "new_file": None,
        },
        config=config(),
    )
    proposal_message = proposal["__interrupt__"][0].value["user_message"]
    assert "是并记住" not in proposal_message
    assert "姓名：张三" in proposal_message
    assert memory.load("test", "u1") == {"name": "张三"}
    state = await graph.ainvoke(Command(resume="是"), config=config())
    assert state["result"]["success"] is True
    assert len(execution_agent.calls) == 1


async def test_long_term_memory_command_lists_all_facts_without_main_agent(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent([reply_decision("不应调用模型")])
    graph, memory = make_graph(tmp_path, main_agent, execution_agent)
    memory.apply(
        "test",
        "u1",
        (
            MemoryOperation("set", "name", "浮瓜", "我是浮瓜"),
            MemoryOperation("set", "job_title", "开发者", "我是开发者"),
        ),
        source_text="我是浮瓜，我是开发者",
    )

    state = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "  /long-term-memory  ",
            "new_file": None,
        },
        config=config(),
    )
    assert state["result"]["reply_text"] == (
        "当前保存的长期记忆：\n姓名：浮瓜\n职位：开发者"
    )
    assert main_agent.decide_calls == []


async def test_long_term_memory_command_reports_empty_profile(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent([reply_decision("不应调用模型")])
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)
    state = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "/long-term-memory",
            "new_file": None,
        },
        config=config(),
    )
    assert state["result"]["reply_text"] == "目前还没有保存任何长期记忆。"
    assert main_agent.decide_calls == []


async def test_known_memory_goes_to_main_agent_not_directly_to_executor(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent([task_decision("为张三写请假条")])
    graph, memory = make_graph(tmp_path, main_agent, execution_agent)
    memory.apply(
        "test",
        "u1",
        (MemoryOperation("set", "name", "张三", "我叫张三"),),
        source_text="我叫张三",
    )

    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "帮我写请假条",
            "new_file": None,
        },
        config=config(),
    )
    await graph.ainvoke(Command(resume="是"), config=config())

    assert main_agent.decide_calls[0].known_facts == {"name": "张三"}
    assert execution_agent.calls[0]["instruction"] == "为张三写请假条"


async def test_file_only_new_turn_clears_previous_result(tmp_path, execution_agent):
    main_agent = FakeMainAgent([reply_decision("第一轮回复")])
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "你好",
            "new_file": None,
        },
        config=config(),
    )

    reference = input_reference("a.docx", b"x")
    state = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": None,
            "new_file": reference,
        },
        config=config(),
    )
    assert state.get("result") is None
    assert state["pending_files"] == (reference,)
    assert "content" not in state["pending_files"][0]


async def test_memory_changes_do_not_leak_into_next_turn(tmp_path, execution_agent):
    main_agent = FakeMainAgent(
        [
            reply_decision(
                "记好了。",
                memory_operations=(
                    MemoryOperation("set", "name", "张三", "我叫张三"),
                ),
            ),
            reply_decision("第二轮回复"),
        ]
    )
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)
    first = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "我叫张三",
            "new_file": None,
        },
        config=config(),
    )
    assert first["memory_changes"]

    second = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "你好",
            "new_file": None,
        },
        config=config(),
    )
    assert second["memory_changes"] is None
    assert second["result"]["reply_text"] == "第二轮回复"


async def test_completed_dialogue_history_flows_to_next_main_agent_turn(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent(
        [reply_decision("我是小帮。"), reply_decision("我们刚才在互相介绍。")]
    )
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "你是谁？",
            "new_file": None,
        },
        config=config(),
    )
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "我们刚才说到哪了？",
            "new_file": None,
        },
        config=config(),
    )
    assert main_agent.decide_calls[1].recent_messages == (
        {"role": "user", "content": "你是谁？"},
        {"role": "assistant", "content": "我是小帮。"},
    )


async def test_non_confirmation_is_reconsidered_by_main_agent(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent(
        [task_decision(), reply_decision("好的，这次先不做。")]
    )
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "帮我写文档",
            "new_file": None,
        },
        config=config(),
    )
    state = await graph.ainvoke(Command(resume="不对，先不做"), config=config())
    assert state["result"]["reply_text"] == "好的，这次先不做。"
    assert main_agent.decide_calls[1].user_text == "帮我写文档\n不对，先不做"
    assert main_agent.decide_calls[1].current_user_text == "不对，先不做"
    assert execution_agent.calls == []


async def test_attachment_during_confirmation_is_not_dropped(tmp_path, execution_agent):
    main_agent = FakeMainAgent([task_decision(), task_decision("总结新文件")])
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "帮我总结",
            "new_file": None,
        },
        config=config(),
    )

    reference = input_reference("new.docx", b"x")
    state = await graph.ainvoke(
        Command(resume={"text": "", "file": reference}), config=config()
    )
    assert "__interrupt__" in state
    assert main_agent.decide_calls[1].input_filenames == ("new.docx",)
    assert execution_agent.calls == []


async def test_confirm_resume_race_merges_full_files_batch_with_dedup(
    tmp_path, execution_agent
):
    """回归 Finding 1（graph 侧）：`_invoke_from_event` 的竞态恢复分支用复数
    ``files`` 键 resume（防抖窗口攒下的一整批，可能不止一个）；``_ask_confirm``
    必须把整批都合并进 `pending_files`，碰撞文件名按 `_merge_pending_files`
    的规则去重，而不是像单数 ``file`` 键那样只能带一个文件、其余静默丢失。"""
    main_agent = FakeMainAgent([task_decision(), task_decision("总结两份新文件")])
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "帮我总结",
            "new_file": None,
        },
        config=config(),
    )

    ref_1 = input_reference("报价单.xlsx", b"1")
    ref_2 = input_reference("报价单.xlsx", b"2")
    state = await graph.ainvoke(
        Command(resume={"text": "", "files": (ref_1, ref_2)}), config=config()
    )
    assert "__interrupt__" in state
    assert main_agent.decide_calls[1].input_filenames == (
        "报价单.xlsx",
        "报价单-2.xlsx",
    )
    assert execution_agent.calls == []


async def test_previous_output_can_be_selected_by_next_task(tmp_path):
    main_agent = FakeMainAgent(
        [
            task_decision("生成初稿"),
            task_decision("修改上一份初稿", use_previous_artifact=True),
        ]
    )
    execution_agent = FakeExecutionAgent(produce_artifact=True)
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)

    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "生成初稿",
            "new_file": None,
        },
        config=config(),
    )
    first = await graph.ainvoke(Command(resume="是"), config=config())
    previous_path = Path(first["active_artifacts"][0]["path"])

    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "继续修改刚才的文件",
            "new_file": None,
        },
        config=config(),
    )
    assert main_agent.decide_calls[1].active_artifact_filenames == ("result.docx",)
    await graph.ainvoke(Command(resume="是"), config=config())
    assert execution_agent.calls[1]["input_paths"] == (previous_path.resolve(),)


async def test_successful_read_only_task_makes_its_input_the_active_artifact(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent([task_decision("总结输入文件")])
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)
    reference = input_reference("new.docx", b"new")
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "总结这个文件",
            "new_file": reference,
        },
        config=config(),
    )
    state = await graph.ainvoke(Command(resume="是"), config=config())
    assert state["active_artifacts"] == (reference,)


async def test_failed_execution_never_publishes_or_activates_partial_artifact(
    tmp_path, monkeypatch
):
    main_agent = FakeMainAgent()
    execution_agent = FakeExecutionAgent(produce_artifact=True)
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)

    def _marker_failure(workdir, report):
        raise OSError("metadata disk full")

    monkeypatch.setattr(graph_module, "_write_execution_marker", _marker_failure)
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "写文档",
            "new_file": None,
        },
        config=config(),
    )
    state = await graph.ainvoke(Command(resume="是"), config=config())
    assert state["result"]["success"] is False
    assert state["result"]["artifacts"] == []
    assert state.get("active_artifacts") in (None, ())


async def test_execution_agent_cannot_publish_artifact_from_sibling_workdir(tmp_path):
    class CrossWorkspaceAgent(ExecutionAgent):
        async def run(self, instruction, input_paths, input_filenames, workdir):
            victim_dir = workdir.parent / "victim-run"
            victim_dir.mkdir()
            victim = victim_dir / "result.docx"
            victim.write_bytes(b"another user's file")
            return ExecutionReport("done", (ExecutionArtifact(victim, victim.name),))

    main_agent = FakeMainAgent()
    graph, _ = make_graph(tmp_path, main_agent, CrossWorkspaceAgent())
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "写文档",
            "new_file": None,
        },
        config=config(),
    )
    state = await graph.ainvoke(Command(resume="是"), config=config())
    assert state["result"]["success"] is False
    assert state["result"]["artifacts"] == []
    assert state.get("active_artifacts") in (None, ())


async def test_main_agent_failure_finishes_turn_instead_of_leaving_pending_task(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent([RuntimeError("boom")])
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)
    state = await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "你好",
            "new_file": None,
        },
        config=config(),
    )
    assert state["result"]["reply_text"].startswith("我这次没能理解")
    snapshot = await graph.aget_state(config())
    assert snapshot.next == ()
    assert snapshot.interrupts == ()


async def test_execution_failure_finishes_turn_without_implicit_retry(tmp_path):
    main_agent = FakeMainAgent()
    execution_agent = FakeExecutionAgent(error=RuntimeError("backend failed"))
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "写文档",
            "new_file": None,
        },
        config=config(),
    )
    state = await graph.ainvoke(Command(resume="是"), config=config())
    assert state["result"]["success"] is False
    assert len(execution_agent.calls) == 1
    snapshot = await graph.aget_state(config())
    assert snapshot.next == ()


async def test_turn_log_failure_cannot_turn_success_into_pending_execute(
    tmp_path, monkeypatch
):
    main_agent = FakeMainAgent()
    execution_agent = FakeExecutionAgent()
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)

    async def _broken_log(record):
        raise OSError("log disk full")

    monkeypatch.setattr(graph_module, "log_turn", _broken_log)
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "写文档",
            "new_file": None,
        },
        config=config(),
    )
    state = await graph.ainvoke(Command(resume="是"), config=config())
    assert state["result"]["success"] is True
    assert len(execution_agent.calls) == 1
    assert (await graph.aget_state(config())).next == ()


async def test_old_accumulated_text_cannot_be_reused_as_new_memory_evidence(
    tmp_path, execution_agent
):
    operation = MemoryOperation("set", "name", "旧名", "我叫旧名")
    main_agent = FakeMainAgent(
        [task_decision(), reply_decision("好的。", memory_operations=(operation,))]
    )
    graph, memory = make_graph(tmp_path, main_agent, execution_agent)
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "我叫旧名，帮我写文档",
            "new_file": None,
        },
        config=config(),
    )
    await graph.ainvoke(Command(resume="先不做"), config=config())
    assert memory.load("test", "u1") == {}


async def test_multiple_files_in_one_window_are_merged_into_pending_files(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent()
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)

    file_a = input_reference("a.docx", b"a")
    file_b = input_reference("b.docx", b"b")
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "合并这两份",
            "new_files": (file_a, file_b),
        },
        config=config(),
    )
    # main_agent 收到了两个文件名
    assert main_agent.decide_calls[0].input_filenames == ("a.docx", "b.docx")


async def test_filename_collision_in_same_window_gets_display_filename_suffix(
    tmp_path, execution_agent
):
    main_agent = FakeMainAgent()
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)

    file_1 = input_reference("报价单.xlsx", b"1")
    file_2 = input_reference("报价单.xlsx", b"2")
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "都看一下",
            "new_files": (file_1, file_2),
        },
        config=config(),
    )
    assert main_agent.decide_calls[0].input_filenames == ("报价单.xlsx", "报价单-2.xlsx")


async def test_deduped_display_names_reach_execution_backend_on_collision(
    tmp_path, execution_agent
):
    """回归 Finding 4：碰撞去重后的 display_filename 不仅要让主 Agent 看到，还要
    实际落到执行后端收到的 input_filenames 上，而不是仅停在 DialogueContext。"""
    main_agent = FakeMainAgent()
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)

    file_1 = input_reference("报价单.xlsx", b"1")
    file_2 = input_reference("报价单.xlsx", b"2")
    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "都看一下",
            "new_files": (file_1, file_2),
        },
        config=config(),
    )
    await graph.ainvoke(Command(resume="是"), config=config())

    call = execution_agent.calls[0]
    assert len(set(call["input_paths"])) == 2
    assert call["input_filenames"] == ("报价单.xlsx", "报价单-2.xlsx")


async def test_execute_produces_multiple_artifacts_in_result(tmp_path):
    main_agent = FakeMainAgent()
    execution_agent = FakeExecutionAgent(artifact_names=("out1.docx", "out2.docx"))
    graph, _ = make_graph(tmp_path, main_agent, execution_agent)

    await graph.ainvoke(
        {
            "platform": "test",
            "user_id": "u1",
            "new_text": "生成两份文档",
            "new_files": (),
        },
        config=config(),
    )
    state = await graph.ainvoke(
        Command(resume={"text": "是", "file": None}), config=config()
    )
    assert [item["filename"] for item in state["result"]["artifacts"]] == [
        "out1.docx",
        "out2.docx",
    ]


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("是", True),
        ("是的", True),
        ("确认", True),
        ("没错", True),
        ("Yes", True),
        ("y", True),
        ("是。", True),
        # 语气歧义词移出白名单，进灰区交模型（spec 决策 3）
        ("嗯", False),
        ("好的", False),
        ("好的呢", False),
        ("行", False),
        ("可以", False),
        ("ok", False),
        ("对", False),
        # 原有反例保持
        ("不是", False),
        ("好像不对", False),
        ("可以先别做", False),
        ("是，不过先改一下", False),
        ("换个格式", False),
        ("", False),
    ],
)
def test_confirmation_requires_unambiguous_whole_reply(reply, expected):
    assert _is_confirmation(reply) is expected


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("好像不对", True),
        ("可以先别做", True),
        ("是，不过先改一下", True),
        ("算了", True),
        ("等等", True),
        ("先不用了", True),
        ("暂时不弄", True),
        ("取消吧", True),
        ("换个格式", True),
        ("no", True),
        # 宁宽勿漏的已知误伤（安全方向：只是多澄清一轮）
        ("不错，就这样", True),
        # 不含否定信号的灰区词不在这层拦
        ("嗯", False),
        ("好的", False),
        ("应该行吧", False),
        ("", False),
    ],
)
def test_negation_words_are_hard_vetoed(reply, expected):
    assert _is_negation(reply) is expected
