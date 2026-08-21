import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from walkie_dokie.agents.security import validate_office_artifact


@dataclass(frozen=True)
class ExecutionArtifact:
    """执行 Agent 产出的单个文件，path 必须是 filename 指向的同一个普通文件。"""

    path: Path
    filename: str

    def __post_init__(self) -> None:
        if self.path.name != self.filename:
            raise ValueError("ExecutionArtifact.path 与 filename 不一致")
        if not self.path.is_file():
            raise ValueError("ExecutionArtifact.path 必须指向普通文件")


@dataclass(frozen=True)
class ExecutionReport:
    """执行 Agent 的内部报告，不是给终端用户的自由对话回复。"""

    summary: str
    artifacts: tuple[ExecutionArtifact, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("ExecutionReport.summary 必须是非空字符串")
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(item, str) for item in self.warnings
        ):
            raise ValueError("ExecutionReport.warnings 必须是字符串 tuple")
        if not isinstance(self.artifacts, tuple) or not all(
            isinstance(item, ExecutionArtifact) for item in self.artifacts
        ):
            raise ValueError("ExecutionReport.artifacts 必须是 ExecutionArtifact tuple")
        filenames = [item.filename for item in self.artifacts]
        if len(set(filenames)) != len(filenames):
            raise ValueError("ExecutionReport.artifacts 内 filename 不允许重复")


class ExecutionAgent(ABC):
    """执行后端的统一接口：拿自然语言指令 + 一批可选输入文件，跑代码，产出结果。

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
        input_paths: tuple[Path, ...],
        input_filenames: tuple[str, ...],
        workdir: Path,
        difficulty: str = "standard",
    ) -> ExecutionReport: ...


def safe_input_filename(filename: str | None) -> str:
    """把平台提供的名字收窄为工作目录内的单个文件名。"""
    if not filename:
        return "input"
    name = Path(filename.replace("\\", "/")).name.strip()
    if name in {"", ".", ".."}:
        return "input"
    if name.casefold() == ".walkie-dokie":
        return "input-.walkie-dokie"
    return name


def stage_execution_inputs(
    input_paths: tuple[Path, ...],
    input_filenames: tuple[str, ...],
    workdir: Path,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """校验并拷贝一批输入文件到 workdir，供两个执行后端共用。

    返回 (拷贝到 workdir 后的目标文件名列表, 被排除文件的 warning 列表)。
    input_paths 为空（任务本身不需要输入文件）返回 ((), ())，不是错误。
    input_paths 非空但全部未通过 Office 内容校验时抛 RuntimeError，调用方不应
    再继续调用 backend。
    """

    if len(input_paths) != len(input_filenames):
        raise RuntimeError("input_paths 与 input_filenames 长度必须一致")
    if not input_paths:
        return (), ()

    staged: list[str] = []
    warnings: list[str] = []
    for path, filename in zip(input_paths, input_filenames):
        if not path.is_file():
            raise RuntimeError(f"执行输入不存在或不是普通文件：{path}")
        try:
            validate_office_artifact(path, role="执行输入")
        except RuntimeError as exc:
            warnings.append(f"文件「{filename}」未通过安全校验，已跳过：{exc}")
            continue
        safe_name = safe_input_filename(filename)
        shutil.copyfile(path, workdir / safe_name)
        staged.append(safe_name)

    if not staged:
        raise RuntimeError(
            "本轮全部输入文件都未通过安全校验，没有可执行的输入："
            + "；".join(warnings)
        )
    return tuple(staged), tuple(warnings)


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
