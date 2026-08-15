# walkie-dokie — Progress

更新时间：2026-08-14（Asia/Shanghai）

## 合同智能 Data Spike 第一批

- **当前主线（2026-08-14）**：MVP 已拆为仅面向 PDF/Word 的链路 A（Agentic RAG）与面向 Excel 工程量清单结构化入库的链路 B。链路 A 暂停新增开发，当前只推进链路 B；两个显式 BOQ profile 已分别打通 XLSX/XLS → Evidence → Staging SQL，Admin 已可按同一甲方归属跨项目检索相似历史报价。
- 新增 Django 本地管理入口及合同智能领域模型，覆盖项目、不可变文件版本、原始文件哈希、ParserRun、Evidence、Retrieval Trace、人工最终稿声明和 IndexBuild 草稿。
- 新增 DOCX/XLSX 原生 baseline parser 与 PDF 文字层 baseline；未知或不可靠能力以 warning/失败显式暴露，不静默猜测。
- 新增 Chunk/Evidence 检查页和中文 BM25 Retrieval Test；检索候选、分词、阶段分数及稳定证据 ID 均可检查和回放。
- 第二批继续打通发布、合同问答、价格查询、飞书绑定和 Golden Dataset：发布有 Evidence Manifest 门禁；问答有原子 claim verifier 和一次受限重搜；价格采用白名单 MappingSpec → Staging → 人工 Trusted → 固定查询 → Decimal 计算；飞书私聊选择项目、群聊固定项目。
- 完成首轮代码审查修复：一次问题固定一个 IndexBuild 快照和唯一 QuestionRun，Planner 失败及选路 trace 可审计；飞书同一私聊用户/群聊会话串行处理并记录异常；已准备/发布的 IndexBuild 与既有 MappingSpec 在 Admin 中只读；合同 Django 测试不再污染无关 pytest invocation。
- 完成首批真实样例基线验证：XLSX 共 18 个 sheet、582 个非空行块和 1,075 个可读公式缓存；修复“一个隐藏列导致整行排除”后，默认排除行由 270 降为 0，隐藏列仍保留单元格级元数据。
- 新增 `crland_general_v1` 工程量清单导入：不变的 `BoqImportSpec` 携带项目/甲方上下文，18 个 sheet 保留快照，实体清单与综合单价分析合并为同一明细事实，开办费/附表/计日工与项目汇总分别进入 Staging；Admin 可批量 Trusted/Rejected。
- 真实 XLSX 已导入正式 `admin.db`（2026-08-14）：甲方按用户确认的原文保存为“华润”，314 条明细（实体 136、开办费 29、开办费附表 18、计日工 131）、11 条汇总和 18 个 sheet 快照全部落库；导入运行成功并通过清单/单价分析一一对应、成本分解、塔楼小计、不含税总计、税额和含税总价闭合校验。反查确认项目/甲方上下文唯一，全部保持 Staging，Trusted 为 0，待人工审核。
- 新增 `crland_lighting_xls_v1` 并完成第二份真实旧版 XLS 导入（2026-08-14）：原始甲方保存为“华润置地（深圳）有限公司”，人工归属为“华润”；217 条行证据、99 条明细（实体 52、开办费 31、安全文明施工附表 16）、6 条汇总和 7 个 sheet 快照进入正式 `admin.db`。不含税 5,305,209.07、税额 477,468.82、含税 5,782,677.89 均由叶子明细独立闭合；18 个未识别公式函数只作为 warning，全部记录保持 Staging。
- 新增 Admin 跨项目相似报价检索（2026-08-14）：查询限定同一 `party_a_group` 且排除当前项目，名称/型号/规格采用模糊评分，单位归一后硬匹配，功率/色温默认 ±10% 且可调整；用户指定数值而候选缺失时直接排除。真实数据以 `LED灯带 + m + 16.5W + 3000K` 验证，10% 命中另一项目 `安装LED灯带LL02`（15W、2700K、270.99 元/m），9% 不命中；默认 Trusted-only 为 0，显式勾选后才检查 Staging。
- 完成逐行相似报价入口与 Admin 数值收敛（2026-08-14）：每条 BOQ 明细都有“查相似报价”按钮，搜索页固定当前项目、展示源项并预填名称、标签化型号、完整规格及可唯一提取的功率/色温；只有用户勾选的参数参与搜索，单位始终硬匹配，候选保持同一 BOQ 类型。数据库与计算保留原始 `Decimal`，列表和结果常用数值只显示 3 位小数。定向测试 8 passed，全仓离线套件 154 passed。
- 完成灯具参数浮层查询（2026-08-14）：确认真实清单包含灯带、洗墙灯等灯具实体；新增带单位和原文的确定性参数提取，支持功率/功率密度、色温范围、光束角、电压、光效等独立数值容差，以及 IP、DMX、Ra/R9/SDCM、材质、颜色等可选模糊文本条件。明细列表表头固定，行按钮通过 Admin JSON API 在当前页浮层展示参数和结果表，不再跳转；长度/面积清单中按计价单位兼容 `W` 与 `W/m`、`W/m²` 的灯带写法。全仓离线套件 159 passed；本轮已用正式数据库洗墙灯/灯带行完成 HTTP API 冒烟，浏览器点击交互仍待真机确认。
- 补充工程量名称规格解析（2026-08-15）：名称与描述分开标记来源；`矩形洞孔0.10 m2以内` 解析为 `area_m2` 上限 0.10，`0.10-0.30 m2` 解析为上下限。名称规格目前只展示和留痕，不进入相似报价筛选，等待业务确认连续/离散语义。定向 BOQ 测试 12 passed。
- 完成两份真实 PDF 的只读诊断：318 页合同有 302 页文字层（94.97%），511 页合同有 446 页文字层（87.28%）；后者第 307–342 页为完整的 36 页扫描工程量清单，可与 XLSX sheet 结构对应。当前 PDF parser 只足够做文字层/扫描页预检，不足以作为最终版面和条款解析器。
- 全仓离线套件现为 161 passed；另验证 `pytest tests/test_graph.py` 可在不初始化合同 Django 设置的情况下独立通过（36 passed）。
- 链路 B 尚未完成正式数据 Trusted 审核、飞书查询入口、更多样本 profile 验证和面向报价决策的结果解释；链路 A 的 MinerU/Docling/PaddleOCR、Dense/Reranker 和 Phoenix 均按当前范围决策暂停。

