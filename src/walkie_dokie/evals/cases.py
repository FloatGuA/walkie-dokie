from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml


@dataclass(frozen=True)
class TurnExpect:
    action: Literal["reply", "propose_task"] | None = None
    intent: Literal["chat", "document_task"] | None = None
    executed: bool | None = None
    reply_contains: tuple[str, ...] = ()
    reply_must_not_contain: tuple[str, ...] = ()

    def has_assertion(self) -> bool:
        return (
            self.action is not None
            or self.intent is not None
            or self.executed is not None
            or bool(self.reply_contains)
            or bool(self.reply_must_not_contain)
        )


@dataclass(frozen=True)
class Turn:
    user: str
    files: tuple[str, ...] = ()
    expect: TurnExpect = TurnExpect()


@dataclass(frozen=True)
class FinalExpect:
    memory_must_contain: dict[str, str] = field(default_factory=dict)
    memory_must_not_contain: dict[str, str] = field(default_factory=dict)
    memory_must_be_empty: bool = False
    reply_must_not_contain: tuple[str, ...] = ()

    def has_assertion(self) -> bool:
        return (
            bool(self.memory_must_contain)
            or bool(self.memory_must_not_contain)
            or self.memory_must_be_empty
            or bool(self.reply_must_not_contain)
        )


@dataclass(frozen=True)
class GoldenCase:
    id: str
    category: str
    description: str
    turns: tuple[Turn, ...]
    final: FinalExpect = FinalExpect()


def _parse_expect(raw: dict, case_id: str) -> TurnExpect:
    known = {"action", "intent", "executed", "reply_contains", "reply_must_not_contain"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"样本 {case_id} 的 expect 含未知字段 {sorted(unknown)}")
    expect = TurnExpect(
        action=raw.get("action"),
        intent=raw.get("intent"),
        executed=raw.get("executed"),
        reply_contains=tuple(raw.get("reply_contains", ())),
        reply_must_not_contain=tuple(raw.get("reply_must_not_contain", ())),
    )
    if expect.action is not None and expect.action not in ("reply", "propose_task"):
        raise ValueError(f"样本 {case_id} 的 action 非法：{expect.action!r}")
    if expect.intent is not None and expect.action != "propose_task":
        raise ValueError(
            f"样本 {case_id}：intent 只在 interrupt 状态可观测，"
            "expect.intent 必须与 action: propose_task 同轮出现"
        )
    return expect


def _parse_case(raw: dict, category: str, fixtures_dir: Path) -> GoldenCase:
    case_id = raw.get("id")
    if not case_id or not raw.get("description") or not raw.get("turns"):
        raise ValueError(f"样本缺少 id/description/turns：{raw!r}")
    turns = []
    for item in raw["turns"]:
        files = tuple(item.get("files", ()))
        for name in files:
            if not (fixtures_dir / name).is_file():
                raise ValueError(f"样本 {case_id} 引用的 fixture 不存在：{name}")
        turns.append(
            Turn(
                user=item["user"],
                files=files,
                expect=_parse_expect(item.get("expect", {}), case_id),
            )
        )
    raw_final = raw.get("final", {})
    known = {
        "memory_must_contain",
        "memory_must_not_contain",
        "memory_must_be_empty",
        "reply_must_not_contain",
    }
    unknown = set(raw_final) - known
    if unknown:
        raise ValueError(f"样本 {case_id} 的 final 含未知字段 {sorted(unknown)}")
    final = FinalExpect(
        memory_must_contain=dict(raw_final.get("memory_must_contain", {})),
        memory_must_not_contain=dict(raw_final.get("memory_must_not_contain", {})),
        memory_must_be_empty=bool(raw_final.get("memory_must_be_empty", False)),
        reply_must_not_contain=tuple(raw_final.get("reply_must_not_contain", ())),
    )
    case = GoldenCase(
        id=case_id,
        category=category,
        description=raw["description"],
        turns=tuple(turns),
        final=final,
    )
    if not any(t.expect.has_assertion() for t in case.turns) and not final.has_assertion():
        raise ValueError(f"样本 {case_id} 没有任何断言，空样本会假绿")
    return case


def load_cases(
    cases_dir: Path,
    fixtures_dir: Path,
    extra_reply_blacklist: tuple[str, ...] = (),
) -> tuple[GoldenCase, ...]:
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for path in sorted(cases_dir.glob("*.yaml")):
        raw_list = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw_list, list):
            raise ValueError(f"{path.name} 顶层必须是样本列表")
        for raw in raw_list:
            case = _parse_case(raw, category=path.stem, fixtures_dir=fixtures_dir)
            if case.id in seen:
                raise ValueError(f"样本 id 重复：{case.id}")
            seen.add(case.id)
            if extra_reply_blacklist:
                case = GoldenCase(
                    id=case.id,
                    category=case.category,
                    description=case.description,
                    turns=case.turns,
                    final=FinalExpect(
                        memory_must_contain=case.final.memory_must_contain,
                        memory_must_not_contain=case.final.memory_must_not_contain,
                        memory_must_be_empty=case.final.memory_must_be_empty,
                        reply_must_not_contain=case.final.reply_must_not_contain
                        + tuple(extra_reply_blacklist),
                    ),
                )
            cases.append(case)
    if not cases:
        raise ValueError(f"{cases_dir} 下没有任何样本")
    return tuple(cases)
