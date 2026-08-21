"""飞书适配层的入站归一化：把 SDK 事件对象翻译成 InboundEvent。

这里只覆盖不需要网络的那一半（``_on_message``）。发送侧要真的打飞书 API，
不进标准 pytest。
"""

import asyncio
import json

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from walkie_dokie.platforms.feishu import FeishuAdapter


def _receive_event(*, header: dict | None, text: str = "帮我写份请假条"):
    payload = {
        "schema": "2.0",
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }
    if header is not None:
        payload["header"] = header
    return P2ImMessageReceiveV1(payload)


async def _inbound_from(data) -> object:
    adapter = FeishuAdapter("app-id", "app-secret")
    adapter._loop = asyncio.get_running_loop()
    adapter._on_message(data)
    return await asyncio.wait_for(adapter.receive(), timeout=1)


async def test_event_id_comes_from_the_p2_header():
    """去重键就是 ``data.header.event_id``——飞书重投时它保持不变。"""

    inbound = await _inbound_from(
        _receive_event(header={"event_id": "evt-123", "event_type": "im.message.receive_v1"})
    )

    assert inbound.event_id == "evt-123"
    assert inbound.text == "帮我写份请假条"
    assert inbound.user_id == "ou_1"


async def test_missing_header_leaves_event_id_none():
    """p1 格式的事件没有 header：不猜、不造 id，留 None 让上游照常处理。"""

    inbound = await _inbound_from(_receive_event(header=None))

    assert inbound.event_id is None
