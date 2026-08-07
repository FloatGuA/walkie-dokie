"""烟雾测试：验证 CodexBackend 端到端能跑通（生成一个 docx）。

用法：python scripts/test_codex_backend.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from walkie_dokie.agents.codex_agent import CodexBackend


async def main():
    backend = CodexBackend()
    result = await backend.run(
        instruction="生成一份 docx 文档，标题是《测试》，正文写一句话：你好，这是 walkie-dokie 的第一次测试。",
        input_file=None,
    )
    print(f"reply_text: {result.reply_text}")
    print(f"result_filename: {result.result_filename}")
    if result.result_file is not None:
        print(f"result_file 大小: {len(result.result_file)} bytes")
        out_path = Path(__file__).parent / "_test_output.docx"
        out_path.write_bytes(result.result_file)
        print(f"已写入 {out_path}")
    else:
        print("没有生成文件")


asyncio.run(main())
