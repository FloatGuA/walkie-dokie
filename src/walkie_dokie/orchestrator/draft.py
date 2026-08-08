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


async def generate_draft_task_prompt(accumulated_text: str, input_filename: str | None = None) -> dict:
    """返回 {"task_summary": str, "missing_info": list[str]}。

    input_filename 不为空表示用户已经发过一个文件——草稿要知道这件事，
    不然容易把"文件"错误地列进 missing_info（用户明明发了）。
    """
    prompt = accumulated_text or ""
    if input_filename:
        prompt += f"\n\n（用户已经发来一个文件：{input_filename}，不要把'文件'当成缺失信息再问一遍。）"
    logger.info("生成 task prompt 草稿，accumulated_text=%r input_filename=%r", accumulated_text, input_filename)
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM_PROMPT,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        max_turns=1,
        output_format={"type": "json_schema", "schema": _OUTPUT_SCHEMA},
        setting_sources=[],  # 隔离模式，见 claude_agent.py 的同一条注释 / PITFALLS.md
    )
    draft: dict | None = None
    last_result_message: ResultMessage | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            last_result_message = message
            if message.is_error:
                logger.error(
                    "draft 生成失败：subtype=%r stop_reason=%r terminal_reason=%r "
                    "api_error_status=%r errors=%r result=%r",
                    message.subtype,
                    message.stop_reason,
                    message.terminal_reason,
                    message.api_error_status,
                    message.errors,
                    message.result,
                )
                raise RuntimeError(f"draft 生成失败：subtype={message.subtype} errors={message.errors}")
            draft = message.structured_output

    if draft is None:
        logger.error("draft 生成没有返回结构化结果，最后一条 ResultMessage=%r", last_result_message)
        raise RuntimeError("draft 生成没有返回结构化结果")

    logger.info("task prompt 草稿：%r", draft)
    return draft
