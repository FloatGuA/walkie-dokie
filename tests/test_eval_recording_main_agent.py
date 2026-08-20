import asyncio

import pytest

from walkie_dokie.evals.recording_main_agent import RecordingMainAgent
from walkie_dokie.main_agent.base import (
    ConfirmationContext,
    DialogueContext,
    FinalizeContext,
    MainAgent,
    MainAgentDecision,
    TaskContract,
)
from walkie_dokie.agents.base import ExecutionReport


def _context():
    return DialogueContext(user_text="你好", input_filenames=(), known_facts={})


class _StubMainAgent(MainAgent):
    def __init__(self, decision=None, error=None, final_text="做完了"):
        self._decision = decision
        self._error = error
        self._final_text = final_text
        self.decide_calls = 0

    async def decide(self, context):
        self.decide_calls += 1
        if self._error is not None:
            raise self._error
        return self._decision

    async def finalize(self, context):
        if self._error is not None:
            raise self._error
        return self._final_text

    async def judge_confirmation(self, context):
        raise AssertionError("本测试不应触发确认判定")


async def test_delegates_and_records_nothing_on_success():
    decision = MainAgentDecision(intent="chat", action="reply", user_message="你好呀")
    inner = _StubMainAgent(decision=decision)
    recorder = RecordingMainAgent(inner)

    assert await recorder.decide(_context()) is decision
    assert inner.decide_calls == 1
    assert recorder.errors == []


async def test_finalize_delegates():
    recorder = RecordingMainAgent(_StubMainAgent(final_text="改好了"))
    context = FinalizeContext(
        task=TaskContract(instruction="改标题"),
        report=ExecutionReport(summary="ok"),
    )
    assert await recorder.finalize(context) == "改好了"
    assert recorder.errors == []


async def test_decide_error_is_recorded_and_reraised():
    boom = RuntimeError("DeepSeek API 报错")
    recorder = RecordingMainAgent(_StubMainAgent(error=boom))

    with pytest.raises(RuntimeError, match="DeepSeek API 报错"):
        await recorder.decide(_context())
    assert recorder.errors == [boom]


async def test_finalize_error_is_recorded_and_reraised():
    boom = RuntimeError("DeepSeek API 报错")
    recorder = RecordingMainAgent(_StubMainAgent(error=boom))
    context = FinalizeContext(
        task=TaskContract(instruction="改标题"),
        report=ExecutionReport(summary="ok"),
    )

    with pytest.raises(RuntimeError):
        await recorder.finalize(context)
    assert recorder.errors == [boom]


async def test_cancellation_from_timeout_is_recorded():
    """graph 的 asyncio.timeout(60) 靠取消实现——超时也必须留下记录。"""

    class _HangingMainAgent(MainAgent):
        async def decide(self, context):
            await asyncio.sleep(10)
            raise AssertionError("不该跑到这里")

        async def finalize(self, context):
            raise AssertionError("不该跑到这里")

        async def judge_confirmation(self, context):
            raise AssertionError("本测试不应触发确认判定")

    recorder = RecordingMainAgent(_HangingMainAgent())
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await recorder.decide(_context())
    assert len(recorder.errors) == 1


async def test_judge_confirmation_error_is_recorded_and_reraised():
    class Boom(MainAgent):
        async def decide(self, context):  # pragma: no cover - 不触发
            raise AssertionError

        async def finalize(self, context):  # pragma: no cover - 不触发
            raise AssertionError

        async def judge_confirmation(self, context):
            raise RuntimeError("judge 挂了")

    recorder = RecordingMainAgent(Boom())
    with pytest.raises(RuntimeError, match="judge 挂了"):
        await recorder.judge_confirmation(
            ConfirmationContext(task_instruction="t", proposal_message="p", user_reply="嗯")
        )
    assert len(recorder.errors) == 1
