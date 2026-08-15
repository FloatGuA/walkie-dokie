from __future__ import annotations

import re
import json
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from asgiref.sync import async_to_sync
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .boq import (
    import_boq_spec,
    trust_boq_item_records,
    trust_boq_summary_records,
)
from .boq_search import (
    NumericConstraint,
    SimilarBoqSearchQuery,
    TextConstraint,
    extract_boq_item_parameters,
    extract_numeric_attributes,
    search_similar_boq_items,
)
from .ingestion import ingest_document_version
from .publication import prepare_index_build, publish_index_build
from .models import (
    AuthorityReview,
    BoqImportRun,
    BoqImportSpec,
    BoqItemRecord,
    BoqSheetSnapshot,
    BoqSummaryRecord,
    ComparisonReport,
    Document,
    DocumentVersion,
    EvidenceUnit,
    ExternalProjectBinding,
    EvaluationRun,
    GoldenCase,
    IndexBuild,
    IndexBuildDocument,
    KnowledgeProject,
    ParserRun,
    PriceImportRun,
    PriceMappingSpec,
    PriceRecord,
    ProjectMembership,
    QuestionRun,
    RetrievalTrace,
    RetrievalUnit,
    SourceFile,
)
from .pricing import import_price_mapping, trust_price_records


admin.site.site_header = "合同智能管理台"
admin.site.site_title = "合同智能"
admin.site.index_title = "项目、文档版本与证据检查"


class ProjectScopedAdminMixin:
    """Basic tenant visibility for trusted staff; superusers retain global access."""

    project_path: str

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        project_ids = ProjectMembership.objects.filter(user=request.user).values_list(
            "project_id", flat=True
        )
        if self.project_path == "self":
            return queryset.filter(pk__in=project_ids)
        return queryset.filter(**{f"{self.project_path}__in": project_ids}).distinct()


class ProjectMembershipInline(admin.TabularInline):
    model = ProjectMembership
    extra = 1
    autocomplete_fields = ("user",)


@admin.register(KnowledgeProject)
class KnowledgeProjectAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "self"
    list_display = (
        "name",
        "slug",
        "is_active",
        "current_index_build",
        "ask_link",
        "updated_at",
    )
    search_fields = ("name", "slug")
    list_filter = ("is_active",)
    readonly_fields = ("id", "created_at", "updated_at", "current_index_build")
    inlines = (ProjectMembershipInline,)

    @admin.display(description="MVP 问答")
    def ask_link(self, obj):
        if obj.current_index_build_id is None:
            return "尚未发布"
        return format_html(
            '<a href="{}">提问 / 查看证据</a>',
            reverse("admin:contract_project_ask", args=(obj.pk,)),
        )

    def get_urls(self):
        custom = [
            path(
                "<uuid:project_id>/ask/",
                self.admin_site.admin_view(self.ask_view),
                name="contract_project_ask",
            )
        ]
        return custom + super().get_urls()

    def ask_view(self, request, project_id):
        project = get_object_or_404(
            self.get_queryset(request).select_related("current_index_build"),
            pk=project_id,
        )
        question = ""
        result = None
        if request.method == "POST":
            question = request.POST.get("question", "").strip()
            if not question:
                self.message_user(request, "问题不能为空", messages.ERROR)
            elif project.current_index_build_id is None:
                self.message_user(request, "项目尚无 Published IndexBuild", messages.ERROR)
            else:
                from .agent import ask_intelligence_question

                try:
                    result = async_to_sync(ask_intelligence_question)(
                        authorized_project_id=str(project.id),
                        question=question,
                        actor_user=request.user,
                        platform="admin",
                    )
                except Exception as exc:
                    self.message_user(
                        request, f"问答运行失败：{type(exc).__name__}: {exc}", messages.ERROR
                    )
        return render(
            request,
            "admin/contract_intelligence/knowledgeproject/ask.html",
            {
                **self.admin_site.each_context(request),
                "title": f"高精度问答：{project}",
                "project": project,
                "question": question,
                "result": result,
                "opts": self.model._meta,
            },
        )


@admin.register(ProjectMembership)
class ProjectMembershipAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "project"
    list_display = ("project", "user", "role", "created_at")
    list_filter = ("role", "project")
    search_fields = ("project__name", "user__username")
    autocomplete_fields = ("project", "user")
    readonly_fields = ("created_at",)


@admin.register(Document)
class DocumentAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "project"
    list_display = ("name", "project", "kind", "updated_at")
    list_filter = ("kind", "project")
    search_fields = ("name", "project__name")
    autocomplete_fields = ("project",)
    readonly_fields = ("id", "created_at", "updated_at")


class SourceFileInline(admin.TabularInline):
    model = SourceFile
    extra = 1
    fields = (
        "role",
        "file",
        "original_name",
        "sha256",
        "size_bytes",
        "created_at",
    )
    readonly_fields = ("original_name", "sha256", "size_bytes", "created_at")


