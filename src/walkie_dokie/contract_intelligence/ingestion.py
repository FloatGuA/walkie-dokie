"""Application service for repeatable parser runs and evidence persistence.

The functions are synchronous so they can be called by a management command today and
by a Celery task later.  They do not depend on an HTTP request or a platform adapter.
"""

from __future__ import annotations

import hashlib
import re
import traceback
from difflib import SequenceMatcher
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from .domain import canonical_json, normalize_text, retrieval_text, stable_evidence_id
from .models import (
    ComparisonReport,
    DocumentVersion,
    EvidenceUnit,
    ParserRun,
    RetrievalUnit,
    SourceFile,
)
from .parsers import baseline_parser_registry
from .providers import ParserRegistry

_HIGH_RISK_TOKEN_RE = re.compile(
    r"(?:第[〇零一二三四五六七八九十百千万两0-9]+条(?:之[一二三四五六七八九十0-9]+)?"
    r"|(?:人民币|RMB|CNY|￥|¥)?\s*-?\d[\d,]*(?:\.\d+)?\s*(?:%|元|万元|亿元|天|日|月|年)?"
    r"|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)"
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}".strip()
    return message[:8_000]


def run_parser_for_source(
    source_file_id,
    *,
    provider_name: str | None = None,
    config: dict | None = None,
    registry: ParserRegistry | None = None,
    raise_errors: bool = False,
) -> ParserRun:
    """Run one explicit parser and persist a complete, inspectable snapshot."""

    source = SourceFile.objects.select_related(
        "document_version__document__project"
    ).get(pk=source_file_id)
    version = source.document_version
    if version.status == DocumentVersion.Status.PUBLISHED:
        raise RuntimeError("已发布 DocumentVersion 不允许创建新的 ParserRun")
    path = Path(source.file.path)
    parser_registry = registry or baseline_parser_registry()
    provider = parser_registry.resolve(path, provider_name=provider_name)
    effective_config = dict(config or {})

    run = ParserRun.objects.create(
        document_version=version,
        source_file=source,
        provider_name=provider.name,
        provider_version=provider.version,
        config=effective_config,
        config_sha256=_sha256(canonical_json(effective_config)),
        status=ParserRun.Status.RUNNING,
    )
    DocumentVersion.objects.filter(pk=version.pk).update(
        status=DocumentVersion.Status.PROCESSING
    )

    try:
        result = provider.parse(path)
        evidence_rows: list[EvidenceUnit] = []
        retrieval_rows: list[RetrievalUnit] = []
        for block in result.blocks:
            evidence_key = stable_evidence_id(
                document_version_id=str(version.id),
                source_sha256=source.sha256,
                block=block,
            )
            evidence = EvidenceUnit(
                evidence_id=evidence_key,
                document_version=version,
                parser_run=run,
                source_file=source,
                kind=block.kind,
                ordinal=block.ordinal,
                structural_path=list(block.structural_path),
                title_path=list(block.title_path),
                clause_ref=block.clause_ref,
                source_anchor=block.source_anchor,
                page_physical=block.page_physical,
                page_printed=block.page_printed,
                text=block.text,
                normalized_text=block.normalized_text,
                context_prefix=block.context_prefix,
                content_sha256=block.content_sha256,
                metadata=block.metadata,
            )
            evidence_rows.append(evidence)
            retrieval_rows.append(
                RetrievalUnit(
                    retrieval_id=f"rt_{_sha256(f'{run.id}:{evidence_key}')}",
                    evidence=evidence,
                    retrieval_text=retrieval_text(
                        block, document_name=version.document.name
                    ),
                    metadata={
                        "project_id": str(version.document.project_id),
                        "document_version_id": str(version.id),
                        "evidence_id": evidence_key,
                        "source_role": source.role,
                        "excluded_by_default": bool(
                            block.metadata.get("excluded_by_default", False)
                        ),
                    },
                )
            )

        with transaction.atomic():
            EvidenceUnit.objects.bulk_create(evidence_rows, batch_size=500)
            RetrievalUnit.objects.bulk_create(retrieval_rows, batch_size=500)
            ParserRun.objects.filter(pk=run.pk).update(
                status=ParserRun.Status.SUCCEEDED,
                provider_name=result.provider_name,
                provider_version=result.provider_version,
                metadata={**result.metadata, "evidence_count": len(evidence_rows)},
                warnings=list(result.warnings),
                finished_at=timezone.now(),
            )
            DocumentVersion.objects.filter(pk=version.pk).update(
                status=DocumentVersion.Status.REVIEW
            )
        run.refresh_from_db()
        _create_comparison_if_ready(version.id)
        return run
    except Exception as exc:
        ParserRun.objects.filter(pk=run.pk).update(
            status=ParserRun.Status.FAILED,
            error=_bounded_error(exc),
            metadata={"traceback": traceback.format_exc(limit=20)[-12_000:]},
            finished_at=timezone.now(),
        )
        has_success = ParserRun.objects.filter(
            document_version=version, status=ParserRun.Status.SUCCEEDED
        ).exists()
        DocumentVersion.objects.filter(pk=version.pk).update(
            status=(
                DocumentVersion.Status.REVIEW
                if has_success
                else DocumentVersion.Status.FAILED
            )
        )
        run.refresh_from_db()
        if raise_errors:
            raise
        return run


