"""Dedicated Feishu entry point for published contract/price factual QA."""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "walkie_dokie.contract_admin.settings")

from dotenv import load_dotenv

load_dotenv()

import django

django.setup()

from asgiref.sync import sync_to_async

from walkie_dokie.contract_intelligence.agent import ask_intelligence_question
from walkie_dokie.contract_intelligence.bindings import (
    list_user_projects,
    resolve_external_project,
    select_user_project,
)
from walkie_dokie.logging_config import setup_logging
from walkie_dokie.platforms.base import InboundEvent, OutboundMessage
from walkie_dokie.platforms.feishu import FeishuAdapter

_PREFIX = "/contract"


def _render_answer(result: dict) -> str:
    lines = [result["answer"]]
    calculation = result.get("calculation")
    if calculation:
        lines.append(
            f"计算：{calculation['operands']['unit_price']} × "
            f"{calculation['operands']['quantity']} = {calculation['result']}"
        )
    evidence = result.get("evidence", [])
    if evidence:
        lines.append("\n证据：")
        for index, item in enumerate(evidence, start=1):
            location = []
            if item.get("page_physical"):
                location.append(f"PDF物理页 {item['page_physical']}")
            if item.get("page_printed"):
                location.append(f"印刷页 {item['page_printed']}")
            if item.get("clause_ref"):
                location.append(item["clause_ref"])
            anchor = item.get("source_anchor") or {}
            if anchor.get("sheet"):
                cells = ",".join(anchor.get("cells", []))
                location.append(f"{anchor['sheet']}!{cells}")
            lines.append(
                f"[{index}] {item['document']} / {item['source_file']}"
                f" / {'；'.join(location) or '原文件锚点'}\n{item['text']}"
            )
    lines.append(f"\n状态：{result['status']}；QuestionRun：{result.get('question_run_id', '-')}")
    return "\n".join(lines)[:12_000]


async def _private_command(event: InboundEvent, platform: FeishuAdapter) -> bool:
    text = (event.text or "").strip()
    if event.conversation_type == "group" or not text.startswith(_PREFIX):
        return False
    command = text[len(_PREFIX) :].strip()
    if command == "projects":
        bindings = await sync_to_async(list, thread_sensitive=True)(
            list_user_projects(platform=event.platform, user_id=event.user_id)
        )
        lines = [
            f"{'✓ ' if item.is_selected else ''}{item.project.slug}：{item.project.name}"
            for item in bindings
        ]
        await platform.send(
            event.reply_target,
            OutboundMessage(text="可用合同项目：\n" + ("\n".join(lines) or "暂无授权项目")),
        )
        return True
    if command.startswith("use "):
        slug = command.removeprefix("use ").strip()
        try:
            project = await sync_to_async(select_user_project, thread_sensitive=True)(
                platform=event.platform, user_id=event.user_id, project_slug=slug
            )
        except LookupError as exc:
            await platform.send(event.reply_target, OutboundMessage(text=str(exc)))
        else:
            await platform.send(
                event.reply_target,
                OutboundMessage(text=f"当前合同项目已切换为：{project.name}"),
            )
        return True
    if not command:
        await platform.send(
            event.reply_target,
            OutboundMessage(
                text="用法：/contract projects；/contract use 项目标识；/contract 你的问题"
            ),
        )
        return True
    return False


async def handle_contract_event(event: InboundEvent, platform: FeishuAdapter) -> None:
    if event.file is not None:
        await platform.send(event.reply_target, OutboundMessage(text="合同知识库文件只能由管理员 Web 上传。"))
        return
    if await _private_command(event, platform):
        return
    text = (event.text or "").strip()
    if event.conversation_type != "group":
        if not text.startswith(_PREFIX):
            return
        text = text[len(_PREFIX) :].strip()
    if not text:
        return
    try:
        project = await sync_to_async(resolve_external_project, thread_sensitive=True)(
            platform=event.platform,
            user_id=event.user_id,
            conversation_type=event.conversation_type or "private",
            conversation_id=event.conversation_id,
        )
        result = await ask_intelligence_question(
            authorized_project_id=str(project.id),
            question=text,
            platform="feishu",
            actor_key=f"{event.user_id}:{event.conversation_id or 'private'}",
        )
        reply = _render_answer(result)
    except LookupError as exc:
        reply = f"当前无法查询合同项目：{exc}"
    except Exception:
        reply = "本次合同查询失败，系统没有生成未经验证的答案。请联系管理员查看 QuestionRun。"
    await platform.send(event.reply_target, OutboundMessage(text=reply))


async def main() -> None:
    setup_logging()
    platform = FeishuAdapter(os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"])
    platform.start()
    tasks: set[asyncio.Task] = set()
    try:
        while True:
            event = await platform.receive()
            task = asyncio.create_task(handle_contract_event(event, platform))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
