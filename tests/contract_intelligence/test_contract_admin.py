import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from walkie_dokie.contract_intelligence.admin import IndexBuildDocumentInline
from walkie_dokie.contract_intelligence.models import (
    BoqImportSpec,
    IndexBuild,
    KnowledgeProject,
    PriceMappingSpec,
)


@pytest.mark.django_db
def test_admin_makes_prepared_build_and_its_inline_snapshot_read_only():
    user = get_user_model().objects.create_superuser(
        username="build-admin",
        email="build-admin@example.com",
        password="safe-test-password",
    )
    request = RequestFactory().get("/admin/")
    request.user = user
    project = KnowledgeProject.objects.create(name="后台项目", slug="admin-build")
    draft = IndexBuild.objects.create(project=project, name="draft")
    ready = IndexBuild.objects.create(
        project=project,
        name="ready",
        status=IndexBuild.Status.READY,
    )
    model_admin = admin.site._registry[IndexBuild]
    inline = IndexBuildDocumentInline(IndexBuild, admin.site)

    assert model_admin.has_change_permission(request, draft) is True
    assert model_admin.has_change_permission(request, ready) is False
    assert {"name", "project", "parser_configuration"}.issubset(
        model_admin.get_readonly_fields(request, ready)
    )
    assert inline.has_add_permission(request, ready) is False
    assert inline.has_change_permission(request, ready) is False
    assert inline.has_delete_permission(request, ready) is False


@pytest.mark.django_db
def test_admin_requires_a_new_price_mapping_spec_version_for_changes():
    user = get_user_model().objects.create_superuser(
        username="mapping-admin",
        email="mapping-admin@example.com",
        password="safe-test-password",
    )
    request = RequestFactory().get("/admin/")
    request.user = user
    model_admin = admin.site._registry[PriceMappingSpec]
    existing = PriceMappingSpec()

    assert model_admin.has_change_permission(request, existing) is False
    assert model_admin.has_delete_permission(request, existing) is False
    assert {"name", "version", "field_columns"}.issubset(
        model_admin.get_readonly_fields(request, existing)
    )


@pytest.mark.django_db
def test_admin_requires_a_new_boq_import_spec_version_for_changes():
    user = get_user_model().objects.create_superuser(
        username="boq-admin",
        email="boq-admin@example.com",
        password="safe-test-password",
    )
    request = RequestFactory().get("/admin/")
    request.user = user
    model_admin = admin.site._registry[BoqImportSpec]
    existing = BoqImportSpec()

    assert model_admin.has_change_permission(request, existing) is False
    assert model_admin.has_delete_permission(request, existing) is False
    assert {"project_name", "party_a_name", "party_a_group", "profile"}.issubset(
        model_admin.get_readonly_fields(request, existing)
    )
