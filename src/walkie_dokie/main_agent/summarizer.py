"""短期历史压缩：模型提摘要候选，代码做确定性校验（结构对照 memory.py）。"""

from __future__ import annotations

_MAX_FACT_CHARS = 200


def validate_entries(
    candidates,
    *,
    source_texts: tuple[str, ...],
    max_entries: int = 6,
) -> tuple[tuple[dict, ...], tuple[str, ...]]:
    accepted: list[dict] = []
    rejected: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            rejected.append(f"候选不是 dict：{candidate!r}")
            continue
        fact = candidate.get("fact")
        if not isinstance(fact, str) or not fact.strip():
            rejected.append(f"fact 缺失或为空：{candidate!r}")
            continue
        if len(fact) > _MAX_FACT_CHARS:
            rejected.append(f"fact 超过 {_MAX_FACT_CHARS} 字符：{fact[:40]!r}…")
            continue
        evidence = candidate.get("evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) and item for item in evidence)
        ):
            rejected.append(f"evidence 缺失/为空/含非字符串：{candidate!r}")
            continue
        missing = [
            item
            for item in evidence
            if not any(item in source for source in source_texts)
        ]
        if missing:
            rejected.append(f"evidence 非源文本逐字子串：{missing!r}")
            continue
        if len(accepted) >= max_entries:
            rejected.append(f"超出单次上限 {max_entries} 条，截断：{fact!r}")
            continue
        accepted.append({"fact": fact, "evidence": list(evidence)})
    return tuple(accepted), tuple(rejected)
