from .base import InboundEvent, OutboundMessage, PlatformAdapter


class WeComAdapter(PlatformAdapter):
    """企业微信自建应用适配器。

    TODO: 接入企业微信自建应用回调 API（URL 验证、消息加解密、被动回复）。
    参考 https://developer.work.weixin.qq.com/document/path/90930
    """

    async def receive(self) -> InboundEvent:
        raise NotImplementedError

    async def send(self, user_id: str, message: OutboundMessage) -> None:
        raise NotImplementedError
