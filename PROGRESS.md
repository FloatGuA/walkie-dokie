# walkie-dokie — Progress

## 状态快照

项目方向：面向中老年人群的多平台机器人办公助手，核心场景是 Word/Excel 文档的生成/编辑/问答（原「物业家政多Agent平台」方向已搁置，见 [DECISION.md](DECISION.md)）。

**MVP 端到端闭环已跑通并验证，orchestrator 也已接入，防抖+确认环节也跑通了**：飞书发一句文字指令 → LangGraph 状态机（`orchestrator/`：防抖攒消息 → 生成任务草稿 → 列出缺失信息等用户确认 → 确认后强制执行不再追问）→ `ClaudeAgentSDKBackend` 用 python-docx 生成 docx → 飞书把文件和文字回复发回用户。每条消息独立工作目录（`var/workspaces/`，持久化不清理）+ 结构化留痕（`var/logs/turns.jsonl`）+ 项目本地日志（`var/logs/walkie-dokie.log`，DEBUG 粒度）。平台选型定为飞书自建应用（长连接，见 DECISION.md），执行后端目前只有 Claude 这一条能跑，Codex 卡在订阅额度。

## 待处理 / 下一步

- **执行结果文字里偶尔混入无关内容**：实测有一次 `ClaudeAgentSDKBackend` 的 `reply_text` 末尾多了一句"claude.ai 的 Gmail/Calendar/Drive 连接器尚未授权"之类的提示，跟任务本身无关，只出现过一次，原因未查——如果这段文字被转发给真实用户会很困惑，需要留意会不会重复出现，重复出现的话要查根因（可能是 Claude Agent SDK 环境里某种连接器状态检查漏进了最终回复）
- 用户发文件给 bot 这条链路还没实现——`FeishuAdapter._on_message` 目前收到文件消息时 `file` 字段写死 `None`
- Codex 执行后端订阅额度问题未解决，暂缓，不阻塞主线
- `lark_oapi` 自己的 logger 会被我们的 root logger 重复打印一遍（不影响功能，未处理）
- `lark_oapi` 报了个不影响功能的噪音错误：`processor not found, type: im.message.reaction.created_v1`（用户在飞书给消息点了个表情反应，触发了一个我们没注册处理器的事件类型），不影响主流程，未处理
- 针对"文档办公"场景重新梳理适老化交互设计（旧方向的语音优先设计不完全适用，具体怎么做还没讨论）
- 周计划/路线图还没细化，留到下一步单独讨论
- **进程常驻/自动重启**：现在是手动敲命令跑的开发脚本，关终端/重启/崩溃都没人管。用户拍板"这个好做，以后再做"，方案已讨论过（Windows 服务化包装，或挪到云主机 + 进程管理器），暂不实现
- **部署目标机器未定**：本地先跑通，以后要挪云主机，但云主机是 Linux/macOS/Windows 都还没定，用户明确说"现在还不知道"——先不依赖任何特定 OS 的实现细节
- **日志粒度后续要调粗**：现在项目早期，`var/logs/walkie-dokie.log` 存 DEBUG 粒度（连第三方库如 websockets 的底层帧都记了），文件用轮转限制了体积（10MB×5）暂时不会失控。用户明确说了等过了高频调试阶段再调粗，不用现在处理

## 进行中

（无）

## 已完成

