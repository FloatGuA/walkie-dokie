from .base import ExecutionAgent, ExecutionResult


class CodexBackend(ExecutionAgent):
    """基于 Codex 的执行后端。

    TODO: 通过 Codex CLI/SDK 起一个带代码执行能力的会话，把 instruction 和
    输入文件喂给它，取回生成/修改后的文件。
    """

    async def run(self, instruction: str, input_file: bytes | None) -> ExecutionResult:
        raise NotImplementedError
