# walkie-dokie — Technical

本文记录当前 v2 的跨模块契约。设计演化见 [DECISION.md](DECISION.md)，验证状态见 [PROGRESS.md](PROGRESS.md)。

## 三个角色必须分开

- `MainAgent` 是唯一的用户语义层。它理解对话、维护“小帮”的身份、形成 `TaskContract`、提出长期记忆候选，并编写确认与完成话术。当前 `DeepSeekMainAgent` 走普通 chat API，没有 shell 或文件工具。
- LangGraph 是控制平面。它运行节点和边、保存 thread 内短期状态、在确认处暂停和恢复；它没有人格，也不判断什么值得长期记忆。
- `ExecutionAgent` 是 coding-agent 执行层。Claude Code/Codex 只接收已经确认的任务契约与可选文件路径，返回内部 `ExecutionReport`；它不看对话历史、不写长期档案、不直接向用户说话。

平台故障的确定性降级文案由 application/presenter 生成，不意味着执行 Agent 获得用户话术职责。

## 当前数据流

```text
InboundEvent
  → Debouncer(platform, user_id)
  → 输入附件落 var/inputs，得到 ArtifactReference（无 bytes）
  → UserLocks[platform:user_id]
  → LangGraph
      collect
        → main_agent.decide
          ├─ reply → END
          └─ ask_confirm(interrupt)
               resume 后四层确定性预判（`_route_confirm`，纯函数）：
               ├─ 白名单（是/是的/确认/没错）→ prepare_execution → execute → END
               ├─ 放弃词完整匹配（算了/不做了/取消…）→ cancel_task（清 pending、
               │   固定话术、保留 active_artifacts）→ END
               ├─ 否定词命中（硬否决，模型无权推翻）→ collect → main_agent
               └─ 灰区 → judge_confirm 节点（main_agent.judge_confirmation，
                     用户零感知，verdict 只进日志）
                     ├─ confirm → prepare_execution → execute → END
                     ├─ revise → collect → main_agent
                     └─ cancel → cancel_task → END
                   判定异常在节点内降级为 revise，绝不降级为 confirm
  → ExecutionReport
  → main_agent.finalize
  → 回合终点：文件、文字按序写进持久 outbox（`var/outbox.db`），回合到此结束
      投递 worker（进程内唯一调 `platform.send` 的地方）异步取件：
      每 session 只取队头保序、失败退避重试、第 4 次失败转死信、
      启动时 `reset_sending` 复位崩溃残留（at-least-once）
  → 入队后（同一 session 锁内）：被挤出窗口的历史消息攒满 6 条时，
    专用 compact invoke → haiku 摘要 → 逐字 evidence 机械校验 →
    conversation_summary（随 checkpoint 持久，facts 注入后续 decide）
```

`prepare_execution` 先生成稳定 `execution_id/workdir`；生产入口显式传 `durability="sync"`，保证该 superstep 的 checkpoint 完成后才进入有外部副作用的 `execute`。编排元数据写在执行 Agent cwd 之外：调用 backend 前先写 started marker，完成后写原子 report marker。若恢复时只有 started、没有可信 report，系统拒绝自动重跑并把 outcome 视为未知；若已有 report，则复用它。这降低了重复风险，但不能判断未知执行究竟完成到哪一步，也不构成通用 exactly-once 保证。

## MainAgent 契约

接口位于 `main_agent/base.py`：

1. `decide(DialogueContext) -> MainAgentDecision` 输出显式 `intent`：`chat` 必须对应 `action=reply`，由 MainAgent 直接回答；`document_task` 必须对应 `action=propose_task`，经确认后才进入 ExecutionAgent。控制平面拒绝不一致的协议结果。
2. `TaskContract.instruction` 必须自包含；缺失信息采用什么默认值或占位符由主 Agent 写进契约，graph 不追加业务 prompt。
3. `use_previous_artifact` 只有主 Agent 能根据“继续修改刚才的文件”等语义设置。执行层不会自行猜测上一轮文件。
4. `finalize(FinalizeContext) -> str` 把内部执行报告改写成用户回复。finalize 失败时使用确定性完成文案，不重新执行已经产生的副作用。
5. 主 Agent 只收到 artifact 文件名等元数据，不获得任意文件系统工具。
6. `judge_confirmation(ConfirmationContext) -> ConfirmationVerdict` 对灰区确认回复输出 confirm/revise/cancel 三分类；只在四层确定性预判都不命中时被调用。不变式：该路径上任何异常/非法输出只允许落向 revise（多澄清一轮），绝不落向 confirm（误执行）。

