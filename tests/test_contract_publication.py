from io import BytesIO

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from docx import Document as WordDocument

from walkie_dokie.contract_intelligence.ingestion import run_parser_for_source
from walkie_dokie.contract_intelligence.models import (
    AuthorityReview,
    Document,
    DocumentVersion,
    IndexBuild,
    IndexBuildDocument,
    KnowledgeProject,
    SourceFile,
)
from walkie_dokie.contract_intelligence.publication import (
    prepare_index_build,
    publish_index_build,
)


def _source_bytes() -> bytes:
    buffer = BytesIO()
    document = WordDocument()
    document.add_paragraph("第一条 服务费为人民币100元。")
    document.save(buffer)
    return buffer.getvalue()


def _draft_build(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    user = get_user_model().objects.create_user(username="reviewer")
    project = KnowledgeProject.objects.create(name="发布项目", slug="publish")
    document = Document.objects.create(
        project=project, name="合同", kind=Document.Kind.CONTRACT
    )
    version = DocumentVersion.objects.create(document=document, version_label="v1")
    source = SourceFile.objects.create(
        document_version=version,
        role=SourceFile.Role.STRUCTURED_SOURCE,
        file=SimpleUploadedFile("contract.docx", _source_bytes()),
    )
    run = run_parser_for_source(source.pk, raise_errors=True)
    AuthorityReview.objects.create(
        document_version=version,
        authoritative_source=source,
        reviewer=user,
        note="DOCX 是最终稿",
    )
    build = IndexBuild.objects.create(project=project, name="build-1")
    IndexBuildDocument.objects.create(
        index_build=build, document_version=version, parser_run=run
    )
    return project, version, build


@pytest.mark.django_db
def test_prepare_and_publish_switches_project_revision_atomically(settings, tmp_path):
    project, version, build = _draft_build(settings, tmp_path)

    prepared = prepare_index_build(build.pk)
    assert prepared.status == IndexBuild.Status.READY
    assert len(prepared.evidence_manifest_sha256) == 64

    published = publish_index_build(build.pk)

    project.refresh_from_db()
    version.refresh_from_db()
    assert published.status == IndexBuild.Status.PUBLISHED
    assert project.current_index_build_id == build.id
    assert version.status == DocumentVersion.Status.PUBLISHED


@pytest.mark.django_db
def test_prepare_refuses_version_without_authority_review(settings, tmp_path):
    project, version, build = _draft_build(settings, tmp_path)
    AuthorityReview.objects.filter(document_version=version).delete()

    with pytest.raises(ValidationError, match="人工最终稿确认"):
        prepare_index_build(build.pk)
