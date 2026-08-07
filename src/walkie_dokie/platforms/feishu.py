import asyncio
import io
import json
import logging
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)

from .base import IncomingFile, InboundEvent, OutboundMessage, PlatformAdapter

logger = logging.getLogger(__name__)


class FeishuAdapter(PlatformAdapter):
    """飞书自建应用适配器，走长连接接收消息（不需要公网回调地址）。

    文件消息的接收暂未实现——先跑通纯文本指令这条链路，收文件是下一步。
    """

    def __init__(self, app_id: str, app_secret: str):
        self._client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        self._ws_client = lark.ws.Client(
            app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.INFO
        )

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        message = data.event.message
        text = None
        if message.message_type == "text":
            text = json.loads(message.content).get("text")

        user_id = data.event.sender.sender_id.open_id
        logger.info("收到飞书消息 user_id=%s message_type=%s text=%r", user_id, message.message_type, text)

        inbound = InboundEvent(
            platform="feishu",
            user_id=user_id,
            text=text,
            file=None,
        )
        assert self._loop is not None, "FeishuAdapter.start() 还没调用就收到消息了"
        self._loop.call_soon_threadsafe(self._queue.put_nowait, inbound)

    def start(self) -> None:
        """启动长连接监听。必须在 asyncio 事件循环内调用一次，长连接本身跑在后台线程。"""
        self._loop = asyncio.get_running_loop()
        threading.Thread(target=self._ws_client.start, daemon=True).start()
        logger.info("飞书长连接启动中")

    async def receive(self) -> InboundEvent:
        return await self._queue.get()

    async def send(self, user_id: str, message: OutboundMessage) -> None:
        if message.file is not None:
            file_key = await self._upload_file(message.file)
            content = lark.JSON.marshal({"file_key": file_key})
            msg_type = "file"
        else:
            content = lark.JSON.marshal({"text": message.text or ""})
            msg_type = "text"

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(user_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(self._client.im.v1.message.create, request)
        if not response.success():
            logger.error("飞书发消息失败 user_id=%s code=%s msg=%s", user_id, response.code, response.msg)
            raise RuntimeError(f"飞书发消息失败：code={response.code} msg={response.msg}")
        logger.info("飞书发消息成功 user_id=%s msg_type=%s", user_id, msg_type)

    async def _upload_file(self, file: IncomingFile) -> str:
        request = (
            CreateFileRequest.builder()
            .request_body(
                CreateFileRequestBody.builder()
                .file_type("stream")
                .file_name(file.filename)
                .file(io.BytesIO(file.content))
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(self._client.im.v1.file.create, request)
        if not response.success():
            logger.error("飞书文件上传失败 filename=%s code=%s msg=%s", file.filename, response.code, response.msg)
            raise RuntimeError(f"飞书文件上传失败：code={response.code} msg={response.msg}")
        logger.info("飞书文件上传成功 filename=%s file_key=%s", file.filename, response.data.file_key)
        return response.data.file_key
