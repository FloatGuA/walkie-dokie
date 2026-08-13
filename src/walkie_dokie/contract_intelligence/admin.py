from __future__ import annotations

from django.contrib import admin, messages
from asgiref.sync import async_to_sync
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .ingestion import ingest_document_version
from .publication import prepare_index_build, publish_index_build
from .models import (
    AuthorityReview,
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


@admin.register(IndexBuild)
class IndexBuildAdmin(ProjectScopedAdminMixin, admin.ModelAdmin):
    project_path = "project"
    list_display = ("name", "project", "status", "evidence_manifest_sha256", "created_at")
    list_filter = ("status", "project")
    search_fields = ("name", "project__name")
    readonly_fields = ("id", "status", "evidence_manifest_sha256", "published_at", "created_at")
    inlines = (IndexBuildDocumentInline,)
    actions = ("prepare_builds", "publish_builds")

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
