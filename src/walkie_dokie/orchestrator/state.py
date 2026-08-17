from typing import TypedDict

class SessionState(TypedDict):
    """按 ``platform:user_id`` thread key 做 checkpoint 的会话状态。

    pending_* 字段跨消息累积：用户可能分几条消息把文件和指令发过来，
    图里的 collect 节点负责拼，够了才派发给执行 agent。
    new_* 字段是单次调用图时传入的"这一条新消息带来了什么"，
    collect 节点消费完就清空，不跨调用持久化。
    """

    platform: str
    user_id: str
    # 输入文件在入图前落盘；这里只保存 plain dict artifact reference，不保存 bytes
    # 或自定义 dataclass，避免 SQLite checkpoint 膨胀和序列化兼容风险。
    pending_files: tuple[dict, ...]
    # Debouncer 批量派发的新文件，只在 collect 消费时并入 pending_files；
    # ask_confirm/ask_memory 恢复时的单文件补充仍走下面的 new_file，两条路径
    # 分开是因为确认阶段的追加文件本来就不是这次多文件设计要处理的范围。
    new_files: tuple[dict, ...]
    pending_instruction: str | None
    new_text: str | None
    new_file: dict | None
    # 本次 collect 消费的最后一条用户原文，专供 memory evidence；不同于可能跨
    # 多条消息累积的 pending_instruction。
    current_user_text: str | None
    # 最近可复用的输入/输出 artifact 集合。主 Agent 可显式在 TaskContract 中选择它，
    # 支持“继续修改刚才生成的文件”，但执行层不会自行猜测。
    active_artifacts: tuple[dict, ...]
    # 已确认执行的稳定标识与工作目录；prepare 节点先 checkpoint，再进入有副作用
    # 的 execute 节点，便于用落盘 report marker 抵御 checkpoint 后置失败的重跑。
    execution: dict | None
    # 最近已完成的用户/助手回合，供 MainAgent 理解“继续刚才那个”这类跨回合引用；
    # 固定上限避免 checkpoint 无限膨胀。确认中的当前任务由 pending_* 表达，
    # 不提前写入 history，避免补充说明时重复。
    recent_messages: list[dict[str, str]]
    # 主 Agent 的结构化决策：intent/action/user_message/task/memory_operations。确认通过后
    # 只把 task contract 交给执行 Agent，不把对话历史或整份长期档案倾倒过去。
    decision: dict | None
    # dict 而不是 ExecutionReport dataclass 直接存——checkpointer 序列化自定义类会报
    # deprecation 警告（未注册类型），存 plain dict 更省心。这里是给平台投递的结果：
    # reply_text / artifacts / success；artifacts 是引用列表，不是大块文件 bytes。
    result: dict | None
    # 本回合实际落盘的 memory set/delete 操作。透明回显已经并进主 Agent 的
    # user_message；保留此字段用于诊断/测试，不跨回合复用。
    memory_changes: list[dict] | None
    # 仅用于恢复旧版“确认后保存”checkpoint；新回合会隐式保存长期记忆，并把
    # 确定性结果直接追加到 decision.user_message。不跨回合复用。
    memory_feedback: str | None
