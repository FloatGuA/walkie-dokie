from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from walkie_dokie.agents.base import ExecutionReport

MemoryField = Literal["name", "department", "job_title", "preferred_address"]
MemoryAction = Literal["set", "delete"]
DecisionAction = Literal["reply", "propose_task"]


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


@dataclass(frozen=True)
class DialogueContext:
    user_text: str
    input_filename: str | None
    known_facts: dict[str, str]
    recent_messages: tuple[dict[str, str], ...] = ()
    active_artifact_filename: str | None = None
    # user_text 可以是确认前多条消息累积出的任务上下文；长期记忆证据只能来自
    # 最后一条真实用户文本，不能从旧消息或助手话术中重新抽取。
    current_user_text: str | None = None


@dataclass(frozen=True)
class MainAgentDecision:
    action: DecisionAction
    user_message: str
    task: TaskContract | None = None
    memory_operations: tuple[MemoryOperation, ...] = ()


@dataclass(frozen=True)
class FinalizeContext:
    task: TaskContract
    report: ExecutionReport


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


def decision_to_dict(decision: MainAgentDecision) -> dict:
    task = decision.task
    return {
        "action": decision.action,
        "user_message": decision.user_message,
        "task": (
            {
                "instruction": task.instruction,
                "missing_info": list(task.missing_info),
                "use_previous_artifact": task.use_previous_artifact,
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
    )
