"""Deterministic cross-project similarity search over verified BOQ SQL facts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
import re
from typing import Iterable

from rapidfuzz import fuzz

from .domain import normalize_text
from .models import BoqItemRecord


_POWER_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*W\b", re.IGNORECASE)
_COLOR_TEMPERATURE_RANGE_RE = re.compile(
    r"(?<!\d)(\d{3,5})\s*(?:-|~|–|—|至)\s*(\d{3,5})\s*K\b",
    re.IGNORECASE,
)
_COLOR_TEMPERATURE_RE = re.compile(r"(?<!\d)(\d{3,5})\s*K\b", re.IGNORECASE)
_COMPACT_RE = re.compile(r"[\s\-_:/,.;，。；（）()【】\[\]]+")
_LABELLED_NUMBER_RE = re.compile(
    r"(?P<label>光束角|出光角|角度|电压|输入电压|工作电压|光效|功率|整灯功率|光源功率)"
    r"\s*[:：]?\s*(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>W(?:\s*/\s*m(?:2|²)?)?|V|lm\s*/\s*W|°|度)",
    re.IGNORECASE,
)
_LABELED_POWER_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*W\s*(?P<suffix>/\s*m(?:2|²)?)?\b",
    re.IGNORECASE,
)
_AREA_RANGE_RE = re.compile(
    r"(?P<minimum>\d+(?:\.\d+)?)\s*(?:-|~|–|—|至)\s*"
    r"(?P<maximum>\d+(?:\.\d+)?)\s*m\s*(?:2|²|\^2)\b",
    re.IGNORECASE,
)
_AREA_MAX_RE = re.compile(
    r"(?P<maximum>\d+(?:\.\d+)?)\s*m\s*(?:2|²|\^2)\s*(?P<qualifier>以内|以下|不超过)",
    re.IGNORECASE,
)
_TEXT_PARAMETER_RES = (
    ("model", "型号/编号", re.compile(r"(?:型号|编号)\s*[:：]?\s*[A-Za-z0-9][A-Za-z0-9._\-/]+", re.IGNORECASE)),
    ("protection_level", "防护等级", re.compile(r"\bIP\s*\d{2}\b", re.IGNORECASE)),
    ("control_mode", "调光/控制", re.compile(r"(?:DMX\s*[^，,；;\n]+|调光\s*[:：]?\s*[^，,；;\n]+)", re.IGNORECASE)),
    ("color_rendering", "显色/色容差", re.compile(r"(?:Ra\s*[>＜<≤≥=]?\s*\d+|R9\s*[>＜<≤≥=]?\s*\d+|SDCM\s*[>＜<≤≥=]?\s*\d+)", re.IGNORECASE)),
    ("material", "材质", re.compile(r"材质\s*[:：]\s*[^，,；;\n]+")),
    ("color", "颜色", re.compile(r"颜色\s*[:：]\s*[^，,；;\n]+")),
)

_UNIT_ALIASES = {
    "m": "m",
    "米": "m",
    "延米": "m",
    "m2": "m2",
    "m²": "m2",
    "㎡": "m2",
    "平方米": "m2",
    "套": "套",
    "台": "台",
    "个": "个",
    "项": "项",
}


def normalize_unit(value: str) -> str:
    compact = _COMPACT_RE.sub("", normalize_text(value).casefold())
    return _UNIT_ALIASES.get(compact, compact)


def _similarity_text(value: str) -> str:
    return _COMPACT_RE.sub("", normalize_text(value).casefold())


@dataclass(frozen=True, slots=True)
class ExtractedParameter:
    key: str
    label: str
    kind: str
    unit: str
    value: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None
    text: str
    raw_text: str
    source: str = "description"
    searchable: bool = True


@dataclass(frozen=True, slots=True)
class ExtractedNumericAttributes:
    powers_w: tuple[Decimal, ...]
    color_temperatures_k: tuple[Decimal, ...]
    color_temperature_ranges_k: tuple[tuple[Decimal, Decimal], ...]
    parameters: tuple[ExtractedParameter, ...] = ()


def extract_numeric_attributes(text: str, *, source: str = "description") -> ExtractedNumericAttributes:
    normalized = normalize_text(text)
    powers = tuple(dict.fromkeys(Decimal(value) for value in _POWER_RE.findall(normalized)))
    temperature_ranges = tuple(
        dict.fromkeys(
            (
                min(Decimal(start), Decimal(end)),
                max(Decimal(start), Decimal(end)),
            )
            for start, end in _COLOR_TEMPERATURE_RANGE_RE.findall(normalized)
        )
    )
    range_endpoints = {
        endpoint
        for temperature_range in temperature_ranges
        for endpoint in temperature_range
    }
    temperatures = tuple(
        value
        for value in dict.fromkeys(
            Decimal(value) for value in _COLOR_TEMPERATURE_RE.findall(normalized)
        )
        if value not in range_endpoints
    )
    parameters: list[ExtractedParameter] = []
    for match in _LABELLED_NUMBER_RE.finditer(normalized):
        label = match.group("label")
        value = Decimal(match.group("value"))
        raw_unit = match.group("unit").replace(" ", "")
        if raw_unit.casefold().startswith("w"):
            if "/m2" in raw_unit.casefold() or "/m²" in raw_unit.casefold():
                key, unit = "power_density_w_m2", "W/m2"
            elif "/m" in raw_unit.casefold():
                key, unit = "power_density_w_m", "W/m"
            else:
                key, unit = "power_w", "W"
        elif raw_unit.casefold() == "v":
            key, unit = "voltage_v", "V"
        elif raw_unit.casefold().startswith("lm"):
            key, unit = "efficacy_lm_w", "lm/W"
        else:
            key, unit = "beam_angle_deg", "°"
        parameters.append(
            ExtractedParameter(key, label, "numeric", unit, value, None, None, str(value), match.group(0), source)
        )
    for match in _LABELED_POWER_RE.finditer(normalized):
        suffix = match.group("suffix") or ""
        suffix_compact = suffix.replace(" ", "").casefold()
        if suffix_compact in ("/m2", "/m²"):
            key, unit = "power_density_w_m2", "W/m2"
        elif suffix_compact == "/m":
            key, unit = "power_density_w_m", "W/m"
        else:
            key, unit = "power_w", "W"
        value = Decimal(match.group("value"))
        if any(
            parameter.key == key
            and parameter.unit == unit
            and parameter.value == value
            for parameter in parameters
        ):
            continue
        parameters.append(
            ExtractedParameter(key, "功率", "numeric", unit, value, None, None, str(value), match.group(0), source)
        )
    for minimum, maximum in temperature_ranges:
        parameters.append(
            ExtractedParameter(
                "color_temperature_k", "色温", "numeric", "K", None, minimum, maximum,
                f"{minimum}-{maximum}K", f"{minimum}-{maximum}K", source,
            )
        )
    for value in temperatures:
        if any(parameter.value == value and parameter.key == "color_temperature_k" for parameter in parameters):
            continue
        parameters.append(
            ExtractedParameter("color_temperature_k", "色温", "numeric", "K", value, None, None, str(value), f"{value}K", source)
        )
    for key, label, pattern in _TEXT_PARAMETER_RES:
        for match in pattern.finditer(normalized):
            raw = match.group(0).strip()
            parameters.append(
                ExtractedParameter(key, label, "text", "", None, None, None, raw, raw, source)
            )
    return ExtractedNumericAttributes(
        powers_w=powers,
        color_temperatures_k=temperatures,
        color_temperature_ranges_k=temperature_ranges,
        parameters=tuple(parameters),
    )


def extract_boq_item_parameters(
    item_name: str,
    item_description: str,
) -> ExtractedNumericAttributes:
    """Extract description parameters plus name-level specifications.

    Name-level area brackets are intentionally marked non-searchable until their
    pricing-band semantics are decided; the parser still exposes their bounds
    and original text for review.
    """

    description_attributes = extract_numeric_attributes(item_description, source="description")
    name_parameters: list[ExtractedParameter] = []
    normalized_name = normalize_text(item_name)
    for match in _AREA_RANGE_RE.finditer(normalized_name):
        minimum = Decimal(match.group("minimum"))
        maximum = Decimal(match.group("maximum"))
        name_parameters.append(
            ExtractedParameter(
                "area_m2", "面积规格", "numeric", "m2", None, minimum, maximum,
                match.group(0), match.group(0), "item_name", False,
            )
        )
    for match in _AREA_MAX_RE.finditer(normalized_name):
        maximum = Decimal(match.group("maximum"))
        name_parameters.append(
            ExtractedParameter(
                "area_m2", "面积规格", "numeric", "m2", None, None, maximum,
                match.group(0), match.group(0), "item_name", False,
            )
        )
    return ExtractedNumericAttributes(
        powers_w=description_attributes.powers_w,
        color_temperatures_k=description_attributes.color_temperatures_k,
        color_temperature_ranges_k=description_attributes.color_temperature_ranges_k,
        parameters=tuple(name_parameters) + description_attributes.parameters,
    )


@dataclass(frozen=True, slots=True)
class NumericConstraint:
    key: str
    value: Decimal | None
    unit: str
    tolerance_percent: Decimal
    minimum: Decimal | None = None
    maximum: Decimal | None = None

    def __post_init__(self) -> None:
        if self.value is None and (self.minimum is None or self.maximum is None):
            raise ValueError("数值约束必须提供单值或完整范围")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("数值约束范围下限不能大于上限")


@dataclass(frozen=True, slots=True)
class TextConstraint:
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class SimilarBoqSearchQuery:
    current_project_id: str
    query: str
    unit: str
    kind: str = BoqItemRecord.Kind.ENTITY
    model_hint: str = ""
    specification_hint: str = ""
    power_w: Decimal | None = None
    color_temperature_k: Decimal | None = None
    numeric_constraints: tuple[NumericConstraint, ...] = ()
    text_constraints: tuple[TextConstraint, ...] = ()
    tolerance_percent: Decimal = Decimal("10")
    include_staging: bool = False
    limit: int = 20

    def __post_init__(self) -> None:
        if not str(self.current_project_id).strip():
            raise ValueError("当前项目不能为空")
        if not normalize_unit(self.unit):
            raise ValueError("计价单位不能为空")
        if self.kind not in BoqItemRecord.Kind.values:
            raise ValueError(f"工程量清单记录类型不受支持：{self.kind!r}")
        if not any(
            (
                normalize_text(self.query),
                normalize_text(self.model_hint),
                normalize_text(self.specification_hint),
                self.power_w is not None,
                self.color_temperature_k is not None,
                self.numeric_constraints,
                self.text_constraints,
            )
        ):
            raise ValueError("至少填写名称、型号、规格、功率或色温中的一项")
        if self.tolerance_percent < 0 or self.tolerance_percent > 100:
            raise ValueError("容差百分比必须在 0 到 100 之间")
        if self.limit < 1 or self.limit > 100:
            raise ValueError("返回数量必须在 1 到 100 之间")


@dataclass(frozen=True, slots=True)
class SimilarBoqMatch:
    record: BoqItemRecord
    score: Decimal
    name_score: Decimal | None
    model_score: Decimal | None
    specification_score: Decimal | None
    matched_power_w: Decimal | None
    power_delta_percent: Decimal | None
    matched_color_temperature_k: Decimal | None
    color_temperature_delta_percent: Decimal | None
    extracted_attributes: ExtractedNumericAttributes
    matched_parameters: tuple[tuple[str, Decimal | None, Decimal | None], ...] = ()


@dataclass(frozen=True, slots=True)
class SimilarBoqSearchResponse:
    party_a_group: str
    considered_count: int
    matches: tuple[SimilarBoqMatch, ...]


def _score(query: str, candidate: str) -> Decimal:
    normalized_query = _similarity_text(query)
    normalized_candidate = _similarity_text(candidate)
    if not normalized_query or not normalized_candidate:
        return Decimal("0")
    value = max(
        fuzz.WRatio(normalized_query, normalized_candidate),
        fuzz.partial_ratio(normalized_query, normalized_candidate),
    )
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _nearest_decimal(
    expected: Decimal,
    values: Iterable[Decimal],
) -> tuple[Decimal, Decimal] | None:
    values = tuple(values)
    if not values:
        return None
    matched = min(values, key=lambda value: abs(value - expected))
    delta = abs(matched - expected) / expected if expected else abs(matched - expected)
    return matched, delta


def _nearest_temperature(
    expected: Decimal,
    attributes: ExtractedNumericAttributes,
) -> tuple[Decimal, Decimal] | None:
    candidates: list[tuple[Decimal, Decimal]] = []
    nearest_single = _nearest_decimal(expected, attributes.color_temperatures_k)
    if nearest_single is not None:
        candidates.append(nearest_single)
    for minimum, maximum in attributes.color_temperature_ranges_k:
        if minimum <= expected <= maximum:
            candidates.append((expected, Decimal("0")))
        else:
            endpoint = minimum if expected < minimum else maximum
            delta = abs(endpoint - expected) / expected if expected else abs(endpoint)
            candidates.append((endpoint, delta))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[1])


def _parameter_values(parameter: ExtractedParameter) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    if parameter.value is not None:
        values.append(parameter.value)
    if parameter.minimum is not None and parameter.maximum is not None:
        values.append(parameter.minimum)
        values.append(parameter.maximum)
    return tuple(values)


def _nearest_parameter(
    expected: Decimal,
    parameter: ExtractedParameter,
) -> tuple[Decimal, Decimal] | None:
    if parameter.minimum is not None and parameter.maximum is not None:
        if parameter.minimum <= expected <= parameter.maximum:
            return expected, Decimal("0")
        endpoint = parameter.minimum if expected < parameter.minimum else parameter.maximum
        delta = abs(endpoint - expected) / expected if expected else abs(endpoint)
        return endpoint, delta
    return _nearest_decimal(expected, _parameter_values(parameter))


def _find_numeric_match(
    constraint: NumericConstraint,
    attributes: ExtractedNumericAttributes,
    record_unit: str,
) -> tuple[Decimal, Decimal] | None:
    key = constraint.key
    unit = constraint.unit
    compatible_keys = {key}
    normalized_record_unit = normalize_unit(record_unit)
    if normalized_record_unit == "m" and key in {"power_w", "power_density_w_m"}:
        compatible_keys.update({"power_w", "power_density_w_m"})
    elif normalized_record_unit == "m2" and key in {"power_w", "power_density_w_m2"}:
        compatible_keys.update({"power_w", "power_density_w_m2"})
    candidates = (
        parameter
        for parameter in attributes.parameters
        if parameter.kind == "numeric"
        and parameter.key in compatible_keys
        and (
            normalize_unit(parameter.unit) == normalize_unit(unit)
            or (
                key == "power_w"
                and parameter.key == "power_density_w_m"
                and normalized_record_unit == "m"
            )
            or (
                key == "power_w"
                and parameter.key == "power_density_w_m2"
                and normalized_record_unit == "m2"
            )
            or (
                key == "power_density_w_m"
                and parameter.key == "power_w"
                and normalized_record_unit == "m"
            )
            or (
                key == "power_density_w_m2"
                and parameter.key == "power_w"
                and normalized_record_unit == "m2"
            )
        )
    )
    matches: list[tuple[Decimal, Decimal]] = []
    for parameter in candidates:
        if constraint.value is not None:
            match = _nearest_parameter(constraint.value, parameter)
            if match is not None:
                matches.append(match)
            continue
        assert constraint.minimum is not None and constraint.maximum is not None
        query_minimum = constraint.minimum
        query_maximum = constraint.maximum
        if parameter.minimum is not None and parameter.maximum is not None:
            if parameter.maximum >= query_minimum and parameter.minimum <= query_maximum:
                matches.append((max(parameter.minimum, query_minimum), Decimal("0")))
            else:
                gap = query_minimum - parameter.maximum if parameter.maximum < query_minimum else parameter.minimum - query_maximum
                baseline = query_minimum if parameter.maximum < query_minimum else query_maximum
                matches.append((parameter.maximum if parameter.maximum < query_minimum else parameter.minimum, abs(gap) / baseline if baseline else abs(gap)))
        else:
            for candidate_value in _parameter_values(parameter):
                if query_minimum <= candidate_value <= query_maximum:
                    matches.append((candidate_value, Decimal("0")))
                elif candidate_value < query_minimum:
                    matches.append((candidate_value, (query_minimum - candidate_value) / query_minimum if query_minimum else abs(query_minimum - candidate_value)))
                else:
                    matches.append((candidate_value, (candidate_value - query_maximum) / query_maximum if query_maximum else abs(candidate_value - query_maximum)))
    return min(matches, key=lambda item: item[1]) if matches else None


def _find_text_match(
    expected: str,
    key: str,
    attributes: ExtractedNumericAttributes,
) -> Decimal | None:
    expected_score = _similarity_text(expected)
    if not expected_score:
        return None
    scores = [
        _score(expected_score, parameter.text)
        for parameter in attributes.parameters
        if parameter.kind == "text" and parameter.key == key
    ]
    if not scores:
        return None
    return max(scores)


def _numeric_score(delta: Decimal) -> Decimal:
    return max(Decimal("0"), Decimal("100") * (Decimal("1") - delta))


def _weighted_score(parts: list[tuple[Decimal, Decimal]]) -> Decimal:
    weight_total = sum((weight for _, weight in parts), Decimal("0"))
    if not weight_total:
        return Decimal("0")
    score = sum((value * weight for value, weight in parts), Decimal("0"))
    return (score / weight_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def search_similar_boq_items(
    record_queryset,
    search_query: SimilarBoqSearchQuery,
) -> SimilarBoqSearchResponse:
    """Search only SQL facts visible in ``record_queryset``; callers own authorization."""

    current_groups = list(
        record_queryset.filter(
            document_version__document__project_id=search_query.current_project_id,
            status__in=(BoqItemRecord.Status.STAGING, BoqItemRecord.Status.TRUSTED),
        )
        .order_by()
        .values_list("party_a_group", flat=True)
        .distinct()
    )
    if not current_groups:
        raise ValueError("当前项目尚无可用的工程量清单甲方归属")
    if len(current_groups) != 1:
        raise ValueError(f"当前项目存在多个甲方归属：{current_groups}")
    party_a_group = current_groups[0]

    statuses = [BoqItemRecord.Status.TRUSTED]
    if search_query.include_staging:
        statuses.append(BoqItemRecord.Status.STAGING)
    candidate_queryset = (
        record_queryset.filter(
            party_a_group=party_a_group,
            kind=search_query.kind,
            status__in=statuses,
            unit_price__isnull=False,
        )
        .exclude(
            document_version__document__project_id=search_query.current_project_id
        )
        .select_related("document_version__document__project", "row_evidence")
    )
    normalized_unit = normalize_unit(search_query.unit)
    tolerance = search_query.tolerance_percent / Decimal("100")
    numeric_constraints = list(search_query.numeric_constraints)
    if search_query.power_w is not None:
        numeric_constraints.append(
            NumericConstraint("power_w", search_query.power_w, "W", search_query.tolerance_percent)
        )
    if search_query.color_temperature_k is not None:
        numeric_constraints.append(
            NumericConstraint("color_temperature_k", search_query.color_temperature_k, "K", search_query.tolerance_percent)
        )
    matches: list[SimilarBoqMatch] = []
    considered_count = 0
    for record in candidate_queryset.iterator(chunk_size=500):
        if normalize_unit(record.unit) != normalized_unit:
            continue
        considered_count += 1
        combined_text = "\n".join(
            part
            for part in (record.item_name, record.item_description, record.item_code)
            if normalize_text(part)
        )
        name_score = _score(search_query.query, record.item_name) if search_query.query else None
        if name_score is not None and name_score < Decimal("45"):
            continue
        model_score = (
            _score(search_query.model_hint, combined_text)
            if search_query.model_hint
            else None
        )
        specification_score = (
            _score(search_query.specification_hint, record.item_description)
            if search_query.specification_hint
            else None
        )
        attributes = extract_numeric_attributes(record.item_description)
        matched_power = None
        power_delta = None
        if search_query.power_w is not None:
            power_match = _nearest_decimal(search_query.power_w, attributes.powers_w)
            if power_match is None or power_match[1] > tolerance:
                continue
            matched_power, power_delta = power_match
        matched_temperature = None
        temperature_delta = None
        if search_query.color_temperature_k is not None:
            temperature_match = _nearest_temperature(
                search_query.color_temperature_k, attributes
            )
            if temperature_match is None or temperature_match[1] > tolerance:
                continue
            matched_temperature, temperature_delta = temperature_match

        matched_parameters: list[tuple[str, Decimal | None, Decimal | None]] = []
        for constraint in numeric_constraints:
            # Legacy power/color-temperature constraints are already evaluated above.
            if constraint.key == "power_w" and search_query.power_w is not None:
                matched = (matched_power, power_delta)
            elif constraint.key == "color_temperature_k" and search_query.color_temperature_k is not None:
                matched = (matched_temperature, temperature_delta)
            else:
                matched = _find_numeric_match(constraint, attributes, record.unit)
            if matched is None or matched[1] > constraint.tolerance_percent / Decimal("100"):
                break
            matched_parameters.append(
                (
                    constraint.key,
                    matched[0],
                    (matched[1] * Decimal("100")).quantize(Decimal("0.01")),
                )
            )
        else:
            for constraint in search_query.text_constraints:
                text_score = _find_text_match(constraint.value, constraint.key, attributes)
                if text_score is None or text_score < Decimal("60"):
                    break
            else:
                weighted_parts: list[tuple[Decimal, Decimal]] = []
                if name_score is not None:
                    weighted_parts.append((name_score, Decimal("0.50")))
                if model_score is not None:
                    weighted_parts.append((model_score, Decimal("0.15")))
                if specification_score is not None:
                    weighted_parts.append((specification_score, Decimal("0.15")))
                if power_delta is not None:
                    weighted_parts.append((_numeric_score(power_delta), Decimal("0.10")))
                if temperature_delta is not None:
                    weighted_parts.append((_numeric_score(temperature_delta), Decimal("0.10")))
                if matched_parameters:
                    weighted_parts.append(
                        (
                            _weighted_score(
                                [
                                    (_numeric_score(delta / Decimal("100")), Decimal("1"))
                                    for _, _, delta in matched_parameters
                                    if delta is not None
                                ]
                            ),
                            Decimal("0.10"),
                        )
                    )

                matches.append(
                    SimilarBoqMatch(
                        record=record,
                        score=_weighted_score(weighted_parts),
                        name_score=name_score,
                        model_score=model_score,
                        specification_score=specification_score,
                        matched_power_w=matched_power,
                        power_delta_percent=(
                            (power_delta * Decimal("100")).quantize(Decimal("0.01"))
                            if power_delta is not None
                            else None
                        ),
                        matched_color_temperature_k=matched_temperature,
                        color_temperature_delta_percent=(
                            (temperature_delta * Decimal("100")).quantize(Decimal("0.01"))
                            if temperature_delta is not None
                            else None
                        ),
                        extracted_attributes=attributes,
                        matched_parameters=tuple(matched_parameters),
                    )
                )
                continue
        # A selected parameter was missing or outside its independent tolerance.
        continue
    matches.sort(
        key=lambda match: (
            -match.score,
            match.record.project_name,
            match.record.item_name,
            match.record.source_row,
        )
    )
    return SimilarBoqSearchResponse(
        party_a_group=party_a_group,
        considered_count=considered_count,
        matches=tuple(matches[: search_query.limit]),
    )
