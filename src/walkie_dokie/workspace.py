"""执行 agent 的工作目录管理。

不用 tempfile 那种"用完即焚"的临时目录——早期阶段要能复盘，生成过什么、
执行 agent 实际读写了哪些文件都要能事后打开看，所以工作目录落在项目里，
按 platform_userid/日期/run_id 分层，不自动清理。
"""

import uuid
from datetime import datetime
from pathlib import Path

_VAR_ROOT = Path(__file__).parent.parent.parent / "var"
WORKSPACES_ROOT = _VAR_ROOT / "workspaces"


def create_workspace_dir(platform: str, user_id: str) -> Path:
    run_id = uuid.uuid4().hex[:8]
    date = datetime.now().strftime("%Y%m%d")
    workdir = WORKSPACES_ROOT / f"{platform}_{user_id}" / date / run_id
    workdir.mkdir(parents=True, exist_ok=False)
    return workdir
