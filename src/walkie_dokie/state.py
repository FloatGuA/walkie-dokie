from typing import Literal, TypedDict


class WorkOrderState(TypedDict):
    raw_input: str
    normalized: str
    missing_slots: list[str]
    clarify_rounds: int
    order_draft: dict
    risk_level: Literal["safe", "confirm", "escalate"]
    approval: dict | None
