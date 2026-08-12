import asyncio


class UserLocks:
    """按 session key 分锁，串行化对同一个 LangGraph thread 的 ainvoke() 调用。

    实测验证过：同一用户在 execute 还没跑完时又发一条消息，会对同一个
    thread_id 触发第二次并发 ainvoke()，两次调用各自独立完成、互不报错，
    但最终 checkpoint 状态是错的（其中一次的 result 会丢，卡在一个跟原始
    任务对不上的新确认问题上，见 PITFALLS.md）。LangGraph 不会替我们做这层
    互斥，得自己加。不同用户之间不受影响，各自的锁互不相干。
    """

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())