## 当前结论

用户指出的根因成立：旧架构不是完全没有“主 Agent 逻辑”，而是这部分职责散落在 `draft.py`、graph、memory extractor、runner 和 coding-agent 输出里，没有唯一 owner。Claude Code/Codex 同时干文档、判断用户身份、提取长期记忆、写最终话术，导致机器人身份与用户身份很容易串在一起。

v2 已完成职责拆分：

```text
PlatformAdapter → Session coordination → LangGraph control plane
                                         ↓
                 MainAgent（理解、记忆候选、确认/最终话术）
                                         ↓ 确认后的 TaskContract
                 ExecutionAgent（只处理文件，返回内部报告）
```

旧 `orchestrator/draft.py` 与执行后额外提取的 `orchestrator/memory.py` 已删除。`ClaudeAgentSDKBackend`/`CodexBackend` 不再负责长期记忆或终端用户话术。

## 已验证

- `pytest` 与 `pytest tests/`：全量离线套件通过。套件不联网、不调用真实飞书/DeepSeek/Claude/Codex。
- MainAgent/ExecutionAgent 接口隔离；执行层只收 `TaskContract + input_path`，返回 `ExecutionReport`。
- MainAgent 输出显式 `chat/document_task` 意图并与 `reply/propose_task` 交叉校验；知识问答、闲聊以及 Word/Excel 方法咨询直接回复，只有实际文件操作进入执行单元。
- “你是小帮”即使被模型错误输出成合法 `name=小帮`，也会因缺少第一人称当前原文证据被 repository 拒绝。
- memory 只接受四个白名单字段、set/delete、当前原文逐字 evidence 与第一人称字段语义；支持“我是浮瓜，是这个项目的开发者”等中文连续陈述。校验通过后隐式写入并透明回显，不再二次确认；精确命令 `/long-term-memory` 确定性返回全部档案，不经过模型。
- 输入附件先写 `var/inputs/`，checkpoint 只保存 plain JSON reference；输出同样只保存 `var/workspaces/` 引用，不保存文件 bytes。
- 确认阶段附件通过单个可序列化 `Command(resume={text,file})` 恢复，不再使用 `aupdate_state` 的隐式 `as_node` 推断。
- runner 只把真实 `snapshot.interrupts` 且节点为 `ask_confirm`（或升级前遗留的 `ask_memory`）当成可恢复确认；失败节点的 `next` 不会吞掉下一条用户消息或自动重跑 execute。
- 生产入口所有 fresh/resume invoke 都显式使用 `durability="sync"`；`prepare_execution` 的 execution ID/workdir checkpoint 完成后才进入执行。独立元数据区的 started/report marker 会在未知结果时拒绝自动重跑，并覆盖“报告已返回但后置 checkpoint 失败”的常见窗口。
- 主 Agent/执行 Agent 的业务异常都结束为明确失败回合；turn log 失败不再把成功执行变成 pending execute。
- 上一轮输出 artifact 可由 MainAgent 通过 `use_previous_artifact` 显式选入下一任务，“继续修改刚才文件”不再只有文字上下文却拿不到文件。
- 确认词使用完整匹配；“好像不对”“可以先别做”“是，不过先改”不会误执行。
- 标准 `pytest` 不再收集时启动真实 backend smoke script；两个脚本已有 `__main__` guard，pytest 配置限定 `tests/`。
- ExecutionAgent prompt-injection 权限边界已收紧：Claude 使用 fail-closed Bash sandbox，Codex 使用最小 permission profile；两者都无执行网络、无 MCP/skills/子 Agent、无应用 secret 环境变量，只能写本轮 workspace。Codex profile 已在宿主 Linux 实测不能读取项目 README 或独立 CODEX_HOME，同时能写指定 workspace 并加载 python-docx/openpyxl。
- `.docx/.xlsx` 在执行前后经过确定性 OOXML 校验；宏、嵌入对象、外部关系、危险字段/公式、异常 ZIP 会被拒绝。graph 会再次验证执行报告和产物，不信任 backend 自报路径。
- 当前全量离线测试为 154 passed。

