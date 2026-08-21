"""逐轮驱动真 graph 的回归 driver。

刻意复用生产入口 ``scripts/run_mvp.py`` 的 ``_invoke_from_event`` 与
``build_outbound_messages``：eval 走的是线上同一条“状态查询 -> resume 或新回合”
路径、与生产同一个出站组装函数，否则测出来的是 driver 自己的胶水，不是产品行为。
观测只到组装为止——真正的发送在生产里由投递侧完成，eval 不需要平台替身。

driver 不捕获基础设施异常（fixture 缺失、图内部错误等），让它冒泡给入口脚本，
避免把 harness 故障静默记成样本失败。

已知不覆盖的支线：投递后的 compaction 触发只写在 ``run_mvp`` 的两个调用点
（``dispatch_fresh`` / ``handle_event``）里，driver 不走那两个函数，因此 eval
样本永远不会触发压缩回合。压缩行为由 ``tests/test_graph.py`` 和
``tests/test_run_mvp.py`` 覆盖。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from scripts.run_mvp import _invoke_from_event, build_outbound_messages
from walkie_dokie.evals.cases import GoldenCase
from walkie_dokie.evals.checks import TurnObservation, check_final, check_turn
from walkie_dokie.evals.fake_execution import RecordingExecutionAgent
from walkie_dokie.platforms.base import IncomingFile

_EVAL_PLATFORM = "eval"


@dataclass
class CaseResult:
    case_id: str
    category: str
    passed: bool
    failures: tuple[str, ...]
    turns: tuple[TurnObservation, ...]
    # 某轮断言失败中止时=该轮下标；全部跑完为 None。
    aborted_at_turn: int | None
    duration_ms: int
    # Task 7 之后由入口脚本填充的 LLM judge 结果。
    judge: dict | None = None


async def run_case(
    case: GoldenCase,
    *,
    graph,
    recorder: RecordingExecutionAgent,
    memory_repository,
    fixtures_dir: Path,
) -> CaseResult:
    started = time.monotonic()
    # 全案话术按轮次累积，供 final 断言查全局违禁词。
    all_replies: list[str] = []
    # thread_id/user_id 都带上 case.id：单次运行内，样本之间零共享 checkpoint 与长期档案。
    # 跨运行不隔离——这两个 key 都是跨运行稳定的，调用方必须每次运行构造全新
    # checkpointer 和全新 memory 目录，否则上次运行的残留档案会让 memory 断言假绿/假红。
    config = {"configurable": {"thread_id": f"{_EVAL_PLATFORM}:{case.id}"}}
    observations: list[TurnObservation] = []
    failures: list[str] = []
    aborted_at: int | None = None

    for index, turn in enumerate(case.turns):
        files = tuple(
            IncomingFile(
                filename=name,
                content=(fixtures_dir / name).read_bytes(),
                mime_type="application/octet-stream",
            )
            for name in turn.files
        )
        calls_before = len(recorder.calls)
        state, _trace_id = await _invoke_from_event(
            graph,
            config=config,
            platform_name=_EVAL_PLATFORM,
            user_id=case.id,
            text=turn.user,
            files=files,
            trace_id=f"{case.id}-turn{index}",
        )
        messages, _summary = build_outbound_messages(state)
        # 文件消息不产生话术，断言语料只看 text——与旧的平台捕获口径一致。
        replies = tuple(
            payload["text"] for kind, payload in messages if kind == "text"
        )
        all_replies.extend(replies)
        interrupted = "__interrupt__" in state
        obs = TurnObservation(
            action="propose_task" if interrupted else "reply",
            # intent 只在 interrupt 快照上可观测；回复轮的 decision 已被清空。
            intent=(
                (state.get("decision") or {}).get("intent") if interrupted else None
            ),
            executed=len(recorder.calls) > calls_before,
            replies=replies,
        )
        observations.append(obs)
        turn_failures = check_turn(turn.expect, obs, index)
        if turn_failures:
            failures.extend(turn_failures)
            aborted_at = index
            break

    memory = memory_repository.load(_EVAL_PLATFORM, case.id)
    failures.extend(check_final(case.final, memory, tuple(all_replies)))
    return CaseResult(
        case_id=case.id,
        category=case.category,
        passed=not failures,
        failures=tuple(failures),
        turns=tuple(observations),
        aborted_at_turn=aborted_at,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
