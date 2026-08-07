"""会话状态图骨架。

这张图管"这个用户这次对话走到哪一步了"（SessionState 的流转），
不管执行 agent 内部具体怎么把代码写出来跑通——那部分是 agents/ 下
各执行后端自己的 agentic loop，图里只把它当一个黑盒节点调用。

TODO: 用 langgraph.graph.StateGraph 定义节点：
    收到消息 -> 判断是否需要澄清 -> 调用执行 agent -> 等待用户确认 -> 回传结果
并接入 checkpoint 存储，支撑跨消息的暂停/恢复。
"""

from walkie_dokie.orchestrator.state import SessionState

__all__ = ["SessionState"]
