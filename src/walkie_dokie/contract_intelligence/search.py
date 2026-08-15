"""Scope-enforcing retrieval application service."""

from __future__ import annotations

from django.db import transaction

from .models import (
    IndexBuild,
    IndexBuildDocument,
    KnowledgeProject,
    ParserRun,
    RetrievalTrace,
    RetrievalUnit,
)
from .retrieval import (
    LocalChineseBm25Retriever,
    RetrievalDocument,
    RetrievalProvider,
    RetrievalResult,
)


def _documents(queryset) -> tuple[RetrievalDocument, ...]:
    return tuple(
        RetrievalDocument(
            retrieval_id=unit.retrieval_id,
            evidence_id=unit.evidence.evidence_id,
            text=unit.retrieval_text,
            metadata=unit.metadata,
        )
        for unit in queryset.select_related("evidence")
    )


def _candidate_payload(result: RetrievalResult) -> list[dict]:
    return [
        {
            "rank": candidate.rank,
            "retrieval_id": candidate.retrieval_id,
            "evidence_id": candidate.evidence_id,
            "score": candidate.score,
            "stage_scores": candidate.stage_scores,
            "metadata": candidate.metadata,
        }
        for candidate in result.candidates
    ]


def search_parser_run(
    parser_run_id,
    *,
    query: str,
    top_k: int = 10,
    actor_user=None,
    provider: RetrievalProvider | None = None,
) -> tuple[RetrievalResult, RetrievalTrace]:
    """Admin-only draft search for inspecting one exact parser snapshot."""

    run = ParserRun.objects.select_related(
        "document_version__document__project"
    ).get(pk=parser_run_id)
    if run.status != ParserRun.Status.SUCCEEDED:
        raise ValueError("只有成功的 ParserRun 可以执行 retrieval test")
    units = RetrievalUnit.objects.filter(
        evidence__parser_run=run,
        metadata__excluded_by_default=False,
    ).order_by("evidence__ordinal")
    retriever = provider or LocalChineseBm25Retriever()
    result = retriever.search(query=query, documents=_documents(units), top_k=top_k)
    trace = RetrievalTrace.objects.create(
        project=run.document_version.document.project,
        parser_run=run,
        actor_user=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        query=query,
        provider_name=result.provider_name,
        provider_version=result.provider_version,
        parameters={"top_k": top_k, "scope": "parser_run"},
        query_tokens=list(result.query_tokens),
        candidates=_candidate_payload(result),
    )
    return result, trace


def search_published_project(
    authorized_project_id,
    *,
    query: str,
    top_k: int = 10,
    actor_user=None,
    provider: RetrievalProvider | None = None,
) -> tuple[RetrievalResult, RetrievalTrace]:
    """Search only the server-selected published revision of one authorized project."""

    project = KnowledgeProject.objects.select_related("current_index_build").get(
        pk=authorized_project_id, is_active=True
    )
    build = project.current_index_build
    if build is None or build.status != build.Status.PUBLISHED:
        raise LookupError("项目没有已发布的 IndexBuild")
    return _search_index_build(
        project=project,
        build=build,
        query=query,
        top_k=top_k,
        actor_user=actor_user,
        provider=provider,
    )


def search_index_build(
    authorized_project_id,
    index_build_id,
    *,
    query: str,
    top_k: int = 10,
    actor_user=None,
    provider: RetrievalProvider | None = None,
) -> tuple[RetrievalResult, RetrievalTrace]:
    """Search one immutable build snapshot pinned when the question started."""

    project = KnowledgeProject.objects.get(pk=authorized_project_id, is_active=True)
    build = IndexBuild.objects.get(pk=index_build_id, project=project)
    if build.status not in {IndexBuild.Status.PUBLISHED, IndexBuild.Status.RETIRED}:
        raise ValueError("问答只能查询已发布或已退役的 IndexBuild 快照")
    return _search_index_build(
        project=project,
        build=build,
        query=query,
        top_k=top_k,
        actor_user=actor_user,
        provider=provider,
    )


def _search_index_build(
    *,
    project: KnowledgeProject,
    build: IndexBuild,
    query: str,
    top_k: int,
    actor_user,
    provider: RetrievalProvider | None,
) -> tuple[RetrievalResult, RetrievalTrace]:
    run_ids = IndexBuildDocument.objects.filter(index_build=build).values_list(
        "parser_run_id", flat=True
    )
    units = RetrievalUnit.objects.filter(
        evidence__parser_run_id__in=run_ids,
        metadata__project_id=str(project.id),
        metadata__excluded_by_default=False,
    ).order_by("evidence__document_version_id", "evidence__ordinal")
    retriever = provider or LocalChineseBm25Retriever()
    result = retriever.search(query=query, documents=_documents(units), top_k=top_k)
    with transaction.atomic():
        trace = RetrievalTrace.objects.create(
            project=project,
            index_build=build,
            actor_user=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            query=query,
            provider_name=result.provider_name,
            provider_version=result.provider_version,
            parameters={"top_k": top_k, "scope": "published_index_build"},
            query_tokens=list(result.query_tokens),
            candidates=_candidate_payload(result),
        )
    return result, trace
