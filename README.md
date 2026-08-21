# Walkie-Dokie — 多平台办公助手

小帮 · 说一句话，文档就给你办好。

## 这是什么

面向中老年用户的多平台机器人办公助手。用户发一句话或一份文件，小帮可以生成 Word、处理 Excel、读取或总结文档。目前飞书是技术主线，个人微信仍是后续面向真实目标用户的平台方向，选型过程见 [DECISION.md](DECISION.md)。

这个项目也用于展示 Agent 系统工程能力：主 Agent 与执行 Agent 分层、可恢复的跨消息状态机、长期记忆治理、多平台适配，以及可插拔的 coding-agent 执行后端。

## 当前状态

第一版飞书端到端闭环已经真实跑通。2026-08-12 完成了第二版架构重构：

```text
飞书 → 会话协调/防抖 → LangGraph 控制流 → MainAgent（理解、记忆、用户话术）
                                      ↓ 用户确认
                              ExecutionAgent（只处理文档）
                                      ↓ 内部执行报告
                                  MainAgent 整理回复 → 飞书
```

`ClaudeAgentSDKBackend`/`CodexBackend` 现在都只是执行单元，不再判断长期记忆，也不直接决定给用户说什么。LangGraph 是可恢复的工作流运行时，不是主 Agent。测试与真实飞书冒烟状态见 [PROGRESS.md](PROGRESS.md)。

主 Agent 会先输出显式意图：知识问答、解释、建议和闲聊是 `chat`，直接由 DeepSeek 回复；只有明确要求生成、修改、读取或分析实际 Word/Excel 文件时才是 `document_task`，进入用户确认和 ExecutionAgent。仅仅询问 Word/Excel 的使用方法不会调用执行单元。

## 安装

需要 Python 3.11+。Linux 上的 Claude 执行后端还要求 `bubblewrap` 和 `socat`；
缺少任一项时沙箱会 fail closed，不会退回到无沙箱执行：

```bash
sudo apt-get install bubblewrap socat
```

安装 Python 依赖：

```bash
pip install -e ".[claude]"
```

本机管理观测台（可选）：`pip install -e ".[admin]"`（这台机器需加 `--user --break-system-packages`，见 PITFALLS）。

运行测试：

```bash
pip install -e ".[claude,dev]"
pytest tests/
```

主 Agent 通过 OpenAI 兼容 SDK 调用 DeepSeek，执行 Agent 当前默认用 Claude Agent SDK。执行模型按主 Agent 判定的任务难度路由（simple→haiku、standard→sonnet、complex→opus），设 `EXECUTION_AGENT_MODEL` 可锁死单模型。鉴权和对外使用边界见 [.env.example](.env.example) 与 [PITFALLS.md](PITFALLS.md)。

执行任务把用户指令、文件名和文档内容全部视为不可信输入。Claude 后端只开放沙箱内 Bash，禁用 MCP、skills、子 Agent、网页与网络，清除应用凭证环境变量，并且只读 Python 运行时、只写本轮用户工作区；Codex 后端使用等价的最小 permission profile。输入和输出只接受经过确定性检查的 `.docx/.xlsx`，宏、嵌入对象、外部关系、危险字段/公式和异常压缩包会在 Agent 前后被拒绝。prompt 约束只是辅助，权限边界由 OS 沙箱和产物校验承担。

## 运行 MVP

复制 `.env.example` 为 `.env`，配置飞书凭证、`DEEPSEEK_API_KEY`，并为 Claude Agent SDK 配好鉴权，然后运行：

```bash
python scripts/run_mvp.py
```

在飞书里给自建应用机器人发一句话，例如“帮我写一份请假条”。10 秒防抖窗口结束后，小帮会先确认理解，用户明确回复“是”才调用执行单元。

用户明确说出的姓名、部门、职位或常用称呼会在逐字证据校验通过后自动写入长期记忆，并透明回显实际变更，不再要求二次确认。单独发送 `/long-term-memory` 可查看当前保存的全部长期记忆；该命令不经过模型。

