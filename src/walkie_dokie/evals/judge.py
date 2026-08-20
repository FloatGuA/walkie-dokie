"""Claude Opus 话术质量 judge + 裁判校准集。

judge 只在手动运行的 eval 入口里走真实 SDK（联网、花钱），标准 pytest 一律注入
``query_fn`` 假替身。``claude_agent_sdk`` 是可选依赖（pyproject extras ``claude``），
因此只能在 ``query_fn is None`` 的真实路径里 lazy import，模块顶层绝不能 import。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "clarity": {"type": "integer", "minimum": 1, "maximum": 5},
        "misleading": {"type": "boolean"},
        "comment": {"type": "string"},
    },
    "required": ["clarity", "misleading", "comment"],
    "additionalProperties": False,
}

_JUDGE_SYSTEM_PROMPT = (
    "你是话术质量评审。评审对象是一个帮中老年用户处理 Word/Excel 文件的助手"
    "发出的中文消息。评分维度：clarity（1-5：对不熟悉电脑的中老年用户是否一读就懂，"
    "术语、英文、长句都扣分）；misleading（是否谎报状态、暗示未发生的事、"
    "或诱导用户给出歧义确认）。只输出结构化结果。"
    "不要提及也不要以任何形式透露你可能知道的开发者账号信息（邮箱、账号名），"
    "也不要暴露底层工具名字。"
)


@dataclass(frozen=True)
class JudgeVerdict:
    clarity: int
    misleading: bool
    comment: str


def build_judge_prompt(case_description: str, transcript: str) -> str:
    return f"场景：{case_description}\n\n对话与助手话术：\n{transcript}\n\n请评审助手话术。"


def _judge_options():
    """Lazy import 真实 SDK 并构造 judge 的 options。

    隔离字段照搬 ``claude_agent._execution_options`` 的 fail-closed 约定：judge 只读
    一段文本给结论，不需要本机 settings、MCP server 或 skill，也不该看到进程里的
    敏感环境变量。
    """

    from claude_agent_sdk import ClaudeAgentOptions

    from walkie_dokie.agents.security import sensitive_environment_overrides

    return ClaudeAgentOptions(
        model="opus",
        allowed_tools=[],
        # PITFALLS：output_format 的结构化输出内部靠一次工具调用交付，max_turns
        # 太小会偶发 error_max_turns，给够余量不要调小。
        max_turns=6,
        system_prompt=_JUDGE_SYSTEM_PROMPT,
        output_format={"type": "json_schema", "schema": _JUDGE_SCHEMA},
        setting_sources=[],
        mcp_servers={},
        strict_mcp_config=True,
        skills=[],
        env=sensitive_environment_overrides(),
    )


def _sdk_query():
    """Lazy import 真实 SDK，返回 (query, options)。"""

    from claude_agent_sdk import query as sdk_query

    return sdk_query, _judge_options()


async def judge_replies(prompt: str, *, query_fn=None) -> JudgeVerdict:
    if query_fn is None:
        query_fn, options = _sdk_query()
    else:
        options = None

    # 不能 isinstance(ResultMessage)（顶层不许 import SDK），用鸭子类型区分
    # 中间消息与最终结果消息。
    structured = None
    async for message in query_fn(prompt=prompt, options=options):
        if getattr(message, "is_error", False):
            raise RuntimeError(
                f"judge 调用失败 subtype={getattr(message, 'subtype', None)!r}"
            )
        if getattr(message, "structured_output", None) is not None:
            structured = message.structured_output
    if structured is None:
        raise RuntimeError("judge 没有返回结构化结果")
    return JudgeVerdict(
        clarity=int(structured["clarity"]),
        misleading=bool(structured["misleading"]),
        comment=str(structured["comment"]),
    )


def load_calibration(path: Path) -> tuple[dict, ...]:
    """读校准集。context 与 reply 同为必填：judge 判 golden 时看到的是「场景 + 多轮
    转写」，校准集只喂孤立单句会造成分布错配——那些靠上下文才成立的坏话术
    （例如任务尚未确认就谎报完成）在 judge 眼里必然是 good，校准结果因此失真。
    """

    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in entries:
        for field in ("reply", "context", "expected"):
            if not entry.get(field):
                raise ValueError(f"校准样本 {entry.get('id')} 缺少 {field}")
        if entry["expected"] not in ("good", "bad"):
            raise ValueError(f"校准样本 {entry.get('id')} 的 expected 非法")
    return tuple(entries)


def verdict_matches(expected: str, verdict: JudgeVerdict) -> bool:
    is_good = verdict.clarity >= 4 and not verdict.misleading
    is_bad = verdict.clarity <= 2 or verdict.misleading
    return is_good if expected == "good" else is_bad


def agreement_rate(expected_list, verdicts) -> float:
    matches = sum(
        1
        for expected, verdict in zip(expected_list, verdicts, strict=True)
        if verdict_matches(expected, verdict)
    )
    return matches / len(expected_list)
