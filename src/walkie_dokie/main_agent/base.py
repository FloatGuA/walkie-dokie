from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from walkie_dokie.agents.base import ExecutionReport

MemoryField = Literal["name", "department", "job_title", "preferred_address"]
MemoryAction = Literal["set", "delete"]
DecisionAction = Literal["reply", "propose_task"]
DialogueIntent = Literal["chat", "document_task"]


@dataclass(frozen=True)
class MemoryOperation:
    """主 Agent 提出的长期记忆操作。

    这里只表达候选操作；MemoryRepository 还会做字段白名单、当前用户原文 evidence
    和取值校验。删除操作的 value 必须为 None，避免用空字符串暗示删除。
    """

    action: MemoryAction
    field: MemoryField
    value: str | None = None
    # 必须逐字来自当前用户消息。Repository 会再次验证 evidence 与字段语义，
    # 避免模型仅凭“你是小帮”之类的话把助手身份写成用户姓名。
    evidence: str | None = None


@dataclass(frozen=True)
class TaskContract:
    """主 Agent 交给执行 Agent 的最小任务契约。"""

    instruction: str
    missing_info: tuple[str, ...] = ()
    # 只有主 Agent 能根据对话语义选择上一轮产物；控制平面不自行猜测“继续”。
    use_previous_artifact: bool = False
    # 主 Agent 判定的任务难度（simple/standard/complex），执行后端据此选模型。
    # 判定是模型的事，映射到具体模型是后端代码的事（models judge, code decides）。
    difficulty: str = "standard"


@dataclass(frozen=True)
class DialogueContext:
    user_text: str
    input_filenames: tuple[str, ...]
    known_facts: dict[str, str]
    recent_messages: tuple[dict[str, str], ...] = ()
    active_artifact_filenames: tuple[str, ...] = ()
    # 更早对话压缩沉淀的事实清单（只有 fact，不含 evidence）；纯背景参考，
    # 不能当作当前指令，也不能反过来当作长期记忆 evidence 的来源。
    conversation_summary: tuple[str, ...] = ()
    # user_text 可以是确认前多条消息累积出的任务上下文；长期记忆证据只能来自
    # 最后一条真实用户文本，不能从旧消息或助手话术中重新抽取。
    current_user_text: str | None = None
    # 纯埋点身份：只用于成本记账，不进 prompt、不参与任何判断。默认 None，
    # 让不关心成本的调用方（测试、eval）无需改造。
    platform: str | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class MainAgentDecision:
    intent: DialogueIntent
    action: DecisionAction
    user_message: str
    task: TaskContract | None = None
    memory_operations: tuple[MemoryOperation, ...] = ()


@dataclass(frozen=True)
class FinalizeContext:
    task: TaskContract
    report: ExecutionReport
    # 见 DialogueContext：纯成本埋点身份，不进 prompt。
    platform: str | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class ConfirmationContext:
    """判定用户对已提案任务的回复属于哪一类所需的全部上下文。"""

    task_instruction: str
    proposal_message: str
    user_reply: str
    # 见 DialogueContext：纯成本埋点身份，不进 prompt。
    platform: str | None = None
    user_id: str | None = None


ConfirmationDecision = Literal["confirm", "revise", "cancel"]


@dataclass(frozen=True)
class ConfirmationVerdict:
    decision: ConfirmationDecision
    # reason 只进日志，不面向用户，也不参与任何控制流分支。
    reason: str


class MainAgent(ABC):
    """唯一面向用户语义的 Agent。

    ``decide`` 负责理解当前用户回合；``finalize`` 把纯内部执行报告转换成最终
    用户回复。实现不得拥有 shell/文件系统工具，避免把对话判断交给 coding
    agent harness。
    """

    @abstractmethod
    async def decide(self, context: DialogueContext) -> MainAgentDecision: ...

    @abstractmethod
    async def finalize(self, context: FinalizeContext) -> str: ...

    @abstractmethod
    async def judge_confirmation(
        self, context: ConfirmationContext
    ) -> ConfirmationVerdict: ...


def decision_to_dict(decision: MainAgentDecision) -> dict:
    task = decision.task
    return {
        "intent": decision.intent,
        "action": decision.action,
        "user_message": decision.user_message,
        "task": (
            {
                "instruction": task.instruction,
                "missing_info": list(task.missing_info),
                "use_previous_artifact": task.use_previous_artifact,
                "difficulty": task.difficulty,
            }
            if task
            else None
        ),
        "memory_operations": [
            {
                "action": op.action,
                "field": op.field,
                "value": op.value,
                "evidence": op.evidence,
            }
            for op in decision.memory_operations
        ],
    }


def task_from_dict(value: dict) -> TaskContract:
    use_previous = value.get("use_previous_artifact", False)
    if not isinstance(use_previous, bool):
        raise RuntimeError("task.use_previous_artifact 必须是 boolean")
    return TaskContract(
        instruction=value["instruction"],
        missing_info=tuple(value.get("missing_info", ())),
        use_previous_artifact=use_previous,
        # 旧 checkpoint 里的 task dict 没有这个键，缺省中档。
        difficulty=value.get("difficulty", "standard"),
    )