class AuthorityReviewInline(admin.StackedInline):
    model = AuthorityReview
    extra = 0
    max_num = 1
    fields = (
        "authoritative_source",
        "files_are_same_final_version",
        "reviewer",
        "note",
        "reviewed_hashes",
        "reviewed_at",
    )
    readonly_fields = ("reviewer", "reviewed_hashes", "reviewed_at")


@admin.register(DocumentVersion)
class DocumentVersionAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "document__project"
    list_display = (
        "document",
        "version_label",
        "status",
        "source_count",
        "evidence_count",
        "inspect_link",
        "created_at",
    )
    list_filter = ("status", "document__kind", "document__project")
    search_fields = ("document__name", "version_label")
    autocomplete_fields = ("document",)
    readonly_fields = ("id", "status", "created_by", "created_at", "published_at")
    inlines = (SourceFileInline, AuthorityReviewInline)
    actions = ("run_baseline_ingestion",)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for deleted in formset.deleted_objects:
            deleted.delete()
        for instance in instances:
            if isinstance(instance, AuthorityReview):
                instance.reviewer = request.user
            instance.save()
        formset.save_m2m()

    @admin.display(description="源文件数")
    def source_count(self, obj):
        return obj.source_files.count()

    @admin.display(description="证据数")
    def evidence_count(self, obj):
        return obj.evidence_units.count()

    @admin.display(description="检查")
    def inspect_link(self, obj):
        url = reverse("admin:contract_document_version_inspect", args=(obj.pk,))
        return format_html('<a href="{}">Chunk / Evidence</a>', url)

    @admin.action(description="运行 baseline ingestion（MVP 同步执行）")
    def run_baseline_ingestion(self, request, queryset):
        succeeded = 0
        failed = 0
        for version in queryset:
            try:
                runs = ingest_document_version(version.pk)
            except Exception as exc:
                failed += 1
                self.message_user(
                    request, f"{version}: 无法启动 ingestion：{exc}", level=messages.ERROR
                )
                continue
            succeeded += sum(run.status == ParserRun.Status.SUCCEEDED for run in runs)
            failed += sum(run.status == ParserRun.Status.FAILED for run in runs)
        self.message_user(
            request,
            f"解析完成：成功 {succeeded} 个源文件，失败 {failed} 个；详情见 ParserRun。",
            level=messages.WARNING if failed else messages.SUCCESS,
        )

    def get_urls(self):
        custom = [
            path(
                "<uuid:version_id>/inspect/",
                self.admin_site.admin_view(self.inspect_view),
                name="contract_document_version_inspect",
            )
        ]
        return custom + super().get_urls()

    def inspect_view(self, request, version_id):
        version = get_object_or_404(
            self.get_queryset(request).select_related("document__project"), pk=version_id
        )
        runs = version.parser_runs.select_related("source_file").order_by("-started_at")
        selected_run = None
        run_id = request.GET.get("run")
        if run_id:
            selected_run = runs.filter(pk=run_id).first()
            if selected_run is None:
                raise Http404("ParserRun 不属于该 DocumentVersion")
        if selected_run is None:
            selected_run = runs.filter(status=ParserRun.Status.SUCCEEDED).first() or runs.first()

        retrieval_result = None
        retrieval_trace = None
        retrieval_query = ""
        retrieval_top_k = 10
        if request.method == "POST":
            retrieval_query = request.POST.get("retrieval_q", "").strip()
            try:
                retrieval_top_k = int(request.POST.get("top_k", "10"))
            except ValueError:
                retrieval_top_k = 10
            if not retrieval_query:
                self.message_user(request, "Retrieval query 不能为空", level=messages.ERROR)
            elif selected_run is None:
                self.message_user(request, "请先运行成功的 ParserRun", level=messages.ERROR)
            else:
                from .search import search_parser_run

                try:
                    retrieval_result, retrieval_trace = search_parser_run(
                        selected_run.pk,
                        query=retrieval_query,
                        top_k=retrieval_top_k,
                        actor_user=request.user,
                    )
                except Exception as exc:
                    self.message_user(
                        request, f"Retrieval test 失败：{exc}", level=messages.ERROR
                    )

        evidence = EvidenceUnit.objects.none()
        if selected_run is not None:
            evidence = selected_run.evidence_units.select_related("source_file")
            query = request.GET.get("q", "").strip()
            kind = request.GET.get("kind", "").strip()
            if query:
                evidence = evidence.filter(
                    Q(normalized_text__icontains=query)
                    | Q(clause_ref__icontains=query)
                )
            if kind:
                evidence = evidence.filter(kind=kind)
        paginator = Paginator(evidence.order_by("ordinal"), 50)
        page = paginator.get_page(request.GET.get("page"))
        context = {
            **self.admin_site.each_context(request),
            "title": f"解析与证据检查：{version}",
            "version": version,
            "runs": runs,
            "selected_run": selected_run,
            "evidence_page": page,
            "kinds": (
                selected_run.evidence_units.order_by()
                .values_list("kind", flat=True)
                .distinct()
                if selected_run
                else ()
            ),
            "query": request.GET.get("q", ""),
            "selected_kind": request.GET.get("kind", ""),
            "comparison": version.comparison_reports.first(),
            "retrieval_result": retrieval_result,
            "retrieval_trace": retrieval_trace,
            "retrieval_query": retrieval_query,
            "retrieval_top_k": retrieval_top_k,
            "opts": self.model._meta,
        }
        return render(
            request,
            "admin/contract_intelligence/documentversion/inspect.html",
            context,
        )