## 尚未验证

- v2 尚未在正常部署 OS 上跑完真实 `DeepSeek → AsyncSqliteSaver → Claude → 飞书` 全链路。
- 当前 Codex 受管 sandbox 禁止 asyncio socketpair/self-pipe 唤醒；历史同步图因此看似卡死，`aiosqlite` 的 worker thread 在这里也受影响。离线 `InMemorySaver` 通过不能替代生产 SQLite 冒烟。
- memory 的 fake-client contract 与确定性 policy 已测，但真实 DeepSeek 的多轮 adversarial/golden eval 尚未完成。
- Claude 真实冒烟当前被账户月度额度上限拒绝，尚未进入工具调用；安全配置和错误路径已加载，但正常 docx/xlsx 生成、超时、取消后的子进程/远端状态仍需在额度恢复后重新冒烟。
- Codex 最小权限 profile 已做本地命令隔离测试，但独立 `var/codex_home` 尚未登录，真实模型文档任务仍未冒烟。
- 飞书重投、发送半成功、进程在 checkpoint/投递边界崩溃尚无端到端故障注入测试。

## 当前数据流

```text
飞书消息 → FeishuAdapter → InboundEvent
  → 非确认消息：Debouncer(platform,user_id) 10 秒聚合
  → 附件写 ArtifactStore，bytes 到此为止；图只收到 reference
  → UserLocks[platform:user_id]
  → graph.ainvoke(thread_id="platform:user_id")

  collect：合并 pending input，清理上回合 result/memory delta
  main_agent：
      输入 task context、当前用户原文、附件/上一产物文件名、短期历史、白名单档案
      输出 reply/propose_task、用户话术、TaskContract、memory operations + evidence
      repository 保守校验 → 通过后隐式落盘，并透明回显实际变更

  reply → END
  propose_task → ask_confirm interrupt
      补充/否定 → collect → main_agent
      “是” → prepare_execution → execute

  精确命令 /long-term-memory → 确定性读取当前用户全部长期档案

  execute：ExecutionAgent 只收自包含 task + 可选 input path
      → ExecutionReport(summary/artifact/warnings)
      → MainAgent.finalize
      → graph result 只存 artifact reference
      → runner 先发文件、再发文字
```