## 运行 Golden Eval（回归评估）

改 prompt / 记忆逻辑后手动跑（联网、花钱，标准 `pytest` 不含它）。必须从仓库根目录运行，需要 `.env` 里的 `DEEPSEEK_API_KEY` 和本机 Claude 登录态（judge 用）：

```bash
export EVAL_REPLY_BLACKLIST="你的邮箱,Claude"   # 敏感话术黑名单，不入库
python3 -m scripts.run_golden_eval --calibrate  # 先校准 judge（6 次调用）
python3 -m scripts.run_golden_eval              # 全量回归：真实 DeepSeek + fake 执行后端
python3 -m scripts.run_golden_eval --real-execution  # 冒烟：真实 Claude/Codex 执行后端
```

报告写入 `var/evals/<时间戳>.json`。退出码：0 全过 / 1 断言失败 / 2 基础设施异常。设计与决策见 `docs/superpowers/specs/2026-08-20-eval-harness-design.md` 与 DECISION.md。

## 查看模型调用成本

所有模型调用（DeepSeek 与压缩用的 Claude CLI）自动记账到 `var/logs/model_calls.jsonl`。查看汇总：

```bash
python3 -m scripts.report_costs --days 7                      # 终端汇总
python3 -m scripts.report_costs --days 30 --html var/logs/costs.html  # 单文件 HTML 报表
```

金额为保守上界估算（官方定价页未确认 `deepseek-chat` 别名映射），对账以 DeepSeek 控制台账单为准。

## Admin 观测台（只读）

本机 web 控制台，收拢四块观测仪器：对话回合流、成本仪表、记忆与对话摘要、eval 报告趋势。只绑 127.0.0.1、无鉴权、纯只读（无任何写端点；注意记忆板块会展示用户档案与逐字 evidence，勿将端口暴露到本机之外）：

```bash
python3 -m scripts.run_admin --port 8788
# 浏览器打开 http://127.0.0.1:8788
```

Host 头不是 `127.0.0.1` / `localhost` 的请求一律 400（挡 DNS rebinding）。注意 WSL2 下"本机"的边界包含 Windows 宿主：WSL 的 localhost 端口会被自动转发，宿主浏览器直接 `http://127.0.0.1:8788` 就能打开这个面板。

数据每 10 秒自动刷新。可写配置为二期（连同"改配置强制过 golden 回归"机制一起设计，见 DECISION.md）。

## 合同智能

原本内置的 `contract_intelligence` Data Spike 领域模块已于 2026-08-15 拆分为独立仓库
[contract-intelligence](../contract-intelligence)（保留完整 git 历史，用
`git filter-repo` 提取），不再是本仓库的一部分。设计决策、进度和使用说明见该仓库自己的
README/DECISION/PROGRESS/TECHNICAL.md。

## 架构边界

| 组件 | 位置 | 唯一职责 |
|---|---|---|
| 平台适配 | `platforms/` | 飞书等平台协议与内部 Event/Message 互转 |
| 会话协调 | `scripts/run_mvp.py`、`orchestrator/debounce.py`、`locks.py` | 防抖、复合会话键、同会话串行化、结果投递 |
| 工作流控制平面 | `orchestrator/graph.py` | LangGraph 状态转移、checkpoint、确认中断与恢复 |
| 主 Agent | `main_agent/` | 对话身份、意图理解、任务契约、记忆候选/纠错、用户话术 |
| 执行 Agent | `agents/` | 在隔离工作目录执行已确认的文档任务，返回内部报告和产物引用 |
| Artifact 存储 | `artifacts.py`、`var/inputs/`、`var/workspaces/` | 附件先落盘，图内只传 JSON 引用；保存输入与执行产物 |
| 持久化与留痕 | `var/memory/`、`var/logs/` | 长期档案、结构化 turn log 和运行日志 |

稳定接口和 LangGraph 运行语义见 [TECHNICAL.md](TECHNICAL.md)，本次架构审阅及未完成风险见 [架构审阅](docs/architecture-review-2026-08-12.md)。
