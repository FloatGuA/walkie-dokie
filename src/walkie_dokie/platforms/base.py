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
