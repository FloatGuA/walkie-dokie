import logging
import tempfile
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .base import ExecutionAgent, ExecutionResult

logger = logging.getLogger(__name__)

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply_text": {"type": "string"},
        "filename": {"type": "string"},
    },
    "required": ["reply_text", "filename"],
    "additionalProperties": False,
}


class ClaudeAgentSDKBackend(ExecutionAgent):
    """基于 Claude Agent SDK 的执行后端。

    走本机 `claude login` 缓存的订阅鉴权（MVP 阶段用户知情接受的风险，见 DECISION.md）。
    """

    async def run(
        self, instruction: str, input_file: bytes | None, input_filename: str | None = None
    ) -> ExecutionResult:
        logger.info("Claude Agent SDK 开始执行，instruction=%r input_filename=%r", instruction, input_filename)
        with tempfile.TemporaryDirectory(prefix="walkie-dokie-claude-") as workdir_str:
            workdir = Path(workdir_str)
            if input_file is not None:
                (workdir / (input_filename or "input")).write_bytes(input_file)

            prompt = (
                "你在当前工作目录里，需要用 Python 代码（Word 用 python-docx，"
                "Excel 用 openpyxl）完成下面这个文档操作请求，不要手动编辑。\n\n"
                f"用户请求：{instruction}\n"
            )
            if input_filename:
                prompt += f"\n工作目录下有用户提供的输入文件：{input_filename}\n"
            prompt += (
                "\n完成后把最终产出的文件保存在当前目录，返回 "
                "reply_text（给用户看的简短自然语言回复）和 filename"
                "（生成文件相对当前目录的文件名；如果没有生成文件，filename 留空字符串）。"
            )

            options = ClaudeAgentOptions(
                cwd=str(workdir),
                permission_mode="bypassPermissions",
                output_format={"type": "json_schema", "schema": _OUTPUT_SCHEMA},
            )

            structured: dict | None = None
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    if message.is_error:
                        logger.error("Claude Agent SDK 执行失败：%s", message.result)
                        raise RuntimeError(f"Claude Agent SDK 执行失败：{message.result}")
                    structured = message.structured_output

            if structured is None:
                logger.error("Claude Agent SDK 没有返回结构化结果")
                raise RuntimeError("Claude Agent SDK 没有返回结构化结果")

            filename = structured.get("filename") or None
            result_file = None
            if filename:
                file_path = workdir / filename
                if not file_path.exists():
                    actual_files = [p.name for p in workdir.iterdir()]
                    logger.error(
                        "Claude 汇报生成了 %r，但工作目录里没有这个文件。实际内容：%s", filename, actual_files
                    )
                    raise RuntimeError(
                        f"Claude 汇报生成了 {filename!r}，但工作目录里没有这个文件。"
                        f"工作目录实际内容：{actual_files}"
                    )
                result_file = file_path.read_bytes()

            logger.info("Claude Agent SDK 执行完成，filename=%r", filename)
            return ExecutionResult(
                reply_text=structured["reply_text"],
                result_file=result_file,
                result_filename=filename,
            )
