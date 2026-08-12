import asyncio
import logging
import shutil
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .base import (
    ExecutionAgent,
    ExecutionReport,
    resolve_output_file,
    safe_input_filename,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_APPEND = (
    "你现在是 walkie-dokie 的文档处理执行单元，只做一件事：用 Python 代码"
    "（Word 用 python-docx，Excel 用 openpyxl）在当前工作目录里完成用户的文档请求"
    "（生成、编辑或读取问答 Word/Excel 文件）。不要提及、也不要尝试使用任何跟这个"
    "任务无关的能力（比如 Gmail、日历、云盘之类的连接器/授权流程）——那些在这个"
    "环境里不存在也用不上，提了只会让用户困惑。"
    "你不是面向用户的主 Agent：不要判断用户意图、维护长期记忆或自由地与用户"
    "对话，只执行已经确认的任务契约并返回客观内部报告。"
    "\n\n"
    "严格规则：不要提及、也不要以任何形式透露你可能知道的开发者账号信息"
    "（比如邮箱地址、账号名）——那是运行环境本身携带的信息，跟当前对话的用户"
    "无关，绝对不能说出来（`exclude_dynamic_sections` 挡不住这类信息，实测"
    "验证过，见 PITFALLS.md），也不要说「Claude Code」这类底层工具名字。"
)

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "filename": {"type": "string"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "filename", "warnings"],
    "additionalProperties": False,
}


class ClaudeAgentSDKBackend(ExecutionAgent):
    """基于 Claude Agent SDK 的执行后端。

    走本机 `claude login` 缓存的订阅鉴权（MVP 阶段用户知情接受的风险，见 DECISION.md）。
    """

    async def run(
        self,
        instruction: str,
        input_path: Path | None,
        workdir: Path,
        input_filename: str | None = None,
    ) -> ExecutionReport:
        logger.info(
            "Claude Agent SDK 开始执行，instruction=%r input_filename=%r workdir=%s",
            instruction,
            input_filename,
            workdir,
        )
        safe_filename = safe_input_filename(input_filename)
        if input_path is not None:
            if not input_path.is_file():
                raise RuntimeError(f"执行输入不存在或不是普通文件：{input_path}")
            shutil.copyfile(input_path, workdir / safe_filename)

        prompt = (
            "你在当前工作目录里，需要用 Python 代码（Word 用 python-docx，"
            "Excel 用 openpyxl）完成下面这个文档操作请求，不要手动编辑。\n\n"
            f"用户请求：{instruction}\n"
        )
        if input_path is not None:
            prompt += f"\n工作目录下有用户提供的输入文件：{safe_filename}\n"
        prompt += (
            "\n完成后把最终产出的文件保存在当前目录，返回 "
            "summary（供主 Agent 阅读的客观内部执行摘要）、filename"
            "（生成文件相对当前目录的文件名；如果没有生成文件，filename 留空字符串）"
            "和 warnings（需要主 Agent 告知用户的限制或注意事项，没有则为空数组）。"
            "不要直接和用户对话，不要决定或讨论用户的长期记忆。"
        )

        options = ClaudeAgentOptions(
            cwd=str(workdir),
            permission_mode="bypassPermissions",
            output_format={"type": "json_schema", "schema": _OUTPUT_SCHEMA},
            # 隔离模式：不读开发者本机的 ~/.claude 全局配置/CLAUDE.md，行为只由这里
            # 显式传的 system_prompt/options 决定，不会被个人日常用的 Claude Code
            # 配置污染（见 PITFALLS.md，Codex 那边同类问题的 --ignore-user-config）。
            setting_sources=[],
            # 保留 Claude Code 自带的代码能力（怎么安全地用工具、写代码），只追加
            # 我们自己的任务框定，并且去掉 auto-memory/git status 这类跟每个用户
            # 绑定的动态段落——怀疑就是这类动态内容导致过一次回复里混进了跟任务
            # 无关的"Gmail/Calendar 连接器"提示，见 PROGRESS.md。
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": _SYSTEM_PROMPT_APPEND,
                "exclude_dynamic_sections": True,
            },
        )

        structured: dict | None = None
        try:
            async with asyncio.timeout(900):
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, ResultMessage):
                        if message.is_error:
                            logger.error("Claude Agent SDK 执行失败：%s", message.result)
                            raise RuntimeError(
                                f"Claude Agent SDK 执行失败：{message.result}"
                            )
                        structured = message.structured_output
        except TimeoutError as exc:
            raise RuntimeError("Claude Agent SDK 执行超过 15 分钟，已取消") from exc

        if structured is None:
            logger.error("Claude Agent SDK 没有返回结构化结果")
            raise RuntimeError("Claude Agent SDK 没有返回结构化结果")

        filename = structured.get("filename") or None
        artifact_path = None
        if filename:
            artifact_path = resolve_output_file(workdir, filename)

        logger.info("Claude Agent SDK 执行完成，filename=%r", filename)
        return ExecutionReport(
            summary=structured["summary"],
            artifact_path=artifact_path,
            result_filename=filename,
            warnings=tuple(structured.get("warnings", ())),
        )
