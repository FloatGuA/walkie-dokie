# Debounce + Graph Concurrency Regression Tests Implementation Plan

> **状态：✅ 已于 2026-08-20 全部执行完毕**（commits `cf5b981`、`a758e28`、`94378b0`，`pytest` 142 passed）。留档备查，不要重复执行。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove, with real `asyncio.gather`-driven concurrency (not sequential simulation), that `UserLocks` actually serializes graph access across the two call sites that use it (`scripts/run_mvp.py`'s `handle_event` and `dispatch_fresh`) — closing the gap flagged in `docs/agent-system-self-check.md`'s debounce row: "现有 7 个测试没覆盖并发场景."

**Architecture:** No production code is expected to change. This is a characterization/regression-test task: a spike already confirmed (see "Spike findings" below) that the existing lock usage correctly serializes both scenarios under genuine concurrent scheduling. The two new tests make that guarantee permanent and self-verifying, using a "swap the lock instance" trick to prove each test can actually detect interleaving (its RED phase) before confirming the real (shared-lock) production wiring is race-free (its GREEN phase).

**Tech Stack:** pytest, pytest-asyncio (already configured, `asyncio_mode=auto` per `pyproject.toml`), `asyncio.gather`, `unittest`-style `SimpleNamespace` fakes (matching existing `tests/test_run_mvp.py` conventions — no new dependencies).

**Spec:** No separate spec file — this is a Bounded-path task approved inline in chat during a 2026-08-20 session (existing flow, no new subsystem). This plan document doubles as the spec.

## Global Constraints

- Follow the existing fake/test-double style in `tests/test_run_mvp.py` (local `class Graph:` per test, `FakePlatform`, `monkeypatch.setattr("scripts.run_mvp.log_turn", ...)`) — do not introduce mocking libraries.
- `UserLocks()` (from `walkie_dokie.orchestrator.locks`) must be a single **shared** instance passed to both concurrent calls in the GREEN version of each test — that's what production `main()` actually does (one `locks = UserLocks()` shared across all events).
- Every new test must genuinely exercise `asyncio.gather` (real concurrent scheduling), not sequential `await` calls — sequential simulation is exactly what the existing 7 tests already do and is the gap being closed.
- Do not modify `debounce.py`, `graph.py`, or `run_mvp.py` production code as part of this plan. If a task's RED-phase spike reveals an actual interleaving bug (contradicting the spike findings below), STOP and flag it to the user before writing a fix — do not silently patch and continue; the scope of this plan is regression-test coverage, not new bug remediation.

## Spike findings (already verified 2026-08-20, informs Global Constraints above)

A throwaway script (not committed) ran both scenarios below through `asyncio.gather` against the real `dispatch_fresh`/`handle_event`/`UserLocks`:

- **Scenario A** (two concurrent `handle_event` calls, same session, both landing on the `ask_confirm` resume branch): with a **shared** `UserLocks()` instance, `graph.aget_state`/`graph.ainvoke` calls came back strictly serialized (`start, end, start, end, ...`, never interleaved). With two **separate** `UserLocks()` instances (simulating "no shared lock"), the calls interleaved (`start, start, end, start, end, end`).
- **Scenario B** (`dispatch_fresh` racing a concurrent `handle_event` resume call, same session): identical result — shared lock serializes, separate locks interleave.

This confirms the existing code is already correct for both scenarios; the two tasks below exist purely to make that guarantee a permanent, real-concurrency-based regression test instead of tribal knowledge from a one-off spike.

---

### Task 1: Two concurrent `handle_event` calls on the same session never interleave graph access

**Files:**
- Modify: `tests/test_run_mvp.py` (append new test; imports `asyncio` already present at top of file, `SimpleNamespace` already imported, `UserLocks` needs a new import, `InboundEvent` already imported, `handle_event` already imported)

**Interfaces:**
- Consumes: `scripts.run_mvp.handle_event(graph, platform, debouncer, locks, memory_repository, event)` (existing signature — see `scripts/run_mvp.py:298`), `walkie_dokie.orchestrator.locks.UserLocks` (existing — `.get(session_key)` returns an `asyncio.Lock`), `walkie_dokie.platforms.base.InboundEvent(platform, user_id, text, file)` (existing).
- Produces: nothing new — this task only adds a test function, no new production interface.

- [ ] **Step 1: Add the `UserLocks` import**

In `tests/test_run_mvp.py`, the top-of-file imports currently read (around line 13):

```python
from walkie_dokie.orchestrator.locks import UserLocks
```

Check first — `UserLocks` is likely already imported (it's used by `test_fresh_direct_reply_is_written_to_conversation_turn_log` and others). If it's already there, skip this step.

- [ ] **Step 2: Write the failing test — force it to fail via a lock-instance mismatch**

Add this test to `tests/test_run_mvp.py` (anywhere among the other `handle_event`-related tests, e.g. right after `test_handle_event_confirm_resume_reuses_snapshots_trace_id`):

```python
async def test_concurrent_handle_event_calls_do_not_interleave_graph_access(
    monkeypatch,
):
    """两个几乎同时到达的 handle_event 调用（同一 session，都命中确认分支）必须
    被 UserLocks 完全序列化——graph.aget_state/ainvoke 不能交错执行。这是
    handle_event 里"查询状态和 resume 决策必须和 ainvoke 使用同一把锁"那条注释
    背后的真实承诺；此前只有顺序模拟测过结果形状，从没用真并发（asyncio.gather）
    验证过锁真的挡住了交错。"""

    order = []

    class Graph:
        async def aget_state(self, config):
            order.append("aget_state-start")
            await asyncio.sleep(0.02)
            order.append("aget_state-end")
            return SimpleNamespace(
                next=("ask_confirm",), interrupts=(object(),), values={"trace_id": "t0"}
            )

        async def ainvoke(self, value, config, durability=None):
            order.append("ainvoke-start")
            await asyncio.sleep(0.02)
            order.append("ainvoke-end")
            return {
                "result": {"artifacts": [], "reply_text": "ok", "success": True}
            }

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    graph = Graph()
    platform = FakePlatform()
    # RED: two SEPARATE UserLocks() instances simulate "no shared lock between
    # these two calls" — proves this test can actually detect interleaving.
    locks_a = UserLocks()
    locks_b = UserLocks()

    await asyncio.gather(
        handle_event(
            graph,
            platform,
            object(),  # debouncer.add is unreachable once resumed_state is set
            locks_a,
            object(),  # memory_repository is unreachable outside /long-term-memory
            InboundEvent("test", "u1", "是", None),
        ),
        handle_event(
            graph,
            platform,
            object(),
            locks_b,
            object(),
            InboundEvent("test", "u1", "是，确认", None),
        ),
    )

    for i in range(0, len(order), 2):
        assert order[i].endswith("-start")
        assert order[i + 1].endswith("-end")
        assert order[i].split("-")[0] == order[i + 1].split("-")[0], (
            f"interleaved graph access detected: {order}"
        )
```

- [ ] **Step 3: Run it to verify it fails, and fails for the right reason**

Run: `python3 -m pytest tests/test_run_mvp.py::test_concurrent_handle_event_calls_do_not_interleave_graph_access -v`

Expected: **FAIL**, with the assertion message `interleaved graph access detected: [...]` showing an order list where `aget_state-start` appears twice before any `-end` (e.g. `['aget_state-start', 'aget_state-start', 'aget_state-end', ...]`). This proves the test can detect real interleaving — if it passes at this point, something is wrong with the test itself (stop and re-examine before continuing).

- [ ] **Step 4: Fix it to GREEN — use a single shared `UserLocks()` instance**

Change `locks_a = UserLocks()` / `locks_b = UserLocks()` to:

```python
    locks = UserLocks()
```

And update both `handle_event(...)` calls to pass `locks` instead of `locks_a`/`locks_b`. This matches production: `scripts/run_mvp.py`'s `main()` creates exactly one `UserLocks()` instance shared across every event (see `scripts/run_mvp.py` around line 453, `locks = UserLocks()`).

- [ ] **Step 5: Run it to verify it passes**

Run: `python3 -m pytest tests/test_run_mvp.py::test_concurrent_handle_event_calls_do_not_interleave_graph_access -v`

Expected: **PASS**.

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `python3 -m pytest -q`

Expected: all tests pass (140 baseline + 1 new = 141).

- [ ] **Step 7: Commit**

```bash
git add tests/test_run_mvp.py
git commit -m "$(cat <<'EOF'
test: prove concurrent handle_event calls serialize under real asyncio.gather

Existing confirm-race coverage only simulated the aftermath sequentially.
This exercises actual concurrent scheduling and confirms UserLocks holds.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `dispatch_fresh` racing a concurrent `handle_event` resume never interleaves graph access

**Files:**
- Modify: `tests/test_run_mvp.py` (append new test; needs `dispatch_fresh` import, already present at top of file per existing imports)

**Interfaces:**
- Consumes: `scripts.run_mvp.dispatch_fresh(graph, platform, platform_name, user_id, combined_text, files, locks, trace_id)` (existing signature, see `scripts/run_mvp.py:208` — note `trace_id` is a required keyword-or-positional arg added in the 2026-08-20 trace_id work, already merged to `master`), `scripts.run_mvp.handle_event(...)` (same as Task 1).
- Produces: nothing new.

This is the scenario closest to the real critical bug fixed in commit `1201650` ("fix: prevent silent file loss on confirm-race") — but proven here under genuine concurrent scheduling instead of a hand-assembled resume payload.

- [ ] **Step 1: Write the failing test — force it to fail via a lock-instance mismatch**

Add this test to `tests/test_run_mvp.py`:

```python
async def test_concurrent_dispatch_fresh_and_handle_event_resume_do_not_interleave(
    monkeypatch,
):
    """一次由 debounce 触发的 dispatch_fresh，和一条几乎同时到达、直接命中确认
    分支的 handle_event，必须被同一把 UserLocks 完全序列化——这是上次真实
    confirm-race bug（commit 1201650）的场景，但这次用 asyncio.gather 真并发
    压出来，而不是手工摆好 resume payload 形状去验证结果。"""

    order = []

    class Graph:
        async def aget_state(self, config):
            order.append("aget_state-start")
            await asyncio.sleep(0.02)
            order.append("aget_state-end")
            return SimpleNamespace(
                next=("ask_confirm",), interrupts=(object(),), values={"trace_id": "t0"}
            )

        async def ainvoke(self, value, config, durability=None):
            order.append("ainvoke-start")
            await asyncio.sleep(0.02)
            order.append("ainvoke-end")
            return {
                "result": {"artifacts": [], "reply_text": "ok", "success": True}
            }

    async def fake_log_turn(record):
        pass

    monkeypatch.setattr("scripts.run_mvp.log_turn", fake_log_turn)
    graph = Graph()
    platform = FakePlatform()
    # RED: two SEPARATE UserLocks() instances simulate "no shared lock".
    locks_a = UserLocks()
    locks_b = UserLocks()

    await asyncio.gather(
        dispatch_fresh(
            graph,
            platform,
            "test",
            "u1",
            "新一批消息",
            (),
            locks_a,
            trace_id="new-batch",
        ),
        handle_event(
            graph,
            platform,
            object(),
            locks_b,
            object(),
            InboundEvent("test", "u1", "是", None),
        ),
    )

    for i in range(0, len(order), 2):
        assert order[i].endswith("-start")
        assert order[i + 1].endswith("-end")
        assert order[i].split("-")[0] == order[i + 1].split("-")[0], (
            f"interleaved graph access detected: {order}"
        )
```

- [ ] **Step 2: Run it to verify it fails, and fails for the right reason**

Run: `python3 -m pytest tests/test_run_mvp.py::test_concurrent_dispatch_fresh_and_handle_event_resume_do_not_interleave -v`

Expected: **FAIL** with `interleaved graph access detected: [...]`, same shape of failure as Task 1 Step 3.

- [ ] **Step 3: Fix it to GREEN — use a single shared `UserLocks()` instance**

Replace `locks_a`/`locks_b` with one shared `locks = UserLocks()`, passed to both the `dispatch_fresh(...)` call and the `handle_event(...)` call.

- [ ] **Step 4: Run it to verify it passes**

Run: `python3 -m pytest tests/test_run_mvp.py::test_concurrent_dispatch_fresh_and_handle_event_resume_do_not_interleave -v`

Expected: **PASS**.

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python3 -m pytest -q`

Expected: all tests pass (141 baseline from Task 1 + 1 new = 142).

- [ ] **Step 6: Commit**

```bash
git add tests/test_run_mvp.py
git commit -m "$(cat <<'EOF'
test: prove dispatch_fresh vs handle_event confirm-resume serialize under real concurrency

Regresses the class of bug fixed in 1201650, but via asyncio.gather-driven
real concurrent scheduling instead of a hand-assembled resume payload.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Update the self-check checklist and push

**Files:**
- Modify: `docs/agent-system-self-check.md` (the debounce row in "一、状态与调度层面")
- Modify: `PROGRESS.md` (append a verified-item bullet, following the file's existing structure — read the top of the file first to match its established style, do not restructure it)

**Interfaces:** None — documentation only.

- [ ] **Step 1: Update the debounce row in `docs/agent-system-self-check.md`**

Find the row:

```
| 时间窗口/debounce | `orchestrator/debounce.py` | 7 | 已实现 | **重点复查**：上次 critical race bug（confirm 期间丢整批文件）就出在这里，说明现有 7 个测试没覆盖并发场景 |
```

Replace with (test count is now 9 after Tasks 1-2; update the note to reflect the real-concurrency coverage now in place):

```
| 时间窗口/debounce | `orchestrator/debounce.py` | 9 | 已实现 | 2026-08-20 补了两个用 `asyncio.gather` 真并发验证的回归测试（`handle_event` 双发、`dispatch_fresh` vs `handle_event` 竞态），确认现有 `UserLocks` 确实序列化了这两个场景，无需生产代码改动 |
```

- [ ] **Step 2: Append a "复查记录" line**

At the end of the file's "复查记录" section, add:

```
- 2026-08-20：debounce+graph 并发场景补了两个真并发回归测试（Task 1/2），确认现有锁机制已正确工作，无需修复。
```

- [ ] **Step 3: Add a verified bullet to `PROGRESS.md`**

Read `PROGRESS.md`'s "已验证" section first to match its existing bullet style (one bullet per line, technical and specific, no headers). Append a bullet describing: two new real-concurrency regression tests added for `handle_event`/`dispatch_fresh` under `UserLocks`, confirming no interleaving in either scenario, test count now 142.

- [ ] **Step 4: Commit and push**

```bash
git add docs/agent-system-self-check.md PROGRESS.md
git commit -m "$(cat <<'EOF'
docs: close the debounce concurrency gap in the self-check checklist

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
git push origin master
```

(This repo's remote is already configured: `https://github.com/FloatGuA/walkie-dokie.git`, `gh auth` is already set up in this environment as of the 2026-08-20 session. If push fails with a credentials error, run `gh auth login` interactively first.)

---

## Self-review

- **Spec coverage:** Both concurrency scenarios identified in the approved chat design (handle_event×handle_event, dispatch_fresh×handle_event) have a task each. Checklist/PROGRESS bookkeeping has its own task so the plan is fully self-closing.
- **Placeholder scan:** No TBD/TODO; all test code is complete and was spike-verified against the real codebase on 2026-08-20 (see "Spike findings").
- **Type consistency:** `dispatch_fresh`'s `trace_id` keyword and `handle_event`'s 6-positional-arg signature match `scripts/run_mvp.py` as of commit `d82ca93` (the trace_id feature, already on `master`).
- **Scope:** Single subsystem (test coverage for one existing locking mechanism), no decomposition needed.