def ingest_document_version(
    document_version_id,
    *,
    registry: ParserRegistry | None = None,
) -> tuple[ParserRun, ...]:
    version = DocumentVersion.objects.prefetch_related("source_files").get(
        pk=document_version_id
    )
    if version.status == DocumentVersion.Status.PUBLISHED:
        raise RuntimeError("已发布 DocumentVersion 不允许重新 ingestion")
    runs = [
        run_parser_for_source(source.id, registry=registry)
        for source in version.source_files.all()
    ]
    return tuple(runs)


def _latest_successful_run(source: SourceFile) -> ParserRun | None:
    return (
        source.parser_runs.filter(status=ParserRun.Status.SUCCEEDED)
        .order_by("-finished_at")
        .first()
    )


def _high_risk_tokens(text: str) -> set[str]:
    return {normalize_text(match.group(0)) for match in _HIGH_RISK_TOKEN_RE.finditer(text)}


def _create_comparison_if_ready(document_version_id) -> ComparisonReport | None:
    version = DocumentVersion.objects.prefetch_related("source_files").get(
        pk=document_version_id
    )
    source_by_role = {source.role: source for source in version.source_files.all()}
    structured = source_by_role.get(SourceFile.Role.STRUCTURED_SOURCE)
    executed = source_by_role.get(SourceFile.Role.EXECUTED_COPY)
    if structured is None or executed is None:
        return None
    structured_run = _latest_successful_run(structured)
    executed_run = _latest_successful_run(executed)
    if structured_run is None or executed_run is None:
        return None

    left = "\n".join(
        structured_run.evidence_units.order_by("ordinal").values_list(
            "normalized_text", flat=True
        )
    )
    right = "\n".join(
        executed_run.evidence_units.order_by("ordinal").values_list(
            "normalized_text", flat=True
        )
    )
    if not left or not right:
        status = ComparisonReport.Status.WARNING
        similarity = 0.0
        missing_left: list[str] = []
        missing_right: list[str] = []
        summary = "至少一份文件没有可靠文字层，无法完成机器一致性检查"
    else:
        similarity = SequenceMatcher(None, left, right, autojunk=False).ratio()
        left_tokens = _high_risk_tokens(left)
        right_tokens = _high_risk_tokens(right)
        missing_left = sorted(right_tokens - left_tokens)
        missing_right = sorted(left_tokens - right_tokens)
        if missing_left or missing_right:
            status = ComparisonReport.Status.MISMATCH
            summary = "检测到金额、日期、比例或条款号等高风险 token 差异"
        elif similarity >= 0.90:
            status = ComparisonReport.Status.MATCH
            summary = "机器检查未检测到实质差异；仍不等同于法律一致性证明"
        elif similarity >= 0.70:
            status = ComparisonReport.Status.WARNING
            summary = "文字相似但版式/提取差异较大，需要人工复核"
        else:
            status = ComparisonReport.Status.MISMATCH
            summary = "文本差异超过 baseline 阈值，需要上传正确文件或选择单一权威源"

    return ComparisonReport.objects.create(
        document_version=version,
        structured_source=structured,
        executed_copy=executed,
        status=status,
        summary=summary,
        metrics={
            "text_similarity": round(similarity, 6),
            "structured_parser_run_id": str(structured_run.id),
            "executed_parser_run_id": str(executed_run.id),
            "algorithm": "sequence_matcher_plus_high_risk_token_set_v0.1",
            "disclaimer": "启发式机器检查，不构成法律一致性证明",
        },
        differences=[
            {"side": "structured_source", "missing_tokens": missing_left},
            {"side": "executed_copy", "missing_tokens": missing_right},
        ],
    )
