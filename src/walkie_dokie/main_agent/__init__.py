"""面向用户的主 Agent 层。

主 Agent 负责理解对话、生成任务契约、提出长期记忆变更，并把执行报告组织成
用户能看懂的回复。它不运行代码；Claude Code/Codex 之类的 coding agent 只在
``walkie_dokie.agents`` 中作为执行后端存在。
"""

from walkie_dokie.main_agent.base import (
    DialogueContext,
    FinalizeContext,
    MainAgent,
    MainAgentDecision,
    MemoryOperation,
    TaskContract,
)
from walkie_dokie.main_agent.deepseek import DeepSeekMainAgent
from walkie_dokie.main_agent.memory import (
    JsonMemoryRepository,
    MemoryRepository,
    render_memory_proposal,
)

__all__ = [
    "DeepSeekMainAgent",
    "DialogueContext",
    "FinalizeContext",
    "JsonMemoryRepository",
    "MainAgent",
    "MainAgentDecision",
    "MemoryOperation",
    "MemoryRepository",
    "render_memory_proposal",
    "TaskContract",
]
