"""长期用户记忆的确定性存储与写入策略。

主 Agent 可以提出 set/delete 操作，但这里只接受固定的个人资料字段。模型不直接
操作 JSON 文件，也不能动态发明字段；这是“语义判断归主 Agent，落盘约束归存储
策略”的边界。
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from walkie_dokie.main_agent.base import MemoryOperation

logger = logging.getLogger(__name__)

_VAR_ROOT = Path(__file__).parent.parent.parent.parent / "var"
MEMORY_DIR = _VAR_ROOT / "memory"

FIELD_LABELS = {
    "name": "姓名",
    "department": "部门",
    "job_title": "职位",
    "preferred_address": "常用称呼",
}
_LEGACY_FIELD_ALIASES = {
    "姓名": "name",
    "部门": "department",
    "职位": "job_title",
    "常用称呼": "preferred_address",
}
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_key_part(value: str) -> str:
    """Return a fixed cryptographic key; never put an external ID in a path."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _legacy_safe_key_part(value: str) -> str:
    """v1 文件名算法，仅用于读取已有档案；它可能碰撞，不能再用于写入。"""
    return _SAFE_KEY_RE.sub("_", value).strip("._") or "unknown"


_DELETE_TERMS = re.compile(r"忘记|删除|清除|别记|不要记|forget|delete|remove", re.I)
_FIELD_DELETE_TERMS = {
    "name": re.compile(r"姓名|名字|name", re.I),
    "department": re.compile(r"部门|department", re.I),
    "job_title": re.compile(r"职位|职务|岗位|job|title|position", re.I),
    "preferred_address": re.compile(r"称呼|怎么叫|address|call me", re.I),
}
_NON_ASSERTION_CONTEXT = re.compile(
    r"(?:文档|文章|例句|示例|翻译|改写|引用|原文|写一句|写上|内容|标题|"
    r"用户说|他说|她说|对方说|别人说|不是|并非|如果|假设|以为|别说|不要说|"
    r"document|example|translate|quote|sentence|if|suppose)",
    re.I,
)
_NEGATED_DELETE = re.compile(
    r"(?:不要|别|不能|无需|不必|请勿).{0,6}(?:忘记|删除|清除)|"
    r"(?:不要|别|不能|无需|不必|请勿)(?:忘记|删除|清除)",
    re.I,
)


def _set_evidence_pattern(field: str, value: str) -> re.Pattern[str]:
    escaped = re.escape(value.strip())
    prefixes = {
        "name": (
            r"(?:我(?:的)?(?:名字|姓名)(?:叫|是|改成|改为)?|我叫|"
            r"本人(?:的)?(?:名字|姓名)?(?:叫|是)|my\s+name\s+is|i\s+am)"
        ),
        "department": (
            r"(?:我(?:的)?部门(?:是|改成|改为)?|我(?:在|属于|来自)|"
            r"my\s+department\s+is|i\s+work\s+in)"
        ),
        "job_title": (
            r"(?:我(?:的)?(?:职位|职务|岗位)(?:是|改成|改为)?|我(?:是|担任|做)|"
            r"my\s+(?:job\s+title|position)\s+is|i\s+am(?:\s+an?|\s+the)?)"
        ),
        "preferred_address": (
            r"(?:请?叫我|称呼我(?:为)?|可以叫我|我(?:的)?称呼(?:是)?|call\s+me)"
        ),
    }
    return re.compile(
        prefixes[field] + r"[\s:：为叫是，,\"'“”‘’「」『』]*" + escaped,
        re.I,
    )


def _operation_has_user_evidence(
    operation: MemoryOperation, source_text: str
) -> bool:
    """Conservatively check that a candidate is grounded in this user turn.

    This is intentionally stricter than a model prompt.  A false negative merely means
    we do not remember a fact; a false positive can silently poison long-term identity.
    """

    evidence = (operation.evidence or "").strip()
    if not evidence or evidence not in source_text:
        return False
    if operation.action == "delete":
        if source_text.rstrip().endswith(("?", "？")) or _NEGATED_DELETE.search(
            source_text
        ):
            return False
        return bool(
            _DELETE_TERMS.search(evidence)
            and _FIELD_DELETE_TERMS[operation.field].search(evidence)
        )
    value = (operation.value or "").strip()
    if not value or value not in evidence:
        return False
    if source_text.rstrip().endswith(("?", "？")):
        return False
    start = source_text.find(evidence)
    context_through_evidence = source_text[: start + len(evidence)]
    if _NON_ASSERTION_CONTEXT.search(context_through_evidence):
        return False
    opening_quotes = {'"', "'", "“", "‘", "「", "『"}
    closing_quotes = {'"', "'", "”", "’", "」", "』"}
    before = source_text[start - 1] if start else ""
    after_index = start + len(evidence)
    after = source_text[after_index] if after_index < len(source_text) else ""
    if before in opening_quotes and after in closing_quotes:
        return False
    return bool(_set_evidence_pattern(operation.field, value).search(evidence))


class MemoryRepository(ABC):
    @abstractmethod
    def load(self, platform: str, user_id: str) -> dict[str, str]: ...

    @abstractmethod
    def validate(
        self, operations: tuple[MemoryOperation, ...], *, source_text: str
    ) -> tuple[MemoryOperation, ...]: ...

    @abstractmethod
    def apply(
        self,
        platform: str,
        user_id: str,
        operations: tuple[MemoryOperation, ...],
        *,
        source_text: str,
    ) -> list[dict]: ...


