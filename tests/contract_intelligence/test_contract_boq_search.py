from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from walkie_dokie.contract_intelligence.boq_search import (
    NumericConstraint,
    SimilarBoqSearchQuery,
    TextConstraint,
    extract_boq_item_parameters,
    extract_numeric_attributes,
    normalize_unit,
    search_similar_boq_items,
)
from walkie_dokie.contract_intelligence.models import (
    BoqImportRun,
    BoqImportSpec,
    BoqItemRecord,
    Document,
    DocumentVersion,
    EvidenceUnit,
    KnowledgeProject,
    ParserRun,
    SourceFile,
)


def _create_item(
    settings,
    tmp_path,
    *,
    slug,
    project_name,
    party_a_name,
    party_a_group,
    item_name,
    description,
    unit="m",
    unit_price="100",
    status=BoqItemRecord.Status.STAGING,
    kind=BoqItemRecord.Kind.ENTITY,
):
    settings.MEDIA_ROOT = tmp_path / "media"
    project = KnowledgeProject.objects.create(name=project_name, slug=slug)
    document = Document.objects.create(
        project=project,
        name="工程量清单",
        kind=Document.Kind.PRICE_LIST,
    )
    version = DocumentVersion.objects.create(document=document, version_label="v1")
    source = SourceFile.objects.create(
        document_version=version,
        role=SourceFile.Role.STRUCTURED_SOURCE,
        file=SimpleUploadedFile(f"{slug}.xlsx", b"test-only"),
    )
    parser_run = ParserRun.objects.create(
        document_version=version,
        source_file=source,
        provider_name="test",
        provider_version="1",
        config_sha256="0" * 64,
        status=ParserRun.Status.SUCCEEDED,
    )
    evidence = EvidenceUnit.objects.create(
        evidence_id=f"ev_{slug}",
        document_version=version,
        parser_run=parser_run,
        source_file=source,
        kind="spreadsheet_row",
        ordinal=1,
        source_anchor={"type": "test", "sheet": "清单", "row": 2},
        text=item_name,
        normalized_text=item_name,
        content_sha256="1" * 64,
    )
    spec = BoqImportSpec.objects.create(
        document_version=version,
        source_file=source,
        name="test",
        profile=BoqImportSpec.Profile.CRLAND_GENERAL_V1,
        project_name=project_name,
        party_a_name=party_a_name,
        party_a_group=party_a_group,
    )
    import_run = BoqImportRun.objects.create(
        import_spec=spec,
        status=BoqImportRun.Status.SUCCEEDED,
    )
    item = BoqItemRecord.objects.create(
        import_run=import_run,
        document_version=version,
        status=status,
        project_name=project_name,
        party_a_name=party_a_name,
        party_a_group=party_a_group,
        kind=kind,
        item_code=f"{slug}-1",
        item_name=item_name,
        item_description=description,
        unit=unit,
        quantity=Decimal("1"),
        unit_price=Decimal(unit_price),
        total_price=Decimal(unit_price),
        source_sheet="清单",
        source_row=2,
        row_evidence=evidence,
        header_evidence=evidence,
    )
    return project, item


def test_numeric_attribute_extraction_supports_single_values_and_ranges():
    extracted = extract_numeric_attributes(
        "LED 36W/m 恒功率，2700-6500K 可调色温，备用 3000K"
    )

    assert extracted.powers_w == (Decimal("36"),)
    assert extracted.color_temperature_ranges_k == (
        (Decimal("2700"), Decimal("6500")),
    )
    assert Decimal("3000") in extracted.color_temperatures_k
    assert Decimal("2700") not in extracted.color_temperatures_k
    assert Decimal("6500") not in extracted.color_temperatures_k
    assert normalize_unit("m²") == normalize_unit("平方米") == "m2"


def test_numeric_and_discrete_parameter_extraction_is_explicit():
    extracted = extract_numeric_attributes(
        "LED 16.5W/m，光束角120°，3000K，编号:txL3，IP67，DMX 8段/m，Ra>90"
    )

    assert {
        (parameter.key, parameter.unit, parameter.value)
        for parameter in extracted.parameters
        if parameter.kind == "numeric"
    } >= {
        ("power_density_w_m", "W/m", Decimal("16.5")),
        ("beam_angle_deg", "°", Decimal("120")),
        ("color_temperature_k", "K", Decimal("3000")),
    }
    assert {parameter.text for parameter in extracted.parameters if parameter.kind == "text"} >= {
        "IP67",
        "DMX 8段/m",
        "Ra>90",
        "编号:txL3",
    }


