"""Provider-neutral document IR used by ingestion and later retrieval stages."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


def normalize_text(value: str) -> str:
    """Normalize compatibility characters and layout whitespace, never the source."""

    normalized = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """One source-grounded block before the storage layer creates Evidence Units."""

    kind: str
    ordinal: int
    text: str
    source_anchor: dict[str, Any]
    structural_path: tuple[str, ...] = ()
    title_path: tuple[str, ...] = ()
    clause_ref: str | None = None
    context_prefix: str = ""
    page_physical: int | None = None
    page_printed: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("ParsedBlock.kind 不能为空")
        if self.ordinal < 0:
            raise ValueError("ParsedBlock.ordinal 不能为负数")
        if not normalize_text(self.text):
            raise ValueError("ParsedBlock.text 不能为空")
        if not self.source_anchor:
            raise ValueError("ParsedBlock.source_anchor 不能为空")

    @property
    def normalized_text(self) -> str:
        return normalize_text(self.text)

    @property
    def content_sha256(self) -> str:
        return sha256_text(self.normalized_text)


@dataclass(frozen=True, slots=True)
class ParseResult:
    provider_name: str
    provider_version: str
    blocks: tuple[ParsedBlock, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.provider_name or not self.provider_version:
            raise ValueError("Parser provider 名称和版本不能为空")


def stable_evidence_id(
    *,
    document_version_id: str,
    source_sha256: str,
    block: ParsedBlock,
) -> str:
    """Build an ID stable across retrieval/chunk parameter changes.

    The parser source anchor and normalized content are part of the identity.  A real
    source edit therefore creates a new ID while reranking or context expansion does
    not.
    """

    identity = {
        "document_version_id": str(document_version_id),
        "source_sha256": source_sha256,
        "kind": block.kind,
        "structural_path": block.structural_path,
        "source_anchor": block.source_anchor,
        "content_sha256": block.content_sha256,
    }
    return f"ev_{sha256_text(canonical_json(identity))}"


def retrieval_text(block: ParsedBlock, *, document_name: str) -> str:
    """Create deterministic retrieval enrichment without changing evidence text."""

    parts = [document_name, *block.title_path]
    if block.clause_ref:
        parts.append(block.clause_ref)
    if block.context_prefix:
        parts.append(block.context_prefix)
    parts.append(block.normalized_text)
    return "\n".join(dict.fromkeys(part for part in parts if normalize_text(part)))
