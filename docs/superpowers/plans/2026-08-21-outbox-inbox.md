# 持久 Inbox/Outbox（异步投递）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 所有出站消息先持久化进 `var/outbox.db` 再由独立投递 worker 消费（保序、退避 3 次进死信、at-least-once），平台事件按 event_id 持久去重——图成功后结果永不丢失。

**Architecture:** `orchestrator/outbox.py` 承载存储层（sqlite3 stdlib、WAL、保序取件 SQL）；`run_mvp` 的回合终点从"直接发送"改为"组装清单（`build_outbound_messages` 纯函数）→ 入队 → turn log（success=图成功且入队成功）"；常驻 `delivery_worker` asyncio task 消费（sending 预章 → 发送 → delivered/退避/死信；启动时 sending 复位实现 at-least-once）；`InboundEvent` 加 `event_id`，`handle_event` 入口先记 seen 再处理。eval driver 从 `deliver_graph_output` 迁移到 `build_outbound_messages` 纯函数（不再需要 fake platform 捕获）。

**Tech Stack:** 既有栈 + stdlib sqlite3（零新依赖）。

**Spec:** `docs/superpowers/specs/2026-08-21-outbox-inbox-design.md`（4 个用户拍板决策与被否方案见 DECISION.md 2026-08-21 投递可靠性条目）。

## Global Constraints

- 保序：同 session 前一条 pending/sending 时后一条绝不发；**终态（delivered/dead）放行后续**（死信不堵队列）。
- 退避表 `(30, 120, 600)` 秒共 3 次；第 3 次失败置 `dead` + WARNING；dead 不再被取件。
- at-least-once：发送前置 `sending`；启动 `reset_sending()` 把 sending→pending（attempts 不变）。
- inbox 去重**先记 seen 再处理**；无 event_id 的平台（eval/test）行为不变。
- 入队失败（磁盘错）fail fast 冒泡，不兜底（比照 turn_log 哲学）。
- worker 单条异常不杀死循环；`platform.send` 受 `asyncio.timeout(30)` 约束。
- turn log `success` 语义收窄为"图产出成功且已入队"；error 字段不再承载投递失败。
- 时间全部经注入的 `now`/clock 参数（测试可控时钟，不 monkeypatch time 模块）。
- **既有 test_run_mvp / test_eval_driver 的投递断言逐条迁移，不许静默删除断言**（迁移对照表进报告）。
- 标准 pytest 绝不联网；不引入 mock 库。TDD。当前全量基线 **414 passed**。
- commit trailer 按执行时 harness 规则。

---

### Task 1: `orchestrator/outbox.py` 存储层

**Files:**
- Create: `src/walkie_dokie/orchestrator/outbox.py`
- Test: `tests/test_outbox.py`（新）

**Interfaces:**
- Produces（Task 3/4/5 依赖的精确签名）:

```python
OUTBOX_DB_PATH = _VAR_ROOT / "outbox.db"      # _VAR_ROOT 推导照 turn_log.py 的写法
_BACKOFF_SECONDS = (30, 120, 600)
_MAX_ATTEMPTS = 3
_SEEN_TTL_DAYS = 7

class Outbox:
    def __init__(self, db_path: Path = OUTBOX_DB_PATH): ...
        # 幂等建表（spec 的两张表 schema 逐字）+ PRAGMA journal_mode=WAL
    def enqueue(self, session_key: str, trace_id: str | None,
                messages: list[tuple[str, dict]], *, now: datetime) -> None
        # 单事务批量插入；seq 按列表序递增；status='pending'、next_attempt_at=now
    def due_batch(self, now: datetime) -> list[dict]
        # 每 session 至多一条：该 session 最早（MIN(id)）的未终态行，且它是 pending 且 due
        # SQL 形状（保序核心，照写）：
        #   SELECT o.* FROM outbox o JOIN (
        #     SELECT session_key, MIN(id) AS head_id FROM outbox
        #     WHERE status IN ('pending','sending') GROUP BY session_key
        #   ) h ON o.id = h.head_id
        #   WHERE o.status='pending' AND o.next_attempt_at <= ?
    def mark_sending(self, message_id: int) -> None
    def mark_delivered(self, message_id: int, *, now: datetime) -> None
    def mark_failed(self, message_id: int, error: str, *, now: datetime) -> None
        # attempts+1；attempts >= _MAX_ATTEMPTS → status='dead' + logger.warning（session/trace/error）
        # 否则 status='pending'、next_attempt_at = now + _BACKOFF_SECONDS[attempts-1]
    def reset_sending(self) -> int          # sending→pending，attempts 不变；返回复位条数
    def seen_event(self, event_id: str) -> bool
    def record_event(self, event_id: str, *, now: datetime) -> None
    def purge_expired_seen(self, *, now: datetime) -> int   # 删除 seen_at 早于 now-7天 的行
    def dead_letters(self, session_key: str | None = None) -> list[dict]   # 死信查询（控制台数据源）
```

