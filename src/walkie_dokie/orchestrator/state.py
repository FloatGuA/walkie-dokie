from typing import Literal, TypedDict

from walkie_dokie.platforms.base import Platform


class SessionState(TypedDict):
    platform: Platform
    user_id: str
    pending_file: dict | None
    instruction: str | None
    backend: Literal["claude_agent_sdk", "codex"]
    status: Literal["awaiting_input", "running", "awaiting_confirm", "done"]
    result_file: dict | None
