from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook

from walkie_dokie.contract_intelligence.ingestion import run_parser_for_source
from walkie_dokie.contract_intelligence.models import (
    AuthorityReview,
    Document,
    DocumentVersion,
    IndexBuild,
    IndexBuildDocument,
    KnowledgeProject,
    PriceMappingSpec,
    PriceRecord,
    SourceFile,
)
from walkie_dokie.contract_intelligence.pricing import (
    PriceQuery,
    calculate_total,
    import_price_mapping,
    query_published_prices,
    trust_price_records,
    validate_mapping_spec,
)
from walkie_dokie.contract_intelligence.publication import (
    prepare_index_build,
    publish_index_build,
)


def _xlsx_bytes(tmp_path) -> bytes:
    path = tmp_path / "prices.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "价目表"
    sheet.append(["商品编码", "商品", "地区", "价格", "币种", "单位", "含税"])
    sheet.append(["P001", "安装服务", "上海", "100.50", "CNY", "次", "含税"])
    sheet.append(["P001", "安装服务", "北京", "120.00", "CNY", "次", "含税"])
    workbook.save(path)
    return path.read_bytes()


def _published_prices(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    user = get_user_model().objects.create_user(username="price-reviewer")
    project = KnowledgeProject.objects.create(name="价格项目", slug="prices")
    document = Document.objects.create(
        project=project, name="服务价目表", kind=Document.Kind.PRICE_LIST
    )
    version = DocumentVersion.objects.create(document=document, version_label="2026")
    source = SourceFile.objects.create(
        document_version=version,
        role=SourceFile.Role.STRUCTURED_SOURCE,
        file=SimpleUploadedFile("prices.xlsx", _xlsx_bytes(tmp_path)),
    )
    parser_run = run_parser_for_source(source.pk, raise_errors=True)
    spec = PriceMappingSpec(
        document_version=version,
        source_file=source,
        name="上海北京服务价",
        sheet_name="价目表",
        field_columns={
            "product_code": "A",
            "product_name": "B",
            "region": "C",
            "unit_price": "D",
            "currency": "E",
            "unit": "F",
            "tax_included": "G",
        },
        created_by=user,
    )
    spec.full_clean()
    spec.save()
    import_run = import_price_mapping(spec.pk)
    assert import_run.status == import_run.Status.SUCCEEDED
    assert trust_price_records(import_run.price_records.all(), reviewer=user) == 2
    AuthorityReview.objects.create(
        document_version=version,
        authoritative_source=source,
        reviewer=user,
        note="XLSX 为最终价目表",
    )
    build = IndexBuild.objects.create(project=project, name="price-build")
    IndexBuildDocument.objects.create(
        index_build=build, document_version=version, parser_run=parser_run
    )
    prepare_index_build(build.pk)
    publish_index_build(build.pk)
    return project, import_run


@pytest.mark.django_db
def test_mapping_import_query_and_decimal_calculation(settings, tmp_path):
    project, import_run = _published_prices(settings, tmp_path)

    lookup = query_published_prices(
        project.pk,
        PriceQuery(product_code="P001", region="上海", quantity=Decimal("3")),
    )

    assert lookup.status == "found"
    assert lookup.records[0].unit_price == Decimal("100.50000000")
    assert lookup.records[0].row_evidence.source_anchor["row"] == 2
    ledger = calculate_total(lookup.records[0].unit_price, Decimal("3"))
    assert ledger["result"] == "301.50000000"
    assert PriceRecord.objects.filter(import_run=import_run, status="trusted").count() == 2


@pytest.mark.django_db
def test_price_query_requests_missing_region_instead_of_guessing(settings, tmp_path):
    project, _ = _published_prices(settings, tmp_path)

    lookup = query_published_prices(
        project.pk, PriceQuery(product_code="P001")
    )

    assert lookup.status == "ambiguous"
    assert lookup.missing_dimension == "region"


def test_mapping_spec_rejects_unknown_fields_or_executable_content():
    with pytest.raises(ValidationError, match="不支持的字段"):
        validate_mapping_spec(
            {"product_name": "A", "unit_price": "B", "python": "C"}
        )
    with pytest.raises(ValidationError, match="Excel 列字母"):
        validate_mapping_spec(
            {"product_name": "A", "unit_price": "__import__('os').system('id')"}
        )
