from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ExecutionResult:
    reply_text: str
    result_file: bytes | None
    result_filename: str | None


class ExecutionAgent(ABC):
    """执行后端的统一接口：拿自然语言指令 + 可选附件，跑代码，产出结果。

    Claude Agent SDK / Codex 两个后端都实现这个接口，orchestrator 只认接口，
    不关心具体是哪个在跑、也不关心它内部怎么写代码操作文档。

    workdir 由调用方创建并传入（见 walkie_dokie.workspace.create_workspace_dir），
    不是执行后端自己起临时目录——这样生成过程留在项目里，能事后复盘，用完也
    不自动删。
    """

    @abstractmethod
    async def run(
        self,
        instruction: str,
        input_file: bytes | None,
        workdir: Path,
        input_filename: str | None = None,
    ) -> ExecutionResult: ...
