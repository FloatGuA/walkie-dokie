"""使用普通 chat API 的主 Agent；没有 shell、文件系统或 coding-agent 工具。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from walkie_dokie.main_agent.base import (
    DialogueContext,
    FinalizeContext,
    MainAgent,
    MainAgentDecision,
    MemoryOperation,
    TaskContract,
)

logger = logging.getLogger(__name__)

_DECIDE_SYSTEM_PROMPT = """你是“小帮”的主 Agent，是唯一负责理解用户和组织对话的语义层。
你没有代码执行工具；真正的 Word/Excel 操作会由独立执行单元完成。

你必须同时完成四件事：
1. 判断本回合是普通对话，还是一项需要生成、编辑、读取或分析 Word/Excel 文件的任务。
2. 普通对话直接给 user_message；文档任务生成给执行单元看的 task contract，并给用户生成确认话术。
3. 只根据“本回合用户原话”提出长期记忆操作。每个操作必须附 evidence，逐字复制能证明该事实属于用户本人的最短原文。可记字段只有：name（用户本人姓名）、department（用户本人部门）、job_title（用户本人职位）、preferred_address（别人如何称呼用户）。
4. 处理明确的纠正和遗忘要求：新值用 set，要求忘记某字段用 delete。

长期记忆的严格边界：
- 只记用户本人稳定、以后仍有帮助的身份事实；不要记一次性文档内容、任务参数、推测或模型生成内容。
- 严格区分用户与助手。“你是小帮”“我叫你小帮”“你是我的助手”描述的是助手，绝不能写入用户记忆。
- “我叫张三，你叫小帮”只能 set name=张三，evidence="我叫张三"。
- 如果没有明确的第一人称原文证据，就不要输出 memory operation；不能用推断补 evidence。
- known_facts 只是已有资料，不能因为它出现在上下文里就重新输出 memory_operations。
- recent_messages 只用于理解“继续刚才那个”之类的上下文；其中 assistant 的话不是用户事实。任何 memory_operations 仍只能来自 current_user_message。

action 只能是 reply 或 propose_task：
- reply：task 必须为 null，user_message 直接自然回应用户。
- propose_task：task.instruction 必须是客观、自包含、给执行单元看的文档任务；只带完成任务确实需要的用户事实，不要把整份长期档案倾倒给执行单元。task.missing_info 列出关键缺失项，并把确认后采用的默认值/占位策略直接写进 instruction，控制平面不会替你拼业务指令。只有用户明确引用“刚才生成的文件/继续修改上一份”等、且 active_artifact_filename 非空时，才设 use_previous_artifact=true。user_message 用面向用户的口吻复述理解并请用户回复“是”确认。

