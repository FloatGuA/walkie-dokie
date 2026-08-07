# Walkie-Dokie — 多平台办公助手

小帮 · 说一句话，文档就给你办好。

## 这是什么

面向中老年人群的多平台（企业微信 / QQ / 微信）机器人办公助手。给机器人发一条消息或一份文件，它帮你生成 Word 文档、处理 Excel 表格、回答文档里的问题——不用自己打开 Office，不用学怎么操作电脑。

同时这是一个求职作品项目，用来展示 Agent 系统工程能力：多平台适配层设计、跨消息会话状态管理、可插拔执行后端。

## 现状

骨架阶段：三层目录结构和抽象接口（`PlatformAdapter` / `SessionState` / `ExecutionAgent`）已就位，均为占位实现，尚无可运行功能。详细进度见 [PROGRESS.md](PROGRESS.md)。

## 安装（开发环境）

```bash
pip install -e .
```

需要 Python 3.11+。如果要用 Claude Agent SDK 作为执行后端：

```bash
pip install -e ".[claude]"
```

## 架构一览

| 层 | 目录 | 职责 |
|---|---|---|
| 平台适配层 | `src/walkie_dokie/platforms/` | 把企业微信/QQ/微信等平台的消息统一转成内部 Event（MVP 先实现企业微信自建应用） |
| 编排层 | `src/walkie_dokie/orchestrator/` | LangGraph 管理跨消息的会话状态机（等待指令/执行中/等待确认/完成） |
| 执行层 | `src/walkie_dokie/agents/` | 可插拔的 coding agent 执行后端（Claude Agent SDK / Codex），在沙箱里写代码完成文档操作 |

每层为什么这么划分、否掉了哪些方案，见 [DECISION.md](DECISION.md)。