行以 plain dict 返回（键=列名）。全同步 sqlite3（本地毫秒级，与 turn_log 同取舍）；连接每方法短开短关或实例持有单连接（实现者选，注释理由；注意 worker 与入队方是同进程不同 task——单连接需 `check_same_thread=False` 且本项目全在一个事件循环线程，无跨线程）。

- [ ] **Step 1: 写失败测试**（`tests/test_outbox.py`，全部 tmp_path 建库、`datetime` 固定时钟变量推进；断言写全）：

```python
测试清单（每条断言期望值与实际值均具体）：
1. test_enqueue_then_due_batch_returns_head_message
   入队 session A 三条（file,file,text）→ due_batch(now) 只返回 seq=0 那条；字段齐全。
2. test_ordering_head_blocks_tail
   A 的头条 mark_sending 后 due_batch 为空（sending 挡后续）；mark_delivered 后 due_batch 返回第二条。
3. test_dead_head_releases_tail
   头条连败 3 次成 dead → due_batch 返回第二条（终态放行）；dead_letters() 含头条。
4. test_backoff_schedule
   mark_failed 一次后 due_batch(now) 空、due_batch(now+31s) 有；再败后 now+121s；三败成 dead + caplog WARNING。
5. test_multi_session_isolation
   A/B 两 session 各有队列，B 不受 A 的 sending 阻挡；due_batch 每 session 至多一条。
6. test_reset_sending_restores_pending_without_touching_attempts
7. test_at_least_once_replay
   sending → reset → due → mark_delivered，delivered_at 只写一次。
8. test_inbox_seen_roundtrip_and_ttl
   record 后 seen True；purge(now+8天) 删除并返回 1；purge 后 seen False。
9. test_enqueue_is_transactional
   messages 含一条非法（如 kind 非 file/text 时 raise？——设计：enqueue 校验 kind ∈ {'file','text'}，非法整批 ValueError 且零插入）。
```

- [ ] **Step 2: RED** → `ModuleNotFoundError`。
- [ ] **Step 3: 实现**（schema 照 spec；WARNING 走 module logger）。
- [ ] **Step 4: `python3 -m pytest tests/test_outbox.py -v && python3 -m pytest -q`** → 全 PASS。
- [ ] **Step 5: Commit** `feat: durable outbox storage with ordered pickup and dead letters`

---

### Task 2: `build_outbound_messages` 纯函数 + eval driver 迁移

**Files:**
- Modify: `scripts/run_mvp.py`（抽纯函数；`deliver_graph_output` 本任务改为内部调用它后发送——**行为不变的中间态**，Task 3 才移除发送）
- Modify: `src/walkie_dokie/evals/driver.py`（迁移到纯函数，删除 `_CapturePlatform` 与 `deliver_graph_output` 依赖）
- Test: `tests/test_run_mvp.py`、`tests/test_eval_driver.py`（迁移+新增）

**Interfaces:**
- Produces（Task 3 依赖）:

```python
def build_outbound_messages(state: dict) -> tuple[list[tuple[str, dict]], dict]:
    # messages: 有序 [('file', ArtifactReference dict), ..., ('text', {'text': str})]
    # summary:  {'output_text': str|None, 'output_filename': str|None, 'success': bool}
    # 迁移现有 deliver_graph_output 的全部组装逻辑：
    #   interrupt → [('text', {'text': payload['user_message']})], success True
    #   result None + pending_files → 「收到文件…」话术一条 text
    #   result None 无文件 → 空清单, output_text None, success True
    #   result → 每个 artifact 一条 ('file', reference dict)（不读 bytes！引用原样入清单），
    #            末尾一条 text=result['reply_text']；summary 沿用现返回三元组语义
```

要点：file 消息只放 reference dict（bytes 由 worker 发送时读），这与旧 deliver 在发送时才 `read_bytes` 的语义一致。eval driver 迁移：`_invoke_from_event` 后改调 `messages, summary = build_outbound_messages(state)`，`obs.replies = tuple(payload['text'] for kind, payload in messages if kind == 'text')`——**观测语义不变**（旧 replies 就是 platform 捕获的 text）；driver docstring 的"与生产同路径"表述更新为"与生产同一组装函数"。

