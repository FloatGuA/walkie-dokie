# 给另一个 Codex 的 LangGraph 源码导读提示词

下面 5 条按顺序复制给另一个 Codex。每条都能单独使用；前 4 条要求它只读本地项目和已安装源码，第 5 条用于检验理解。

## 1. 先分清 MainAgent、LangGraph 与 ExecutionAgent

```text
你现在是我的 LangGraph 教练。请基于本地真实代码讲解，不要泛泛背诵概念，也不要替我修改项目。

项目：/home/fgua/projects/walkie-dokie
环境：Python 3.12，langgraph 1.2.11，langgraph-checkpoint 4.2.0，langgraph-checkpoint-sqlite 3.1.1。

硬性限制：
1. 只读；不创建、修改或删除文件。
2. 不联网，不安装/升级/降级依赖，不调用飞书、DeepSeek、Claude 或 Codex backend，不运行 scripts/run_mvp.py。
3. 先 cd /home/fgua/projects/walkie-dokie。若需要 Python，使用 PYTHONDONTWRITEBYTECODE=1；不要做没有 timeout 的实验。
4. 每章末尾分别列“源码确认 / 项目约定 / 尚未验证”，不要在每句话后重复标签。

先读：
- README.md
- TECHNICAL.md
- docs/architecture-review-2026-08-12.md
- src/walkie_dokie/main_agent/base.py
- src/walkie_dokie/orchestrator/state.py
- src/walkie_dokie/orchestrator/graph.py
- src/walkie_dokie/agents/base.py
- src/walkie_dokie/artifacts.py

请面向熟悉 Python、但不了解 LangGraph 的开发者回答：
1. MainAgent、LangGraph、ExecutionAgent 分别是什么、不是什么；为什么 StateGraph 或 CompiledStateGraph 不是“主 Agent”。
2. PlatformAdapter、SessionCoordinator（当前部分仍在 run_mvp.py）、MemoryRepository、ArtifactStore 各自拥有什么职责。
3. SessionState 中 durable workflow state、单次输入、用户输出、artifact reference 分别有哪些字段；哪些数据明确不该进入 checkpoint。
4. 从飞书消息开始，逐步讲一遍普通对话路径，以及文档任务“理解→确认→执行→finalize→投递”路径。
5. 解释为什么 Claude Code/Codex 适合做 execution agent，却不适合拥有长期用户记忆和终端话术。

画一张角色架构 ASCII 图和一张当前节点状态图。注意当前还有独立的 `ask_memory` 用户确认：普通对话中的记忆候选只有回复“记住”才保存；文档任务回复“是”只执行，回复“是并记住”才先保存再执行。确认通过后的执行路径是 ask_confirm→prepare_execution→execute，不要沿用旧 draft.py 架构。

最后只问我一道题：“StateGraph、CompiledStateGraph、MainAgent、ExecutionAgent 四者有什么区别？”等我回答后再纠正。
```

## 2. 从 StateGraph.compile 追进 Pregel runtime

```text
请作为源码导读老师，基于 /home/fgua/projects/walkie-dokie 当前虚拟环境中的 LangGraph 1.2.11，解释 StateGraph 编译后究竟如何运行。只读、不联网、不改项目或依赖、不运行真实服务。

先 cd /home/fgua/projects/walkie-dokie，并只用 rg/sed/inspect 定位：
- StateGraph.compile、CompiledStateGraph
- Pregel.ainvoke、Pregel.astream
- AsyncPregelLoop、PregelRunner
- graph.py 当前的 build_graph 和所有 node/conditional route

如用 inspect，命令必须带 PYTHONDONTWRITEBYTECODE=1。引用本地源码时给出文件和行号，但不要大段复制源文件。

请按调用链讲清：
1. StateGraph builder 如何收集 state schema、nodes、edges、branches；compile 做哪些验证，如何生成 CompiledStateGraph(Pregel)。
2. TypedDict 字段如何成为 channel；START、节点订阅的 trigger、节点局部 dict write、目标节点 trigger 之间如何联系。
3. 当前 SessionState 没有 Annotated reducer，因此为什么默认是覆盖语义；未来两个并行节点同时写同一字段可能发生什么。
4. ainvoke 为什么本质上消费 astream；AsyncPregelLoop 如何加载输入/checkpoint、恢复 channel/version/pending writes 并规划 task。
5. 一个 Pregel/BSP superstep 的 plan→execute→buffer writes→apply writes→next trigger/checkpoint 顺序。
6. “同一步并行 actors 看不到彼此 writes”是什么意思。
7. 一个容易讲错的细节：StateGraph conditional branch 为什么能通过 fresh state 看见它所附着的源节点刚产生的局部更新；这与第 6 点为什么不矛盾。请结合 attach_branch/writer pipeline 的本地实现解释。
8. checkpoint durability 的 sync/async/exit（以本地 1.2.11 实现为准）分别意味着什么；不要把 after_tick 说成每次都已同步事务提交。
9. 当前项目为什么把所有注册 node 和 conditional route 都写成 async def。

给出“编译期”和“运行期”两张简洁调用图。每章末尾列“源码确认 / 项目约定 / 尚未验证”。最后让我复述第一次 ainvoke 从 input 到 collect 再到 main_agent 经过了哪些层。
```