@admin.register(SourceFile)
class SourceFileAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "document_version__document__project"
    list_display = (
        "original_name",
        "document_version",
        "role",
        "short_sha256",
        "size_bytes",
        "created_at",
    )
    list_filter = ("role", "document_version__document__project")
    search_fields = ("original_name", "sha256", "document_version__document__name")
    readonly_fields = (
        "id",
        "original_name",
        "sha256",
        "size_bytes",
        "created_at",
    )

    @admin.display(description="SHA-256")
    def short_sha256(self, obj):
        return f"{obj.sha256[:16]}…"


class ReadOnlyEvidenceAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ParserRun)
class ParserRunAdmin(ReadOnlyEvidenceAdmin):
    project_path = "document_version__document__project"
    list_display = (
        "provider_name",
        "provider_version",
        "source_file",
        "status",
        "evidence_count",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "provider_name", "document_version__document__project")
    search_fields = ("source_file__original_name", "document_version__document__name")

    @admin.display(description="证据数")
    def evidence_count(self, obj):
        return obj.evidence_units.count()


@admin.register(EvidenceUnit)
class EvidenceUnitAdmin(ReadOnlyEvidenceAdmin):
    project_path = "document_version__document__project"
    list_display = (
        "evidence_id_short",
        "document_version",
        "kind",
        "clause_ref",
        "page_physical",
        "ordinal",
    )
    list_filter = ("kind", "document_version__document__project")
    search_fields = ("evidence_id", "normalized_text", "clause_ref")

    @admin.display(description="Evidence ID")
    def evidence_id_short(self, obj):
        return f"{obj.evidence_id[:20]}…"


@admin.register(RetrievalUnit)
class RetrievalUnitAdmin(ReadOnlyEvidenceAdmin):
    project_path = "evidence__document_version__document__project"
    list_display = ("retrieval_id", "evidence", "created_at")
    search_fields = ("retrieval_id", "retrieval_text", "evidence__evidence_id")


@admin.register(RetrievalTrace)
class RetrievalTraceAdmin(ReadOnlyEvidenceAdmin):
    project_path = "project"
    list_display = (
        "provider_name",
        "provider_version",
        "project",
        "query",
        "parser_run",
        "index_build",
        "created_at",
    )
    list_filter = ("provider_name", "project")
    search_fields = ("query", "candidates")


@admin.register(QuestionRun)
class QuestionRunAdmin(ReadOnlyEvidenceAdmin):
    project_path = "project"
    list_display = (
        "status",
        "project",
        "question",
        "platform",
        "index_build",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "platform", "project")
    search_fields = ("question", "answer_payload", "workflow_trace")


@admin.register(ComparisonReport)
class ComparisonReportAdmin(ReadOnlyEvidenceAdmin):
    project_path = "document_version__document__project"
    list_display = ("document_version", "status", "summary", "created_at")
    list_filter = ("status", "document_version__document__project")


@admin.register(AuthorityReview)
class AuthorityReviewAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "document_version__document__project"
    list_display = (
        "document_version",
        "authoritative_source",
        "files_are_same_final_version",
        "reviewer",
        "reviewed_at",
    )
    readonly_fields = ("reviewed_hashes", "reviewed_at")

    def save_model(self, request, obj, form, change):
        obj.reviewer = request.user
        super().save_model(request, obj, form, change)


class IndexBuildDocumentInline(admin.TabularInline):
    model = IndexBuildDocument
    extra = 1
    autocomplete_fields = ("document_version", "parser_run")

    @staticmethod
    def _is_mutable(obj) -> bool:
        return obj is None or obj.status in {
            IndexBuild.Status.DRAFT,
            IndexBuild.Status.FAILED,
        }

    def has_add_permission(self, request, obj=None):
        return self._is_mutable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._is_mutable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._is_mutable(obj) and super().has_delete_permission(request, obj)