### 长期记忆治理

`MemoryOperation` 只允许 `name/department/job_title/preferred_address` 与 `set/delete`。每个候选必须附带逐字来自当前用户消息的 `evidence`；repository 先做保守校验：

- evidence 是当前这一条用户文本的子串，而不是旧历史或助手回复；
- set 的值出现在 evidence 中，并匹配保守的第一人称字段句式；
- delete 同时包含删除意图和对应字段；
- 用户文件键包含原始 ID 的 hash，清洗后相同的两个 ID 不会串档案；
- 写入使用同目录临时文件加 `os.replace`，实际变更会透明回显。

校验通过后会立即、幂等地写入，并透明回显实际变更；不再要求二次确认。安全边界因此依赖“当前原文逐字 evidence + 字段语义规则 + 白名单 + 用户可见回显”，而不是模型输出本身。精确命令 `/long-term-memory` 由入口层优先处理，并在同一用户锁内读取全部档案；图内也有确定性兜底，不经过 MainAgent。升级前已停在 `ask_memory` 的 checkpoint 仍可恢复，但新回合不会再进入该节点。这仍不等于形式化语义证明；更强方案是带 `turn_id/source` 的可撤销 ledger。

`pending_instruction` 可能累积确认前多条消息；`current_user_text` 单独保存最后一条用户原文，只有后者可以作为记忆证据。

## Artifact 与 ExecutionAgent 契约

1. 平台附件在入图前写入 `var/inputs/`，`SessionState.pending_files/active_artifacts` 保存 plain JSON `ArtifactReference(kind,path,filename,display_filename,mime_type)` 的 tuple；`display_filename` 仅在同一防抖窗口内文件名碰撞时才被赋值（如 `报价单-2.xlsx`），否则为 `None`，此时展示名回退到 `filename`。
2. 输出保存在 `var/workspaces/{platform_user}/{date}/{run_id}/`。图状态同样只保存引用，不保存 docx/xlsx bytes；一次执行可以产出多个文件。
3. `ExecutionAgent.run(instruction, input_paths, input_filenames, workdir)` 接受两个等长的 tuple（路径、对应文件名），不接受附件 bytes；后端把输入复制进自己的工作目录。两个 backend 共用 `agents/base.py` 的 `stage_execution_inputs()` 做校验+拷贝，不再各自重复实现。
4. 输入名被收窄为 basename。执行器输出必须是工作目录内现存的普通文件（可以是多个）；绝对路径、`..`、子目录、越界 symlink、目录冒充文件都会被拒绝。
5. `ExecutionReport(summary, artifacts, warnings)` 中 `artifacts: tuple[ExecutionArtifact, ...]`，`ExecutionArtifact(path, filename)` 的运行时不变量是 `path.name == filename` 且 `path` 指向普通文件；`ExecutionReport` 还要求 `artifacts` 内 `filename` 不重复，`artifacts=()` 合法代表无产出，`warnings` 是字符串 tuple。graph 在插件边界会对每个 artifact 按本轮 workdir + basename 重建路径并要求与报告一致，拒绝 sibling workspace 产物。
6. Codex 的内部 output schema 位于保留子目录 `.walkie-dokie/`，不会覆盖同名用户上传文件。

### 不可信文档与 prompt injection 边界

