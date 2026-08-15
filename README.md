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

运行测试：

```bash
pip install -e ".[claude,dev]"
pytest tests/
```

主 Agent 通过 OpenAI 兼容 SDK 调用 DeepSeek，执行 Agent 当前默认用 Claude Agent SDK。鉴权和对外使用边界见 [.env.example](.env.example) 与 [PITFALLS.md](PITFALLS.md)。

执行任务把用户指令、文件名和文档内容全部视为不可信输入。Claude 后端只开放沙箱内 Bash，禁用 MCP、skills、子 Agent、网页与网络，清除应用凭证环境变量，并且只读 Python 运行时、只写本轮用户工作区；Codex 后端使用等价的最小 permission profile。输入和输出只接受经过确定性检查的 `.docx/.xlsx`，宏、嵌入对象、外部关系、危险字段/公式和异常压缩包会在 Agent 前后被拒绝。prompt 约束只是辅助，权限边界由 OS 沙箱和产物校验承担。

## 运行 MVP

复制 `.env.example` 为 `.env`，配置飞书凭证、`DEEPSEEK_API_KEY`，并为 Claude Agent SDK 配好鉴权，然后运行：

```bash
python scripts/run_mvp.py
```

在飞书里给自建应用机器人发一句话，例如“帮我写一份请假条”。10 秒防抖窗口结束后，小帮会先确认理解，用户明确回复“是”才调用执行单元。

用户明确说出的姓名、部门、职位或常用称呼会在逐字证据校验通过后自动写入长期记忆，并透明回显实际变更，不再要求二次确认。单独发送 `/long-term-memory` 可查看当前保存的全部长期记忆；该命令不经过模型。

## 合同智能 Data Spike 管理台

仓库内已经加入独立的 `contract_intelligence` 领域模块。当前第一刀用于接收真实 DOCX/XLS/XLSX/PDF 样例并检查结构，不会把尚未接入的 Dense/Reranker/OCR 冒充为完整 Hybrid RAG。

初始化本地管理数据库并创建管理员：

```bash
python scripts/manage_contracts.py migrate
python scripts/manage_contracts.py createsuperuser
python scripts/manage_contracts.py runserver 127.0.0.1:8000
```

打开 `http://127.0.0.1:8000/admin/`，依次创建知识库项目、逻辑文档和不可变文档版本，在版本页上传结构化原件及正式 PDF。回到版本列表执行“baseline ingestion”，随后通过“Chunk / Evidence”查看：

- DOCX 条款、标题和表格行及其 source anchor；
- Excel 97–2003 XLS 的 sheet/行/单元格锚点、缓存值和公式能力限制；
- XLSX sheet、行、单元格、公式、缓存值、隐藏区域和合并区域；
- PDF 物理页、印刷页标签以及无文字层/OCR 待办；
- Parser warning、原件/PDF baseline 一致性报告；
- 中文 BM25 Retrieval Test 的分词、候选、分数、稳定 Evidence ID 和持久化 Trace。

完成最终稿人工确认后，创建 `IndexBuild` 并选择每份文档的成功 `ParserRun`。列表页依次执行“校验并准备 IndexBuild”和“原子发布 READY IndexBuild”。项目发布后可从项目列表进入“MVP 问答”页面：

- 普通合同事实走受限 LangGraph：BM25 → DeepSeek Atomic Claims → 独立 Evidence Verifier；第一次不足时最多改写检索一次，仍不足则拒答。
- 价格先建立版本化 `PriceMappingSpec`，导入后进入 Staging；只有管理员人工确认为 Trusted 的记录能被查询。缺地区等条件会澄清，冲突会拒答，数量计算使用 `Decimal` 并返回计算账本。
- 每次查询持久化 `QuestionRun`、Retrieval Trace、Provider 版本和 verifier 结果。
- `GoldenCase` 可标注回答/拒答/澄清、期望证据和数值结果；评估输出 Retrieval Recall@K、Answer/Citation/Numeric Accuracy 与 Hallucination Rate。未接 Reranker 时该指标明确为空。

当前 MVP 主线已转为 Excel 工程量清单结构化入库，PDF/Word Agentic RAG 暂停新增开发。在上传与 profile 对应的 XLSX 或 XLS 并完成 baseline ingestion 后：

1. 在 Admin 创建“工程量清单导入配置”，选择已验证 profile，填写项目全称、原文件甲方全称和人工维护的甲方归属（例如原名“华润置地（深圳）有限公司”、归属“华润”）。
2. 执行“按已验证模板导入工程量清单到 Staging”。项目名称与封皮不一致、甲方为空、表头漂移、公式无缓存、清单/单价分析不对应或金额不闭合时，整批失败。
3. 通过“工程量清单 Sheet 快照”、“工程量清单明细”和“工程量清单汇总记录”检查导入结果，只将人工复核通过的记录批量标为 Trusted。
4. 在“工程量清单明细”列表中，直接点击目标行的“查相似报价”。当前页浮层会展示源项和带原文的技术参数：功率/功率密度、色温（含范围）、光束角、电压、光效等数值可逐项启用并独立调整容差；IP、型号、DMX、Ra/R9/SDCM、材质、颜色等离散属性可选模糊文本匹配。名称中的工程规格（例如 `0.10 m2以内`、`0.10-0.30 m2`）也会解析并标记来源，但在匹配语义确认前不会参与筛选。长度/面积计价的灯带会兼容 `W` 与 `W/m`、`W/m²` 的常见写法。结果仍只查同一甲方归属下其他项目、同一 BOQ 类型的 Trusted 明细，单位始终硬匹配，默认数值容差 10% 且可调整；管理员需要检查未审核数据时可显式勾选“包含 Staging”。列表表头固定，列表和结果显示 3 位小数，数据库仍保留原始精度。

现有 profile 只支持已用真实样例验证的 18-sheet XLSX 模板和 7-sheet 商业泛光照明 XLS 模板。其他工程量清单格式应新增显式 profile，不会模糊猜列。相似搜索返回历史事实、参数差异和源行证据，不等于系统推荐价。

飞书合同入口使用独立进程：

```bash
python scripts/run_contract_feishu.py
```

管理员先创建“飞书项目绑定”：私聊用户可以授权多个项目并选择一个；群聊使用 `chat_id` 固定一个项目。私聊命令为 `/contract projects`、`/contract use 项目标识`、`/contract 问题`；群聊消息固定查询绑定项目。该进程和现有 Office 文档生成机器人分离，生产部署时同一飞书应用不能同时由两个长连接进程消费，应使用独立应用，或在统一入口中做确定性分流。

SQLite 只用于本地 Data Spike。设置 `CONTRACT_DB_ENGINE=postgresql` 后可切 PostgreSQL；安装驱动使用 `pip install -e ".[postgres]"`。Django 开发服务器只适合本机调试，不是生产部署方式。

工程量清单后台 ingestion 目前同步执行，没有 Celery；跨项目相似报价当前仅在 Admin 提供，飞书尚未接入且未来只允许查询 Trusted。合同链路仍只有 BM25 和 PDF 文字层解析，没有 Dense/Reranker/OCR；RAGFlow/Phoenix 也尚未接入，且按当前范围决策暂停。

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
| 合同智能 | `contract_intelligence/`、`contract_admin/` | 不可变版本、原生解析、Evidence、检索 Trace 与本地管理入口 |

稳定接口和 LangGraph 运行语义见 [TECHNICAL.md](TECHNICAL.md)，本次架构审阅及未完成风险见 [架构审阅](docs/architecture-review-2026-08-12.md)。
