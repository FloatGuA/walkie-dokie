# walkie-dokie — Progress

## 状态快照

项目方向已转为：面向中老年人群的多平台（企业微信/QQ/微信）机器人办公助手，核心场景是 Word/Excel 文档的生成/编辑/问答（原「物业家政多Agent平台」方向已搁置，见 [DECISION.md](DECISION.md)）。

骨架已按新方向重建：三层目录 + 抽象接口就位，均为占位实现，无可运行逻辑。`pip install -e .` 已重新验证通过。

## 待处理 / 下一步

- 确定第一个可验证的最小闭环切哪里（例如：企业微信收到"帮我生成一份请假条"的文本消息 → 调用一个执行后端 → 返回一份 docx，端到端跑通）
- 企业微信自建应用的具体接入实现（URL 验证、消息加解密、被动回复）
- 至少一个执行后端（Claude Agent SDK 或 Codex）的具体实现
- LangGraph 图的节点/边定义（`orchestrator/graph.py` 目前只有占位注释）
- 针对"文档办公"场景重新梳理适老化交互设计（旧方向的语音优先设计不完全适用，具体怎么做还没讨论）
- 周计划/路线图还没细化，留到下一步单独讨论

## 进行中

（无）

## 已完成

- **骨架按新方向重建**（2026-08-07，已验证：`pip install -e . --no-deps` 在独立 venv 中成功，`import walkie_dokie.{platforms.base, orchestrator.state, agents.base}` 通过；未验证任何实际业务逻辑，因为还没写）
  - 删除物业家政方向的旧代码：`graphs/`、旧 `agents/`、`tools/`、`memory/`、`integrations/` 目录，`state.py`（`WorkOrderState`），空目录 `eval/`、`n8n/`、`docs/`
  - 新增 `src/walkie_dokie/platforms/`：`base.py`（`PlatformAdapter` 抽象接口、`InboundEvent`/`OutboundMessage`/`IncomingFile`）、`wecom.py`（`WeComAdapter` 占位，`NotImplementedError`）
  - 新增 `src/walkie_dokie/orchestrator/`：`state.py`（`SessionState`，字段 platform/user_id/pending_file/instruction/backend/status/result_file）、`graph.py`（占位，图节点未定义）
  - 新增 `src/walkie_dokie/agents/`：`base.py`（`ExecutionAgent` 抽象接口）、`claude_agent.py`（`ClaudeAgentSDKBackend` 占位）、`codex_agent.py`（`CodexBackend` 占位），均 `NotImplementedError`
  - `pyproject.toml`：依赖改为 `langgraph`，新增可选依赖组 `claude`（`claude-agent-sdk`），移除不再需要的 `langchain-anthropic`/`openai`/`anthropic`
  - README.md、DECISION.md 同步更新为新方向

- **项目骨架搭建（物业家政方向，已作废）**（2026-08-07）
  - 见上方"骨架按新方向重建"，此前的目录结构和 `WorkOrderState` 已被删除，历史记录见 git log
