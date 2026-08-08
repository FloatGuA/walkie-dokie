import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from .base import ExecutionAgent, ExecutionResult

logger = logging.getLogger(__name__)

_CODEX_EXECUTABLE = shutil.which("codex")
if _CODEX_EXECUTABLE is None:
    raise RuntimeError("找不到 codex CLI，确认已安装并在 PATH 里（`codex --version` 能跑通）")

# 独立 CODEX_HOME，跟开发者本机 ~/.codex 的 config.toml/AGENTS.md/rules/skills
# 彻底隔离——实测踩过这三层配置分别泄漏进执行结果的坑，见 PITFALLS.md。
# 首次使用需要单独 `codex login`（这个目录下没有 auth.json，见 README.md）。
_VAR_ROOT = Path(__file__).parent.parent.parent.parent / "var"
CODEX_HOME_DIR = _VAR_ROOT / "codex_home"
CODEX_HOME_DIR.mkdir(parents=True, exist_ok=True)

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply_text": {"type": "string"},
        "filename": {"type": "string"},
    },
    "required": ["reply_text", "filename"],
    "additionalProperties": False,
}


class CodexBackend(ExecutionAgent):
    """基于 Codex CLI（`codex exec`）的执行后端。

    走独立 `CODEX_HOME`（见 `CODEX_HOME_DIR`）下缓存的 ChatGPT 订阅鉴权，不设
    CODEX_API_KEY。这个目录跟开发者本机的 ~/.codex 完全隔离，首次使用需要单独
    登录一次：`CODEX_HOME=<CODEX_HOME_DIR> codex login`（见 README.md）。
    """

    async def run(
        self,
        instruction: str,
        input_file: bytes | None,
        workdir: Path,
        input_filename: str | None = None,
    ) -> ExecutionResult:
        logger.info(
            "Codex 开始执行，instruction=%r input_filename=%r workdir=%s", instruction, input_filename, workdir
        )
        if input_file is not None:
            (workdir / (input_filename or "input")).write_bytes(input_file)

        schema_path = workdir / "_output_schema.json"
        schema_path.write_text(json.dumps(_OUTPUT_SCHEMA), encoding="utf-8")

        prompt = (
            "你在当前工作目录里，需要用 Python 代码（Word 用 python-docx，"
            "Excel 用 openpyxl）完成下面这个文档操作请求，不要手动编辑。\n\n"
            f"用户请求：{instruction}\n"
        )
        if input_filename:
            prompt += f"\n工作目录下有用户提供的输入文件：{input_filename}\n"
        prompt += (
            "\n完成后把最终产出的文件保存在当前目录，按 schema 要求返回："
            "reply_text（给用户看的简短自然语言回复）和 filename"
            "（生成文件相对当前目录的文件名；如果没有生成文件，filename 留空字符串）。"
        )

        env = {**os.environ, "CODEX_HOME": str(CODEX_HOME_DIR)}
        proc = await asyncio.create_subprocess_exec(
            _CODEX_EXECUTABLE,
            "exec",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            prompt,
            cwd=workdir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("codex exec 失败，退出码 %s：%s", proc.returncode, stderr.decode(errors="replace"))
            raise RuntimeError(
                f"codex exec 失败（退出码 {proc.returncode}）：{stderr.decode(errors='replace')}"
            )

        result = json.loads(stdout.decode())
        filename = result.get("filename") or None
        result_file = None
        if filename:
            file_path = workdir / filename
            if not file_path.exists():
                actual_files = [p.name for p in workdir.iterdir()]
                logger.error(
                    "Codex 汇报生成了 %r，但工作目录里没有这个文件。实际内容：%s", filename, actual_files
                )
                raise RuntimeError(
                    f"Codex 汇报生成了 {filename!r}，但工作目录里没有这个文件。"
                    f"工作目录实际内容：{actual_files}"
                )
            result_file = file_path.read_bytes()

        logger.info("Codex 执行完成，filename=%r", filename)
        return ExecutionResult(
            reply_text=result["reply_text"],
            result_file=result_file,
            result_filename=filename,
        )
