"""Deterministic IndexBuild preparation and atomic publication."""

from __future__ import annotations

import hashlib

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .domain import canonical_json
from .models import (
    ComparisonReport,
    DocumentVersion,
    IndexBuild,
    IndexBuildDocument,
    ParserRun,
)


def _validated_build_rows(build: IndexBuild) -> list[IndexBuildDocument]:
    rows = list(
        build.indexbuilddocument_set.select_related(
            "document_version__document",
            "parser_run__source_file",
        ).order_by("document_version_id")
    )
    if not rows:
        raise ValidationError("IndexBuild 至少需要一个 DocumentVersion")

    errors: list[str] = []
    for row in rows:
        try:
            row.full_clean()
        except ValidationError as exc:
            errors.extend(exc.messages)
            continue
        version = row.document_version
        try:
            review = version.authority_review
        except DocumentVersion.authority_review.RelatedObjectDoesNotExist:
            errors.append(f"{version} 尚未完成人工最终稿确认")
            continue
        current_hashes = {
            str(source.id): source.sha256 for source in version.source_files.all()
        }
        if review.reviewed_hashes != current_hashes:
            errors.append(f"{version} 的源文件哈希已偏离人工审核快照")
        if review.authoritative_source.document_version_id != version.id:
            errors.append(f"{version} 的权威源文件不属于该版本")

        parser_source = row.parser_run.source_file
        has_pair = version.source_files.filter(
            role__in=("structured_source", "executed_copy")
        ).count() >= 2
        if has_pair and not review.files_are_same_final_version:
            if parser_source.id != review.authoritative_source_id:
                errors.append(
                    f"{version} 未确认原件/PDF 同稿，只能发布权威源文件自身的解析结果"
                )
        comparison = version.comparison_reports.first()
        if (
            comparison is not None
            and comparison.status == ComparisonReport.Status.MISMATCH
            and parser_source.id != review.authoritative_source_id
        ):
            errors.append(f"{version} 存在关键差异，辅助文件解析结果不能发布")
        if row.parser_run.status != ParserRun.Status.SUCCEEDED:
            errors.append(f"{version} 的 ParserRun 未成功")
        if not row.parser_run.evidence_units.exists():
            errors.append(f"{version} 的 ParserRun 没有 Evidence Unit")

    if errors:
        raise ValidationError(errors)
    return rows


def build_evidence_manifest(build: IndexBuild) -> tuple[dict, ...]:
    rows = _validated_build_rows(build)
    manifest: list[dict] = []
    for row in rows:
        run = row.parser_run
        evidence = list(
            run.evidence_units.order_by("ordinal").values(
                "evidence_id", "content_sha256", "source_anchor", "ordinal"
            )
        )
        manifest.append(
            {
                "document_version_id": str(row.document_version_id),
                "document_kind": row.document_version.document.kind,
                "parser_run_id": str(run.id),
                "provider": f"{run.provider_name}@{run.provider_version}",
                "config_sha256": run.config_sha256,
                "source_file_id": str(run.source_file_id),
                "source_sha256": run.source_file.sha256,
                "evidence": evidence,
            }
        )
    return tuple(manifest)


def prepare_index_build(build_id) -> IndexBuild:
    with transaction.atomic():
        build = IndexBuild.objects.select_for_update().get(pk=build_id)
        if build.status not in {IndexBuild.Status.DRAFT, IndexBuild.Status.FAILED}:
            raise ValidationError("只有草稿或失败的 IndexBuild 可以重新准备")
        manifest = build_evidence_manifest(build)
        manifest_hash = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
        IndexBuild.objects.filter(pk=build.pk).update(
            status=IndexBuild.Status.READY,
            evidence_manifest_sha256=manifest_hash,
        )
    build.refresh_from_db()
    return build


def publish_index_build(build_id) -> IndexBuild:
    """Atomically switch one project's current published knowledge revision."""

    with transaction.atomic():
        build = IndexBuild.objects.select_for_update().select_related("project").get(
            pk=build_id
        )
        if build.status != IndexBuild.Status.READY:
            raise ValidationError("只有 READY IndexBuild 可以发布")
        manifest = build_evidence_manifest(build)
        current_hash = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
        if not build.evidence_manifest_sha256 or current_hash != build.evidence_manifest_sha256:
            raise ValidationError("Evidence Manifest 已变化，必须重新 prepare")

        now = timezone.now()
        previous_id = build.project.current_index_build_id
        if previous_id and previous_id != build.id:
            IndexBuild.objects.filter(pk=previous_id).update(status=IndexBuild.Status.RETIRED)
        version_ids = build.indexbuilddocument_set.values_list(
            "document_version_id", flat=True
        )
        DocumentVersion.objects.filter(pk__in=version_ids).exclude(
            status=DocumentVersion.Status.PUBLISHED
        ).update(status=DocumentVersion.Status.PUBLISHED, published_at=now)
        IndexBuild.objects.filter(pk=build.pk).update(
            status=IndexBuild.Status.PUBLISHED, published_at=now
        )
        type(build.project).objects.filter(pk=build.project_id).update(
            current_index_build=build
        )
    build.refresh_from_db()
    return build
