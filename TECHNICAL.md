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
          ├─ ask_memory(interrupt) → 保存/丢弃候选 → END
          └─ ask_confirm(interrupt)
               ├─ 补充/否定 → collect → main_agent
               └─ 确认 → prepare_execution → execute → END
  → ExecutionReport
  → main_agent.finalize
  → 文件、文字投递
```

`prepare_execution` 先生成稳定 `execution_id/workdir`；生产入口显式传 `durability="sync"`，保证该 superstep 的 checkpoint 完成后才进入有外部副作用的 `execute`。编排元数据写在执行 Agent cwd 之外：调用 backend 前先写 started marker，完成后写原子 report marker。若恢复时只有 started、没有可信 report，系统拒绝自动重跑并把 outcome 视为未知；若已有 report，则复用它。这降低了重复风险，但不能判断未知执行究竟完成到哪一步，也不构成通用 exactly-once 保证。

## MainAgent 契约

接口位于 `main_agent/base.py`：

1. `decide(DialogueContext) -> MainAgentDecision` 只能直接回复或提出文档任务。
2. `TaskContract.instruction` 必须自包含；缺失信息采用什么默认值或占位符由主 Agent 写进契约，graph 不追加业务 prompt。
3. `use_previous_artifact` 只有主 Agent 能根据“继续修改刚才的文件”等语义设置。执行层不会自行猜测上一轮文件。
4. `finalize(FinalizeContext) -> str` 把内部执行报告改写成用户回复。finalize 失败时使用确定性完成文案，不重新执行已经产生的副作用。
5. 主 Agent 只收到 artifact 文件名等元数据，不获得任意文件系统工具。

### 长期记忆治理

`MemoryOperation` 只允许 `name/department/job_title/preferred_address` 与 `set/delete`。每个候选必须附带逐字来自当前用户消息的 `evidence`；repository 先做保守校验：

- evidence 是当前这一条用户文本的子串，而不是旧历史或助手回复；
- set 的值出现在 evidence 中，并匹配保守的第一人称字段句式；
- delete 同时包含删除意图和对应字段；
- 用户文件键包含原始 ID 的 hash，清洗后相同的两个 ID 不会串档案；
- 写入使用同目录临时文件加 `os.replace`，实际变更会透明回显。

校验通过不等于立即写入：普通对话会进入 `ask_memory`，用户回复“记住”才保存，回复“不用记”则丢弃；文档任务回复“是”只执行，回复“是并记住”才同时保存。这样 regex/模型误判只会产生可拒绝候选，不会静默污染档案。这仍不等于形式化语义证明；真实模型需做 golden eval，未来更强方案是带 `turn_id/source` 的可撤销 ledger。

`pending_instruction` 可能累积确认前多条消息；`current_user_text` 单独保存最后一条用户原文，只有后者可以作为记忆证据。

## Artifact 与 ExecutionAgent 契约

1. 平台附件在入图前写入 `var/inputs/`，`SessionState.pending_file/new_file` 只保存 plain JSON `ArtifactReference(kind,path,filename,mime_type)`。
2. 输出保存在 `var/workspaces/{platform_user}/{date}/{run_id}/`。图状态同样只保存引用，不保存 docx/xlsx bytes。
3. `ExecutionAgent.run(instruction, input_path, workdir, input_filename)` 接受路径，不接受附件 bytes；后端把输入复制进自己的工作目录。
4. 输入名被收窄为 basename。执行器输出必须是工作目录内现存的单个普通文件；绝对路径、`..`、子目录、越界 symlink、目录冒充文件都会被拒绝。
5. `ExecutionReport(summary, artifact_path, result_filename, warnings)` 有运行时不变量：path/filename 同时存在或同时为空，名字一致，path 指向普通文件，warnings 是字符串 tuple。graph 在插件边界还会按本轮 workdir + basename 重建路径并要求与报告一致，拒绝 sibling workspace 产物。
6. Codex 的内部 output schema 位于保留子目录 `.walkie-dokie/`，不会覆盖同名用户上传文件。

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
- `StateSnapshot.next` 表示该快照待执行的节点，不等于“正在等用户确认”。runner 只在 `snapshot.interrupts` 非空且 next 是已知的 `ask_confirm` 或 `ask_memory` 时 resume。
- `interrupt(value)` 不是普通 return，也不保存 Python 栈。恢复时被中断节点从头运行，到同一 interrupt 序号取得 resume 值；payload/resume 必须可序列化，不能捕获框架内部中断异常，也不能在重跑时随意改变多个 interrupt 的顺序。
- 确认回复使用一个可序列化 resume object：`{"text": ..., "file": ArtifactReference | null}`。附件不再通过 `aupdate_state` 插入暂停点，因此没有隐式 `as_node` 推断和两次调用之间的崩溃窗口。
- 项目文件名 `checkpoints-v2.db` 中的 v2 是本项目 state schema 版本，与 LangGraph `ainvoke(version="v2")` 返回格式无关；当前仍使用默认 v1 输出并读取 `state["__interrupt__"]`。

checkpoint 保存工作流短期状态，不保存或事务性覆盖：长期档案、ArtifactStore、turn log、防抖 buffer、平台投递确认。默认 durability 也不应被描述成“每个节点返回时已经同步事务提交”。

## 并发与可靠性边界

- checkpointer 不会自动串行同一 thread 的并发 `ainvoke()`。`UserLocks` 以复合 session key 串行化当前单进程入口；多进程或多实例需要外部队列/分布式锁与版本冲突策略。
- MainAgent API、ExecutionAgent、JSON memory、工作目录与平台发送不和 SQLite checkpoint 构成一个事务。report marker 只覆盖其中一部分 crash window。
- 执行或主 Agent 的业务异常会被节点转换成完成态错误结果，避免下一条用户消息误触发旧节点重跑。checkpointer/进程级崩溃仍需要显式恢复策略。
- 平台投递当前没有持久 outbox；文件成功但文字失败会形成半投递。平台事件也尚无持久 inbox/event-id 去重。这两项是对外使用前的可靠性待办。
- `recent_messages` 同时按 12 条、单条 2,000 字符、总计 12,000 字符截断；它不是无限对话历史。

## 为什么当前 sandbox 曾让 `ainvoke()` 看似卡死

历史同步图在这次 Codex 受管 Linux sandbox 中会卡住，但根因不是 LangGraph 1.2.11 或 `InMemorySaver` 死锁：该环境对 Unix `socketpair.send()` 返回 `EPERM`。asyncio 的 `call_soon_threadsafe()` 依赖 selector self-pipe 唤醒；LangGraph/`langchain-core` 又用 executor 适配同步节点和同步 route。worker 已经计算完成，却无法唤醒 event loop，于是 await 一直 pending。纯 `asyncio.to_thread()` 也可复现，说明 LangGraph、interrupt、InMemorySaver 都不是这个现象的必要条件。

当前所有图节点和 route 都是 `async def`，所以 `InMemorySaver` 离线流程正常。但 `aiosqlite` 自身使用 worker thread；当前受限 sandbox 仍不能代表生产 runner 能启动。正常部署 OS 必须允许跨线程 event-loop 唤醒，并需重新做真实 `AsyncSqliteSaver + DeepSeek + Claude + 飞书` 冒烟。

## 测试边界

`pytest` 通过 `testpaths=["tests"]` 只收集离线套件；两个真实 backend smoke script 有 `__main__` guard，不会在 collection 时启动外部 Agent。图测试使用 `InMemorySaver`、fake agents 和临时 Artifact/Memory/Workspace 根目录。它能验证协议和状态流，但不能替代真实模型语义 eval、生产 SQLite、平台重投/半投递或 sandbox 安全测试。
