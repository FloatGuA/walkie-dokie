"""一次 eval 运行的结果报告：构建 + 落盘 JSON。

报告是 harness 唯一的产出物，因此 FAILED_INFRA 也照样写：跑到一半崩掉时，
已完成样本的结果加上 error 描述比一个空目录有用得多。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from walkie_dokie.evals.driver import CaseResult


@dataclass
class RunReport:
    mode: str
    status: str
    git_commit: str
    deepseek_model: str
    judge_model: str | None
    started_at: str
    error: str | None
    case_results: list[dict]
    summary: dict


def _git_commit() -> str:
    """取当前短 commit；git 不可用或不在仓库里时退化成 "unknown"。

    这是对外部工具的边界处理，不是内部路径兜底：报告缺个 commit 号可以接受，
    因此没装 git 就让 eval 跑不起来则不可接受。
    """
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def build_report(
    mode: str,
    status: str,
    case_results: list[CaseResult],
    *,
    deepseek_model: str,
    judge_model: str | None,
    error: str | None = None,
) -> RunReport:
    # judge 契约固化后 clarity 必然存在（缺键就让 KeyError 当场炸出来），
    # 这里只区分「判过分」和「没判到分」两种情况。
    clarity = [c.judge["clarity"] for c in case_results if c.judge]
    return RunReport(
        mode=mode,
        status=status,
        git_commit=_git_commit(),
        deepseek_model=deepseek_model,
        judge_model=judge_model,
        started_at=datetime.now(timezone.utc).isoformat(),
        error=error,
        case_results=[asdict(c) for c in case_results],
        summary={
            "total": len(case_results),
            "passed": sum(1 for c in case_results if c.passed),
            "failed": sum(1 for c in case_results if not c.passed),
            "judge_clarity_avg": (sum(clarity) / len(clarity)) if clarity else None,
        },
    )


def write_report(report: RunReport, out_dir: Path = Path("var/evals")) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}.json"
    path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path
