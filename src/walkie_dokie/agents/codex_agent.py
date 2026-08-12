import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from .base import (
    ExecutionAgent,
    ExecutionReport,
    resolve_output_file,
    safe_input_filename,
)

logger = logging.getLogger(__name__)

# 独立 CODEX_HOME，跟开发者本机 ~/.codex 的 config.toml/AGENTS.md/rules/skills
# 彻底隔离——实测踩过这三层配置分别泄漏进执行结果的坑，见 PITFALLS.md。
# 首次使用需要单独 `codex login`（这个目录下没有 auth.json，见 README.md）。
_VAR_ROOT = Path(__file__).parent.parent.parent.parent / "var"
CODEX_HOME_DIR = _VAR_ROOT / "codex_home"

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


class CodexBackend(ExecutionAgent):
    """基于 Codex CLI（`codex exec`）的执行后端。

    走独立 `CODEX_HOME`（见 `CODEX_HOME_DIR`）下缓存的 ChatGPT 订阅鉴权，不设
    CODEX_API_KEY。这个目录跟开发者本机的 ~/.codex 完全隔离，首次使用需要单独
    登录一次：`CODEX_HOME=<CODEX_HOME_DIR> codex login`（见 README.md）。
    """

    def __init__(
        self, executable: str | None = None, codex_home: Path | None = None
    ) -> None:
        # Import 这个可选 backend 不应要求机器已经安装 CLI；到真正实例化时再守门，
        # 这样 pytest collection 和只使用 Claude 的部署都不会被模块副作用破坏。
        self._executable = executable or shutil.which("codex")
        if self._executable is None:
            raise RuntimeError(
                "找不到 codex CLI，确认已安装并在 PATH 里（`codex --version` 能跑通）"
            )
        self._codex_home = codex_home or CODEX_HOME_DIR
        self._codex_home.mkdir(parents=True, exist_ok=True)

    async def run(
        self,
        instruction: str,
        input_path: Path | None,
        workdir: Path,
        input_filename: str | None = None,
    ) -> ExecutionReport:
        logger.info(
            "Codex 开始执行，instruction=%r input_filename=%r workdir=%s", instruction, input_filename, workdir
        )
        safe_filename = safe_input_filename(input_filename)
        if input_path is not None:
            if not input_path.is_file():
                raise RuntimeError(f"执行输入不存在或不是普通文件：{input_path}")
            shutil.copyfile(input_path, workdir / safe_filename)

        # 内部 schema 放进保留子目录；用户上传同名文件不会再被静默覆盖。
        internal_dir = workdir / ".walkie-dokie"
        internal_dir.mkdir(exist_ok=True)
        schema_path = internal_dir / "output-schema.json"
        schema_path.write_text(json.dumps(_OUTPUT_SCHEMA), encoding="utf-8")

        prompt = (
            "你在当前工作目录里，需要用 Python 代码（Word 用 python-docx，"
            "Excel 用 openpyxl）完成下面这个文档操作请求，不要手动编辑。\n\n"
            f"用户请求：{instruction}\n"
        )
        if input_path is not None:
            prompt += f"\n工作目录下有用户提供的输入文件：{safe_filename}\n"
        prompt += (
            "\n完成后把最终产出的文件保存在当前目录，按 schema 要求返回："
            "summary（供主 Agent 阅读的客观内部执行摘要）、filename"
            "（生成文件相对当前目录的文件名；如果没有生成文件，filename 留空字符串）"
            "和 warnings（需要主 Agent 告知用户的限制或注意事项，没有则为空数组）。"
            "不要直接和用户对话，不要决定或讨论用户的长期记忆。"
        )

        env = {**os.environ, "CODEX_HOME": str(self._codex_home)}
        proc = await asyncio.create_subprocess_exec(
            self._executable,
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
        try:
            async with asyncio.timeout(900):
                stdout, stderr = await proc.communicate()
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RuntimeError("codex exec 超过 15 分钟，已终止") from exc
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0:
            logger.error("codex exec 失败，退出码 %s：%s", proc.returncode, stderr.decode(errors="replace"))
            raise RuntimeError(
                f"codex exec 失败（退出码 {proc.returncode}）：{stderr.decode(errors='replace')}"
            )

        result = json.loads(stdout.decode())
        filename = result.get("filename") or None
        artifact_path = None
        if filename:
            artifact_path = resolve_output_file(workdir, filename)

        logger.info("Codex 执行完成，filename=%r", filename)
        return ExecutionReport(
            summary=result["summary"],
            artifact_path=artifact_path,
            result_filename=filename,
            warnings=tuple(result.get("warnings", ())),
        )