- [ ] **Step 1: 写失败测试**：`tests/test_run_mvp.py` 加纯函数测试（四种 state 形状对应上面四分支，断言 messages 与 summary 精确值——从既有 deliver 测试的 fixture 改造）；`tests/test_eval_driver.py` 既有 4 条测试改走新观测路径后必须保持原断言语义（replies 内容不变）。
- [ ] **Step 2: RED**（ImportError）。
- [ ] **Step 3: 实现**（deliver_graph_output 中间态：`messages, summary = build_outbound_messages(state)` 后循环发送——file kind 在此处 resolve+read_bytes；返回 summary 三元组。既有 deliver 相关测试应全绿不动）。
- [ ] **Step 4: 全量回归** → 全 PASS。
- [ ] **Step 5: Commit** `refactor: extract outbound message assembly as a pure function`

---

### Task 3: 回合终点改造——入队取代直接发送

**Files:**
- Modify: `scripts/run_mvp.py`（两调用点 + fallback + main 装配 Outbox；`deliver_graph_output` 删除）
- Test: `tests/test_run_mvp.py`（大迁移）

**Interfaces:**
- Consumes: Task 1 `Outbox.enqueue`、Task 2 `build_outbound_messages`。
- Produces（Task 4/5 依赖）: `dispatch_fresh(..., outbox, wakeup=None)` 与 `handle_event(..., outbox, wakeup=None)` 新增 **required keyword `outbox: Outbox`** 与可选 `wakeup: asyncio.Event | None`；`main()` 构造 `Outbox()` 与 `asyncio.Event` 并贯穿（含 debouncer on_ready lambda）。

语义要点（全部要有测试）：
1. 正常回合：图 → `build_outbound_messages` → `outbox.enqueue(session_key, trace_id, messages, now=datetime.now())` → turn log（`success = summary['success']`，output_text/filename 来自 summary；**不再有 delivery_error**）→ compaction 检查（位置不变）→ 放锁后 `wakeup.set()`（wakeup 非 None 时）。
2. 异常路径 fallback：原 `platform.send(fallback_text)` 改为 `outbox.enqueue(..., [('text', {'text': fallback_text})], ...)`；turn log success=False 语义不变。
3. 空清单（result None 无文件）不入队（enqueue 空列表直接跳过）。
4. `deliver_graph_output` 删除；`grep -rn deliver_graph_output` 全仓零命中（Task 2 已迁 driver）。
5. 既有 test_run_mvp 投递断言迁移：凡断言 `platform.sent` 的投递内容改为查 outbox 行（tmp db）；并发/trace/confirm-race 等非投递断言不动。**迁移对照表进报告**。

- [ ] **Step 1: 写失败测试**（新增：入队行内容/顺序/trace_id；fallback 入队；wakeup 被 set；空清单不入队。既有迁移先改断言跑 RED）。
- [ ] **Step 2: RED** → **Step 3: 实现** → **Step 4: 全量回归 PASS**（重点确认 test_run_mvp 全部迁移后总数只增不减）。
- [ ] **Step 5: Commit** `feat: turns end at the durable outbox instead of direct platform sends`

---

### Task 4: 投递 worker

**Files:**
- Modify: `scripts/run_mvp.py`（`deliver_due_once` + `delivery_worker` + main 启动 task + 启动 `reset_sending()`）
- Test: `tests/test_run_mvp.py`（追加）

**Interfaces:**
- Produces:

```python
async def deliver_due_once(outbox: Outbox, platform, *, now: datetime) -> int:
    # 取 due_batch → 逐条：mark_sending → asyncio.timeout(30) 内 platform.send
    #   file kind：resolve_artifact_reference(reference) 读 bytes → OutboundMessage(file=IncomingFile(...))
    #   text kind：OutboundMessage(text=payload['text'])
    # → 成功 mark_delivered / 异常 mark_failed（含超时）；返回处理条数
    # 单条异常绝不中断本批其余条目

async def delivery_worker(outbox: Outbox, platform, wakeup: asyncio.Event) -> None:
    # 启动即 reset = outbox.reset_sending()；reset>0 记 INFO（at-least-once 重寄）
    # 永循环：deliver_due_once(now=datetime.now()) → 处理 0 条时
    #   await wait(wakeup, timeout=1s) 后 wakeup.clear()
    # 按小时级节流调 purge_expired_seen；外层 except Exception: logger.exception 后继续
```

`main()`：`asyncio.create_task(delivery_worker(outbox, platform, wakeup))`（生命周期随主循环；不做优雅停机——进程退出即止，未送达消息重启后由 reset+due 继续，正是 at-least-once 语义）。

- [ ] **Step 1: 写失败测试**（fake platform 记录 send/可编程抛错；固定时钟推进）：

