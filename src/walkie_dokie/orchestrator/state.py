from typing import TypedDict

from walkie_dokie.platforms.base import IncomingFile


class SessionState(TypedDict):
    """按 user_id 做 checkpoint 的会话状态。

    pending_* 字段跨消息累积：用户可能分几条消息把文件和指令发过来，
    图里的 collect 节点负责拼，够了才派发给执行 agent。
    new_* 字段是单次调用图时传入的"这一条新消息带来了什么"，
    collect 节点消费完就清空，不跨调用持久化。
    """

    platform: str
    user_id: str
    pending_file: IncomingFile | None
    pending_instruction: str | None
    new_text: str | None
    new_file: IncomingFile | None
    # 生成好、等用户确认的任务描述：{"task_summary": str, "missing_info": list[str]}。
    # 确认通过后才拿 task_summary 喂给执行 agent（不是拿 pending_instruction 原文喂——
    # draft 就是为了把原文提炼干净）。
    draft_task_prompt: dict | None
    # dict 而不是 ExecutionResult dataclass 直接存——checkpointer 序列化自定义类会报
    # deprecation 警告（未注册类型），存 plain dict 更省心。字段对齐 ExecutionResult：
    # reply_text / result_file / result_filename。
    result: dict | None
