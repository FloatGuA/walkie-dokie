# walkie-dokie — Progress

更新时间：2026-08-12（Asia/Shanghai）

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

- `pytest` 与 `pytest tests/`：**84 passed**。套件不联网、不调用真实飞书/DeepSeek/Claude/Codex。
- MainAgent/ExecutionAgent 接口隔离；执行层只收 `TaskContract + input_path`，返回 `ExecutionReport`。
- “你是小帮”即使被模型错误输出成合法 `name=小帮`，也会因缺少第一人称当前原文证据被 repository 拒绝。
- memory 只接受四个白名单字段、set/delete、逐字 evidence；旧消息不能冒充当前证据；清洗后相同的两个用户 ID 不会再碰撞。通过校验的操作也只是一份候选，普通对话需用户回复“记住”，文档任务需回复“是并记住”才会落盘；单独回复“是”只执行任务。
- 输入附件先写 `var/inputs/`，checkpoint 只保存 plain JSON reference；输出同样只保存 `var/workspaces/` 引用，不保存文件 bytes。
- 确认阶段附件通过单个可序列化 `Command(resume={text,file})` 恢复，不再使用 `aupdate_state` 的隐式 `as_node` 推断。
- runner 只把真实 `snapshot.interrupts` 且节点为 `ask_confirm/ask_memory` 当成可恢复确认；失败节点的 `next` 不会吞掉下一条用户消息或自动重跑 execute。
- 生产入口所有 fresh/resume invoke 都显式使用 `durability="sync"`；`prepare_execution` 的 execution ID/workdir checkpoint 完成后才进入执行。独立元数据区的 started/report marker 会在未知结果时拒绝自动重跑，并覆盖“报告已返回但后置 checkpoint 失败”的常见窗口。
- 主 Agent/执行 Agent 的业务异常都结束为明确失败回合；turn log 失败不再把成功执行变成 pending execute。
- 上一轮输出 artifact 可由 MainAgent 通过 `use_previous_artifact` 显式选入下一任务，“继续修改刚才文件”不再只有文字上下文却拿不到文件。
- 确认词使用完整匹配；“好像不对”“可以先别做”“是，不过先改”不会误执行。
- 标准 `pytest` 不再收集时启动真实 backend smoke script；两个脚本已有 `__main__` guard，pytest 配置限定 `tests/`。

## 尚未验证

- v2 尚未在正常部署 OS 上跑完真实 `DeepSeek → AsyncSqliteSaver → Claude → 飞书` 全链路。
- 当前 Codex 受管 sandbox 禁止 asyncio socketpair/self-pipe 唤醒；历史同步图因此看似卡死，`aiosqlite` 的 worker thread 在这里也受影响。离线 `InMemorySaver` 通过不能替代生产 SQLite 冒烟。
- memory 的 fake-client contract 与确定性 policy 已测，但真实 DeepSeek 的多轮 adversarial/golden eval 尚未完成。
- Claude/Codex 对真实 docx/xlsx 的质量、超时、取消后的子进程/远端状态需要重新冒烟。
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
      repository 保守校验 → ask_memory/任务确认展示候选
      用户明确同意后才落盘，并透明回显实际变更

  reply → END
  propose_task → ask_confirm interrupt
      补充/否定 → collect → main_agent
      “是” → prepare_execution → execute（不保存 memory 候选）
      “是并记住” → 保存候选 → prepare_execution → execute

  reply + memory candidate → ask_memory interrupt
      “记住” → 保存并 END；“不用记” → 丢弃并 END

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
4. 公开给开发者本人以外的人之前，移除 `bypassPermissions`/订阅登录风险，采用合规 API key 与真正 OS/container 沙箱。

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
- 收窄 `agents/`：只执行任务并返回内部报告；输入由 bytes 改为 path；graph 会重新验证产物确属本轮 workdir；Codex schema 保留名冲突已消除。
- 更新 `run_mvp.py`：复合 session key 贯穿 debounce/lock/thread，附件入图前落盘，只按真实 interrupt resume。
- 更新 README、TECHNICAL、DECISION、PITFALLS 与 LangGraph 教学提示词。

早期 v1 的真实飞书闭环、Ollama/DeepSeek 选型、Windows Codex 限制等历史仍保留在 [DECISION.md](DECISION.md)、[PITFALLS.md](PITFALLS.md) 与 [progress archive](docs/progress-archive.md)；它们不应被误读为 v2 当前实现。
