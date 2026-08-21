# 持久 Inbox/Outbox（异步投递）设计

日期：2026-08-21
状态：已与用户逐项对齐定稿（4 个决策点用户拍板 + 技术细节经确认）
关联：PROGRESS P0.2；DECISION.md 2026-08-21 定稿条目；`scripts/run_mvp.py` 中"正式版应改为 durable outbox"的预告注释即本设计还的债。

## 目的

消灭三类真实风险：图跑成功但平台发送失败→结果永久丢；文件成功文字失败→半投递无恢复；进程在 checkpoint/投递边界崩溃→无迹可寻。同时以 outbox 模式把"生成结果"与"送达用户"解耦为两个独立环节（锁持有不再等待平台网络），并给平台事件加持久去重。

## 已拍板决策

| # | 决策点 | 拍板 | 被否方案及原因 |
|---|--------|------|----------------|
| 1 | 总体形态 | **用户拍板**：方案 B 完全异步 outbox——所有出站消息一律先持久化入队，独立投递 worker 消费；理由：宁可设计之初多付出，换系统稳固性（含面试叙事价值）。用户在听完通俗解释（发件箱/邮递员比方）后确认理解并选择 | 方案 A（同步先试失败才落 outbox）——改动面小但锁内仍等网络，且异步化的存量投资要二次支付 |
| 2 | 存储 | 独立 SQLite `var/outbox.db`（stdlib sqlite3，WAL；inbox 去重表同库） | 复用 checkpoints-v2.db（生命周期互相牵扯、langgraph 升级风险）；jsonl（状态更新不适合追加文件） |
| 3 | 重试语义 | 退避 30s/2m/10m 共 3 次；败进死信（dead）不自动打扰用户（"发送失败"通知本身也可能发不出去；死信进控制台数据源人工处理） | 无限重试（永久故障的死信堵队列头） |
| 4 | 投递保证 | at-least-once：发送前置 `sending` 预章，重启把 `sending` 复位 `pending` 重寄——崩溃窗口内宁可用户偶收重复也不丢结果（窗口毫秒级，概率极低） | at-most-once（sending 直接判死——退回接近现状，只是丢得有记录） |

技术细节（设计给定，经用户确认的整体设计一并批准）：保序=每 session 只发最早未终态消息，**终态（delivered/dead）放行后续**（死信不堵队列，代价是死信造成的半投递进死信区人工补）；inbox 去重**先记 seen 再处理**（防重复处理优先于防极小概率丢弃——飞书重投时首次处理往往正在进行）；turn log `success` 语义收窄为"图产出成功且已入队"。

## 存储 schema（`var/outbox.db`）

```sql
CREATE TABLE outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL,        -- "platform:user_id"
  trace_id TEXT,
  seq INTEGER NOT NULL,             -- 同回合内顺序：文件在前、文字最后
  kind TEXT NOT NULL,               -- 'file' | 'text'
  payload TEXT NOT NULL,            -- JSON：text={"text":...}；file=ArtifactReference dict（路径引用，发送时读 workspace）
  status TEXT NOT NULL,             -- 'pending' | 'sending' | 'delivered' | 'dead'
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,    -- ISO8601；pending 且 <= now 才 due
  created_at TEXT NOT NULL,
  delivered_at TEXT,
  last_error TEXT
);
CREATE TABLE inbox_seen (
  event_id TEXT PRIMARY KEY,
  seen_at TEXT NOT NULL             -- 7 天 TTL，worker 空闲时顺带清理
);
```

workspace 文件"用完不自动删"的既有策略保证 file payload 的路径引用在重试期内有效。

## 组件

### 1. `orchestrator/outbox.py`（新）：存储层 + 语义

- `Outbox(db_path)`：`enqueue(session_key, trace_id, messages)`（messages=有序 (kind, payload) 列表，事务内批量插入，seq 递增）；`due_batch(now)`（每 session 取最早未终态且 due 的一条——保序 SQL）；`mark_sending(id)` / `mark_delivered(id)` / `mark_failed(id, error, now)`（退避表 [30s, 120s, 600s]，第 3 次失败置 dead + WARNING）；`reset_sending()`（启动恢复：sending→pending，attempts 不变）；`seen_event(event_id)` / `record_event(event_id)`；`purge_expired_seen(now)`（7 天）。
- 全部同步 sqlite3 调用（本地毫秒级，无需 aiosqlite；调用点都在 async 函数中但阻塞时长可忽略——与 turn_log 同一取舍）。