@admin.register(IndexBuild)
class IndexBuildAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "project"
    list_display = ("name", "project", "status", "evidence_manifest_sha256", "created_at")
    list_filter = ("status", "project")
    search_fields = ("name", "project__name")
    readonly_fields = ("id", "status", "evidence_manifest_sha256", "published_at", "created_at")
    inlines = (IndexBuildDocumentInline,)
    actions = ("prepare_builds", "publish_builds")

    @staticmethod
    def _is_mutable(obj) -> bool:
        return obj is None or obj.status in {
            IndexBuild.Status.DRAFT,
            IndexBuild.Status.FAILED,
        }

    def has_change_permission(self, request, obj=None):
        return self._is_mutable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        if self._is_mutable(obj):
            return self.readonly_fields
        return tuple(field.name for field in self.model._meta.fields)

    @admin.action(description="校验并准备 IndexBuild")
    def prepare_builds(self, request, queryset):
        for build in queryset:
            try:
                prepare_index_build(build.pk)
            except Exception as exc:
                self.message_user(request, f"{build}: prepare 失败：{exc}", messages.ERROR)
            else:
                self.message_user(request, f"{build}: 已生成 Evidence Manifest", messages.SUCCESS)

    @admin.action(description="原子发布 READY IndexBuild")
    def publish_builds(self, request, queryset):
        for build in queryset:
            try:
                publish_index_build(build.pk)
            except Exception as exc:
                self.message_user(request, f"{build}: 发布失败：{exc}", messages.ERROR)
            else:
                self.message_user(request, f"{build}: 已发布", messages.SUCCESS)


@admin.register(PriceMappingSpec)
class PriceMappingSpecAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "document_version__document__project"
    list_display = (
        "name",
        "version",
        "document_version",
        "sheet_name",
        "header_row",
        "data_start_row",
        "created_at",
    )
    list_filter = ("document_version__document__project", "sheet_name")
    search_fields = ("name", "document_version__document__name")
    readonly_fields = ("id", "created_by", "created_at")
    actions = ("run_price_import",)

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return self.readonly_fields
        return tuple(field.name for field in self.model._meta.fields)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.full_clean()
        super().save_model(request, obj, form, change)

    @admin.action(description="按 MappingSpec 导入到 Staging")
    def run_price_import(self, request, queryset):
        for spec in queryset:
            run = import_price_mapping(spec.pk)
            if run.status == PriceImportRun.Status.SUCCEEDED:
                self.message_user(
                    request,
                    f"{spec}: 导入 {run.imported_count} 条 Staging 价格记录",
                    messages.SUCCESS,
                )
            else:
                self.message_user(
                    request, f"{spec}: 导入失败：{run.error}", messages.ERROR
                )


@admin.register(PriceImportRun)
class PriceImportRunAdmin(ReadOnlyEvidenceAdmin):
    project_path = "mapping_spec__document_version__document__project"
    list_display = (
        "mapping_spec",
        "status",
        "imported_count",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "mapping_spec__document_version__document__project")


@admin.register(PriceRecord)
class PriceRecordAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "document_version__document__project"
    list_display = (
        "product_name",
        "product_code",
        "region",
        "unit_price",
        "currency",
        "unit",
        "status",
        "source_sheet",
        "source_row",
    )
    list_filter = (
        "status",
        "currency",
        "region",
        "document_version__document__project",
    )
    search_fields = ("product_name", "product_code", "customer", "notes")
    readonly_fields = (
        "id",
        "import_run",
        "document_version",
        "source_sheet",
        "source_row",
        "source_cells",
        "row_evidence",
        "header_evidence",
        "reviewed_by",
        "reviewed_at",
        "created_at",
    )
    actions = ("mark_trusted", "mark_rejected")

    def has_add_permission(self, request):
        return False

    @admin.action(description="人工确认选中记录为 Trusted")
    def mark_trusted(self, request, queryset):
        changed = trust_price_records(queryset, reviewer=request.user)
        self.message_user(request, f"已确认 {changed} 条可信价格记录", messages.SUCCESS)

    @admin.action(description="拒绝选中的 Staging 记录")
    def mark_rejected(self, request, queryset):
        changed = queryset.filter(status=PriceRecord.Status.STAGING).update(
            status=PriceRecord.Status.REJECTED,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f"已拒绝 {changed} 条价格记录", messages.WARNING)


@admin.register(BoqImportSpec)
class BoqImportSpecAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "document_version__document__project"
    list_display = (
        "name",
        "version",
        "profile",
        "project_name",
        "party_a_name",
        "party_a_group",
        "document_version",
        "created_at",
    )
    list_filter = ("profile", "document_version__document__project")
    search_fields = (
        "name",
        "project_name",
        "party_a_name",
        "party_a_group",
        "document_version__document__name",
    )
    readonly_fields = ("id", "created_by", "created_at")
    actions = ("run_boq_import",)

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return self.readonly_fields
        return tuple(field.name for field in self.model._meta.fields)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.full_clean()
        super().save_model(request, obj, form, change)

    @admin.action(description="按已验证模板导入工程量清单到 Staging")
    def run_boq_import(self, request, queryset):
        for spec in queryset:
            run = import_boq_spec(spec.pk)
            if run.status == BoqImportRun.Status.SUCCEEDED:
                self.message_user(
                    request,
                    (
                        f"{spec}: 导入 {run.imported_item_count} 条明细、"
                        f"{run.imported_summary_count} 条汇总记录"
                    ),
                    messages.SUCCESS,
                )
            else:
                self.message_user(
                    request, f"{spec}: 导入失败：{run.error}", messages.ERROR
                )


