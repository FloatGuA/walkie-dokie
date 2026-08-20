"""包一层主 Agent，把它抛出的异常记录下来供 eval 入口判定基础设施故障。

graph 的 ``main_agent`` 节点会捕获 ``decide``/``finalize`` 的任何异常并降级成一句
确定性 reply——生产上这是对的（用户不该看到 traceback），但在 eval 里同一个行为会
让「DeepSeek 全挂」伪装成一堆正常 reply 轮：少数样本以错误理由变绿，其余红成假回归。
这里在异常原样 re-raise 之前留下记录，入口脚本每个样本跑完检查 ``errors``，非空即
判 FAILED_INFRA 立即终止。
"""

from __future__ import annotations

from walkie_dokie.main_agent.base import (
    ConfirmationContext,
    ConfirmationVerdict,
    DialogueContext,
    FinalizeContext,
    MainAgent,
    MainAgentDecision,
)


class RecordingMainAgent(MainAgent):
    def __init__(self, inner: MainAgent):
        self._inner = inner
        self.errors: list[BaseException] = []

    async def decide(self, context: DialogueContext) -> MainAgentDecision:
        # 捕获 BaseException 而不是 Exception：graph 的 asyncio.timeout(60) 是靠
        # 取消内层任务实现的，超时在这里表现为 CancelledError。
        try:
            return await self._inner.decide(context)
        except BaseException as exc:
            self.errors.append(exc)
            raise

    async def finalize(self, context: FinalizeContext) -> str:
        try:
            return await self._inner.finalize(context)
        except BaseException as exc:
            self.errors.append(exc)
            raise

    async def judge_confirmation(
        self, context: ConfirmationContext
    ) -> ConfirmationVerdict:
        try:
            return await self._inner.judge_confirmation(context)
        except BaseException as exc:
            self.errors.append(exc)
            raise
