import asyncio

from walkie_dokie.orchestrator.locks import UserLocks


def test_same_user_returns_same_lock_object():
    locks = UserLocks()
    assert locks.get("u1") is locks.get("u1")


def test_different_users_return_different_lock_objects():
    locks = UserLocks()
    assert locks.get("u1") is not locks.get("u2")


async def test_same_user_lock_serializes_concurrent_access():
    """回归测试：这就是 2026-08-09 那次并发竞态修复要守住的行为——
    同一用户不能有两次并发的图调用交错跑。"""
    locks = UserLocks()
    order = []

    async def task(name, delay):
        async with locks.get("u1"):
            order.append(f"{name}-start")
            await asyncio.sleep(delay)
            order.append(f"{name}-end")

    await asyncio.gather(task("a", 0.05), task("b", 0.01))
    assert order == ["a-start", "a-end", "b-start", "b-end"]


async def test_different_users_are_not_serialized():
    locks = UserLocks()
    order = []

    async def task(user_id, name, delay):
        async with locks.get(user_id):
            order.append(f"{name}-start")
            await asyncio.sleep(delay)
            order.append(f"{name}-end")

    await asyncio.gather(task("u1", "a", 0.05), task("u2", "b", 0.01))
    assert order.index("b-end") < order.index("a-end")
