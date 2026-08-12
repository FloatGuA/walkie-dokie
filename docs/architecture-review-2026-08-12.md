# Architecture Review — 2026-08-12

## 结论

原判断正确：问题不只是一个 memory extraction prompt 写错，而是旧架构没有显式、唯一的主 Agent owner。对话理解、机器人身份、任务规划、记忆、确认话术和最终回复分散在多个模块，执行型 coding agent 又直接参与这些判断。长期记忆把“小帮”当成用户名，是职责泄漏的一个可见症状。

旧实现更准确的描述不是“完全没有主 Agent 逻辑”，而是“隐形主 Agent 被拆散了”：

```text
draft.py       ─┐ 意图、闲聊、任务、确认话术
graph.py       ─┼ memory 读取/写入、路由、回复拼装
memory.py      ─┼ 执行后另一次提取
ExecutionAgent ─┼ 文档执行 + 用户最终话术
run_mvp.py     ─┘ memory 回显 + 会话/投递策略
```

没有模块对“用户是谁、机器人是谁、什么值得跨会话记住、最终应向用户说什么”负全责，也没有稳定协议阻止 coding-agent harness 的环境身份和偏好进入用户层。

## 决策演化为何走到这里

`DECISION.md` 早期选择把“理解和执行都交给 coding agent 的 agentic loop”，随后把 LangGraph 称为会话层。意图质量出现问题后，项目增加一次轻量 draft 调用；draft 又逐步承担闲聊、身份和用户话术，最终变成一个没有被命名和约束的 MainAgent。memory 再作为执行后的额外模型调用接入 graph，形成三个相互独立的语义调用。

LangGraph 能持久化和路由状态，但它不是语义主体。把“有会话状态机”误当成“已有主 Agent 层”，是旧架构的核心概念混淆。

## 已实施的目标边界

```text
┌──────────────────┐
│ PlatformAdapter  │  协议转换与收发
└────────┬─────────┘
         ▼
┌──────────────────┐
│ Session coord.   │  防抖、session key、单会话串行、投递
└────────┬─────────┘
         ▼
┌──────────────────┐      profile/history metadata
│ LangGraph        │◄────────────────────────────┐
│ control plane    │                             │
└───┬──────────┬───┘                             │
    │          │ confirmed TaskContract          │
    ▼          ▼                                 │
┌──────────┐  ┌────────────────┐                 │
│MainAgent │  │ExecutionAgent  │                 │
│semantics │  │files/code only │                 │
└────┬─────┘  └───────┬────────┘                 │
     │                │ ExecutionReport          │
     └────────────────┘                          │
             │                                   │
             ▼                                   │
   user-facing response          MemoryRepository/ArtifactStore
```

### MainAgent

- 唯一拥有“小帮”身份、用户对话与最终话术；
- 普通 chat API，无 shell、CLI、文件系统工具；
- `decide()` 输出 reply 或自包含 `TaskContract`；
- 只提出带当前用户原文 evidence 的 memory operations；
- `finalize()` 把内部执行报告变成用户回复。

### LangGraph

- 只负责 collect、路由、confirm interrupt/resume、prepare/execute 生命周期和 checkpoint；
- 不发明缺失信息策略，不做长期记忆语义，不把 `next` 当成 interrupt；
- state 中只保存短期状态与 artifact reference，不保存文档 bytes。

### ExecutionAgent

- 只收到已确认的 instruction、可选 `input_path`、隔离 workdir；
- 只返回 `ExecutionReport`，没有 `reply_text`；
- 不读取 profile/history，不判断用户身份，不决定 memory；
- Claude Code/Codex 的 coding loop 被当成黑盒执行器，而不是终端助手。

### MemoryRepository / ArtifactStore

- repository 执行白名单、evidence、第一人称、长度校验；通过后仍只是候选，必须由用户明确确认才原子落盘；
- artifact store 在入图前持久化附件，graph/checkpoint 只保存 JSON reference；
- 上一轮 artifact 只有在 MainAgent 明确设置 `use_previous_artifact` 时才交给执行器。

## 本次审阅发现并已修复

- 旧 `result/new_facts` 没有跨轮清理，可能重复发送上回合结果；
- 确认前缀会把“好像不对”“可以先别做”当肯定；
- 确认时附件被 runner 丢弃；
- `snapshot.next` 被错误当成 pending interrupt，失败节点会吞下一条消息并重跑旧 execute；
- 输入附件 bytes 与自定义 dataclass 被重复写入 SQLite checkpoint；
- 输出路径只检查存在，不检查普通文件/metadata 一致；
- Codex 内部 `_output_schema.json` 可覆盖同名上传文件；
- memory 文件名清洗会让不同用户 ID 碰撞；
- graph 自行拼“缺失信息用默认值”的业务 prompt；
- turn log 失败会让已成功的 execute 保持 pending；
- 标准 `pytest` 会导入 smoke script 并启动真实 backend；
- 只保存文字历史导致“继续修改刚才文件”拿不到 artifact。
- 默认 async durability 不能保证 prepare checkpoint 先于外部副作用落盘；生产入口已显式改为 sync。
- 插件返回的 artifact 可能来自另一个 sibling workdir；graph 已在写 marker/发布前按本轮 workdir 重建可信路径。

相应回归测试已纳入当前 84 项离线套件。

## 仍然存在的架构债

### 对外使用前必须解决

1. **无持久 inbox/outbox**：平台重投会重复处理；graph 成功后发送失败会丢结果；文件和文字可能半投递。
2. **外部副作用非 exactly-once**：prepare + started/report marker 会阻止未知结果被自动重跑，但 coding agent 是否已经完成仍无法自动判定，需要人工恢复或 backend 幂等键。
3. **安全隔离不足**：Claude backend 的 `bypassPermissions` 与订阅登录不适合对外产品。
4. **真实链路未验证**：当前 sandbox 的 asyncio self-pipe 限制使 `aiosqlite` 不能代表正常部署；必须在真实 OS 冒烟。

### 扩展到多实例前必须解决

- `UserLocks` 只在当前进程内有效；
- 本地 artifact path 不是跨机器标识；
- `checkpoints-v2.db` 没有应用 schema version/migration metadata；
- debounce 与在途 delivery 仍是内存状态；
- runner 还兼任 SessionCoordinator；虽已追踪/取消在途 task，并把单 session 投递纳入顺序域，但持久重试、outbox 和完整生命周期仍没有正式 application-service owner。

### 记忆进一步治理

当前 evidence policy 会偏保守，且任何候选都要再经用户明确确认，所以模型误判不会静默落盘。它仍不是完整语义证明；下一版应把已确认变更记录为带 `turn_id/source/evidence` 的 ledger，支持幂等 apply、纠正、删除、撤销和通知重放，避免 JSON memory、graph checkpoint 与 outbound 三份状态不一致。

## 对原 PROGRESS.md 的审阅

旧文档混淆了 checkpoint 与长期 memory，曾把三个独立模型调用描述成优点，并声称“checkpoint 不存文件 bytes”“记错可改”等超过实现能力的结论。它还保留旧 draft/memory/ExecutionResult、历史测试数量和 `aupdate_state` 附件流程。

当前 `PROGRESS.md` 已改为：

- 分开列出“已验证”与“尚未验证”；
- 记录实际 84 项离线测试，不把 fake 测试冒充真实模型验证；
- 明确输入和输出都只以 artifact reference 进入 checkpoint；
- 明确项目 state v2 与 LangGraph invoke `version="v2"` 无关；
- 把 inbox/outbox、exactly-once、多实例锁、schema migration 和真实冒烟保留为待办。
