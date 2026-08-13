"""Django persistence model for the contract-intelligence bounded context."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


def _safe_filename(value: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip()
    return name if name not in {"", ".", ".."} else "source"


def source_upload_path(instance: "SourceFile", filename: str) -> str:
    safe_name = _safe_filename(filename)
    return (
        f"projects/{instance.document_version.document.project_id}/"
        f"versions/{instance.document_version_id}/{instance.id}-{safe_name}"
    )


class KnowledgeProject(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    current_index_build = models.ForeignKey(
        "IndexBuild",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_by_projects",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "知识库项目"
        verbose_name_plural = "知识库项目"
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class ProjectMembership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "管理员"
        QUERY = "query", "仅查询"

    project = models.ForeignKey(
        KnowledgeProject, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="contract_project_memberships",
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.QUERY)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "项目成员"
        verbose_name_plural = "项目成员"
        constraints = [
            models.UniqueConstraint(
                fields=("project", "user"), name="contract_unique_project_member"
            )
        ]

    def __str__(self) -> str:
        return f"{self.project} / {self.user} ({self.get_role_display()})"


class Document(models.Model):
    class Kind(models.TextChoices):
        CONTRACT = "contract", "合同"
        PRICE_LIST = "price_list", "价目表"
        ATTACHMENT = "attachment", "附件"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        KnowledgeProject, on_delete=models.PROTECT, related_name="documents"
    )
    name = models.CharField(max_length=300)
    kind = models.CharField(max_length=30, choices=Kind.choices)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "逻辑文档"
        verbose_name_plural = "逻辑文档"
        ordering = ("project__name", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("project", "name"), name="contract_unique_document_name"
            )
        ]

    def __str__(self) -> str:
        return f"{self.project} / {self.name}"


class DocumentVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PROCESSING = "processing", "解析中"
        REVIEW = "review", "待复核"
        READY = "ready", "可构建索引"
        FAILED = "failed", "失败"
        PUBLISHED = "published", "已发布"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        Document, on_delete=models.PROTECT, related_name="versions"
    )
    version_label = models.CharField(max_length=120)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_contract_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "不可变文档版本"
        verbose_name_plural = "不可变文档版本"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("document", "version_label"),
                name="contract_unique_document_version",
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).only("status").first()
            if old is not None and old.status == self.Status.PUBLISHED:
                raise ValidationError("已发布 DocumentVersion 不允许原地修改")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.PUBLISHED:
            raise ValidationError("已发布 DocumentVersion 不允许删除")
        return super().delete(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.document} / {self.version_label}"


class SourceFile(models.Model):
    class Role(models.TextChoices):
        STRUCTURED_SOURCE = "structured_source", "结构化原件"
        EXECUTED_COPY = "executed_copy", "正式 PDF"
        ATTACHMENT = "attachment", "附件"
        DERIVED_RENDER = "derived_render", "系统渲染快照"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document_version = models.ForeignKey(
        DocumentVersion, on_delete=models.PROTECT, related_name="source_files"
    )
    role = models.CharField(max_length=30, choices=Role.choices)
    file = models.FileField(upload_to=source_upload_path, max_length=500)
    original_name = models.CharField(max_length=300, editable=False)
    mime_type = models.CharField(max_length=150, blank=True)
    sha256 = models.CharField(max_length=64, editable=False, db_index=True)
    size_bytes = models.PositiveBigIntegerField(editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "源文件"
        verbose_name_plural = "源文件"
        ordering = ("role", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("document_version", "role"),
                condition=models.Q(
                    role__in=("structured_source", "executed_copy", "derived_render")
                ),
                name="contract_one_primary_source_per_role",
            )
        ]

    def clean(self) -> None:
        super().clean()
        filename = self.file.name if self.file else self.original_name
        suffix = Path(filename).suffix.casefold()
        allowed = {".docx", ".xlsx", ".pdf"}
        if suffix not in allowed:
            raise ValidationError({"file": "MVP 只接受 DOCX、XLSX 或 PDF"})
        if self.role == self.Role.STRUCTURED_SOURCE and suffix not in {".docx", ".xlsx"}:
            raise ValidationError({"file": "结构化原件必须是 DOCX 或 XLSX"})
        if self.role in {self.Role.EXECUTED_COPY, self.Role.DERIVED_RENDER} and suffix != ".pdf":
            raise ValidationError({"file": "正式副本或渲染快照必须是 PDF"})
        if self.document_version_id and self.document_version.status == DocumentVersion.Status.PUBLISHED:
            raise ValidationError("已发布版本不允许增加或替换源文件")

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("SourceFile 不允许原地修改；请创建新的 DocumentVersion")
        self.original_name = _safe_filename(self.file.name)
        digest = hashlib.sha256()
        size = 0
        self.file.open("rb")
        for chunk in self.file.chunks():
            digest.update(chunk)
            size += len(chunk)
        self.file.seek(0)
        self.sha256 = digest.hexdigest()
        self.size_bytes = size
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.document_version.status == DocumentVersion.Status.PUBLISHED:
            raise ValidationError("已发布版本的源文件不允许删除")
        return super().delete(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.get_role_display()} / {self.original_name}"


class ParserRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "运行中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document_version = models.ForeignKey(
        DocumentVersion, on_delete=models.PROTECT, related_name="parser_runs"
    )
    source_file = models.ForeignKey(
        SourceFile, on_delete=models.PROTECT, related_name="parser_runs"
    )
    provider_name = models.CharField(max_length=100)
    provider_version = models.CharField(max_length=100)
    config = models.JSONField(default=dict)
    config_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=20, choices=Status.choices)
    metadata = models.JSONField(default=dict)
    warnings = models.JSONField(default=list)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "解析运行"
        verbose_name_plural = "解析运行"
        ordering = ("-started_at",)

    def __str__(self) -> str:
        return f"{self.provider_name}@{self.provider_version} / {self.source_file}"


class EvidenceUnit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    evidence_id = models.CharField(max_length=67, db_index=True)
    document_version = models.ForeignKey(
        DocumentVersion, on_delete=models.PROTECT, related_name="evidence_units"
    )
    parser_run = models.ForeignKey(
        ParserRun, on_delete=models.PROTECT, related_name="evidence_units"
    )
    source_file = models.ForeignKey(
        SourceFile, on_delete=models.PROTECT, related_name="evidence_units"
    )
    kind = models.CharField(max_length=50)
    ordinal = models.PositiveIntegerField()
    structural_path = models.JSONField(default=list)
    title_path = models.JSONField(default=list)
    clause_ref = models.CharField(max_length=100, null=True, blank=True)
    source_anchor = models.JSONField()
    page_physical = models.PositiveIntegerField(null=True, blank=True)
    page_printed = models.CharField(max_length=50, null=True, blank=True)
    text = models.TextField()
    normalized_text = models.TextField()
    context_prefix = models.TextField(blank=True)
    content_sha256 = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "证据单元"
        verbose_name_plural = "证据单元"
        ordering = ("parser_run", "ordinal")
        constraints = [
            models.UniqueConstraint(
                fields=("parser_run", "evidence_id"),
                name="contract_unique_run_evidence",
            )
        ]

    def __str__(self) -> str:
        location = self.clause_ref or self.page_physical or self.ordinal
        return f"{self.evidence_id[:15]}… / {location}"


class RetrievalUnit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    retrieval_id = models.CharField(max_length=67, unique=True)
    evidence = models.OneToOneField(
        EvidenceUnit, on_delete=models.PROTECT, related_name="retrieval_unit"
    )
    retrieval_text = models.TextField()
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "检索单元"
        verbose_name_plural = "检索单元"
        ordering = ("evidence__ordinal",)

    def __str__(self) -> str:
        return self.retrieval_id


class RetrievalTrace(models.Model):
    """Replayable retrieval inputs and outputs; candidates are never hidden in logs."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        KnowledgeProject, on_delete=models.PROTECT, related_name="retrieval_traces"
    )
    index_build = models.ForeignKey(
        "IndexBuild",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="retrieval_traces",
    )
    parser_run = models.ForeignKey(
        ParserRun,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="retrieval_traces",
    )
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="contract_retrieval_traces",
    )
    query = models.TextField()
    provider_name = models.CharField(max_length=100)
    provider_version = models.CharField(max_length=100)
    parameters = models.JSONField(default=dict)
    query_tokens = models.JSONField(default=list)
    candidates = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "检索 Trace"
        verbose_name_plural = "检索 Trace"
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.provider_name} / {self.query[:60]}"


class QuestionRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "运行中"
        ANSWERED = "answered", "已回答"
        REFUSED = "refused", "证据不足拒答"
        CLARIFICATION = "clarification", "需要澄清"
        FAILED = "failed", "失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        KnowledgeProject, on_delete=models.PROTECT, related_name="question_runs"
    )
    index_build = models.ForeignKey(
        "IndexBuild", on_delete=models.PROTECT, related_name="question_runs"
    )
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="contract_question_runs",
    )
    platform = models.CharField(max_length=30, default="admin")
    actor_key_hash = models.CharField(max_length=64, blank=True)
    question = models.TextField()
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.RUNNING
    )
    answer_payload = models.JSONField(default=dict)
    workflow_trace = models.JSONField(default=dict)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "合同问答运行"
        verbose_name_plural = "合同问答运行"
        ordering = ("-started_at",)

    def __str__(self) -> str:
        return f"{self.get_status_display()} / {self.question[:60]}"


class PriceMappingSpec(models.Model):
    """Versioned, declarative XLSX column mapping; never executable code."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document_version = models.ForeignKey(
        DocumentVersion, on_delete=models.PROTECT, related_name="price_mapping_specs"
    )
    source_file = models.ForeignKey(
        SourceFile, on_delete=models.PROTECT, related_name="price_mapping_specs"
    )
    name = models.CharField(max_length=200)
    version = models.PositiveIntegerField(default=1)
    sheet_name = models.CharField(max_length=200)
    header_row = models.PositiveIntegerField(default=1)
    data_start_row = models.PositiveIntegerField(default=2)
    data_end_row = models.PositiveIntegerField(null=True, blank=True)
    field_columns = models.JSONField(
        help_text=(
            "受支持字段到 Excel 列字母，例如 "
            '{"product_name":"A","unit_price":"D","currency":"E"}'
        )
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_price_mapping_specs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "价目表 MappingSpec"
        verbose_name_plural = "价目表 MappingSpec"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("document_version", "name", "version"),
                name="contract_unique_price_mapping_version",
            )
        ]

    def clean(self) -> None:
        super().clean()
        from .pricing import validate_mapping_spec

        if self.source_file_id:
            if self.source_file.document_version_id != self.document_version_id:
                raise ValidationError("MappingSpec 源文件必须属于同一 DocumentVersion")
            if Path(self.source_file.original_name).suffix.casefold() != ".xlsx":
                raise ValidationError("MappingSpec 只能引用 XLSX")
        validate_mapping_spec(self.field_columns)
        if self.data_start_row <= self.header_row:
            raise ValidationError("data_start_row 必须晚于 header_row")
        if self.data_end_row is not None and self.data_end_row < self.data_start_row:
            raise ValidationError("data_end_row 不能早于 data_start_row")

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("PriceMappingSpec 不允许原地修改；请创建新版本")
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.document_version} / {self.name} v{self.version}"


class PriceImportRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "运行中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    mapping_spec = models.ForeignKey(
        PriceMappingSpec, on_delete=models.PROTECT, related_name="import_runs"
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    imported_count = models.PositiveIntegerField(default=0)
    rejected_rows = models.JSONField(default=list)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "价格导入运行"
        verbose_name_plural = "价格导入运行"
        ordering = ("-started_at",)


class PriceRecord(models.Model):
    class Status(models.TextChoices):
        STAGING = "staging", "待人工确认"
        TRUSTED = "trusted", "可信"
        REJECTED = "rejected", "已拒绝"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    import_run = models.ForeignKey(
        PriceImportRun, on_delete=models.PROTECT, related_name="price_records"
    )
    document_version = models.ForeignKey(
        DocumentVersion, on_delete=models.PROTECT, related_name="price_records"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.STAGING
    )
    product_code = models.CharField(max_length=200, blank=True)
    product_name = models.CharField(max_length=500)
    region = models.CharField(max_length=200, blank=True)
    customer = models.CharField(max_length=200, blank=True)
    channel = models.CharField(max_length=200, blank=True)
    price_kind = models.CharField(max_length=100, blank=True)
    unit_price = models.DecimalField(max_digits=30, decimal_places=8)
    currency = models.CharField(max_length=20, blank=True)
    unit = models.CharField(max_length=100, blank=True)
    tax_included = models.BooleanField(null=True, blank=True)
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    minimum_quantity = models.DecimalField(
        max_digits=30, decimal_places=8, null=True, blank=True
    )
    maximum_quantity = models.DecimalField(
        max_digits=30, decimal_places=8, null=True, blank=True
    )
    notes = models.TextField(blank=True)
    extensions = models.JSONField(default=dict)
    source_sheet = models.CharField(max_length=200)
    source_row = models.PositiveIntegerField()
    source_cells = models.JSONField(default=dict)
    row_evidence = models.ForeignKey(
        EvidenceUnit, on_delete=models.PROTECT, related_name="price_row_records"
    )
    header_evidence = models.ForeignKey(
        EvidenceUnit, on_delete=models.PROTECT, related_name="price_header_records"
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_price_records",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "结构化价格记录"
        verbose_name_plural = "结构化价格记录"
        ordering = ("product_name", "region", "valid_from")
        indexes = [
            models.Index(fields=("document_version", "status", "product_code")),
            models.Index(fields=("document_version", "status", "product_name")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("import_run", "source_sheet", "source_row"),
                name="contract_unique_price_import_row",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.row_evidence_id and self.row_evidence.document_version_id != self.document_version_id:
            raise ValidationError("价格行证据必须属于同一个 DocumentVersion")
        if self.header_evidence_id and self.header_evidence.document_version_id != self.document_version_id:
            raise ValidationError("价格表头证据必须属于同一个 DocumentVersion")
        if self.minimum_quantity is not None and self.maximum_quantity is not None:
            if self.minimum_quantity > self.maximum_quantity:
                raise ValidationError("minimum_quantity 不能大于 maximum_quantity")

    def __str__(self) -> str:
        return f"{self.product_name} / {self.unit_price} {self.currency}"


class ExternalProjectBinding(models.Model):
    class SubjectType(models.TextChoices):
        USER = "user", "私聊用户"
        CHAT = "chat", "群聊"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform = models.CharField(max_length=30, default="feishu")
    subject_type = models.CharField(max_length=20, choices=SubjectType.choices)
    subject_id = models.CharField(max_length=300)
    project = models.ForeignKey(
        KnowledgeProject, on_delete=models.CASCADE, related_name="external_bindings"
    )
    is_active = models.BooleanField(default=True)
    is_selected = models.BooleanField(
        default=False,
        help_text="私聊用户可绑定多个项目，但同时只能选择一个；群聊固定项目必须勾选。",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "飞书项目绑定"
        verbose_name_plural = "飞书项目绑定"
        constraints = [
            models.UniqueConstraint(
                fields=("platform", "subject_type", "subject_id", "project"),
                name="contract_unique_external_project_binding",
            ),
            models.UniqueConstraint(
                fields=("platform", "subject_type", "subject_id"),
                condition=models.Q(is_active=True, is_selected=True),
                name="contract_one_selected_external_project",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.subject_type == self.SubjectType.CHAT and self.is_active and not self.is_selected:
            raise ValidationError("群聊固定项目必须设为 selected")

    def __str__(self) -> str:
        return f"{self.platform}:{self.subject_type}:{self.subject_id} → {self.project}"


class GoldenCase(models.Model):
    class ExpectedStatus(models.TextChoices):
        ANSWERED = "answered", "应回答"
        REFUSED = "refused", "应拒答"
        CLARIFICATION = "clarification", "应澄清"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        KnowledgeProject, on_delete=models.PROTECT, related_name="golden_cases"
    )
    index_build = models.ForeignKey(
        "IndexBuild",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="golden_cases",
        help_text="为空时评估当前 Published IndexBuild；固定后用于历史复现。",
    )
    question = models.TextField()
    expected_status = models.CharField(max_length=30, choices=ExpectedStatus.choices)
    expected_evidence_ids = models.JSONField(default=list)
    expected_answer_contains = models.JSONField(
        default=list, help_text="回答必须包含的人工确认事实片段；不是完整标准话术。"
    )
    expected_calculation_result = models.CharField(max_length=200, blank=True)
    tags = models.JSONField(default=list)
    notes = models.TextField(blank=True)
    is_verified = models.BooleanField(default=False)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_golden_cases",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Golden QA"
        verbose_name_plural = "Golden QA"
        ordering = ("project", "created_at")

    def clean(self) -> None:
        super().clean()
        for field_name in (
            "expected_evidence_ids",
            "expected_answer_contains",
            "tags",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise ValidationError(f"{field_name} 必须是非空字符串组成的 JSON list")
        if self.expected_status != self.ExpectedStatus.ANSWERED:
            if self.expected_evidence_ids or self.expected_calculation_result:
                raise ValidationError("拒答/澄清样本不能要求答案证据或计算结果")
        if self.index_build_id and self.index_build.project_id != self.project_id:
            raise ValidationError("GoldenCase IndexBuild 必须属于同一项目")

    def __str__(self) -> str:
        return f"{self.get_expected_status_display()} / {self.question[:70]}"


class EvaluationRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "运行中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        KnowledgeProject, on_delete=models.PROTECT, related_name="evaluation_runs"
    )
    index_build = models.ForeignKey(
        "IndexBuild", on_delete=models.PROTECT, related_name="evaluation_runs"
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    provider_configuration = models.JSONField(default=dict)
    metrics = models.JSONField(default=dict)
    case_results = models.JSONField(default=list)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "评估运行"
        verbose_name_plural = "评估运行"
        ordering = ("-started_at",)

    def __str__(self) -> str:
        return f"{self.project} / {self.get_status_display()} / {self.started_at}"


class ComparisonReport(models.Model):
    class Status(models.TextChoices):
        NOT_RUN = "not_run", "未运行"
        MATCH = "match", "未发现实质差异"
        WARNING = "warning", "需要人工复核"
        MISMATCH = "mismatch", "检测到关键差异"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document_version = models.ForeignKey(
        DocumentVersion, on_delete=models.PROTECT, related_name="comparison_reports"
    )
    structured_source = models.ForeignKey(
        SourceFile, on_delete=models.PROTECT, related_name="structured_comparisons"
    )
    executed_copy = models.ForeignKey(
        SourceFile, on_delete=models.PROTECT, related_name="executed_comparisons"
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    summary = models.TextField()
    metrics = models.JSONField(default=dict)
    differences = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "原件/PDF 一致性报告"
        verbose_name_plural = "原件/PDF 一致性报告"
        ordering = ("-created_at",)


class AuthorityReview(models.Model):
    document_version = models.OneToOneField(
        DocumentVersion,
        primary_key=True,
        on_delete=models.PROTECT,
        related_name="authority_review",
    )
    authoritative_source = models.ForeignKey(
        SourceFile, on_delete=models.PROTECT, related_name="authority_reviews"
    )
    files_are_same_final_version = models.BooleanField(default=False)
    reviewed_hashes = models.JSONField(default=dict, editable=False)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="contract_authority_reviews",
    )
    note = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "最终稿人工确认"
        verbose_name_plural = "最终稿人工确认"

    def clean(self) -> None:
        super().clean()
        if (
            self.authoritative_source_id
            and self.authoritative_source.document_version_id != self.document_version_id
        ):
            raise ValidationError("权威源文件必须属于同一个 DocumentVersion")
        if self.document_version.status == DocumentVersion.Status.PUBLISHED:
            raise ValidationError("已发布版本的最终稿声明不允许修改")

    def save(self, *args, **kwargs):
        self.reviewed_hashes = {
            str(source.id): source.sha256
            for source in self.document_version.source_files.all()
        }
        return super().save(*args, **kwargs)


class IndexBuild(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        READY = "ready", "可发布"
        PUBLISHED = "published", "已发布"
        RETIRED = "retired", "已退役"
        FAILED = "failed", "失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        KnowledgeProject, on_delete=models.PROTECT, related_name="index_builds"
    )
    name = models.CharField(max_length=200)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    document_versions = models.ManyToManyField(
        DocumentVersion, through="IndexBuildDocument", related_name="index_builds"
    )
    parser_configuration = models.JSONField(default=dict)
    retrieval_configuration = models.JSONField(default=dict)
    evidence_manifest_sha256 = models.CharField(max_length=64, blank=True)
    external_mappings = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "索引构建"
        verbose_name_plural = "索引构建"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("project", "name"), name="contract_unique_index_build_name"
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).only("status").first()
            if old is not None and old.status == self.Status.PUBLISHED:
                raise ValidationError("已发布 IndexBuild 不允许原地修改")
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.project} / {self.name}"


class IndexBuildDocument(models.Model):
    index_build = models.ForeignKey(IndexBuild, on_delete=models.CASCADE)
    document_version = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT)
    parser_run = models.ForeignKey(ParserRun, on_delete=models.PROTECT)

    class Meta:
        verbose_name = "索引文档快照"
        verbose_name_plural = "索引文档快照"
        constraints = [
            models.UniqueConstraint(
                fields=("index_build", "document_version"),
                name="contract_unique_build_document",
            )
        ]

    def clean(self) -> None:
        super().clean()
        errors: list[str] = []
        if self.document_version.document.project_id != self.index_build.project_id:
            errors.append("DocumentVersion 必须属于 IndexBuild 的项目")
        if self.parser_run.document_version_id != self.document_version_id:
            errors.append("ParserRun 必须属于所选 DocumentVersion")
        if self.parser_run.status != ParserRun.Status.SUCCEEDED:
            errors.append("只有成功的 ParserRun 可以进入 IndexBuild")
        if errors:
            raise ValidationError(errors)
