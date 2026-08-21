from __future__ import annotations

import shutil
from pathlib import Path

from walkie_dokie.agents.base import ExecutionAgent, ExecutionArtifact, ExecutionReport


class FakeExecutionAgent(ExecutionAgent):
    """回归模式的确定性执行后端：不跑模型，把预制合法 docx 拷进 workdir。"""

    def __init__(self, output_fixture: Path):
        self._output_fixture = output_fixture

    async def run(
        self,
        instruction: str,
        input_paths: tuple[Path, ...],
        input_filenames: tuple[str, ...],
        workdir: Path,
        difficulty: str = "standard",
    ) -> ExecutionReport:
        target = workdir / "output.docx"
        shutil.copyfile(self._output_fixture, target)
        return ExecutionReport(
            summary="已按要求处理完成",
            artifacts=(ExecutionArtifact(path=target, filename="output.docx"),),
        )


class RecordingExecutionAgent(ExecutionAgent):
    """包一层执行后端并记录每次调用，供 driver 判定「本轮是否真的进了 execute」。"""

    def __init__(self, inner: ExecutionAgent):
        self._inner = inner
        self.calls: list[dict] = []

    async def run(
        self,
        instruction: str,
        input_paths: tuple[Path, ...],
        input_filenames: tuple[str, ...],
        workdir: Path,
        difficulty: str = "standard",
    ) -> ExecutionReport:
        self.calls.append(
            {
                "instruction": instruction,
                "input_filenames": input_filenames,
                "difficulty": difficulty,
            }
        )
        return await self._inner.run(
            instruction, input_paths, input_filenames, workdir, difficulty
        )