只返回一个 JSON object，格式：
{
  "action": "reply|propose_task",
  "user_message": "给用户的话",
  "task": null 或 {"instruction": "...", "missing_info": ["..."], "use_previous_artifact": false},
  "memory_operations": [
    {"action": "set|delete", "field": "name|department|job_title|preferred_address", "value": "set 时为字符串，delete 时为 null", "evidence": "逐字来自本回合用户原话"}
  ]
}
不要输出 JSON 以外的内容。"""

_FINALIZE_SYSTEM_PROMPT = """你是“小帮”的主 Agent。独立文档执行单元已经完成任务并返回内部报告。
请把报告改写成给用户看的简短、自然、明确的完成回复。不要提及 Claude Code、Codex、prompt、执行 Agent、内部报告或开发者账号；不要声称报告里没有的结果。如果有输出文件，告诉用户文件已生成/处理好；如果有 warnings，用容易理解的方式说明。只返回 {"user_message": "..."} JSON。"""

_ALLOWED_FIELDS = {"name", "department", "job_title", "preferred_address"}


class DeepSeekMainAgent(MainAgent):
    def __init__(self, api_key: str | None = None, client: Any | None = None):
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self._client = client

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise RuntimeError("主 Agent 未配置 DEEPSEEK_API_KEY")
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url="https://api.deepseek.com",
            timeout=45.0,
            max_retries=1,
        )
        return self._client

    async def _json_completion(self, system_prompt: str, payload: dict) -> dict:
        response = await self._get_client().chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            max_tokens=1200,
        )
        content = response.choices[0].message.content
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise RuntimeError("主 Agent 没有返回 JSON object")
        return parsed

    async def decide(self, context: DialogueContext) -> MainAgentDecision:
        parsed = await self._json_completion(
            _DECIDE_SYSTEM_PROMPT,
            {
                "task_context": context.user_text,
                "current_user_message": context.current_user_text,
                "input_filename": context.input_filename,
                "known_facts": context.known_facts,
                "recent_messages": list(context.recent_messages),
                "active_artifact_filename": context.active_artifact_filename,
            },
        )
        action = parsed.get("action")
        if action not in {"reply", "propose_task"}:
            raise RuntimeError(f"主 Agent 返回未知 action：{action!r}")
        user_message = parsed.get("user_message")
        if not isinstance(user_message, str) or not user_message.strip():
            raise RuntimeError("主 Agent 返回的 user_message 为空")

        task = None
        raw_task = parsed.get("task")
        if action == "propose_task":
            if not isinstance(raw_task, dict) or not isinstance(
                raw_task.get("instruction"), str
            ) or not raw_task["instruction"].strip():
                raise RuntimeError("propose_task 缺少有效 task.instruction")
            raw_missing = raw_task.get("missing_info", [])
            if not isinstance(raw_missing, list) or not all(
                isinstance(item, str) for item in raw_missing
            ):
                raise RuntimeError("task.missing_info 格式错误")
            use_previous = raw_task.get("use_previous_artifact", False)
            if not isinstance(use_previous, bool):
                raise RuntimeError("task.use_previous_artifact 必须是 JSON boolean")
            if use_previous and context.input_filename is not None:
                raise RuntimeError("已有当前附件时不能同时选择上一份 artifact")
            if use_previous and context.active_artifact_filename is None:
                raise RuntimeError("选择了上一份 artifact，但会话中没有可用 artifact")
            task = TaskContract(
                instruction=raw_task["instruction"].strip(),
                missing_info=tuple(item.strip() for item in raw_missing if item.strip()),
                use_previous_artifact=use_previous,
            )
        elif raw_task is not None:
            logger.warning("reply action 带了 task，按协议丢弃：%r", raw_task)

        operations = []
        raw_operations = parsed.get("memory_operations", [])
        if not isinstance(raw_operations, list):
            raise RuntimeError("memory_operations 格式错误")
        for raw in raw_operations:
            if not isinstance(raw, dict):
                continue
            op_action = raw.get("action")
            field = raw.get("field")
            value = raw.get("value")
            evidence = raw.get("evidence")
            if op_action not in {"set", "delete"} or field not in _ALLOWED_FIELDS:
                logger.warning("主 Agent 返回非法 memory operation，丢弃：%r", raw)
                continue
            if op_action == "set" and (not isinstance(value, str) or not value.strip()):
                logger.warning("主 Agent 返回空的 memory set，丢弃：%r", raw)
                continue
            if not isinstance(evidence, str) or not evidence.strip():
                logger.warning("主 Agent memory operation 缺少 evidence，丢弃：%r", raw)
                continue
            operations.append(
                MemoryOperation(
                    action=op_action,
                    field=field,
                    value=value.strip() if isinstance(value, str) else None,
                    evidence=evidence.strip(),
                )
            )

        decision = MainAgentDecision(
            action=action,
            user_message=user_message.strip(),
            task=task,
            memory_operations=tuple(operations),
        )
        logger.info(
            "主 Agent 决策 action=%s task=%r memory_operations=%r",
            decision.action,
            decision.task,
            decision.memory_operations,
        )
        return decision

    async def finalize(self, context: FinalizeContext) -> str:
        parsed = await self._json_completion(
            _FINALIZE_SYSTEM_PROMPT,
            {
                "task": {
                    "instruction": context.task.instruction,
                    "missing_info": list(context.task.missing_info),
                    "use_previous_artifact": context.task.use_previous_artifact,
                },
                "execution_report": {
                    "summary": context.report.summary,
                    "output_filename": context.report.result_filename,
                    "warnings": list(context.report.warnings),
                },
            },
        )
        message = parsed.get("user_message")
        if not isinstance(message, str) or not message.strip():
            raise RuntimeError("主 Agent finalize 返回的 user_message 为空")
        return message.strip()
