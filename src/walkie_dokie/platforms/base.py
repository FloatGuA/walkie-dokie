from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

Platform = Literal["feishu", "wecom", "qq", "wechat"]


@dataclass
class IncomingFile:
    filename: str
    content: bytes
    mime_type: str


@dataclass
class InboundEvent:
    """一次用户消息，已从具体平台格式归一化。"""

    platform: Platform
    user_id: str
    text: str | None
    file: IncomingFile | None
    # Group chat and private chat must not share query context or reply routing.
    conversation_id: str | None = None
    conversation_type: Literal["private", "group"] | None = None
    # 平台侧的事件唯一 id，用于挡住重复推送（飞书回调超时会重投同一条事件）。
    # 平台没给就是 None——不拿 message_id 之类的东西冒充，宁可重复处理一次。
    event_id: str | None = None

    @property
    def reply_target(self) -> str:
        if self.conversation_type == "group" and self.conversation_id:
            return f"chat:{self.conversation_id}"
        return self.user_id


@dataclass
class OutboundMessage:
    text: str | None = None
    file: IncomingFile | None = None


class PlatformAdapter(ABC):
    """平台适配层的统一接口：把具体平台的收发消息格式转换成内部 Event。

    orchestrator 只认这个接口，不关心消息具体是从企业微信、QQ 还是微信来的。
    """

    @abstractmethod
    async def receive(self) -> InboundEvent: ...

    @abstractmethod
    async def send(self, user_id: str, message: OutboundMessage) -> None: ...