@admin.register(BoqImportRun)
class BoqImportRunAdmin(ReadOnlyEvidenceAdmin):
    project_path = "import_spec__document_version__document__project"
    list_display = (
        "import_spec",
        "status",
        "imported_sheet_count",
        "imported_item_count",
        "imported_summary_count",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "import_spec__document_version__document__project")


@admin.register(BoqSheetSnapshot)
class BoqSheetSnapshotAdmin(ReadOnlyEvidenceAdmin):
    project_path = "import_run__import_spec__document_version__document__project"
    list_display = (
        "source_sheet",
        "kind",
        "nonempty_row_count",
        "formula_count",
        "imported_record_count",
        "is_empty_template",
        "import_run",
    )
    list_filter = (
        "kind",
        "is_empty_template",
        "import_run__import_spec__document_version__document__project",
    )
    search_fields = ("source_sheet",)


class _BoqReviewAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "document_version__document__project"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)


_MODEL_HINT_RE = re.compile(
    r"(?:型号|编号)\s*[:：]\s*([^\s，,；;。\n]+)",
    re.IGNORECASE,
)


def _decimal_3(value):
    return "-" if value is None else f"{value:.3f}"


def _extract_model_hint(text: str) -> str:
    match = _MODEL_HINT_RE.search(text)
    return match.group(1).strip() if match else ""


def _source_item_search_initial(record: BoqItemRecord) -> dict:
    attributes = extract_numeric_attributes(record.item_description)
    power_w = attributes.powers_w[0] if len(attributes.powers_w) == 1 else None
    color_temperature_k = (
        attributes.color_temperatures_k[0]
        if not attributes.color_temperature_ranges_k
        and len(attributes.color_temperatures_k) == 1
        else None
    )
    return {
        "current_project": record.document_version.document.project_id,
        "unit": record.unit,
        "use_query": bool(record.item_name.strip()),
        "query": record.item_name,
        "use_model": False,
        "model_hint": _extract_model_hint(record.item_description),
        "use_specification": False,
        "specification_hint": record.item_description,
        "use_power": power_w is not None,
        "power_w": power_w,
        "use_color_temperature": color_temperature_k is not None,
        "color_temperature_k": color_temperature_k,
        "tolerance_percent": 10,
        "include_staging": False,
        "limit": 20,
    }


def _parameter_payload(record: BoqItemRecord) -> list[dict]:
    parameters = [
        {
            "key": parameter.key,
            "label": parameter.label,
            "kind": parameter.kind,
            "unit": parameter.unit,
            "value": str(parameter.value) if parameter.value is not None else None,
            "minimum": str(parameter.minimum) if parameter.minimum is not None else None,
            "maximum": str(parameter.maximum) if parameter.maximum is not None else None,
            "text": parameter.text,
            "raw_text": parameter.raw_text,
            "source": parameter.source,
            "searchable": parameter.searchable,
        }
        for parameter in extract_boq_item_parameters(
            record.item_name, record.item_description
        ).parameters
    ]
    return parameters


