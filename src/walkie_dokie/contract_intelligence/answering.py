"""Provider-neutral answer, citation and verification contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

AnswerStatus = Literal["answered", "refused", "clarification"]


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    document: str
    document_version_id: str
    source_file: str
    text: str
    clause_ref: str | None = None
    page_physical: int | None = None
    page_printed: str | None = None
    source_anchor: dict | None = None
    parser_warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "document": self.document,
            "document_version_id": self.document_version_id,
            "source_file": self.source_file,
            "text": self.text,
            "clause_ref": self.clause_ref,
            "page_physical": self.page_physical,
            "page_printed": self.page_printed,
            "source_anchor": self.source_anchor or {},
            "parser_warnings": list(self.parser_warnings),
        }


@dataclass(frozen=True, slots=True)
class AtomicClaim:
    claim: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.claim.strip():
            raise ValueError("AtomicClaim 不能为空")
        if not self.evidence_ids:
            raise ValueError("AtomicClaim 必须引用至少一个 Evidence ID")

    def to_dict(self) -> dict:
        return {"claim": self.claim, "evidence_ids": list(self.evidence_ids)}


@dataclass(frozen=True, slots=True)
class AnswerDraft:
    status: AnswerStatus
    answer: str
    claims: tuple[AtomicClaim, ...] = ()
    clarification_question: str | None = None
    refusal_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status == "answered" and (not self.answer.strip() or not self.claims):
            raise ValueError("answered draft 必须包含 answer 和 atomic claims")
        if self.status == "clarification" and not self.clarification_question:
            raise ValueError("clarification draft 必须包含澄清问题")
        if self.status == "refused" and not self.refusal_reason:
            raise ValueError("refused draft 必须包含拒答原因")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "answer": self.answer,
            "claims": [claim.to_dict() for claim in self.claims],
            "clarification_question": self.clarification_question,
            "refusal_reason": self.refusal_reason,
        }


@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    claim: str
    supported: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "supported": self.supported,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class VerificationResult:
    supported: bool
    claims: tuple[ClaimVerdict, ...]
    reason: str

    def to_dict(self) -> dict:
        return {
            "supported": self.supported,
            "claims": [claim.to_dict() for claim in self.claims],
            "reason": self.reason,
        }


def validate_draft_citations(
    draft: AnswerDraft, evidence: tuple[EvidenceItem, ...]
) -> None:
    """Reject unknown IDs before an LLM verifier can express an opinion."""

    available = {item.evidence_id for item in evidence}
    for claim in draft.claims:
        unknown = set(claim.evidence_ids) - available
        if unknown:
            raise ValueError(f"AtomicClaim 引用了未检索到的 Evidence ID：{sorted(unknown)}")


@runtime_checkable
class ContractAnswerProvider(Protocol):
    name: str
    version: str

    async def draft_answer(
        self, *, question: str, evidence: tuple[EvidenceItem, ...]
    ) -> AnswerDraft: ...

    async def rewrite_query(
        self,
        *,
        question: str,
        previous_queries: tuple[str, ...],
        failure_reason: str,
        evidence: tuple[EvidenceItem, ...],
    ) -> str: ...


@runtime_checkable
class EvidenceVerifier(Protocol):
    name: str
    version: str

    async def verify(
        self,
        *,
        question: str,
        draft: AnswerDraft,
        evidence: tuple[EvidenceItem, ...],
    ) -> VerificationResult: ...
