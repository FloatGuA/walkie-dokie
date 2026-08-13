"""Security boundary for untrusted document-execution inputs.

User instructions, filenames, and document contents are all attacker-controlled.  A
prompt can help the model remember that fact, but only OS-enforced filesystem/network
limits and deterministic artifact validation are security controls.
"""

from __future__ import annotations

import os
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


MAX_OFFICE_FILE_BYTES = 25 * 1024 * 1024
MAX_OFFICE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_OFFICE_ZIP_ENTRIES = 5_000
MAX_REPORT_TEXT_CHARS = 8_000

_SENSITIVE_ENV_NAME_RE = re.compile(
    r"(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|COOKIE|SESSION|AUTH)", re.I
)
_BLOCKED_PACKAGE_PARTS = (
    "vbaproject.bin",
    "activex/",
    "embeddings/",
    "externallinks/",
    "customui/",
    "oleobject",
)
_BLOCKED_WORD_FIELD = re.compile(
    r"^\s*(?:DDEAUTO|DDE|INCLUDETEXT|INCLUDEPICTURE|LINK)\b", re.I
)
_BLOCKED_EXCEL_FORMULA = re.compile(
    r"\b(?:WEBSERVICE|RTD|REGISTER\.ID)\s*\(|(?:https?|ftp|file)://", re.I
)


def sensitive_environment_names() -> tuple[str, ...]:
    """Return ambient variables that must never reach model-generated commands."""

    return tuple(sorted(name for name in os.environ if _SENSITIVE_ENV_NAME_RE.search(name)))


def sensitive_environment_overrides() -> dict[str, str]:
    """SDK env values override (rather than remove) inherited process variables."""

    return {name: "" for name in sensitive_environment_names()}


def sanitized_subprocess_environment() -> dict[str, str]:
    """Keep the model client functional while removing ambient application secrets."""

    return {
        name: value
        for name, value in os.environ.items()
        if not _SENSITIVE_ENV_NAME_RE.search(name)
    }


def claude_sandbox_settings(workdir: Path) -> dict:
    """Build a fail-closed Claude Code Bash sandbox for exactly one execution.

    The Claude client itself still needs its login and outbound model connection.  These
    settings apply to model-generated Bash commands: they cannot read other users' data,
    access the network, inherit application credentials, or opt out of the sandbox.
    """

    resolved_workdir = workdir.resolve()
    runtime_root = Path(sys.prefix).resolve()
    denied_user_roots = ["/home", "/root", "/tmp", "/var/tmp"]
    return {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        "filesystem": {
            "denyRead": denied_user_roots,
            "allowRead": [str(resolved_workdir), str(runtime_root)],
            "denyWrite": denied_user_roots,
            "allowWrite": [str(resolved_workdir)],
        },
        "network": {
            "allowedDomains": [],
            "deniedDomains": ["*"],
            "allowUnixSockets": [],
            "allowAllUnixSockets": False,
            "allowLocalBinding": False,
        },
        "credentials": {
            "envVars": [
                {"name": name, "mode": "deny"}
                for name in sensitive_environment_names()
            ]
        },
    }


def _validate_zip_member_name(name: str) -> None:
    normalized = name.replace("\\", "/")
    member = PurePosixPath(normalized)
    if member.is_absolute() or ".." in member.parts or normalized.startswith("/"):
        raise RuntimeError(f"Office 文件包含不安全的 ZIP 路径：{name!r}")


def _read_small_xml(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > 5 * 1024 * 1024:
        raise RuntimeError(f"Office XML 部件过大：{info.filename!r}")
    return archive.read(info)


def _local_name(qualified_name: str) -> str:
    return qualified_name.rsplit("}", 1)[-1]


def _parse_xml(archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, role: str):
    try:
        return ElementTree.fromstring(_read_small_xml(archive, info))
    except ElementTree.ParseError as exc:
        raise RuntimeError(f"{role}包含损坏的 XML：{info.filename!r}") from exc


def _validate_relationships(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, role: str
) -> None:
    root = _parse_xml(archive, info, role=role)
    for relation in root:
        target_mode = relation.attrib.get("TargetMode", "")
        target = relation.attrib.get("Target", "")
        if target_mode.casefold() == "external" or re.match(
            r"(?i)^(?:https?|ftp|file):", target.strip()
        ):
            raise RuntimeError(f"{role}包含外部关系，拒绝处理：{info.filename!r}")


def _validate_word_fields(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, role: str
) -> None:
    root = _parse_xml(archive, info, role=role)
    instruction_fragments: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) == "instrText" and element.text:
            instruction_fragments.append(element.text)
        if _local_name(element.tag) == "fldSimple":
            for name, value in element.attrib.items():
                if _local_name(name) == "instr" and _BLOCKED_WORD_FIELD.search(value):
                    raise RuntimeError(
                        f"{role}包含可访问外部资源的 Word 字段：{info.filename!r}"
                    )

    # Field instructions may be split over multiple runs, so inspect both each run
    # and the concatenated instruction stream.
    candidates = [*instruction_fragments, "".join(instruction_fragments)]
    if any(_BLOCKED_WORD_FIELD.search(value) for value in candidates):
        raise RuntimeError(f"{role}包含可访问外部资源的 Word 字段：{info.filename!r}")


