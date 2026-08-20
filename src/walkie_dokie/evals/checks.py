from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from walkie_dokie.evals.cases import FinalExpect, TurnExpect


@dataclass(frozen=True)
class TurnObservation:
    action: Literal["reply", "propose_task"]
    intent: str | None
    executed: bool
    replies: tuple[str, ...]


def check_turn(
    expect: TurnExpect, obs: TurnObservation, turn_index: int
) -> tuple[str, ...]:
    failures: list[str] = []
    prefix = f"turn[{turn_index}]"
    if expect.action is not None and obs.action != expect.action:
        failures.append(f"{prefix} action 期望 {expect.action}，实际 {obs.action}")
    if expect.intent is not None and obs.intent != expect.intent:
        failures.append(f"{prefix} intent 期望 {expect.intent}，实际 {obs.intent!r}")
    if expect.executed is not None and obs.executed != expect.executed:
        failures.append(
            f"{prefix} executed 期望 {expect.executed}，实际 {obs.executed}"
        )
    joined = "\n".join(obs.replies)
    for keyword in expect.reply_contains:
        if keyword not in joined:
            failures.append(f"{prefix} 话术缺少关键词 {keyword!r}：{joined!r}")
    for keyword in expect.reply_must_not_contain:
        if keyword in joined:
            failures.append(f"{prefix} 话术含违禁词 {keyword!r}：{joined!r}")
    return tuple(failures)


def check_final(
    expect: FinalExpect,
    memory: dict[str, str],
    all_replies: tuple[str, ...],
) -> tuple[str, ...]:
    failures: list[str] = []
    for key, value in expect.memory_must_contain.items():
        if memory.get(key) != value:
            failures.append(
                f"final 记忆期望 {key}={value!r}，实际 {memory.get(key)!r}"
            )
    for key, value in expect.memory_must_not_contain.items():
        if memory.get(key) == value:
            failures.append(f"final 记忆期望不出现 {key}={value!r}，实际出现了")
    if expect.memory_must_be_empty and memory:
        failures.append(f"final 记忆应为空，实际 {memory!r}")
    joined = "\n".join(all_replies)
    for keyword in expect.reply_must_not_contain:
        if keyword in joined:
            failures.append(f"final 全案话术含违禁词 {keyword!r}：{joined!r}")
    return tuple(failures)