class JsonMemoryRepository(MemoryRepository):
    def __init__(self, directory: Path | None = None):
        self.directory = directory or MEMORY_DIR

    def _path(self, platform: str, user_id: str) -> Path:
        return self.directory / (
            f"v2_{_safe_key_part(platform)}_{_safe_key_part(user_id)}.json"
        )

    def _legacy_path(self, platform: str, user_id: str) -> Path:
        return self.directory / (
            f"{_legacy_safe_key_part(platform)}_{_legacy_safe_key_part(user_id)}.json"
        )

    def _load(self, platform: str, user_id: str, *, strict: bool) -> dict[str, str]:
        path = self._path(platform, user_id)
        if not path.exists():
            legacy_path = self._legacy_path(platform, user_id)
            # 旧算法对包含 /? 等字符的 ID 不是单射。只对完全不需要清洗、来源
            # 可确定的旧 ID 透明读取；其他 ID 必须做带来源映射的离线迁移。
            legacy_is_unambiguous = (
                _legacy_safe_key_part(platform) == platform
                and _legacy_safe_key_part(user_id) == user_id
            )
            if legacy_is_unambiguous and legacy_path.exists():
                path = legacy_path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if strict:
                raise RuntimeError(f"用户档案不可读，拒绝覆盖：{path}")
            logger.exception("用户档案读取失败，读取时按空档案处理 path=%s", path)
            return {}
        if not isinstance(raw, dict):
            if strict:
                raise RuntimeError(f"用户档案不是 JSON object，拒绝覆盖：{path}")
            logger.warning("用户档案不是 JSON object，按空档案处理 path=%s", path)
            return {}

        normalized: dict[str, str] = {}
        for raw_field, value in raw.items():
            field = _LEGACY_FIELD_ALIASES.get(raw_field, raw_field)
            if field in FIELD_LABELS and isinstance(value, str) and value.strip():
                normalized[field] = value.strip()
        return normalized

    def load(self, platform: str, user_id: str) -> dict[str, str]:
        return self._load(platform, user_id, strict=False)

    def validate(
        self, operations: tuple[MemoryOperation, ...], *, source_text: str
    ) -> tuple[MemoryOperation, ...]:
        validated: list[MemoryOperation] = []
        for operation in operations:
            if operation.field not in FIELD_LABELS:
                logger.warning("拒绝未知 memory 字段 field=%r", operation.field)
                continue
            if not _operation_has_user_evidence(operation, source_text):
                logger.warning(
                    "拒绝缺少当前用户原文证据的 memory 操作 field=%s evidence=%r",
                    operation.field,
                    operation.evidence,
                )
                continue
            if operation.action == "set":
                value = (operation.value or "").strip()
                if not value or len(value) > 200:
                    logger.warning(
                        "拒绝空值或过长的 memory 值 field=%s length=%d",
                        operation.field,
                        len(value),
                    )
                    continue
            validated.append(operation)
        return tuple(validated)

    def apply(
        self,
        platform: str,
        user_id: str,
        operations: tuple[MemoryOperation, ...],
        *,
        source_text: str,
    ) -> list[dict]:
        if not operations:
            return []

        validated = self.validate(operations, source_text=source_text)
        if not validated:
            return []

        facts = self._load(platform, user_id, strict=True)
        changes: list[dict] = []
        for operation in validated:
            if operation.action == "set":
                value = (operation.value or "").strip()
                if facts.get(operation.field) == value:
                    continue
                facts[operation.field] = value
                changes.append(
                    {
                        "action": "set",
                        "field": operation.field,
                        "label": FIELD_LABELS[operation.field],
                        "value": value,
                    }
                )
            elif operation.action == "delete":
                if operation.field not in facts:
                    continue
                facts.pop(operation.field)
                changes.append(
                    {
                        "action": "delete",
                        "field": operation.field,
                        "label": FIELD_LABELS[operation.field],
                        "value": None,
                    }
                )

        if changes:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self._path(platform, user_id)
            # 同一 session 在 runner 里会串行，但原子 replace 仍能避免进程在写到一半
            # 时留下截断 JSON。临时文件与目标同目录，确保 os.replace 同文件系统。
            fd, temporary_name = tempfile.mkstemp(
                dir=self.directory, prefix=f".{target.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as temporary:
                    json.dump(facts, temporary, ensure_ascii=False, indent=2)
                os.replace(temporary_name, target)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
            logger.info(
                "更新用户档案 platform=%s user_id=%s changes=%r",
                platform,
                user_id,
                changes,
            )
        return changes


def render_memory_notice(changes: list[dict]) -> str:
    """把实际落盘的变更透明回显；不复述被策略拒绝的候选。"""
    if not changes:
        return ""
    lines = []
    for change in changes:
        if change["action"] == "set":
            lines.append(f'{change["label"]}：{change["value"]}')
        else:
            lines.append(f'已忘记：{change["label"]}')
    return "我记住了：\n" + "\n".join(lines) + "\n\n如果记错了，直接告诉我修改或忘掉就行。"


def render_memory_proposal(operations: tuple[MemoryOperation, ...]) -> str:
    """Render candidates before mutation; candidates never imply data was saved."""
    lines = []
    for operation in operations:
        label = FIELD_LABELS[operation.field]
        if operation.action == "set":
            lines.append(f"{label}：{operation.value}")
        else:
            lines.append(f"忘记：{label}")
    return "建议保存的长期资料：\n" + "\n".join(lines)