def test_boq_name_area_specification_is_parsed_but_not_searchable_yet():
    extracted = extract_boq_item_parameters("矩形洞孔0.10 m2以内", "")
    parameter = extracted.parameters[0]

    assert parameter.key == "area_m2"
    assert parameter.unit == "m2"
    assert parameter.minimum is None
    assert parameter.maximum == Decimal("0.10")
    assert parameter.raw_text == "0.10 m2以内"
    assert parameter.source == "item_name"
    assert parameter.searchable is False

    ranged = extract_boq_item_parameters("矩形洞孔0.10-0.30 m2", "")
    ranged_parameter = ranged.parameters[0]
    assert ranged_parameter.minimum == Decimal("0.10")
    assert ranged_parameter.maximum == Decimal("0.30")
    assert ranged_parameter.searchable is False


@pytest.mark.django_db
def test_similarity_search_uses_party_group_unit_tolerance_and_staging_gate(
    settings, tmp_path
):
    current_project, _ = _create_item(
        settings,
        tmp_path,
        slug="current",
        project_name="当前项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED 灯带",
        description="编号 txL3，LED 16.5W/m 3000K",
        unit_price="54.43",
    )
    _, expected = _create_item(
        settings,
        tmp_path,
        slug="candidate",
        project_name="其他项目",
        party_a_name="华润置地（深圳）有限公司",
        party_a_group="华润",
        item_name="安装LED灯带LL02",
        description="整灯功率:15W，色温:2700K，IP67，DMX 1段/m",
        unit_price="270.99",
    )
    _create_item(
        settings,
        tmp_path,
        slug="missing-parameter",
        project_name="参数缺失项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED灯带",
        description="未提供功率和色温",
    )
    _create_item(
        settings,
        tmp_path,
        slug="wrong-unit",
        project_name="单位不同项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED灯带",
        description="15W 2700K",
        unit="套",
    )
    _create_item(
        settings,
        tmp_path,
        slug="wrong-party",
        project_name="其他甲方项目",
        party_a_name="其他甲方",
        party_a_group="其他集团",
        item_name="LED灯带",
        description="15W 2700K",
    )
    query = SimilarBoqSearchQuery(
        current_project_id=str(current_project.pk),
        query="LED灯带",
        unit="米",
        model_hint="LL2",
        power_w=Decimal("16.5"),
        color_temperature_k=Decimal("3000"),
        tolerance_percent=Decimal("10"),
        include_staging=True,
    )

    response = search_similar_boq_items(BoqItemRecord.objects.all(), query)

    assert response.party_a_group == "华润"
    assert [match.record.pk for match in response.matches] == [expected.pk]
    match = response.matches[0]
    assert match.power_delta_percent == Decimal("9.09")
    assert match.color_temperature_delta_percent == Decimal("10.00")
    assert match.record.party_a_name == "华润置地（深圳）有限公司"

    trusted_only = search_similar_boq_items(
        BoqItemRecord.objects.all(),
        SimilarBoqSearchQuery(
            current_project_id=str(current_project.pk),
            query="LED灯带",
            unit="m",
            power_w=Decimal("16.5"),
            color_temperature_k=Decimal("3000"),
        ),
    )
    assert trusted_only.matches == ()


@pytest.mark.django_db
def test_similarity_search_allows_adjusting_numeric_tolerance(settings, tmp_path):
    current_project, _ = _create_item(
        settings,
        tmp_path,
        slug="tolerance-current",
        project_name="容差当前项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED灯带",
        description="16.5W 3000K",
    )
    _, candidate = _create_item(
        settings,
        tmp_path,
        slug="tolerance-candidate",
        project_name="容差候选项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED灯带",
        description="15W 2700K",
    )

    narrow = search_similar_boq_items(
        BoqItemRecord.objects.all(),
        SimilarBoqSearchQuery(
            current_project_id=str(current_project.pk),
            query="LED灯带",
            unit="m",
            power_w=Decimal("16.5"),
            color_temperature_k=Decimal("3000"),
            tolerance_percent=Decimal("9.99"),
            include_staging=True,
        ),
    )
    wider = search_similar_boq_items(
        BoqItemRecord.objects.all(),
        SimilarBoqSearchQuery(
            current_project_id=str(current_project.pk),
            query="LED灯带",
            unit="m",
            power_w=Decimal("16.5"),
            color_temperature_k=Decimal("3000"),
            tolerance_percent=Decimal("11"),
            include_staging=True,
        ),
    )

    assert narrow.matches == ()
    assert [match.record.pk for match in wider.matches] == [candidate.pk]