- 用户指令、文件名、Office 文档内容和执行报告都视为不可信数据；提示词会明确标注这一点，但提示词本身不作为安全边界。
- Claude 只开放 `Bash`，关闭 WebFetch/WebSearch、MCP、skills、子 Agent、auto-memory 与配置源；Bash 必须进入 fail-closed OS sandbox，不能申请 unsandboxed fallback。沙箱只读 Python 运行时、只读写本轮 workdir，拒绝其他 home/tmp、网络、Unix socket、本地监听和应用凭证环境变量。
- Codex 不使用传统 `workspace-write` 的广泛读取模型，而加载 app-owned permission profile：`:minimal` 与 Codex/Python runtime 只读，本轮 workspace root 可写，网络关闭；独立 `CODEX_HOME` 不在命令可读范围内。执行为 non-interactive、ephemeral，不加载用户 config、rules、skills 或 web search。
- Office 输入复制前与输出发布前都会解析 OOXML ZIP：只接受 `.docx/.xlsx`，限制压缩/解压体积和成员数，拒绝路径穿越、加密、宏、ActiveX、OLE/嵌入对象、外部关系、危险 Word 字段与会访问外部资源的 Excel 公式。
- graph 在插件边界重新验证 report 长度、artifact basename/真实路径与 Office 内容；MainAgent finalize 没有 shell/文件工具，并把 report 字段当不可信数据。

这些措施限制的是 prompt injection 能获得的能力，不是完整的租户虚拟机隔离。CPU/内存/进程数配额、恶意解析库 0-day 与强制过期清理仍应由容器/cgroup、专用服务账号和生命周期策略补齐。

本地 path 只适合单机持久卷。多机部署必须换成对象存储 ID/URI，不能把某台机器的绝对路径当分布式 artifact 标识。

## LangGraph 如何实现本项目工作流

`StateGraph(SessionState)` 是 builder，`compile()` 生成基于 Pregel runtime 的 `CompiledStateGraph`。TypedDict 字段成为 state channel；本项目没有 `Annotated` reducer，因此字段默认是覆盖语义。节点返回的局部 dict 被写成 channel update，边和条件分支最终触发下一节点。

`ainvoke()` 内部消费 `astream()`。Pregel/BSP 的一个 superstep 大致经历：

1. 从输入或 checkpoint 恢复 channel values、versions 与 pending writes；
2. 根据 trigger channel 规划本步 actors；
3. 运行本步 actors，writes 先缓冲，同一步的并行 actor 看不到彼此更新；
4. 合并 writes、更新 channel version、产生下一批 trigger，并按 durability 配置保存 checkpoint；
5. 无下一任务则结束，遇到 `interrupt()` 则返回暂停信息。

条件分支有一个容易讲错的细节：附着在源节点 writer pipeline 上的 route 可以读取源节点刚产生的局部更新；这不等于同一 superstep 的其他并行 actors 能看到彼此 writes。

## checkpoint、thread、interrupt

- 生产 checkpointer 是 `AsyncSqliteSaver`，分区键为 `thread_id="{platform}:{user_id}"`。`thread_id` 是运行配置 namespace，不是 state 字段本身。
- `Checkpoint`、checkpoint metadata、`CheckpointTuple.pending_writes` 与面向调用者的 `StateSnapshot` 是不同层次，不能统称一个 dict。
- `StateSnapshot.next` 表示该快照待执行的节点，不等于“正在等用户确认”。runner 只在 `snapshot.interrupts` 非空且 next 是已知的 `ask_confirm`，或升级前遗留的 `ask_memory` 时 resume。
- `interrupt(value)` 不是普通 return，也不保存 Python 栈。恢复时被中断节点从头运行，到同一 interrupt 序号取得 resume 值；payload/resume 必须可序列化，不能捕获框架内部中断异常，也不能在重跑时随意改变多个 interrupt 的顺序。
- 确认回复使用一个可序列化 resume object：`{"text": ..., "file": ArtifactReference | null}`（用户在确认阶段的即时回复，一条平台消息最多带一个附件）或 `{"text": ..., "files": [ArtifactReference, ...]}`（防抖批次在派发前发现图已经进入确认态，携带这一整批文件）；`ask_confirm`/`ask_memory` 归一接受两种形状，统一并入 `SessionState.new_files`。附件不再通过 `aupdate_state` 插入暂停点，因此没有隐式 `as_node` 推断和两次调用之间的崩溃窗口。
- 项目文件名 `checkpoints-v2.db` 中的 v2 是本项目 state schema 版本，与 LangGraph `ainvoke(version="v2")` 返回格式无关；当前仍使用默认 v1 输出并读取 `state["__interrupt__"]`。

