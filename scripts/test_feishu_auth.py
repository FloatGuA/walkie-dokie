"""一次性验证脚本：确认飞书 App ID/Secret 有效，机器人能力已开通。

只测鉴权（换 tenant_access_token），不发消息——发消息需要知道接收方的
open_id/chat_id，那个要等事件订阅接回调后才能拿到，属于下一步。

用法：python scripts/test_feishu_auth.py
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]

resp = httpx.post(
    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
    json={"app_id": APP_ID, "app_secret": APP_SECRET},
    timeout=10,
)
data = resp.json()

if data.get("code") == 0:
    token = data["tenant_access_token"]
    print(f"鉴权成功。tenant_access_token（前 8 位）: {token[:8]}...，有效期 {data['expire']} 秒")
else:
    print(f"鉴权失败：code={data.get('code')} msg={data.get('msg')}")
    sys.exit(1)