### 2. `deliver_graph_output` 拆分（`scripts/run_mvp.py`）

- `build_outbound_messages(state) -> list[tuple[kind, payload]]`：纯函数，迁移现有全部组装逻辑（interrupt 确认话术、"收到文件请说明"、文件在前文字在后、result 空态等），只组装不发送。
- 回合终点改为：`outbox.enqueue(...)` → turn log（success=图成功且入队成功；error 不再承载投递失败）→ compaction 检查 → 放锁 → `worker_wakeup.set()`。用户等待时长 = 图 + 本地入队。
- 两个调用点（dispatch_fresh / handle_event resume 分支）同改；异常路径的 fallback 话术同样走 enqueue（不再直接 send）。

### 3. 投递 worker（`scripts/run_mvp.py` 内常驻 asyncio task）

循环：`due_batch` → 逐条：`mark_sending` → `platform.send`（file kind 先 resolve artifact reference 读文件）→ 成功 `mark_delivered` / 异常 `mark_failed`（含退避或死信 WARNING）。空批时 `await asyncio.wait([wakeup.wait()], timeout=1s)` 后清 wakeup；顺带按小时级频率 `purge_expired_seen`。启动时先 `reset_sending()`。worker 异常不退出：外层 `except Exception: logger.exception` 后继续循环（投递系统的存活优先，单条错误已被 mark_failed 语义覆盖）。

### 4. Inbox 去重（`platforms/feishu.py` + `handle_event`）

- `InboundEvent` 加 `event_id: str | None = None`；feishu adapter 从事件 `header.event_id` 提取。
- `handle_event` 入口（任何处理之前）：`event_id` 非 None 且 `seen_event()` → DEBUG 日志丢弃返回；否则 `record_event()` 后继续。eval/test 等无 event_id 平台行为不变。

## 语义要点（全部要有测试）

1. 保序：同 session 前一条 pending/sending 时后一条绝不发；delivered/dead 放行。
2. 退避与死信：30s/2m/10m；第 3 次失败置 dead + WARNING；dead 不再被取件。
3. at-least-once：sending 崩溃 → 启动 reset → 重寄；重寄成功仅一次 delivered 记录。
4. 半投递：文件 delivered、文字 dead（或反之）各自独立终态，死信区可查（admin 数据源后续接入，本轮不做 UI）。
5. 入队失败（磁盘错）：fail fast 冒泡——比照 turn_log 无兜底哲学；这是比平台网络更内环的故障。
6. 去重：同 event_id 第二次到达被丢弃；先记后处理。
7. worker 单条异常不杀死循环；`platform.send` 超时受 worker 侧 `asyncio.timeout(30)` 约束（防单条挂死队列）。

## 可观测性

入队/送达/失败/死信各一行结构化日志（trace_id、session、kind、attempts）；outbox 表本身即投递账本（admin 控制台后续可直读，本轮不做 UI）。

## 明确不做（YAGNI）

死信自动用户通知；admin 投递状态 UI（挂下一轮）；多进程/分布式 worker；飞书之外平台的 event_id 适配；投递优先级；消息合并。

## 测试与验收

存储层单测（schema/enqueue 事务/due 保序 SQL/退避/死信/reset/去重/TTL）；worker 语义离线测（fake platform + 可控时钟：保序、终态放行、at-least-once 复位重寄、单条异常存活、file payload 解析）；`build_outbound_messages` 纯函数测（迁移现有 deliver 组装断言）；run_mvp 集成测（回合终点=入队+turn log 语义、fallback 话术入队、compaction 触发点不变）；inbox 去重端到端测。**既有 test_run_mvp 投递断言大面积迁移，plan 逐条对照，不许静默删除断言**。验收：离线全绿 + 手动起 run_mvp 观察 outbox 表流转；真实飞书发送冒烟留待用户配合场次。

## 已知代价

- 用户看到回复的时刻由 worker 决定（正常几百 ms 内，感知无差）；极端情况下重复消息可能出现（at-least-once 拍板接受）。
- turn log 的 success 不再等于"已送达"——历史数据的语义在此日期前后不同，统计跨界时需注意（PROGRESS 记录）。
- 死信造成的半投递需要人工处理（控制台数据源可见，UI 后续）。
