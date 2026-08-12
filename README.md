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

`ClaudeAgentSDKBackend`/`CodexBackend` 现在都只是执行单元，不再判断长期记忆，也不直接决定给用户说什么。LangGraph 是可恢复的工作流运行时，不是主 Agent。新版已通过 84 项离线测试；重构后的真实飞书链路尚待重新冒烟验证，详细状态见 [PROGRESS.md](PROGRESS.md)。

## 安装

需要 Python 3.11+。运行 Claude 执行后端：

```bash
pip install -e ".[claude]"
```

运行测试：

```bash
pip install -e ".[claude,dev]"
pytest tests/
```

主 Agent 通过 OpenAI 兼容 SDK 调用 DeepSeek，执行 Agent 当前默认用 Claude Agent SDK。鉴权和对外使用边界见 [.env.example](.env.example) 与 [PITFALLS.md](PITFALLS.md)。

## 运行 MVP

复制 `.env.example` 为 `.env`，配置飞书凭证、`DEEPSEEK_API_KEY`，并为 Claude Agent SDK 配好鉴权，然后运行：

```bash
python scripts/run_mvp.py
```

在飞书里给自建应用机器人发一句话，例如“帮我写一份请假条”。10 秒防抖窗口结束后，小帮会先确认理解，用户明确回复“是”才调用执行单元。

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