@pytest.mark.django_db
def test_similarity_search_supports_independent_numeric_and_text_constraints(
    settings, tmp_path
):
    current_project, _ = _create_item(
        settings,
        tmp_path,
        slug="generic-current",
        project_name="通用参数当前项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED灯带",
        description="16.5W/m，3000K，IP67",
    )
    _, candidate = _create_item(
        settings,
        tmp_path,
        slug="generic-candidate",
        project_name="通用参数候选项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED灯带",
        description="15W/m，2700K，IP67",
        status=BoqItemRecord.Status.TRUSTED,
    )

    response = search_similar_boq_items(
        BoqItemRecord.objects.all(),
        SimilarBoqSearchQuery(
            current_project_id=str(current_project.pk),
            query="LED灯带",
            unit="m",
            numeric_constraints=(
                NumericConstraint("power_density_w_m", Decimal("16.5"), "W/m", Decimal("10")),
                NumericConstraint("color_temperature_k", Decimal("3000"), "K", Decimal("10")),
            ),
            text_constraints=(TextConstraint("protection_level", "IP67"),),
            include_staging=False,
        ),
    )

    assert [match.record.pk for match in response.matches] == [candidate.pk]
    assert {key for key, _, _ in response.matches[0].matched_parameters} == {
        "power_density_w_m",
        "color_temperature_k",
    }


@pytest.mark.django_db
def test_power_w_and_power_per_meter_are_compatible_for_meter_boq_items(
    settings, tmp_path
):
    current_project, _ = _create_item(
        settings,
        tmp_path,
        slug="power-unit-current",
        project_name="功率单位当前项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED灯带",
        description="15W，2700K",
    )
    _, candidate = _create_item(
        settings,
        tmp_path,
        slug="power-unit-candidate",
        project_name="功率单位候选项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED灯带",
        description="16.5W/m，3000K",
        status=BoqItemRecord.Status.TRUSTED,
    )

    response = search_similar_boq_items(
        BoqItemRecord.objects.all(),
        SimilarBoqSearchQuery(
            current_project_id=str(current_project.pk),
            query="LED灯带",
            unit="m",
            numeric_constraints=(
                NumericConstraint("power_w", Decimal("15"), "W", Decimal("10")),
            ),
        ),
    )

    assert [match.record.pk for match in response.matches] == [candidate.pk]


@pytest.mark.django_db
def test_numeric_range_constraint_matches_scalar_inside_range(settings, tmp_path):
    current_project, _ = _create_item(
        settings,
        tmp_path,
        slug="range-current",
        project_name="范围当前项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED洗墙灯",
        description="36W/m，2700-6500K 可调色温",
    )
    _, candidate = _create_item(
        settings,
        tmp_path,
        slug="range-candidate",
        project_name="范围候选项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED洗墙灯",
        description="32W/m，4000K",
        status=BoqItemRecord.Status.TRUSTED,
    )

    response = search_similar_boq_items(
        BoqItemRecord.objects.all(),
        SimilarBoqSearchQuery(
            current_project_id=str(current_project.pk),
            query="LED洗墙灯",
            unit="m",
            numeric_constraints=(
                NumericConstraint(
                    "color_temperature_k",
                    None,
                    "K",
                    Decimal("0"),
                    minimum=Decimal("2700"),
                    maximum=Decimal("6500"),
                ),
            ),
        ),
    )

    assert [match.record.pk for match in response.matches] == [candidate.pk]


@pytest.mark.django_db
def test_admin_item_link_prefills_parameters_and_formats_three_decimals(
    client, settings, tmp_path
):
    user = get_user_model().objects.create_superuser(
        username="similarity-admin",
        email="similarity-admin@example.com",
        password="safe-test-password",
    )
    client.force_login(user)
    current_project, source_item = _create_item(
        settings,
        tmp_path,
        slug="admin-current",
        project_name="后台当前项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED 灯带",
        description="名称:LED 灯带\n编号:txL3\n规格:LED 16.5W/m 3000K",
        unit_price="54.42611975",
    )

    change_list = client.get(
        reverse("admin:contract_intelligence_boqitemrecord_changelist")
    )
    item_search_url = reverse(
        "admin:contract_boq_item_similar_search", args=(source_item.pk,)
    )
    assert change_list.status_code == 200
    assert item_search_url in change_list.content.decode("utf-8")
    assert "54.426" in change_list.content.decode("utf-8")
    assert "54.42611975" not in change_list.content.decode("utf-8")

    response = client.get(item_search_url)

    form = response.context["form"]
    assert response.status_code == 200
    assert response.context["source_item"].pk == source_item.pk
    assert form.initial["current_project"] == current_project.pk
    assert form.initial["query"] == "LED 灯带"
    assert form.initial["model_hint"] == "txL3"
    assert form.initial["power_w"] == Decimal("16.5")
    assert form.initial["color_temperature_k"] == Decimal("3000")
    assert form.initial["use_query"] is True
    assert form.initial["use_power"] is True
    assert form.initial["use_color_temperature"] is True
    assert form.initial["use_model"] is False
    assert form.fields["current_project"].disabled is True
    assert "54.426 CNY" in response.content.decode("utf-8")


