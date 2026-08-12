"""执行 agent 的工作目录管理。

不用 tempfile 那种"用完即焚"的临时目录——早期阶段要能复盘，生成过什么、
执行 agent 实际读写了哪些文件都要能事后打开看，所以工作目录落在项目里，
按 platform_userid/日期/run_id 分层，不自动清理。
"""

import uuid
import hashlib
import re
from datetime import datetime
from pathlib import Path

_VAR_ROOT = Path(__file__).parent.parent.parent / "var"
WORKSPACES_ROOT = _VAR_ROOT / "workspaces"
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_segment(value: str) -> str:
    cleaned = _SAFE_SEGMENT_RE.sub("_", value).strip("._") or "unknown"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned}-{digest}"


def create_workspace_dir(platform: str, user_id: str) -> Path:
    run_id = uuid.uuid4().hex[:8]
    date = datetime.now().strftime("%Y%m%d")
    owner_dir = f"{_safe_segment(platform)}_{_safe_segment(user_id)}"
    workdir = WORKSPACES_ROOT / owner_dir / date / run_id
    workdir.mkdir(parents=True, exist_ok=False)
    return workdir


def resolve_artifact_reference(reference: str) -> Path:
    """投递 checkpoint 中的 artifact path 前重新验证它仍在 workspace 根下。"""
    root = WORKSPACES_ROOT.resolve()
    path = Path(reference).resolve()
    if not path.is_relative_to(root):
        raise RuntimeError(f"产物引用越过 workspace 根目录：{reference!r}")
    if not path.is_file():
        raise RuntimeError(f"待投递的执行产物不存在：{path}")
    return path
