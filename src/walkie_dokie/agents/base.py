from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExecutionReport:
    """执行 Agent 的内部报告，不是给终端用户的自由对话回复。"""

    summary: str
    artifact_path: Path | None
    result_filename: str | None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("ExecutionReport.summary 必须是非空字符串")
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(item, str) for item in self.warnings
        ):
            raise ValueError("ExecutionReport.warnings 必须是字符串 tuple")
        if (self.artifact_path is None) != (self.result_filename is None):
            raise ValueError("artifact_path 与 result_filename 必须同时存在或同时为空")
        if self.artifact_path is not None:
            if self.artifact_path.name != self.result_filename:
                raise ValueError("artifact_path 与 result_filename 不一致")
            if not self.artifact_path.is_file():
                raise ValueError("ExecutionReport.artifact_path 必须指向普通文件")


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
        input_path: Path | None,
        workdir: Path,
        input_filename: str | None = None,
    ) -> ExecutionReport: ...


def safe_input_filename(filename: str | None) -> str:
    """把平台提供的名字收窄为工作目录内的单个文件名。"""
    if not filename:
        return "input"
    name = Path(filename.replace("\\", "/")).name.strip()
    if name in {"", ".", ".."}:
        return "input"
    # `.walkie-dokie` 是执行元数据保留名。用户文件若同名则稳定重命名，避免
    # 后端内部目录/marker 与上传内容发生文件-目录冲突。
    if name.casefold() == ".walkie-dokie":
        return "input-.walkie-dokie"
    return name


def resolve_output_file(workdir: Path, filename: str) -> Path:
    """验证执行 Agent 汇报的 artifact 没有通过绝对路径/.. /symlink 越界。"""
    relative = Path(filename)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
        raise RuntimeError(f"执行 Agent 返回了不安全的输出文件名：{filename!r}")
    root = workdir.resolve()
    path = (workdir / relative).resolve()
    if not path.is_relative_to(root):
        raise RuntimeError(f"执行 Agent 返回的文件越过工作目录：{filename!r}")
    if not path.is_file():
        raise RuntimeError(f"执行 Agent 返回的产物不存在或不是普通文件：{filename!r}")
    return path