```python
1. test_deliver_due_once_sends_in_order_and_marks_delivered（file→text 顺序、bytes 来自 workspace tmp 文件）
2. test_send_failure_backs_off_then_dead（三败进 dead；期间 due 窗口按退避表）
3. test_single_failure_does_not_block_other_sessions
4. test_worker_startup_resets_sending_and_redelivers（预置 sending 行 → deliver 前 reset → 重寄成功仅一次 delivered）
5. test_send_timeout_counts_as_failure（fake send await 长睡 → timeout(30) 用小值可注入？——超时值提为模块常量 _SEND_TIMEOUT_SECONDS 供 monkeypatch）
6. test_file_payload_resolves_reference（reference dict → 实际读到 bytes 传给 platform）
```

- [ ] **Step 2: RED** → **Step 3: 实现** → **Step 4: 全量回归 PASS** → **Step 5: Commit** `feat: delivery worker with backoff, dead letters and at-least-once replay`

---

### Task 5: Inbox 去重

**Files:**
- Modify: `src/walkie_dokie/platforms/base.py`（`InboundEvent` 加 `event_id: str | None = None`）
- Modify: `src/walkie_dokie/platforms/feishu.py`（`_on_message` 提取事件 id——先读 SDK 对象实际字段：`data.header.event_id` 形状以现场为准）
- Modify: `scripts/run_mvp.py`（`handle_event` 入口去重）
- Test: `tests/test_run_mvp.py`（追加）

要点：`handle_event` 最前（连 text/file 判空之前）：`event.event_id` 非 None 且 `outbox.seen_event(...)` → `logger.debug` 丢弃返回；否则 `outbox.record_event(...)` 后继续（**先记后处理**，spec 语义）。测试：同 event_id 二连发只处理一次（graph 只被调一次）；event_id None 的事件不受影响（连发两次照常处理两次）；去重在防抖之前生效。

- [ ] **Step 1: RED 测试** → **Step 2: 实现** → **Step 3: 全量回归 PASS** → **Step 4: Commit** `feat: persistent inbound event dedup by event_id`

---

### Task 6: 端到端演练 + 文档 + push

**Files:**
- Test: `tests/test_run_mvp.py`（一条端到端演练测试）
- Modify: `PROGRESS.md`、`TECHNICAL.md`（数据流投递段 + turn log 语义注记）、`docs/agent-system-self-check.md`（幂等与失败语义行更新 + 复查记录）

- [ ] **Step 1: 端到端演练测试**：真实 Outbox（tmp db）+ fake graph/platform：`dispatch_fresh` 产生 2 文件+1 文字入队 → `deliver_due_once` 三轮全部 delivered 且顺序正确 → 手工把一条改回 sending 模拟崩溃 → `reset_sending` → 再 deliver → 恰好补寄该条。断言 turn log success 与投递状态解耦（send 全挂时 turn log 仍 success=True、outbox 里三条 dead）。
- [ ] **Step 2: 文档**：PROGRESS 已验证条目（含 turn log 语义变更注记）+ 尚未验证（真实飞书投递/重投冒烟待用户配合）；TECHNICAL 数据流"文件、文字投递"段改为 outbox→worker 形态 + 并发边界段的"无持久 outbox"表述删除；自查表"幂等与失败语义"行更新（外部投递侧已闭环，DeepSeek/执行侧重试语义仍待办）+ 复查记录。P0.2 从待办清单划掉。
- [ ] **Step 3: Commit + push** `docs: record durable outbox/inbox completion`

---

## Self-review

- **Spec coverage**：schema/存储语义（T1）、组装纯函数与 driver 迁移（T2）、回合终点/turn log 语义/fallback/wakeup（T3）、worker/退避/死信/at-least-once/超时/单条异常存活/TTL 清理（T4）、event_id 提取与先记后处理（T5）、端到端与文档（T6）。spec"明确不做"未越界。
- **Placeholder scan**：T2/T3/T5 测试为语义清单式（迁移面依赖现场断言，plan 给了对照表要求与断言语义）；无 TBD。
- **Type consistency**：`Outbox` 全方法签名 T1 定义、T3/T4/T5 消费；`build_outbound_messages` 返回 `(messages, summary)` T2 定义、T3 消费；`deliver_due_once/delivery_worker` T4 定义、T6 演练消费；messages 的 `('file'|'text', dict)` 形状全程一致。
- **风险注记**：T3 是最大迁移面（test_run_mvp 投递断言），对照表强制；feishu SDK event_id 字段形状带现场核查指令；`_SEND_TIMEOUT_SECONDS` 提常量供测试注入避免真实 30s 等待。