## 3. checkpoint、thread_id 与 interrupt/resume

```text
请深入讲解 /home/fgua/projects/walkie-dokie 当前版本的 checkpoint、thread 与 human-in-the-loop 恢复。只读，不联网，不展示任何真实 checkpoint 或用户正文。

先 cd /home/fgua/projects/walkie-dokie，阅读：
- scripts/run_mvp.py
- src/walkie_dokie/orchestrator/state.py
- src/walkie_dokie/orchestrator/graph.py
- src/walkie_dokie/orchestrator/locks.py
- src/walkie_dokie/artifacts.py
- TECHNICAL.md

再用 rg/inspect 定位本地 InMemorySaver、AsyncSqliteSaver、Checkpoint、CheckpointTuple、StateSnapshot、Command、interrupt 的定义。

请分成 A、B 两部分。

A. 状态和存储
1. 严格区分：Checkpoint 字典（channel values/versions/versions_seen 等）、checkpoint metadata、CheckpointTuple（含 pending_writes/parent config）、面向调用者的 StateSnapshot（values/next/tasks/interrupts 等）。不要把四者混称一个 checkpoint dict。
2. thread_id 为什么是 configurable namespace，不等于 SessionState.user_id；本项目为什么使用 platform:user_id。
3. 同/不同 thread_id 的调用如何隔离和恢复；checkpointer 为什么不会自动串行同一 thread 的并发 invoke。
4. StateSnapshot.next 的准确含义。为什么 next 非空可能是待执行或失败节点，不能证明正在等人工 interrupt；当前 runner 为什么检查 snapshot.interrupts，并只接受 next 为 ('ask_confirm',) 或 ('ask_memory',)。
5. UserLocks 能保证什么；为什么只限单进程，多进程/多实例需要什么。
6. InMemorySaver 与 AsyncSqliteSaver 的生命周期和持久性。
7. checkpoint、JSON 长期档案、debounce buffer、input/output artifact、execution metadata、turn log、平台 outbox 分别由谁保存；哪些不在同一事务。
8. 区分项目文件名 checkpoints-v2.db 的“架构/state schema v2”和 LangGraph ainvoke(version='v2') 的返回协议。当前项目仍用默认 v1，所以为何读取 state['__interrupt__']；切换 LangGraph v2 output 要改什么。

B. interrupt 与恢复
1. interrupt(value) 为什么不是普通 return；第一次调用如何抛内部 GraphInterrupt、保存暂停状态并成为 __interrupt__。
2. Command(resume=value) 为什么必须使用同 thread_id；恢复为何从被中断节点开头重跑，而不是恢复 Python 栈帧。
3. payload/resume 可序列化、不要捕获 interrupt 内部异常、多个 interrupt 顺序/ID 稳定这三条限制。
4. 当前 ask_confirm/ask_memory 的 resume value 为什么是 {text, file_reference}；附件如何在一次 Command(resume=...) 中回到节点。明确说明当前实现不再使用旧 aupdate_state→resume 两调用。
5. 为什么 interrupt 前不能做不可幂等副作用。
6. 结合 prepare_execution、started marker、report marker，逐个说明它们覆盖哪些 crash window；若只有 started 没有可信 report，为什么拒绝自动重跑。不要把 marker 说成通用 exactly-once。
7. memory operation 为什么先成为候选并经过 ask_memory/组合确认，而不是模型一输出就落盘；这如何降低语义误写风险，又为什么仍需要 ledger。
8. memory JSON、coding-agent API/文件写入、LangGraph checkpoint、平台投递为什么无法组成一个原子事务；持久 inbox/outbox 和 backend idempotency 还缺什么。

输出一张存储职责表、一张首次暂停/恢复时序图、一张执行 crash-window 时序图。每部分末尾列“源码确认 / 项目约定 / 尚未验证”。最后问我：“为什么 SQLite checkpointer 不能替代同 session 的锁，也不能自动让外部执行 exactly-once？”
```

