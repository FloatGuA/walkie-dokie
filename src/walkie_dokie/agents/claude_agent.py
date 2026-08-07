from .base import ExecutionAgent, ExecutionResult


class ClaudeAgentSDKBackend(ExecutionAgent):
    """基于 Claude Agent SDK 的执行后端。

    TODO: 用 claude-agent-sdk 起一个带 code execution 能力的会话，把
    instruction 和输入文件喂给它，取回生成/修改后的文件，参考 docx/xlsx skill
    的思路让它自己写 python-docx / openpyxl 代码完成操作。
    """

    async def run(self, instruction: str, input_file: bytes | None) -> ExecutionResult:
        raise NotImplementedError