def _validate_excel_formulas(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, role: str
) -> None:
    root = _parse_xml(archive, info, role=role)
    for element in root.iter():
        if _local_name(element.tag) not in {"f", "definedName"}:
            continue
        formula = "".join(element.itertext())
        if _BLOCKED_EXCEL_FORMULA.search(formula):
            raise RuntimeError(
                f"{role}包含可访问外部资源的 Excel 公式：{info.filename!r}"
            )


def validate_office_artifact(path: Path, *, role: str) -> None:
    """Reject active content, external relationships, zip bombs, and odd formats."""

    resolved = path.resolve()
    if not resolved.is_file():
        raise RuntimeError(f"{role}不存在或不是普通文件：{resolved}")
    if resolved.stat().st_size > MAX_OFFICE_FILE_BYTES:
        raise RuntimeError(f"{role}超过 {MAX_OFFICE_FILE_BYTES // 1024 // 1024} MiB 限制")
    suffix = resolved.suffix.casefold()
    if suffix not in {".docx", ".xlsx"}:
        raise RuntimeError(f"{role}只允许安全的 .docx 或 .xlsx 文件：{resolved.name!r}")
    if not zipfile.is_zipfile(resolved):
        raise RuntimeError(f"{role}不是有效的 Office Open XML 文件：{resolved.name!r}")

    total_uncompressed = 0
    names: set[str] = set()
    with zipfile.ZipFile(resolved) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_OFFICE_ZIP_ENTRIES:
            raise RuntimeError(f"{role}包含过多 ZIP 部件")
        for info in infos:
            _validate_zip_member_name(info.filename)
            if info.flag_bits & 0x1:
                raise RuntimeError(f"{role}包含加密 ZIP 部件，拒绝处理")
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_OFFICE_UNCOMPRESSED_BYTES:
                raise RuntimeError(f"{role}解压后体积超过安全限制")
            if (
                info.file_size > 10 * 1024 * 1024
                and info.compress_size > 0
                and info.file_size / info.compress_size > 1_000
            ):
                raise RuntimeError(f"{role}疑似 ZIP bomb：{info.filename!r}")
            lowered = info.filename.casefold()
            names.add(lowered)
            if any(marker in lowered for marker in _BLOCKED_PACKAGE_PARTS):
                raise RuntimeError(f"{role}包含宏、嵌入对象或外部链接部件：{info.filename!r}")

        required = (
            {"[content_types].xml", "word/document.xml"}
            if suffix == ".docx"
            else {"[content_types].xml", "xl/workbook.xml"}
        )
        if not required.issubset(names):
            raise RuntimeError(f"{role}的 Office 包结构不完整：{resolved.name!r}")

        for info in infos:
            lowered = info.filename.casefold()
            if lowered.endswith(".rels"):
                _validate_relationships(archive, info, role=role)
            elif suffix == ".docx" and lowered.startswith("word/") and lowered.endswith(
                ".xml"
            ):
                _validate_word_fields(archive, info, role=role)
            elif suffix == ".xlsx" and (
                lowered.startswith("xl/worksheets/")
                or lowered == "xl/workbook.xml"
            ) and lowered.endswith(".xml"):
                _validate_excel_formulas(archive, info, role=role)


def validate_report_text(value: str, *, field: str) -> str:
    """Bound untrusted model report fields before another model sees them."""

    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"执行报告 {field} 必须是非空字符串")
    cleaned = value.strip()
    if len(cleaned) > MAX_REPORT_TEXT_CHARS:
        raise RuntimeError(f"执行报告 {field} 过长")
    if "\x00" in cleaned:
        raise RuntimeError(f"执行报告 {field} 包含非法控制字符")
    return cleaned
