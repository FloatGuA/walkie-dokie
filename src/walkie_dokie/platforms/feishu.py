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
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
)

from .base import IncomingFile, InboundEvent, OutboundMessage, PlatformAdapter

logger = logging.getLogger(__name__)


class FeishuAdapter(PlatformAdapter):
    """飞书自建应用适配器，走长连接接收消息（不需要公网回调地址）。"""

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
        file = None
        if message.message_type == "text":
            text = json.loads(message.content).get("text")
        elif message.message_type == "file":
            content = json.loads(message.content)
            file = self._download_message_file(
                message_id=message.message_id, file_key=content["file_key"], file_name=content["file_name"]
            )
        else:
            logger.info("收到不支持的消息类型 message_type=%s，忽略", message.message_type)

        user_id = data.event.sender.sender_id.open_id
        logger.info(
            "收到飞书消息 message_id=%s user_id=%s message_type=%s text=%r file=%r",
            message.message_id,
            user_id,
            message.message_type,
            text,
            file.filename if file else None,
        )

        inbound = InboundEvent(
            platform="feishu",
            user_id=user_id,
            text=text,
            file=file,
            conversation_id=getattr(message, "chat_id", None),
            conversation_type=(
                "group" if getattr(message, "chat_type", None) == "group" else "private"
            ),
        )
        assert self._loop is not None, "FeishuAdapter.start() 还没调用就收到消息了"
        self._loop.call_soon_threadsafe(self._queue.put_nowait, inbound)

    def _download_message_file(self, message_id: str, file_key: str, file_name: str) -> IncomingFile | None:
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type("file")
            .build()
        )
        response = self._client.im.v1.message_resource.get(request)
        if not response.success():
            logger.error(
                "飞书文件下载失败 message_id=%s file_key=%s code=%s msg=%s",
                message_id,
                file_key,
                response.code,
                response.msg,
            )
            return None
        content = response.file.read()
        logger.info("飞书文件下载成功 file_name=%s 大小=%d bytes", file_name, len(content))
        return IncomingFile(filename=file_name, content=content, mime_type="application/octet-stream")

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

        receive_id_type = "open_id"
        receive_id = user_id
        if user_id.startswith("chat:"):
            receive_id_type = "chat_id"
            receive_id = user_id.removeprefix("chat:")
            if not receive_id:
                raise ValueError("飞书 chat reply target 不能为空")

        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(self._client.im.v1.message.create, request)
        if not response.success():
            logger.error("飞书发消息失败 target=%s code=%s msg=%s", user_id, response.code, response.msg)
            raise RuntimeError(f"飞书发消息失败：code={response.code} msg={response.msg}")
        logger.info("飞书发消息成功 target=%s msg_type=%s", user_id, msg_type)

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
