from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ExecutionResult:
    reply_text: str
    result_file: bytes | None
    result_filename: str | None


class ExecutionAgent(ABC):
    """执行后端的统一接口：拿自然语言指令 + 可选附件，跑代码，产出结果。

    Claude Agent SDK / Codex 两个后端都实现这个接口，orchestrator 只认接口，
    不关心具体是哪个在跑、也不关心它内部怎么写代码操作文档。
    """

    @abstractmethod
    async def run(self, instruction: str, input_file: bytes | None) -> ExecutionResult: ...
