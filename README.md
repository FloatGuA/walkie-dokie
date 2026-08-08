# Walkie-Dokie — 多平台办公助手

小帮 · 说一句话，文档就给你办好。

## 这是什么

面向中老年人群的多平台机器人办公助手。给机器人发一条消息或一份文件，它帮你生成 Word 文档、处理 Excel 表格、回答文档里的问题——不用自己打开 Office，不用学怎么操作电脑。目前跑通的是飞书（工程上最省心，先当技术主线），后续计划接入个人微信（中老年家人实际会用的平台），取舍理由见 DECISION.md。

同时这是一个求职作品项目，用来展示 Agent 系统工程能力：多平台适配层设计、跨消息会话状态管理、可插拔执行后端。

## 现状

MVP 端到端闭环已跑通：飞书发消息（文字/文件）→ orchestrator（LangGraph：防抖攒消息 → 生成任务草稿 → 列出缺失信息等确认 → 执行）→ Claude Agent SDK 生成/编辑/总结文档 → 飞书把文件和回复发回来。详细进度见 [PROGRESS.md](PROGRESS.md)。

## 安装（开发环境）

```bash
pip install -e .
```

需要 Python 3.11+，且本机要能跑通 `claude login`（Claude Agent SDK 走订阅鉴权，见 PITFALLS.md）。开发时要跑测试的话：

```bash
pip install -e ".[dev]"
pytest tests/
```

## 运行 MVP

复制 `.env.example` 为 `.env`，填入飞书 App ID / Secret（开发者后台 → 凭证与基础信息），然后：

```bash
python scripts/run_mvp.py
```

跑起来之后，在飞书里找对应的自建应用机器人发一句话（比如"帮我写一份请假条"），等它把生成的文件和回复发回来。Ctrl+C 停止。

## 架构一览

| 层 | 目录 | 职责 |
|---|---|---|
| 平台适配层 | `src/walkie_dokie/platforms/` | 把飞书等平台的消息统一转成内部 Event（`feishu.py` 已实装，走长连接，收发文字/文件都支持） |
| 编排层 | `src/walkie_dokie/orchestrator/` | LangGraph 状态机：防抖攒消息 → 生成任务草稿 → 等用户确认 → 执行，按用户加锁避免并发写同一会话状态 |
| 执行层 | `src/walkie_dokie/agents/` | 可插拔的 coding agent 执行后端（Claude Agent SDK 已跑通 / Codex 在 Windows 上因上游沙箱 bug 暂不可用），在独立工作目录（`var/workspaces/`，不自动清理）里写代码完成文档操作 |

每层为什么这么划分、否掉了哪些方案，见 [DECISION.md](DECISION.md)；`ExecutionAgent` 的临时目录 + 结构化输出协议怎么设计的，见 [TECHNICAL.md](TECHNICAL.md)。
