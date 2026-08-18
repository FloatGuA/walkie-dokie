"""烟雾测试：验证 CodexBackend 端到端能跑通（生成一个 docx）。

用法：python scripts/test_codex_backend.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from walkie_dokie.agents.codex_agent import CodexBackend
from walkie_dokie.workspace import create_workspace_dir


async def main():
    backend = CodexBackend()
    workdir = create_workspace_dir("smoketest", "codex")
    result = await backend.run(
        instruction="生成一份 docx 文档，标题是《测试》，正文写一句话：你好，这是 walkie-dokie 的第一次测试。",
        input_paths=(),
        input_filenames=(),
        workdir=workdir,
    )
    print(f"summary: {result.summary}")
    print(f"warnings: {result.warnings}")
    if result.artifacts:
        for artifact in result.artifacts:
            print(f"artifact filename: {artifact.filename}")
            print(f"artifact 大小: {artifact.path.stat().st_size} bytes")
            print(f"已写入 {artifact.path}")
    else:
        print("没有生成文件")


if __name__ == "__main__":
    asyncio.run(main())