@pytest.mark.django_db
def test_admin_item_search_uses_only_selected_parameters(
    client, settings, tmp_path
):
    user = get_user_model().objects.create_superuser(
        username="parameter-admin",
        email="parameter-admin@example.com",
        password="safe-test-password",
    )
    client.force_login(user)
    _, source_item = _create_item(
        settings,
        tmp_path,
        slug="parameter-current",
        project_name="参数当前项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="措施费",
        description="编号:A1，16.5W，3000K",
        kind=BoqItemRecord.Kind.OPENING,
    )
    _, expected = _create_item(
        settings,
        tmp_path,
        slug="parameter-candidate",
        project_name="参数候选项目",
        party_a_name="华润置地（深圳）有限公司",
        party_a_group="华润",
        item_name="措施费",
        description="编号:B9，5W，6500K",
        kind=BoqItemRecord.Kind.OPENING,
    )
    _create_item(
        settings,
        tmp_path,
        slug="parameter-wrong-kind",
        project_name="类型不同项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="措施费",
        description="16.5W，3000K",
    )
    url = reverse(
        "admin:contract_boq_item_similar_search", args=(source_item.pk,)
    )
    base_params = {
        "search": "1",
        "unit": "m",
        "use_query": "on",
        "query": "措施费",
        "power_w": "16.5",
        "color_temperature_k": "3000",
        "tolerance_percent": "10",
        "include_staging": "on",
        "limit": "20",
    }

    name_only = client.get(url, base_params)
    strict = client.get(
        url,
        {
            **base_params,
            "use_power": "on",
            "use_color_temperature": "on",
        },
    )

    assert [match.record.pk for match in name_only.context["response"].matches] == [
        expected.pk
    ]
    assert strict.context["response"].matches == ()


@pytest.mark.django_db
def test_admin_row_search_api_returns_parameter_payload_and_matches(
    client, settings, tmp_path
):
    user = get_user_model().objects.create_superuser(
        username="api-admin",
        email="api-admin@example.com",
        password="safe-test-password",
    )
    client.force_login(user)
    _, source_item = _create_item(
        settings,
        tmp_path,
        slug="api-current",
        project_name="API当前项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED灯带",
        description="16.5W/m，3000K，IP67",
    )
    _, candidate = _create_item(
        settings,
        tmp_path,
        slug="api-candidate",
        project_name="API候选项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="LED灯带",
        description="15W/m，2700K，IP67",
        status=BoqItemRecord.Status.TRUSTED,
    )
    api_url = reverse(
        "admin:contract_boq_item_similar_search_api", args=(source_item.pk,)
    )

    initial = client.get(api_url)
    assert initial.status_code == 200
    payload = initial.json()
    assert any(item["key"] == "power_density_w_m" for item in payload["parameters"])
    assert any(item["key"] == "protection_level" for item in payload["parameters"])

    result = client.get(
        api_url,
        {
            "search": "1",
            "numeric": '[{"key":"power_density_w_m","value":"16.5","unit":"W/m","tolerance":"10"},{"key":"color_temperature_k","value":"3000","unit":"K","tolerance":"10"}]',
            "text": '[{"key":"protection_level","value":"IP67"}]',
            "tolerance": "10",
        },
    )
    assert result.status_code == 200
    assert [item["item_name"] for item in result.json()["matches"]] == [candidate.item_name]


@pytest.mark.django_db
def test_admin_row_search_api_exposes_name_area_specification_without_enabling_it(
    client, settings, tmp_path
):
    user = get_user_model().objects.create_superuser(
        username="area-api-admin",
        email="area-api-admin@example.com",
        password="safe-test-password",
    )
    client.force_login(user)
    _, source_item = _create_item(
        settings,
        tmp_path,
        slug="area-api-current",
        project_name="面积规格项目",
        party_a_name="华润",
        party_a_group="华润",
        item_name="矩形洞孔0.10 m2以内",
        description="",
        unit="个",
    )
    api_url = reverse(
        "admin:contract_boq_item_similar_search_api", args=(source_item.pk,)
    )

    response = client.get(api_url)

    assert response.status_code == 200
    area_parameter = next(
        parameter for parameter in response.json()["parameters"] if parameter["key"] == "area_m2"
    )
    assert area_parameter["minimum"] is None
    assert area_parameter["maximum"] == "0.10"
    assert area_parameter["source"] == "item_name"
    assert area_parameter["searchable"] is False