def _decimal_from_payload(value, label: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是数字") from exc


class SimilarBoqSearchForm(forms.Form):
    current_project = forms.ModelChoiceField(
        label="当前项目",
        queryset=KnowledgeProject.objects.none(),
    )
    unit = forms.CharField(
        label="计价单位",
        max_length=100,
        help_text="硬约束：例如 m、套、台。米/m、平方米/㎡会归一。",
    )
    use_query = forms.BooleanField(label="使用名称", required=False, initial=True)
    query = forms.CharField(label="名称关键词", max_length=500, required=False)
    use_model = forms.BooleanField(label="使用型号或编号", required=False)
    model_hint = forms.CharField(
        label="型号或编号",
        max_length=300,
        required=False,
        help_text="软匹配，不作精确等值过滤。",
    )
    use_specification = forms.BooleanField(label="使用规格描述", required=False)
    specification_hint = forms.CharField(
        label="其他规格关键词",
        widget=forms.Textarea(attrs={"rows": 5}),
        max_length=8_000,
        required=False,
        help_text="例如 IP67、DMX、Ra>90。",
    )
    use_power = forms.BooleanField(label="使用功率", required=False)
    power_w = forms.DecimalField(
        label="功率 W",
        required=False,
        min_value=0,
        max_digits=20,
        decimal_places=4,
    )
    use_color_temperature = forms.BooleanField(label="使用色温", required=False)
    color_temperature_k = forms.DecimalField(
        label="色温 K",
        required=False,
        min_value=0,
        max_digits=20,
        decimal_places=2,
    )
    tolerance_percent = forms.DecimalField(
        label="数值容差 %",
        initial=10,
        min_value=0,
        max_value=100,
        max_digits=6,
        decimal_places=2,
    )
    include_staging = forms.BooleanField(
        label="包含 Staging 待审核数据",
        required=False,
        help_text="仅管理员调试使用；飞书外部查询始终只使用 Trusted。",
    )
    limit = forms.IntegerField(
        label="最多返回",
        initial=20,
        min_value=1,
        max_value=100,
    )

    def __init__(
        self,
        *args,
        project_queryset,
        lock_current_project=False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.fields["current_project"].queryset = project_queryset
        self.fields["current_project"].disabled = lock_current_project

    def clean(self):
        cleaned = super().clean()
        selected = False
        for use_field, value_field, label in (
            ("use_query", "query", "名称"),
            ("use_model", "model_hint", "型号或编号"),
            ("use_specification", "specification_hint", "规格描述"),
            ("use_power", "power_w", "功率"),
            ("use_color_temperature", "color_temperature_k", "色温"),
        ):
            if not cleaned.get(use_field):
                continue
            selected = True
            value = cleaned.get(value_field)
            if value is None or (isinstance(value, str) and not value.strip()):
                self.add_error(value_field, f"已选择使用{label}，参数值不能为空")
        if not selected:
            raise forms.ValidationError(
                "至少选择名称、型号、规格、功率或色温中的一项"
            )
        return cleaned


@admin.register(BoqItemRecord)
class BoqItemRecordAdmin(_BoqReviewAdmin):
    change_list_template = (
        "admin/contract_intelligence/boqitemrecord/change_list.html"
    )
    list_display = (
        "project_name",
        "party_a_name",
        "party_a_group",
        "kind",
        "tower_name",
        "item_code",
        "item_name",
        "quantity_3",
        "unit_price_3",
        "total_price_3",
        "status",
        "source_sheet",
        "source_row",
        "similar_search_link",
    )
    list_filter = (
        "status",
        "kind",
        "tower_name",
        "project_name",
        "party_a_name",
        "party_a_group",
        "document_version__document__project",
    )
    search_fields = (
        "project_name",
        "party_a_name",
        "party_a_group",
        "item_code",
        "item_name",
        "item_description",
        "section_name",
    )
    actions = ("mark_trusted", "mark_rejected")

    def get_urls(self):
        custom = [
            path(
                "<uuid:item_id>/similar-search/",
                self.admin_site.admin_view(self.similar_search_view),
                name="contract_boq_item_similar_search",
            ),
            path(
                "<uuid:item_id>/similar-search-api/",
                self.admin_site.admin_view(self.similar_search_api),
                name="contract_boq_item_similar_search_api",
            ),
            path(
                "similar-search/",
                self.admin_site.admin_view(self.similar_search_view),
                name="contract_boq_similar_search",
            )
        ]
        return custom + super().get_urls()

    @admin.display(description="工程量", ordering="quantity")
    def quantity_3(self, obj):
        return _decimal_3(obj.quantity)

    @admin.display(description="不含税单价", ordering="unit_price")
    def unit_price_3(self, obj):
        return _decimal_3(obj.unit_price)

    @admin.display(description="不含税合价", ordering="total_price")
    def total_price_3(self, obj):
        return _decimal_3(obj.total_price)

    @admin.display(description="相似报价")
    def similar_search_link(self, obj):
        return format_html(
            '<a class="button js-similar-search" href="{}" data-similar-url="{}">查相似报价</a>',
            reverse("admin:contract_boq_item_similar_search", args=(obj.pk,)),
            reverse("admin:contract_boq_item_similar_search_api", args=(obj.pk,)),
        )

    def _similar_source_item(self, request, item_id):
        return get_object_or_404(
            self.get_queryset(request).select_related(
                "document_version__document__project"
            ),
            pk=item_id,
        )

    def similar_search_api(self, request, item_id):
        source_item = self._similar_source_item(request, item_id)
        if request.method != "GET":
            return JsonResponse({"error": "只支持 GET"}, status=405)
        if request.GET.get("search") != "1":
            return JsonResponse(
                {
                    "source": {
                        "id": str(source_item.pk),
                        "project_name": source_item.project_name,
                        "party_a_name": source_item.party_a_name,
                        "item_name": source_item.item_name,
                        "item_code": source_item.item_code,
                        "unit": source_item.unit,
                        "unit_price": str(source_item.unit_price) if source_item.unit_price is not None else None,
                        "description": source_item.item_description,
                    },
                    "parameters": _parameter_payload(source_item),
                }
            )
        try:
            numeric_constraints = tuple(
                NumericConstraint(
                    key=str(row["key"]),
                    value=(
                        _decimal_from_payload(row["value"], "数值参数")
                        if row.get("value") is not None
                        else None
                    ),
                    unit=str(row["unit"]),
                    tolerance_percent=_decimal_from_payload(row["tolerance"], "参数容差"),
                    minimum=(
                        _decimal_from_payload(row["minimum"], "数值范围下限")
                        if row.get("minimum") is not None
                        else None
                    ),
                    maximum=(
                        _decimal_from_payload(row["maximum"], "数值范围上限")
                        if row.get("maximum") is not None
                        else None
                    ),
                )
                for row in json.loads(request.GET.get("numeric", "[]"))
            )
            text_constraints = tuple(
                TextConstraint(key=str(row["key"]), value=str(row["value"]))
                for row in json.loads(request.GET.get("text", "[]"))
            )
            tolerance_percent = _decimal_from_payload(
                request.GET.get("tolerance", "10"), "默认容差"
            )
            query = SimilarBoqSearchQuery(
                current_project_id=str(source_item.document_version.document.project_id),
                kind=source_item.kind,
                query=request.GET.get("query", ""),
                unit=request.GET.get("unit", source_item.unit),
                model_hint=request.GET.get("model", ""),
                specification_hint=request.GET.get("specification", ""),
                numeric_constraints=numeric_constraints,
                text_constraints=text_constraints,
                tolerance_percent=tolerance_percent,
                include_staging=request.GET.get("include_staging") == "1",
                limit=int(request.GET.get("limit", "20")),
            )
            response = search_similar_boq_items(self.get_queryset(request), query)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse(
            {
                "party_a_group": response.party_a_group,
                "considered_count": response.considered_count,
                "matches": [
                    {
                        "score": str(match.score),
                        "project_name": match.record.project_name,
                        "party_a_name": match.record.party_a_name,
                        "item_name": match.record.item_name,
                        "item_code": match.record.item_code,
                        "unit": match.record.unit,
                        "unit_price": str(match.record.unit_price) if match.record.unit_price is not None else None,
                        "status": match.record.get_status_display(),
                        "source_sheet": match.record.source_sheet,
                        "source_row": match.record.source_row,
                        "change_url": reverse(
                            "admin:contract_intelligence_boqitemrecord_change",
                            args=(match.record.pk,),
                        ),
                        "evidence_url": reverse(
                            "admin:contract_intelligence_evidenceunit_change",
                            args=(match.record.row_evidence_id,),
                        ),
                        "description": match.record.item_description,
                        "matched_parameters": [
                            {
                                "key": key,
                                "value": str(value) if value is not None else None,
                                "delta_percent": str(delta) if delta is not None else None,
                            }
                            for key, value, delta in match.matched_parameters
                        ],
                    }
                    for match in response.matches
                ],
            }
        )

    def similar_search_view(self, request, item_id=None):
        source_item = None
        source_project_id = None
        initial = None
        if item_id is not None:
            source_item = self._similar_source_item(request, item_id)
            source_project_id = source_item.document_version.document.project_id
            initial = _source_item_search_initial(source_item)
        if request.user.is_superuser:
            project_filter = Q(is_active=True)
            if source_project_id is not None:
                project_filter |= Q(pk=source_project_id)
            project_queryset = KnowledgeProject.objects.filter(project_filter)
        else:
            project_queryset = KnowledgeProject.objects.filter(
                memberships__user=request.user,
            ).filter(
                Q(is_active=True) | Q(pk=source_project_id)
            ).distinct()
        submitted = request.GET.get("search") == "1"
        form = SimilarBoqSearchForm(
            request.GET if submitted else None,
            project_queryset=project_queryset,
            lock_current_project=source_item is not None,
            initial=initial,
        )
        response = None
        if submitted and form.is_valid():
            cleaned = form.cleaned_data
            try:
                response = search_similar_boq_items(
                    self.get_queryset(request),
                    SimilarBoqSearchQuery(
                        current_project_id=str(cleaned["current_project"].pk),
                        kind=(
                            source_item.kind
                            if source_item is not None
                            else BoqItemRecord.Kind.ENTITY
                        ),
                        query=(
                            cleaned.get("query", "")
                            if cleaned.get("use_query")
                            else ""
                        ),
                        unit=cleaned["unit"],
                        model_hint=(
                            cleaned.get("model_hint", "")
                            if cleaned.get("use_model")
                            else ""
                        ),
                        specification_hint=(
                            cleaned.get("specification_hint", "")
                            if cleaned.get("use_specification")
                            else ""
                        ),
                        power_w=(
                            cleaned.get("power_w")
                            if cleaned.get("use_power")
                            else None
                        ),
                        color_temperature_k=(
                            cleaned.get("color_temperature_k")
                            if cleaned.get("use_color_temperature")
                            else None
                        ),
                        tolerance_percent=cleaned["tolerance_percent"],
                        include_staging=cleaned.get("include_staging", False),
                        limit=cleaned["limit"],
                    ),
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
        return render(
            request,
            "admin/contract_intelligence/boqitemrecord/similar_search.html",
            {
                **self.admin_site.each_context(request),
                "title": "跨项目相似报价搜索",
                "opts": self.model._meta,
                "form": form,
                "response": response,
                "submitted": submitted,
                "source_item": source_item,
            },
        )

    @admin.action(description="人工确认选中工程量清单明细为 Trusted")
    def mark_trusted(self, request, queryset):
        changed = trust_boq_item_records(queryset, reviewer=request.user)
        self.message_user(request, f"已确认 {changed} 条可信明细", messages.SUCCESS)

    @admin.action(description="拒绝选中的 Staging 工程量清单明细")
    def mark_rejected(self, request, queryset):
        changed = queryset.filter(status=BoqItemRecord.Status.STAGING).update(
            status=BoqItemRecord.Status.REJECTED,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f"已拒绝 {changed} 条明细", messages.WARNING)


@admin.register(BoqSummaryRecord)
class BoqSummaryRecordAdmin(_BoqReviewAdmin):
    list_display = (
        "project_name",
        "party_a_name",
        "party_a_group",
        "kind",
        "summary_name",
        "amount_3",
        "rate_3",
        "status",
        "source_sheet",
        "source_row",
    )
    list_filter = (
        "status",
        "kind",
        "project_name",
        "party_a_name",
        "party_a_group",
        "document_version__document__project",
    )
    search_fields = (
        "project_name",
        "party_a_name",
        "party_a_group",
        "summary_name",
    )
    actions = ("mark_trusted", "mark_rejected")

    @admin.display(description="金额", ordering="amount")
    def amount_3(self, obj):
        return _decimal_3(obj.amount)

    @admin.display(description="比例", ordering="rate")
    def rate_3(self, obj):
        return _decimal_3(obj.rate)

    @admin.action(description="人工确认选中工程量清单汇总为 Trusted")
    def mark_trusted(self, request, queryset):
        changed = trust_boq_summary_records(queryset, reviewer=request.user)
        self.message_user(request, f"已确认 {changed} 条可信汇总", messages.SUCCESS)

    @admin.action(description="拒绝选中的 Staging 工程量清单汇总")
    def mark_rejected(self, request, queryset):
        changed = queryset.filter(status=BoqSummaryRecord.Status.STAGING).update(
            status=BoqSummaryRecord.Status.REJECTED,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f"已拒绝 {changed} 条汇总", messages.WARNING)


@admin.register(ExternalProjectBinding)
class ExternalProjectBindingAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "project"
    list_display = (
        "platform",
        "subject_type",
        "subject_id",
        "project",
        "is_active",
        "is_selected",
        "created_at",
    )
    list_filter = ("platform", "subject_type", "is_active", "is_selected", "project")
    search_fields = ("subject_id", "project__name", "project__slug")
    readonly_fields = ("id", "created_at")

    def save_model(self, request, obj, form, change):
        obj.full_clean()
        super().save_model(request, obj, form, change)


@admin.register(GoldenCase)
class GoldenCaseAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "project"
    list_display = (
        "question",
        "project",
        "expected_status",
        "is_verified",
        "reviewed_by",
        "reviewed_at",
    )
    list_filter = ("expected_status", "is_verified", "project")
    search_fields = ("question", "expected_answer_contains", "expected_evidence_ids")
    readonly_fields = ("id", "reviewed_by", "reviewed_at", "created_at")
    actions = ("verify_cases", "run_project_evaluation")

    def save_model(self, request, obj, form, change):
        obj.full_clean()
        super().save_model(request, obj, form, change)

    @admin.action(description="人工确认选中的 GoldenCase")
    def verify_cases(self, request, queryset):
        count = 0
        for case in queryset:
            try:
                case.full_clean()
            except Exception as exc:
                self.message_user(request, f"{case}: 校验失败：{exc}", messages.ERROR)
                continue
            case.is_verified = True
            case.reviewed_by = request.user
            case.reviewed_at = timezone.now()
            case.save(update_fields=("is_verified", "reviewed_by", "reviewed_at"))
            count += 1
        self.message_user(request, f"已人工确认 {count} 条 GoldenCase", messages.SUCCESS)

    @admin.action(description="按所选样本涉及的项目运行评估")
    def run_project_evaluation(self, request, queryset):
        from .evaluation import run_golden_evaluation

        project_ids = list(queryset.values_list("project_id", flat=True).distinct())
        for project_id in project_ids:
            try:
                run = async_to_sync(run_golden_evaluation)(project_id)
            except Exception as exc:
                self.message_user(request, f"评估失败：{exc}", messages.ERROR)
            else:
                self.message_user(
                    request, f"评估完成 {run.id}：{run.metrics}", messages.SUCCESS
                )


@admin.register(EvaluationRun)
class EvaluationRunAdmin(ReadOnlyEvidenceAdmin):
    project_path = "project"
    list_display = (
        "project",
        "index_build",
        "status",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "project")
    search_fields = ("metrics", "case_results", "error")
