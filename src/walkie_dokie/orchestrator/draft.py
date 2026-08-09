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
    "严格规则：不要提及、也不要以任何形式透露你可能知道的开发者账号信息"
    "（比如邮箱地址、账号名）——那是你运行环境本身携带的信息，跟当前对话的"
    "用户无关，绝对不能说出来，也不要说「Claude Code」「CLAUDE.md」这类底层"
    "工具名字，你的身份只是「小帮」，不需要解释你底层是什么。"
    "\n\n"
    "你在判断用户这段话是不是一个需要生成/编辑/读取 Word 或 Excel 文档的具体"
    "任务请求，还是闲聊、打招呼、身份确认这类不需要执行任何文档操作的对话。"
    "\n\n"
    "is_task=false 的情况：寒暄、自我介绍、跟文档处理无关的提问、内容太空泛"
    "完全看不出想要什么文档。这种时候 user_message 直接用自然、口语化的第一"
    "/第二人称回应用户（比如回答对方的问题，或者问"
    "\"你好，需要我帮你处理什么文档？\"），task_summary 和 missing_info 留空。"
    "\n\n"
    "is_task=true 的情况：明确或大致能看出是要处理一份文档。"
    "task_summary 是给另一个文档处理 agent 看的任务描述（客观、具体，不用"
    "对话口吻）。missing_info 是缺少的关键信息点，每一项是能直接抛给用户的"
    "具体问题措辞。user_message 是**直接说给用户听的确认话术**——用对话口吻"
    "复述你理解的任务、列出还缺的信息（如果有），并说明回'是'就可以用默认值"
    "直接生成；user_message 和 task_summary 服务不同的读者，不要混用同一套"
    "措辞。"
)

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_task": {"type": "boolean"},
        "task_summary": {"type": "string"},
        "missing_info": {"type": "array", "items": {"type": "string"}},
        "user_message": {"type": "string"},
    },
    "required": ["is_task", "task_summary", "missing_info", "user_message"],
    "additionalProperties": False,
}


async def generate_draft_task_prompt(
    accumulated_text: str, input_filename: str | None = None, known_facts: dict | None = None
) -> dict:
    """返回 {"is_task": bool, "task_summary": str, "missing_info": list[str], "user_message": str}。

    is_task=false 时 task_summary/missing_info 无意义，只看 user_message
    （直接回给用户的话，不进 confirm 循环）。

    input_filename 不为空表示用户已经发过一个文件——草稿要知道这件事，
    不然容易把"文件"错误地列进 missing_info（用户明明发了）。

    known_facts 是从这个用户过往对话里提取存下来的个人信息（姓名/部门这类，
    见 orchestrator/memory.py）——已经知道的字段不该再出现在 missing_info 里。
    """
    prompt = accumulated_text or ""
    if input_filename:
        prompt += f"\n\n（用户已经发来一个文件：{input_filename}，不要把'文件'当成缺失信息再问一遍。）"
    if known_facts:
        facts_str = "、".join(f"{k}：{v}" for k, v in known_facts.items())
        prompt += f"\n\n（已知这个用户的信息——{facts_str}。这些字段不用再列进 missing_info。）"
    logger.info("生成 task prompt 草稿，accumulated_text=%r input_filename=%r", accumulated_text, input_filename)
    options = ClaudeAgentOptions(
        # 纯字符串 system_prompt 只是替换了系统提示文本，挡不住"动态上下文"
        # （开发者身份、工作目录这类）——那些是注入进第一条 user message 的，
        # 跟 system_prompt 是不是自定义字符串无关。必须用 preset 形式 +
        # exclude_dynamic_sections=True 才能真正剥掉，见 PITFALLS.md（实测
        # 泄漏过开发者本人的邮箱地址）。
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": _SYSTEM_PROMPT,
            "exclude_dynamic_sections": True,
        },
        allowed_tools=[],
        permission_mode="bypassPermissions",
        # 结构化输出（output_format）内部靠工具调用交付最终答案，实测轮数波动
        # 比预期大，max_turns=1/2 都撞见过被判超限报错（见 PITFALLS.md）。
        # allowed_tools=[] 已经防住了失控调用，这里不需要卡轮数，给够余量。
        max_turns=6,
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