## 4. 专门解释这次“LangGraph 卡死”

```text
请解释 /home/fgua/projects/walkie-dokie 在本次 Codex managed sandbox 中曾出现的 graph.ainvoke 卡死。只读、不联网、不改文件、不重复运行可能挂住的实验；以 TECHNICAL.md、PITFALLS.md 中已记录的实验和本地安装源码为证据。

先 cd /home/fgua/projects/walkie-dokie，阅读：
- TECHNICAL.md 中“为什么当前 sandbox 曾让 ainvoke 看似卡死”
- PITFALLS.md 对应条目
- 当前 graph.py 的 async nodes/routes

然后用 rg/sed/inspect 只读核对这条源码链：
LangGraph 对同步 graph callable 的 Runnable 包装
→ langchain_core 的 run_in_executor
→ ThreadPoolExecutor worker
→ asyncio Future 完成通知
→ call_soon_threadsafe
→ selector self-pipe/socketpair wakeup。

请回答：
1. 做一张证据表，区分：历史同步项目图（会卡）、当前全异步项目图（InMemory 流程正常）、同步最小图（会卡）、异步最小图（正常）、纯 asyncio 跨线程唤醒（会卡）。当前图已经改完，不要声称它仍能复现历史现象。
2. 为什么“同步函数已经完成，但 await 不返回”把直接调查点指向 Future 完成通知/event-loop 唤醒链，而不是节点业务循环。
3. 本次环境实测 socketpair.send 返回 EPERM，asyncio self-pipe 写失败又会吞 OSError；这如何造成 selector 一直睡眠。
4. LangGraph 为什么暴露该环境缺陷：它用 executor 适配同步 node/route。全改 async def 是项目层兼容措施，不是 LangGraph 的一般性要求。
5. 纯 asyncio 也能复现，只能说明 LangGraph、interrupt、InMemorySaver 不是本次现象的必要条件、环境唤醒链是首要解释；它不能证明这些库绝不存在任何独立 bug。请避免“完全排除”的过强措辞。
6. 依赖版本为何元数据兼容，为什么不应把盲目降级当修复；没有完整 lockfile仍是什么风险。
7. 为什么当前 sandbox 即使 graph callable 全异步，AsyncSqliteSaver 仍可能卡：aiosqlite 自己有 worker thread。为什么正常部署 OS 还必须单独冒烟，而不能把 sandbox 现象外推到生产机。

画一张“worker 已完成但 event loop 没被唤醒”的时序图。章节末尾列“源码确认 / 本次 sandbox 实测 / 尚未验证”。最后让我回答：“为什么同步节点已经执行完成，ainvoke 仍可能一直 pending？”
```

## 5. 用苏格拉底式问答检验理解

```text
请作为严格但耐心的老师，检验我是否真正理解 LangGraph 和 /home/fgua/projects/walkie-dokie 当前 v2。只读，不修改项目。

一次只问一道题；等我回答后判断“正确 / 部分正确 / 不正确”，指出一个准确点和一个遗漏。第一次答错只给提示让我重答，第二次仍错再给标准解释。

依次覆盖：
1. StateGraph builder、CompiledStateGraph 与 Pregel runtime。
2. 默认 channel、局部 write、reducer 与 superstep 可见性。
3. conditional branch 如何看到源节点 fresh update。
4. MainAgent、LangGraph、ExecutionAgent 的边界。
5. Checkpoint、CheckpointTuple、StateSnapshot 的区别。
6. thread_id 与 SessionState.user_id 的区别。
7. next 与 interrupts 的区别；为什么不能 if snapshot.next 就 resume。
8. interrupt/Command(resume) 的节点重跑和副作用风险。
9. prepare/started/report marker 能保证什么、不能保证什么。
10. checkpoint、长期 memory、artifact、debounce、outbox 分别由谁保存。
11. UserLocks 为何不能覆盖多进程。
12. 这次 sandbox 卡死证据共同说明什么，又不能说明什么。

十二题后按 100 分评分，再让我做 teach-back：“不用术语堆砌，向 Python 开发者解释本项目为什么同时需要 MainAgent、LangGraph 和 ExecutionAgent，以及 checkpoint 为什么不是数据库事务魔法。”

现在从第 1 题开始。
```
