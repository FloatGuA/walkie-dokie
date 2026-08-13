"""Bounded, single-workflow Agentic Retrieval for contract factual QA."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, TypedDict

from asgiref.sync import sync_to_async
from django.utils import timezone
from langgraph.graph import END, START, StateGraph

from .answering import (
    AnswerDraft,
    ContractAnswerProvider,
    EvidenceItem,
    EvidenceVerifier,
    VerificationResult,
)
from .deepseek import DeepSeekContractAnswerProvider, DeepSeekEvidenceVerifier
from .models import (
    EvidenceUnit,
    IndexBuildDocument,
    KnowledgeProject,
    QuestionRun,
)
from .search import search_published_project


@dataclass(frozen=True, slots=True)
class SearchBundle:
    evidence: tuple[EvidenceItem, ...]
    retrieval_trace_id: str


class ContractSearchTool(Protocol):
    async def search(self, *, project_id: str, query: str, top_k: int) -> SearchBundle: ...


class DjangoPublishedContractSearch:
    """Hydrate only evidence from the server-selected published IndexBuild."""

    def __init__(self, *, actor_user=None):
        self._actor_user = actor_user

    async def search(self, *, project_id: str, query: str, top_k: int) -> SearchBundle:
        return await sync_to_async(self._search_sync, thread_sensitive=True)(
            project_id=project_id, query=query, top_k=top_k
        )

    def _search_sync(self, *, project_id: str, query: str, top_k: int) -> SearchBundle:
        result, trace = search_published_project(
            project_id,
            query=query,
            top_k=top_k,
            actor_user=self._actor_user,
        )
        project = KnowledgeProject.objects.select_related("current_index_build").get(
            pk=project_id
        )
        run_ids = IndexBuildDocument.objects.filter(
            index_build=project.current_index_build
        ).values_list("parser_run_id", flat=True)
        candidate_ids = [candidate.evidence_id for candidate in result.candidates]
        rows = EvidenceUnit.objects.filter(
            parser_run_id__in=run_ids,
            evidence_id__in=candidate_ids,
        ).select_related(
            "document_version__document", "source_file", "parser_run"
        )
        by_id = {row.evidence_id: row for row in rows}
        evidence: list[EvidenceItem] = []
        for candidate in result.candidates:
            row = by_id.get(candidate.evidence_id)
            if row is None:
                continue
            evidence.append(
                EvidenceItem(
                    evidence_id=row.evidence_id,
                    document=row.document_version.document.name,
                    document_version_id=str(row.document_version_id),
                    source_file=row.source_file.original_name,
                    text=row.text,
                    clause_ref=row.clause_ref,
                    page_physical=row.page_physical,
                    page_printed=row.page_printed,
                    source_anchor=row.source_anchor,
                    parser_warnings=tuple(row.parser_run.warnings),
                )
            )
        return SearchBundle(tuple(evidence), str(trace.id))


class ContractQAState(TypedDict, total=False):
    authorized_project_id: str
    question: str
    current_query: str
    previous_queries: list[str]
    attempt: int
    max_attempts: int
    top_k: int
    current_evidence: list[dict]
    accumulated_evidence: list[dict]
    retrieval_trace_ids: list[str]
    draft: dict
    verification: dict
    failure_reason: str
    rewrite_exhausted: bool
    result: dict


def _evidence_from_dict(value: dict) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=value["evidence_id"],
        document=value["document"],
        document_version_id=value["document_version_id"],
        source_file=value["source_file"],
        text=value["text"],
        clause_ref=value.get("clause_ref"),
        page_physical=value.get("page_physical"),
        page_printed=value.get("page_printed"),
        source_anchor=value.get("source_anchor") or {},
        parser_warnings=tuple(value.get("parser_warnings", ())),
    )


def _draft_from_dict(value: dict) -> AnswerDraft:
    from .answering import AtomicClaim

    return AnswerDraft(
        status=value["status"],
        answer=value.get("answer", ""),
        claims=tuple(
            AtomicClaim(item["claim"], tuple(item["evidence_ids"]))
            for item in value.get("claims", ())
        ),
        clarification_question=value.get("clarification_question"),
        refusal_reason=value.get("refusal_reason"),
    )


def build_contract_qa_graph(
    *,
    search_tool: ContractSearchTool,
    answer_provider: ContractAnswerProvider,
    verifier: EvidenceVerifier,
):
    async def retrieve(state: ContractQAState) -> dict:
        query = state.get("current_query") or state["question"]
        bundle = await search_tool.search(
            project_id=state["authorized_project_id"],
            query=query,
            top_k=state.get("top_k", 12),
        )
        existing = {
            item["evidence_id"]: item for item in state.get("accumulated_evidence", [])
        }
        for item in bundle.evidence:
            existing.setdefault(item.evidence_id, item.to_dict())
        return {
            "current_query": query,
            "previous_queries": [*state.get("previous_queries", []), query],
            "attempt": state.get("attempt", 0) + 1,
            "current_evidence": [item.to_dict() for item in bundle.evidence],
            "accumulated_evidence": list(existing.values()),
            "retrieval_trace_ids": [
                *state.get("retrieval_trace_ids", []),
                bundle.retrieval_trace_id,
            ],
            "failure_reason": "没有检索到候选证据" if not bundle.evidence else "",
        }

    async def draft_answer(state: ContractQAState) -> dict:
        evidence = tuple(
            _evidence_from_dict(item) for item in state.get("accumulated_evidence", [])
        )
        draft = await answer_provider.draft_answer(
            question=state["question"], evidence=evidence
        )
        return {"draft": draft.to_dict()}

    async def verify(state: ContractQAState) -> dict:
        evidence = tuple(
            _evidence_from_dict(item) for item in state.get("accumulated_evidence", [])
        )
        draft = _draft_from_dict(state["draft"])
        verdict = await verifier.verify(
            question=state["question"], draft=draft, evidence=evidence
        )
        return {
            "verification": verdict.to_dict(),
            "failure_reason": "" if verdict.supported else verdict.reason,
        }

    async def rewrite(state: ContractQAState) -> dict:
        evidence = tuple(
            _evidence_from_dict(item) for item in state.get("accumulated_evidence", [])
        )
        query = await answer_provider.rewrite_query(
            question=state["question"],
            previous_queries=tuple(state.get("previous_queries", ())),
            failure_reason=state.get("failure_reason") or "证据不足",
            evidence=evidence,
        )
        duplicate = query.casefold() in {
            item.casefold() for item in state.get("previous_queries", [])
        }
        return {"current_query": query, "rewrite_exhausted": duplicate}

    async def finalize(state: ContractQAState) -> dict:
        draft = _draft_from_dict(state["draft"])
        if draft.status == "clarification":
            return {
                "result": {
                    "status": "clarification",
                    "answer": draft.clarification_question,
                    "evidence": [],
                    "document": [],
                    "page_clause": [],
                    "calculation": None,
                    "confidence": {
                        "level": "not_applicable",
                        "basis": ["关键查询条件存在歧义，尚未形成事实回答"],
                    },
                }
            }
        if draft.status == "refused":
            return {
                "result": {
                    "status": "refused",
                    "answer": draft.refusal_reason,
                    "evidence": [],
                    "document": [],
                    "page_clause": [],
                    "calculation": None,
                    "confidence": {
                        "level": "not_applicable",
                        "basis": ["回答 Provider 判定现有证据不足"],
                    },
                }
            }

        evidence_by_id = {
            item["evidence_id"]: item
            for item in state.get("accumulated_evidence", [])
        }
        cited_ids = list(
            dict.fromkeys(
                evidence_id
                for claim in draft.claims
                for evidence_id in claim.evidence_ids
            )
        )
        cited = [evidence_by_id[item] for item in cited_ids]
        has_parser_warnings = any(item.get("parser_warnings") for item in cited)
        return {
            "result": {
                "status": "answered",
                "answer": draft.answer,
                "claims": [claim.to_dict() for claim in draft.claims],
                "evidence": cited,
                "document": list(dict.fromkeys(item["document"] for item in cited)),
                "page_clause": [
                    {
                        "evidence_id": item["evidence_id"],
                        "page_physical": item.get("page_physical"),
                        "page_printed": item.get("page_printed"),
                        "clause_ref": item.get("clause_ref"),
                        "source_anchor": item.get("source_anchor"),
                    }
                    for item in cited
                ],
                "calculation": None,
                "confidence": {
                    "level": "medium" if has_parser_warnings else "high",
                    "basis": [
                        "全部 Atomic Claim 已通过独立 Evidence Verifier",
                        "引用来自当前 Published IndexBuild",
                        *(
                            ["引用对应的 ParserRun 存在需要管理员关注的 warning"]
                            if has_parser_warnings
                            else []
                        ),
                    ],
                },
            }
        }

    async def refuse(state: ContractQAState) -> dict:
        reason = state.get("failure_reason") or "达到检索预算后仍无充分证据"
        return {
            "result": {
                "status": "refused",
                "answer": f"无法根据当前已发布文件可靠回答：{reason}",
                "evidence": [],
                "document": [],
                "page_clause": [],
                "calculation": None,
                "confidence": {
                    "level": "not_applicable",
                    "basis": ["达到检索预算后证据仍不足，系统已拒答"],
                },
            }
        }

    async def after_retrieve(state: ContractQAState) -> str:
        if state.get("current_evidence"):
            return "draft_answer"
        return (
            "rewrite"
            if state.get("attempt", 0) < state.get("max_attempts", 2)
            else "refuse"
        )

    async def after_draft(state: ContractQAState) -> str:
        return "verify" if state["draft"]["status"] == "answered" else "finalize"

    async def after_verify(state: ContractQAState) -> str:
        if state.get("verification", {}).get("supported"):
            return "finalize"
        return (
            "rewrite"
            if state.get("attempt", 0) < state.get("max_attempts", 2)
            else "refuse"
        )

    async def after_rewrite(state: ContractQAState) -> str:
        return "refuse" if state.get("rewrite_exhausted") else "retrieve"

    builder = StateGraph(ContractQAState)
    builder.add_node("retrieve", retrieve)
    builder.add_node("draft_answer", draft_answer)
    builder.add_node("verify", verify)
    builder.add_node("rewrite", rewrite)
    builder.add_node("finalize", finalize)
    builder.add_node("refuse", refuse)
    builder.add_edge(START, "retrieve")
    builder.add_conditional_edges("retrieve", after_retrieve)
    builder.add_conditional_edges("draft_answer", after_draft)
    builder.add_conditional_edges("verify", after_verify)
    builder.add_conditional_edges("rewrite", after_rewrite)
    builder.add_edge("finalize", END)
    builder.add_edge("refuse", END)
    return builder.compile()


async def ask_contract_question(
    *,
    authorized_project_id: str,
    question: str,
    actor_user=None,
    platform: str = "admin",
    actor_key: str | None = None,
    max_attempts: int = 2,
    top_k: int = 12,
    search_tool: ContractSearchTool | None = None,
    answer_provider: ContractAnswerProvider | None = None,
    verifier: EvidenceVerifier | None = None,
) -> dict:
    if not question.strip():
        raise ValueError("question 不能为空")
    if not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts 必须位于 1 到 3")

    project = await sync_to_async(
        KnowledgeProject.objects.select_related("current_index_build").get,
        thread_sensitive=True,
    )(pk=authorized_project_id, is_active=True)
    if project.current_index_build is None:
        raise LookupError("项目没有 Published IndexBuild")
    run = await sync_to_async(QuestionRun.objects.create, thread_sensitive=True)(
        project=project,
        index_build=project.current_index_build,
        actor_user=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        platform=platform,
        actor_key_hash=(
            hashlib.sha256(actor_key.encode("utf-8")).hexdigest() if actor_key else ""
        ),
        question=question.strip(),
    )
    answer = answer_provider or DeepSeekContractAnswerProvider()
    evidence_verifier = verifier or DeepSeekEvidenceVerifier()
    graph = build_contract_qa_graph(
        search_tool=search_tool or DjangoPublishedContractSearch(actor_user=actor_user),
        answer_provider=answer,
        verifier=evidence_verifier,
    )
    try:
        state = await graph.ainvoke(
            {
                "authorized_project_id": str(project.id),
                "question": question.strip(),
                "current_query": question.strip(),
                "previous_queries": [],
                "attempt": 0,
                "max_attempts": max_attempts,
                "top_k": top_k,
                "current_evidence": [],
                "accumulated_evidence": [],
                "retrieval_trace_ids": [],
                "rewrite_exhausted": False,
            }
        )
        result = state["result"]
        status = {
            "answered": QuestionRun.Status.ANSWERED,
            "refused": QuestionRun.Status.REFUSED,
            "clarification": QuestionRun.Status.CLARIFICATION,
        }[result["status"]]
        trace = {
            "answer_provider": f"{answer.name}@{answer.version}",
            "verifier": f"{evidence_verifier.name}@{evidence_verifier.version}",
            "queries": state.get("previous_queries", []),
            "attempts": state.get("attempt", 0),
            "retrieval_trace_ids": state.get("retrieval_trace_ids", []),
            "verification": state.get("verification", {}),
        }
        await sync_to_async(QuestionRun.objects.filter(pk=run.pk).update, thread_sensitive=True)(
            status=status,
            answer_payload=result,
            workflow_trace=trace,
            finished_at=timezone.now(),
        )
        return {**result, "question_run_id": str(run.id), "trace": trace}
    except Exception as exc:
        await sync_to_async(QuestionRun.objects.filter(pk=run.pk).update, thread_sensitive=True)(
            status=QuestionRun.Status.FAILED,
            error=f"{type(exc).__name__}: {exc}"[:8_000],
            finished_at=timezone.now(),
        )
        raise