checkpoint 保存工作流短期状态，不保存或事务性覆盖：长期档案、ArtifactStore、turn log、防抖 buffer、平台投递确认。默认 durability 也不应被描述成“每个节点返回时已经同步事务提交”。

## 并发与可靠性边界

- checkpointer 不会自动串行同一 thread 的并发 `ainvoke()`。`UserLocks` 以复合 session key 串行化当前单进程入口；多进程或多实例需要外部队列/分布式锁与版本冲突策略。
- MainAgent API、ExecutionAgent、JSON memory、工作目录与平台发送不和 SQLite checkpoint 构成一个事务。report marker 只覆盖其中一部分 crash window。
- 执行或主 Agent 的业务异常会被节点转换成完成态错误结果，避免下一条用户消息误触发旧节点重跑。checkpointer/进程级崩溃仍需要显式恢复策略。
- 平台投递走持久 outbox（`orchestrator/outbox.py`）：回合终点只入队，发送由投递 worker 独占，语义是 at-least-once 而不是 exactly-once——进程崩在 `sending` 上时无从判断平台收没收到，启动一律复位重寄，用户可能收到重复消息。同 session 严格保序，所以不会再出现"文字先于文件到达"的半投递；彻底失败的消息进死信区等人工处理，不会静默消失。入站侧按 `InboundEvent.event_id` 去重（`inbox_seen`，7 天 TTL），平台没给事件 id 时不猜、不去重。
- conversation turn log 的 `success` 记的是"图产出成功且已入队"，不是"平台已送达"（2026-08-21 起）。投递成败只体现在 outbox 的行状态与死信区，读成功率时不要把两者混为一谈。
- `recent_messages` 同时按 12 条、单条 2,000 字符、总计 12,000 字符截断；它不是无限对话历史。

## 合同智能已拆分

合同问答快照/审计边界、工程量清单确定性入库边界原本记录在这里，2026-08-15 随
`contract_intelligence`/`contract_admin` 一起拆分到独立仓库
[contract-intelligence](../contract-intelligence)（`git filter-repo` 保留了历史），
见该仓库的 TECHNICAL.md。

## 为什么当前 sandbox 曾让 `ainvoke()` 看似卡死

历史同步图在这次 Codex 受管 Linux sandbox 中会卡住，但根因不是 LangGraph 1.2.11 或 `InMemorySaver` 死锁：该环境对 Unix `socketpair.send()` 返回 `EPERM`。asyncio 的 `call_soon_threadsafe()` 依赖 selector self-pipe 唤醒；LangGraph/`langchain-core` 又用 executor 适配同步节点和同步 route。worker 已经计算完成，却无法唤醒 event loop，于是 await 一直 pending。纯 `asyncio.to_thread()` 也可复现，说明 LangGraph、interrupt、InMemorySaver 都不是这个现象的必要条件。

当前所有图节点和 route 都是 `async def`，所以 `InMemorySaver` 离线流程正常。但 `aiosqlite` 以及 Django ORM 的 `sync_to_async()` 都使用 worker thread；当前受限 sandbox 中后者也会出现 SQL 已完成而 Future 未唤醒。相关集成测试只在测试侧提供 event-loop heartbeat 或以内联 fake 隔离线程桥接；生产代码不轮询掩盖环境问题。正常部署 OS 必须允许跨线程 event-loop 唤醒，并需重新做真实 `AsyncSqliteSaver + DeepSeek + Claude + 飞书` 冒烟。

## 测试边界

`pytest` 通过 `testpaths=["tests"]` 只收集离线套件。两个真实 backend smoke script 有 `__main__` guard，不会在 collection 时启动外部 Agent。图测试使用 `InMemorySaver`、fake agents 和临时 Artifact/Memory/Workspace 根目录。它能验证协议和状态流，但不能替代真实模型语义 eval、生产 SQLite、平台重投/半投递或 sandbox 安全测试。
