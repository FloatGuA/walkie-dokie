"""被动提取并存储用户的个人身份类事实（姓名/部门/职位/常用称呼），减少
下次任务时执行 agent 靠占位符瞎编的情况。

用 DeepSeek 的轻量模型（`deepseek-chat`）做提取——本地 Ollama（qwen2.5:7b、
qwen3:8b）实测过，同一个任务两次都严重跑题、编造原话没有的内容，效果不可靠，
见 PITFALLS.md。这是锦上添花的功能，提取失败不该影响主流程，所以这里是
少数允许"吞异常返回空结果"的地方——调用方永远能拿到一个 dict，不用整个
业务代码里到处 try/except 这一个功能。
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_VAR_ROOT = Path(__file__).parent.parent.parent.parent / "var"
MEMORY_DIR = _VAR_ROOT / "memory"

_EXTRACT_SYSTEM_PROMPT = (
    "任务：从用户原话里逐字核对，只提取原话中明确写出来的个人身份类事实"
    "（姓名、部门、职位、常用称呼）。严格规则：不要推测、不要联想、不要"
    "补充原话没有的任何字段，原话没提到的字段绝对不能编造，也不要输出"
    "任何跟身份信息无关的内容。必须返回 JSON，格式是"
    '{"facts": {"键": "值", ...}}，原话没有任何可提取信息时返回'
    '{"facts": {}}。'
)


def _memory_path(platform: str, user_id: str) -> Path:
    return MEMORY_DIR / f"{platform}_{user_id}.json"


def load_facts(platform: str, user_id: str) -> dict:
    path = _memory_path(platform, user_id)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_facts(platform: str, user_id: str, new_facts: dict) -> None:
    if not new_facts:
        return
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    facts = load_facts(platform, user_id)
    facts.update(new_facts)
    _memory_path(platform, user_id).write_text(
        json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("更新用户档案 platform=%s user_id=%s facts=%r", platform, user_id, facts)


async def extract_facts(conversation_text: str) -> dict:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        logger.debug("没配置 DEEPSEEK_API_KEY，跳过事实提取")
        return {}

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    try:
        response = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": conversation_text},
            ],
            response_format={"type": "json_object"},
            max_tokens=500,
        )
        parsed = json.loads(response.choices[0].message.content)
        facts = parsed.get("facts", {})
        if not isinstance(facts, dict):
            logger.warning("提取结果 facts 不是字典，丢弃：%r", parsed)
            return {}
        facts = {k: v for k, v in facts.items() if isinstance(v, str)}
        logger.info("提取到用户事实：%r", facts)
        return facts
    except Exception:
        logger.exception("事实提取失败，跳过（不影响主流程）")
        return {}
