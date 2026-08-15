import asyncio
import logging
from types import SimpleNamespace

import pytest

import scripts.run_contract_feishu as contract_feishu
from walkie_dokie.orchestrator.locks import UserLocks
from walkie_dokie.platforms.base import InboundEvent


class FakePlatform:
    def __init__(self):
        self.sent = []

    async def send(self, target, message):
        self.sent.append((target, message))


@pytest.fixture(autouse=True)
def run_sync_bindings_inline(monkeypatch):
    def inline_sync_to_async(function, *, thread_sensitive):
        async def call(*args, **kwargs):
            return function(*args, **kwargs)

        return call

    monkeypatch.setattr(contract_feishu, "sync_to_async", inline_sync_to_async)


async def test_private_contract_messages_are_serialized_by_user(monkeypatch):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    call_count = 0

    def resolve_project(**kwargs):
        return SimpleNamespace(id="project-1")

    async def ask_question(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return {
            "status": "refused",
            "answer": "证据不足",
            "evidence": [],
            "question_run_id": f"run-{call_count}",
        }

    monkeypatch.setattr(contract_feishu, "resolve_external_project", resolve_project)
    monkeypatch.setattr(contract_feishu, "ask_intelligence_question", ask_question)
    platform = FakePlatform()
    locks = UserLocks()
    first = InboundEvent("feishu", "user-1", "/contract 第一个问题", None)
    second = InboundEvent("feishu", "user-1", "/contract 第二个问题", None)

    first_task = asyncio.create_task(
        contract_feishu.handle_contract_event(first, platform, locks)
    )
    await first_started.wait()
    second_task = asyncio.create_task(
        contract_feishu.handle_contract_event(second, platform, locks)
    )
    await asyncio.sleep(0)
    second_started_while_first_active = second_started.is_set()

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_started_while_first_active is False
    assert second_started.is_set() is True
    assert len(platform.sent) == 2


async def test_contract_failure_is_logged_before_safe_refusal(monkeypatch, caplog):
    def resolve_project(**kwargs):
        return SimpleNamespace(id="project-1")

    async def fail_question(**kwargs):
        raise RuntimeError("DeepSeek unavailable")

    monkeypatch.setattr(contract_feishu, "resolve_external_project", resolve_project)
    monkeypatch.setattr(contract_feishu, "ask_intelligence_question", fail_question)
    platform = FakePlatform()
    event = InboundEvent("feishu", "user-1", "/contract 合同期限", None)

    with caplog.at_level(logging.ERROR, logger="scripts.run_contract_feishu"):
        await contract_feishu.handle_contract_event(event, platform, UserLocks())

    assert "合同问答处理失败" in caplog.text
    assert "DeepSeek unavailable" in caplog.text
    assert "没有生成未经验证的答案" in platform.sent[0][1].text
