"""生成 task prompt 草稿：把用户攒起来的原始话提炼成一句清楚的任务描述，
并且明确指出还缺什么信息（如果有的话）。

轻量调用——不给工具、不跑代码、`max_turns=1`，跟 agents/ 下真正执行任务的
调用完全不是一回事，只是复用同一套鉴权（走 claude login 订阅登录，
不需要额外申请 API key）。
"""

import logging

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你在帮用户把口语化、可能不完整的请求，整理成任务描述，这个描述会被直接"
    "交给另一个文档处理 agent 去执行。"
    "task_summary 是对任务本身清楚具体的描述（不要用第三人称谈论用户，"
    "直接描述任务是什么）。"
    "missing_info 是完成这个任务缺少的关键信息点，每一项都要是能直接抛给用户"
    "的具体问题措辞（比如「请假事由是什么」而不是「事由不明」），"
    "如果不缺任何信息就返回空数组。"
)

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task_summary": {"type": "string"},
        "missing_info": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["task_summary", "missing_info"],
    "additionalProperties": False,
}


async def generate_draft_task_prompt(accumulated_text: str) -> dict:
    """返回 {"task_summary": str, "missing_info": list[str]}。"""
    logger.info("生成 task prompt 草稿，accumulated_text=%r", accumulated_text)
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM_PROMPT,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        max_turns=1,
        output_format={"type": "json_schema", "schema": _OUTPUT_SCHEMA},
    )
    draft: dict | None = None
    async for message in query(prompt=accumulated_text, options=options):
        if isinstance(message, ResultMessage):
            if message.is_error:
                logger.error("draft 生成失败：%s", message.result)
                raise RuntimeError(f"draft 生成失败：{message.result}")
            draft = message.structured_output

    if draft is None:
        raise RuntimeError("draft 生成没有返回结构化结果")

    logger.info("task prompt 草稿：%r", draft)
    return draft
