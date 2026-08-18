"""Artifact storage boundary used by the conversation workflow.

Large platform attachments are persisted before they enter LangGraph.  Graph state
contains only JSON-serializable references, never the attachment bytes themselves.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Literal, TypedDict

from walkie_dokie.platforms.base import IncomingFile
from walkie_dokie.workspace import WORKSPACES_ROOT

_VAR_ROOT = Path(__file__).parent.parent.parent / "var"
INPUT_ARTIFACTS_ROOT = _VAR_ROOT / "inputs"


class ArtifactReference(TypedDict):
    kind: Literal["input", "output"]
    path: str
    filename: str
    display_filename: str | None
    mime_type: str


def _safe_segment(value: str) -> str:
    readable = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in value
    ).strip("._") or "unknown"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


def _safe_filename(value: str) -> str:
    filename = Path(value.replace("\\", "/")).name.strip()
    return filename if filename not in {"", ".", ".."} else "input"


def store_incoming_file(
    platform: str, user_id: str, incoming: IncomingFile
) -> ArtifactReference:
    """Persist an inbound attachment and return a small checkpoint-safe reference."""

    owner = f"{_safe_segment(platform)}_{_safe_segment(user_id)}"
    directory = INPUT_ARTIFACTS_ROOT / owner / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    filename = _safe_filename(incoming.filename)
    path = directory / filename
    path.write_bytes(incoming.content)
    return {
        "kind": "input",
        "path": str(path.resolve()),
        "filename": filename,
        "display_filename": None,
        "mime_type": incoming.mime_type or "application/octet-stream",
    }


def output_artifact_reference(
    path: Path, filename: str, mime_type: str = "application/octet-stream"
) -> ArtifactReference:
    reference: ArtifactReference = {
        "kind": "output",
        "path": str(path.resolve()),
        "filename": filename,
        "display_filename": None,
        "mime_type": mime_type,
    }
    # Validate before the reference becomes durable graph state.
    resolve_artifact_reference(reference)
    return reference


def display_name(reference: ArtifactReference | dict) -> str:
    """用户/模型可见的文件名：优先用同批次去重后的 display_filename。"""
    return reference.get("display_filename") or reference["filename"]


def resolve_artifact_reference(reference: ArtifactReference | dict) -> Path:
    """Resolve a durable reference and re-check its storage boundary and metadata."""

    kind = reference.get("kind")
    path_value = reference.get("path")
    filename = reference.get("filename")
    if kind not in {"input", "output"}:
        raise RuntimeError(f"未知 artifact kind：{kind!r}")
    if not isinstance(path_value, str) or not isinstance(filename, str):
        raise RuntimeError("artifact reference 缺少 path/filename")
    if _safe_filename(filename) != filename:
        raise RuntimeError(f"artifact filename 不安全：{filename!r}")

    root = (INPUT_ARTIFACTS_ROOT if kind == "input" else WORKSPACES_ROOT).resolve()
    path = Path(path_value).resolve()
    if not path.is_relative_to(root):
        raise RuntimeError(f"artifact 引用越过 {kind} 存储根目录：{path_value!r}")
    if not path.is_file():
        raise RuntimeError(f"artifact 不存在或不是普通文件：{path}")
    if path.name != filename:
        raise RuntimeError(
            f"artifact 路径与展示文件名不一致：{path.name!r} != {filename!r}"
        )
    return path
