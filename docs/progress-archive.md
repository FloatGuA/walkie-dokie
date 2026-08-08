# walkie-dokie — Progress Archive

早期、已经不太会再回看的 PROGRESS.md 条目搬到这里，原处留指针。

- **骨架按新方向重建**（2026-08-07，已验证：`pip install -e . --no-deps` 在独立 venv 中成功，`import walkie_dokie.{platforms.base, orchestrator.state, agents.base}` 通过；未验证任何实际业务逻辑，因为还没写）
  - 删除物业家政方向的旧代码：`graphs/`、旧 `agents/`、`tools/`、`memory/`、`integrations/` 目录，`state.py`（`WorkOrderState`），空目录 `eval/`、`n8n/`、`docs/`
  - 新增 `src/walkie_dokie/platforms/`：`base.py`（`PlatformAdapter` 抽象接口、`InboundEvent`/`OutboundMessage`/`IncomingFile`）、`wecom.py`（`WeComAdapter` 占位，`NotImplementedError`）
  - 新增 `src/walkie_dokie/orchestrator/`：`state.py`（`SessionState`，字段 platform/user_id/pending_file/instruction/backend/status/result_file）、`graph.py`（占位，图节点未定义）
  - 新增 `src/walkie_dokie/agents/`：`base.py`（`ExecutionAgent` 抽象接口）、`claude_agent.py`（`ClaudeAgentSDKBackend` 占位）、`codex_agent.py`（`CodexBackend` 占位），均 `NotImplementedError`
  - `pyproject.toml`：依赖改为 `langgraph`，新增可选依赖组 `claude`（`claude-agent-sdk`），移除不再需要的 `langchain-anthropic`/`openai`/`anthropic`
  - README.md、DECISION.md 同步更新为新方向

- **项目骨架搭建（物业家政方向，已作废）**（2026-08-07）
  - 见上方"骨架按新方向重建"，此前的目录结构和 `WorkOrderState` 已被删除，历史记录见 git log