## 下一步（按优先级）

### P0：真实冒烟与对外使用前可靠性

1. 在正常 OS 上跑真实 v2 闭环，并验证 SQLite 进程重启后的 ask-confirm 恢复。当前 sandbox 不能完成这项证明。
2. 增加持久 inbox/outbox：`InboundEvent.event_id` 去重；文件与文字分别记录 delivery ack/retry。现在图成功后平台发送失败会丢结果，文件成功而文字失败会半投递。
3. 将 execution idempotency 扩展到 backend 边界。started marker 会在结果未知时拒绝自动重跑，但无法判断 coding agent 是否已经完成，只能转人工恢复；这仍不是 exactly-once。
4. 公开给开发者本人以外的人之前，将当前订阅登录改为合规服务鉴权，并把已启用的进程级 OS sandbox 再放进专用服务账号 + container/cgroup，补齐 CPU、内存、进程数和磁盘配额。

### P1：状态和部署边界

1. 将 `run_mvp.py` 中 debounce、locks、任务追踪、delivery queue 收进正式 `SessionCoordinator`；现在入口脚本仍承担过多 application service 职责。
2. `UserLocks` 仅单进程有效。多进程/多实例部署需要按 session 分区的队列或分布式并发控制。
3. `checkpoints-v2.db` 只是换文件名，不是正式 schema migration。增加应用 schema version、兼容性检测和明确迁移策略。
4. 把当前“确认后直接写 JSON”升级为带 `turn_id/evidence/source` 的可撤销 memory ledger，解决 memory 文件写入、graph checkpoint 与用户通知不在同一事务的问题。
5. 引入真正 ArtifactStore ID/URI。绝对本地 path 只适合当前单机持久卷。
6. 生成依赖 lockfile；当前只 pin 了直接 LangGraph 依赖，传递依赖仍可能漂移。

### P2：产品与质量

- 建立真实 DeepSeek memory/task-contract golden set，包括助手身份、他人身份、纠正、删除、引用上一文件等对抗样例。
- 分别评估 MainAgent 的任务理解与 ExecutionAgent 的文档质量，不再把问题笼统归因于一个 extraction/draft prompt。
- 针对中老年用户重新设计确认、纠错、失败恢复与文件连续编辑体验。
- 再决定云主机 OS、常驻进程/自动重启、日志降噪和个人微信接入路线。

## 本次完成的架构变更

- 新增 `main_agent/`：普通 chat 模型、MainAgent contracts、memory policy/repository。
- 新增 `artifacts.py`：输入/输出 artifact reference 的落盘、解析和边界校验。
- 重写 `orchestrator/graph.py`：LangGraph 只做控制流；所有 node/route 为 async；加入 prepare/marker、严格 interrupt 恢复、跨轮 active artifact。
- 收窄 `agents/`：只执行任务并返回内部报告；输入由 bytes 改为 path；graph 会重新验证产物确属本轮 workdir；Codex schema 保留名冲突已消除；Claude/Codex 的最小权限沙箱与 Office 主动内容校验已加入。
- 更新 `run_mvp.py`：复合 session key 贯穿 debounce/lock/thread，附件入图前落盘，只按真实 interrupt resume。
- 更新 README、TECHNICAL、DECISION、PITFALLS 与 LangGraph 教学提示词。

早期 v1 的真实飞书闭环、Ollama/DeepSeek 选型、Windows Codex 限制等历史仍保留在 [DECISION.md](DECISION.md)、[PITFALLS.md](PITFALLS.md) 与 [progress archive](docs/progress-archive.md)；它们不应被误读为 v2 当前实现。
