# walkie-dokie — Progress

## 状态快照

项目方向：面向中老年人群的多平台机器人办公助手，核心场景是 Word/Excel 文档的生成/编辑/问答（原「物业家政多Agent平台」方向已搁置，见 [DECISION.md](DECISION.md)）。

**MVP 端到端闭环已跑通并验证**：飞书发一句文字指令 → `ClaudeAgentSDKBackend` 用 python-docx 生成 docx → 飞书把文件和文字回复发回用户。平台选型定为飞书自建应用（长连接，见 DECISION.md），执行后端目前只有 Claude 这一条能跑，Codex 卡在订阅额度。`orchestrator/` 还没接入，现在是 `scripts/run_mvp.py` 里的纯线性胶水逻辑。

## 待处理 / 下一步

- 用户发文件给 bot 这条链路还没实现——`FeishuAdapter._on_message` 目前收到文件消息时 `file` 字段写死 `None`
- Codex 执行后端订阅额度问题未解决，暂缓，不阻塞主线
- `orchestrator/`（`SessionState` + `graph.py`）还没接入 `run_mvp.py`，跨消息的会话状态（比如"文件已收到但指令不明确"）目前没有承载
- `lark_oapi` 自己的 logger 会被我们的 root logger 重复打印一遍（不影响功能，未处理，属于可以顺手修的小事）
- 针对"文档办公"场景重新梳理适老化交互设计（旧方向的语音优先设计不完全适用，具体怎么做还没讨论）
- 周计划/路线图还没细化，留到下一步单独讨论

## 进行中

（无）

## 已完成

- **MVP 端到端闭环跑通**（2026-08-08，已验证：真实在飞书里发消息、真实收到生成的 docx 文件和文字回复，日志全链路确认——收消息→执行→上传文件→发文件→发文字回复全部成功）
  - `src/walkie_dokie/platforms/feishu.py`：`FeishuAdapter`，用 `lark-oapi` 官方 SDK 长连接收消息（`P2ImMessageReceiveV1` 事件桥接到 `asyncio.Queue`），REST API 发消息/传文件（`file_key` 机制）。删除了过时的 `wecom.py` 占位
  - `src/walkie_dokie/agents/claude_agent.py`：`ClaudeAgentSDKBackend` 实装完成，用 `query()` + `ClaudeAgentOptions(output_format={"type": "json_schema", ...})` 拿结构化输出（`reply_text`/`filename`），在临时目录里跑，走本机 `claude login` 订阅鉴权
  - `src/walkie_dokie/agents/codex_agent.py`：`CodexBackend` 实装完成（`codex exec --sandbox workspace-write --output-schema`），代码可运行但**未实测成功过**，卡在 Codex 订阅额度
  - `src/walkie_dokie/agents/base.py`：`ExecutionAgent.run()` 签名加了 `input_filename` 参数（原来漏了，写 Codex 后端时发现必须要有文件名才能正确处理输入文件）
  - `scripts/run_mvp.py`：不经过 orchestrator 的最小胶水脚本，`platform.receive()` → `backend.run()` → `platform.send()` 线性串联
  - `src/walkie_dokie/logging_config.py`：新增 `setup_logging()`，统一 logging 格式，顺带修了 Windows 控制台 GBK 编码导致中文乱码的问题（见 PITFALLS.md）。`feishu.py`/`claude_agent.py`/`codex_agent.py`/`run_mvp.py` 全部接入
  - 过程中踩了两个环境坑，已记入 PITFALLS.md：Windows 下 `asyncio.create_subprocess_exec` 传 npm CLI 裸命令名找不到文件（要 `shutil.which()`）；Git Bash 里 `/` 开头参数被 MSYS 转成路径
  - `pyproject.toml` 新增依赖：`httpx`、`python-dotenv`、`lark-oapi`
  - 平台选型过程：企业微信自建应用（缺资质）→ 企业微信智能机器人/微信公众号（不支持发文件）→ QQ 官方机器人（缺资质）→ 一度拍板个人微信 `wxauto`（用户接受封号风险）→ 最终定为飞书自建应用（长连接、零封号风险、原生支持发文件），详细取舍见 DECISION.md

- **骨架按新方向重建**（2026-08-07，已验证：`pip install -e . --no-deps` 在独立 venv 中成功，`import walkie_dokie.{platforms.base, orchestrator.state, agents.base}` 通过；未验证任何实际业务逻辑，因为还没写）
  - 删除物业家政方向的旧代码：`graphs/`、旧 `agents/`、`tools/`、`memory/`、`integrations/` 目录，`state.py`（`WorkOrderState`），空目录 `eval/`、`n8n/`、`docs/`
  - 新增 `src/walkie_dokie/platforms/`：`base.py`（`PlatformAdapter` 抽象接口、`InboundEvent`/`OutboundMessage`/`IncomingFile`）、`wecom.py`（`WeComAdapter` 占位，`NotImplementedError`）
  - 新增 `src/walkie_dokie/orchestrator/`：`state.py`（`SessionState`，字段 platform/user_id/pending_file/instruction/backend/status/result_file）、`graph.py`（占位，图节点未定义）
  - 新增 `src/walkie_dokie/agents/`：`base.py`（`ExecutionAgent` 抽象接口）、`claude_agent.py`（`ClaudeAgentSDKBackend` 占位）、`codex_agent.py`（`CodexBackend` 占位），均 `NotImplementedError`
  - `pyproject.toml`：依赖改为 `langgraph`，新增可选依赖组 `claude`（`claude-agent-sdk`），移除不再需要的 `langchain-anthropic`/`openai`/`anthropic`
  - README.md、DECISION.md 同步更新为新方向

- **项目骨架搭建（物业家政方向，已作废）**（2026-08-07）
  - 见上方"骨架按新方向重建"，此前的目录结构和 `WorkOrderState` 已被删除，历史记录见 git log