- **防抖 + 任务草稿确认闭环**（2026-08-09，已验证：真实在飞书测试——发"我写一份请假条" → 10 秒防抖后收到列明 7 项缺失信息的确认消息 → 回"是" → 派发执行时自动加上"用占位符直接完成、不要再问"的限定 → Claude 真的用合理默认值生成了 `请假条.docx`，不再卡在反复追问）
  - `src/walkie_dokie/orchestrator/debounce.py`：新增 `Debouncer`，按 `user_id` 缓冲消息，每条新消息重置一个可取消的 10 秒 `asyncio.Task`，到期把窗口内消息合并派发
  - `src/walkie_dokie/orchestrator/draft.py`：新增 `generate_draft_task_prompt()`，轻量 Claude Agent SDK 调用（`allowed_tools=[]`、`max_turns=1`，不跑代码），结构化输出 `{task_summary, missing_info}`——第一版只输出一句纯文本草稿，实测发现草稿如果自带"应该先问用户"的判断，会在用户确认后被原样喂给执行 agent 导致执行 agent 也去追问，改成结构化拆开 `task_summary`/`missing_info` 两个字段后，confirm 消息才能直接把缺什么列清楚，且执行时能针对性地加"别再问了"的限定
  - `src/walkie_dokie/orchestrator/graph.py`：图从两节点扩成四节点 `collect → draft → ask_confirm → execute`，`ask_confirm` 用 `langgraph.types.interrupt()` 暂停等用户回复，`_route_confirm` 机械判断回复是不是确认词（`是/对/确认/ok/yes` 等固定集合，不是 NLU 意图分类），不是就并回 `pending_instruction` 重新生成草稿；`execute` 节点如果 `draft.missing_info` 非空，会把"这些信息用占位符直接完成，不要再问"拼进喂给执行 agent 的最终指令
  - `scripts/run_mvp.py`：`handle_event` 先查 `graph.aget_state().next` 判断这个用户是不是正卡在 `ask_confirm`——是的话直接 `ainvoke(Command(resume=文本))` 恢复，不走防抖；不是的话才丢给 `Debouncer`
  - 用一个玩具两节点图（`scripts/_scratch_interrupt_test.py`，验证完删了）单独确认了 `interrupt()`/`Command(resume=...)`/`aget_state().next` 的实际行为再动手写正式代码：中断后 `ainvoke()` 返回值带 `__interrupt__` 字段，恢复时节点从头重跑

- **orchestrator 接入 + 日志/留痕基础设施**（2026-08-08，已验证：真实在飞书发消息，工作目录/结构化留痕/项目日志三者都确认落盘正确，见 `var/workspaces/feishu_ou_.../20260808/96025e01/请假条.docx` 和对应的 `var/logs/turns.jsonl` 记录）
  - `src/walkie_dokie/orchestrator/graph.py`：`build_graph()` 用 LangGraph `StateGraph` 实装，两个节点 `collect`（把新消息并入 pending_* 状态）/`execute`（调用执行 agent，记结构化留痕），按 `user_id` 做 checkpoint thread 隔离
  - `src/walkie_dokie/orchestrator/state.py`：`SessionState` 加回 `platform` 字段（工作目录命名要用）；`result` 字段从直接存 `ExecutionResult` dataclass 改成存 plain dict——碰到 LangGraph checkpointer 对未注册自定义类型的 deprecation 警告，改存 dict 更省心
  - `src/walkie_dokie/workspace.py`：新增 `create_workspace_dir(platform, user_id)`，执行目录改为 `var/workspaces/{platform}_{user_id}/{日期}/{run_id}/`，不再用 `tempfile.TemporaryDirectory()` 自动销毁——项目早期要能复盘，生成过程要留得住
  - `src/walkie_dokie/turn_log.py`：新增 `log_turn()`，每轮"输入→输出"结构化记一行 JSONL 到 `var/logs/turns.jsonl`（时间戳/平台/用户/输入/输出/后端/耗时/成功与否），跟人读的日志分开，供以后写脚本查询统计
  - `src/walkie_dokie/logging_config.py`：加了落盘到 `var/logs/walkie-dokie.log` 的 `RotatingFileHandler`（10MB×5 份），文件里存 DEBUG 粒度、控制台保持 INFO，不再依赖会话临时目录存日志
  - `src/walkie_dokie/agents/base.py`/`claude_agent.py`/`codex_agent.py`：`ExecutionAgent.run()` 加了 `workdir: Path` 参数，两个后端都不再自己起临时目录，改用调用方传入的工作目录
  - `src/walkie_dokie/platforms/base.py`：顺手修了一个遗留 bug——`Platform` 类型标注还是 `Literal["wecom", "qq", "wechat"]`，没有 `"feishu"`，跟 `feishu.py` 实际赋的值对不上
  - `scripts/run_mvp.py`：`asyncio.create_task()` 改成每条消息真正并发派发（之前是线性 `while` 循环）；发现新问题——同一用户连续快发消息现在会并发执行成多个独立请求，不是排队/合并，见下方"待处理"

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
