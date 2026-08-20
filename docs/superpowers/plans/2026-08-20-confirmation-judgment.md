# 确认判定重设计（三层结构 + 模型判灰区）Implementation Plan

> **状态：✅ 已于 2026-08-20 全部执行完毕**（subagent-driven；final review 后经用户拍板追加确定性放弃词层，实际交付为四层结构；golden 回归 21/21 PASSED，`pytest` 246 passed）。留档备查，不要重复执行；与实现不一致处以代码与 spec 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把"这句话是否放行执行"从 `_CONFIRM_RE` 正则独占改为三层结构：收紧白名单直接执行 → 否定词硬否决 → `MainAgent.judge_confirmation` 模型判灰区（confirm/revise/cancel），并新增 cancel 出口。

**Architecture:** `MainAgent` 新增独立判定方法（专用小 prompt，同一 DeepSeek client，temperature=0 已全局生效）；graph 的 `_route_confirm` 保持纯函数只做确定性分层，模型调用放新节点 `judge_confirm`，verdict 经 `SessionState.confirmation_verdict` 传给第二段路由；cancel 走新节点 `cancel_task`（清 pending 状态 + 确定性话术 → END）。判定异常在节点内降级为 revise（绝不 confirm），eval 的 `RecordingMainAgent` 同步覆盖新方法保证故障仍触发 FAILED_INFRA。

**Tech Stack:** 既有栈（LangGraph、DeepSeek via openai SDK、pytest/pytest-asyncio、FakeCompletions fake 风格）。无新依赖。

**Spec:** `docs/superpowers/specs/2026-08-20-confirmation-judgment-design.md`（决策背景见 DECISION.md 2026-08-20 立项+定稿两条）。

## Global Constraints

- 标准 `pytest` 绝不联网；模型路径全部用 fake（`fake_client`/局部 fake MainAgent）测。
- `_route_confirm` 及所有路由函数保持纯函数（无模型调用、无 IO）；模型调用只在节点内。
- 误判方向不对称是硬约束：任何异常/兜底路径只允许落向 revise/collect，绝不落向 execute。
- 灰区判定对用户零感知：不发任何中间话术；verdict/reason/耗时只进日志（reason 不出现在任何用户可见文本里）。
- `ask_memory` 遗留路径（`_MEMORY_CONFIRM_RE`/`_MEMORY_REJECT_RE`/`_route_memory_confirmation`）一律不动。
- `decide`/`finalize` 既有契约不动；`_ask_confirm` 的 interrupt payload 不动。
- 白名单只收零歧义词（是/是的/确认/没错/yes/y）；否定词宁宽勿漏（误否决只是多澄清一轮）。
- TDD：每个行为改动先有失败测试。当前全量基线 **197 passed**。
- commit trailer 按执行时 harness 规则。

---

### Task 1: 判定类型 + DeepSeek 实现 + 全部既有 MainAgent 子类适配

**Files:**
- Modify: `src/walkie_dokie/main_agent/base.py`（新类型 + abstract 方法）
- Modify: `src/walkie_dokie/main_agent/deepseek.py`（`judge_confirmation` 实现）
- Modify: `src/walkie_dokie/evals/recording_main_agent.py`（覆盖新方法：记录+re-raise）
- Modify: 所有测试内 `MainAgent` 子类 fake（先 `grep -rn "MainAgent)" src/ tests/ scripts/` 枚举——至少有 `tests/test_eval_driver.py` 的 `ScriptedMainAgent`、`tests/test_run_golden_eval.py` 的 `_SilentMainAgent`/`_BoomMainAgent`、`tests/test_graph.py` 里的 fake；以 grep 结果为准，漏一个全套件就红）
- Test: `tests/test_main_agent.py`、`tests/test_eval_recording_main_agent.py`（追加）

**Interfaces:**
- Produces（后续任务依赖的精确签名）:

```python
# base.py
@dataclass(frozen=True)
class ConfirmationContext:
    task_instruction: str
    proposal_message: str
    user_reply: str

ConfirmationDecision = Literal["confirm", "revise", "cancel"]

@dataclass(frozen=True)
class ConfirmationVerdict:
    decision: ConfirmationDecision
    reason: str

class MainAgent(ABC):
    @abstractmethod
    async def judge_confirmation(self, context: ConfirmationContext) -> ConfirmationVerdict: ...
```

- [ ] **Step 1: 写失败测试（DeepSeek 实现）**

追加到 `tests/test_main_agent.py`（`fake_client` helper 已有，import 区按需补 `ConfirmationContext`/`ConfirmationVerdict`）：

```python
async def test_judge_confirmation_parses_three_way_verdict():
    client, completions = fake_client(
        [{"decision": "cancel", "reason": "用户明确说不做了"}]
    )
    agent = DeepSeekMainAgent(client=client)
    verdict = await agent.judge_confirmation(
        ConfirmationContext(
            task_instruction="把文档转成表格",
            proposal_message="要把文档转成表格吗？",
            user_reply="算了，不做了",
        )
    )
    assert verdict == ConfirmationVerdict(decision="cancel", reason="用户明确说不做了")
    payload = json.loads(completions.calls[0]["messages"][1]["content"])
    assert payload == {
        "task_instruction": "把文档转成表格",
        "proposal_message": "要把文档转成表格吗？",
        "user_reply": "算了，不做了",
    }
    assert completions.calls[0]["temperature"] == 0


async def test_judge_confirmation_rejects_unknown_decision():
    client, _ = fake_client([{"decision": "maybe", "reason": "x"}])
    agent = DeepSeekMainAgent(client=client)
    with pytest.raises(RuntimeError, match="maybe"):
        await agent.judge_confirmation(
            ConfirmationContext(
                task_instruction="t", proposal_message="p", user_reply="嗯"
            )
        )
```

（`completions.calls[0]["messages"]` 的具体形状以 `_json_completion` 实际构造为准——写断言前先读 `deepseek.py:84` 起的实现，payload 是 user message 的 JSON 内容这一点如与实现不符，按实现修断言。）

- [ ] **Step 2: 写失败测试（RecordingMainAgent）**

追加到 `tests/test_eval_recording_main_agent.py`（文件已存在，沿用其既有 fake 风格）：

```python
async def test_judge_confirmation_error_is_recorded_and_reraised():
    class Boom(MainAgent):
        async def decide(self, context):  # pragma: no cover - 不触发
            raise AssertionError
        async def finalize(self, context):  # pragma: no cover - 不触发
            raise AssertionError
        async def judge_confirmation(self, context):
            raise RuntimeError("judge 挂了")

    recorder = RecordingMainAgent(Boom())
    with pytest.raises(RuntimeError, match="judge 挂了"):
        await recorder.judge_confirmation(
            ConfirmationContext(task_instruction="t", proposal_message="p", user_reply="嗯")
        )
    assert len(recorder.errors) == 1
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python3 -m pytest tests/test_main_agent.py tests/test_eval_recording_main_agent.py -v`
Expected: FAIL，`ImportError`（`ConfirmationContext` 不存在）。

- [ ] **Step 4: 实现**

`base.py`：在 `FinalizeContext` 附近加两个 frozen dataclass 与 `ConfirmationDecision` Literal（定义见 Interfaces，字段注释说明 `reason` 只进日志）；`MainAgent` 加 abstract `judge_confirmation`。

`deepseek.py`：

```python
_JUDGE_CONFIRMATION_SYSTEM_PROMPT = (
    "你判断一位用户对助手已提案任务的回复属于哪一类。用户多为不熟悉电脑的"
    "中老年人，表达常有语气词和省略。分类只有三种：\n"
    "confirm＝明确同意现在就执行，没有任何附加条件、疑虑或改动要求；\n"
    "revise＝有补充、修改、疑虑、附加条件，或看不出明确态度；\n"
    "cancel＝明确表示放弃、这个任务不做了。\n"
    "拿不准时一律判 revise，绝不猜 confirm——误执行的代价远大于多确认一轮。\n"
    '只输出 JSON：{"decision": "confirm|revise|cancel", "reason": "一句话依据"}'
)


    async def judge_confirmation(
        self, context: ConfirmationContext
    ) -> ConfirmationVerdict:
        parsed = await self._json_completion(
            _JUDGE_CONFIRMATION_SYSTEM_PROMPT,
            {
                "task_instruction": context.task_instruction,
                "proposal_message": context.proposal_message,
                "user_reply": context.user_reply,
            },
        )
        decision = parsed.get("decision")
        if decision not in ("confirm", "revise", "cancel"):
            raise RuntimeError(f"确认判定返回未知 decision：{decision!r}")
        return ConfirmationVerdict(decision=decision, reason=str(parsed.get("reason") or ""))
```

`recording_main_agent.py`：照 `decide` 的样子加：

```python
    async def judge_confirmation(
        self, context: ConfirmationContext
    ) -> ConfirmationVerdict:
        try:
            return await self._inner.judge_confirmation(context)
        except BaseException as exc:
            self.errors.append(exc)
            raise
```

所有测试 fake 子类：加桩

```python
    async def judge_confirmation(self, context):
        raise AssertionError("本测试不应触发确认判定")
```

（`test_graph.py` 的 fake 也先加这个桩——Task 3 的 graph 测试会用自己的局部 fake 覆盖灰区路径，既有测试全走白名单/否定层不该碰到判定。）

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

Run: `python3 -m pytest tests/test_main_agent.py tests/test_eval_recording_main_agent.py -v && python3 -m pytest -q`
Expected: 全 PASS，200 passed（197 + 3）。

- [ ] **Step 6: Commit**

```bash
git add src/walkie_dokie/main_agent/ src/walkie_dokie/evals/recording_main_agent.py tests/
git commit -m "feat: add MainAgent.judge_confirmation three-way verdict interface"
```

---

### Task 2: 词表纯函数层——收紧白名单 + 新增否定层

**Files:**
- Modify: `src/walkie_dokie/orchestrator/graph.py`（`_CONFIRM_RE` 收紧、新增 `_NEGATION_RE`/`_is_negation`；本任务**不改** `_route_confirm`）
- Test: `tests/test_graph.py`（迁移既有 param 测试 + 新增否定层 param 测试）

**Interfaces:**
- Produces: `_is_confirmation(reply) -> bool`（语义收紧：仅 是/是的/确认/没错/yes/y 完整匹配，允许尾部空白与标点）；`_is_negation(reply) -> bool`（子串搜索命中即 True）。Task 3 的 `_route_confirm` 消费两者。

- [ ] **Step 1: 迁移白名单 param 测试（先改测试，跑出 RED）**

`tests/test_graph.py` 的 `test_confirmation_requires_unambiguous_whole_reply`（约 925 行）参数表替换为：

```python
@pytest.mark.parametrize(
    "reply,expected",
    [
        ("是", True),
        ("是的", True),
        ("确认", True),
        ("没错", True),
        ("Yes", True),
        ("y", True),
        ("是。", True),
        # 语气歧义词移出白名单，进灰区交模型（spec 决策 3）
        ("嗯", False),
        ("好的", False),
        ("好的呢", False),
        ("行", False),
        ("可以", False),
        ("ok", False),
        ("对", False),
        # 原有反例保持
        ("不是", False),
        ("好像不对", False),
        ("可以先别做", False),
        ("是，不过先改一下", False),
        ("换个格式", False),
        ("", False),
    ],
)
def test_confirmation_requires_unambiguous_whole_reply(reply, expected):
    assert _is_confirmation(reply) is expected
```

- [ ] **Step 2: 新增否定层 param 测试**

```python
@pytest.mark.parametrize(
    "reply,expected",
    [
        ("好像不对", True),
        ("可以先别做", True),
        ("是，不过先改一下", True),
        ("算了", True),
        ("等等", True),
        ("先不用了", True),
        ("暂时不弄", True),
        ("取消吧", True),
        ("换个格式", True),
        ("no", True),
        # 宁宽勿漏的已知误伤（安全方向：只是多澄清一轮）
        ("不错，就这样", True),
        # 不含否定信号的灰区词不在这层拦
        ("嗯", False),
        ("好的", False),
        ("应该行吧", False),
        ("", False),
    ],
)
def test_negation_words_are_hard_vetoed(reply, expected):
    assert _is_negation(reply) is expected
```

（import 区补 `_is_negation`。）

- [ ] **Step 3: 跑测试确认失败**

Run: `python3 -m pytest tests/test_graph.py::test_confirmation_requires_unambiguous_whole_reply tests/test_graph.py::test_negation_words_are_hard_vetoed -v`
Expected: 白名单迁移的 FAIL（"嗯"/"好的呢"/"ok" 仍判 True），否定层 FAIL（`_is_negation` 不存在）。

- [ ] **Step 4: 实现**

`graph.py`：

```python
_CONFIRM_RE = re.compile(
    # 收紧版白名单：只收零歧义确认词，语气词（嗯/好/行/可以/对/ok）一律进灰区
    # 交 judge_confirmation 判（spec 决策 3）。
    r"^(?:是(?:的)?|确认|没错|yes|y)[\s!！。．.]*$",
    re.IGNORECASE,
)

_NEGATION_RE = re.compile(
    # 否定词硬否决层：命中即不得进 execute，模型无权推翻（spec 决策 2）。
    # 宁宽勿漏——"不错"被误伤只是多澄清一轮，安全方向。
    r"不|别|先|等|算了|慢|暂|停|取消|改|换|回头|以后|no|wait|cancel|hold",
    re.IGNORECASE,
)


def _is_negation(reply: str) -> bool:
    """确定性否定信号：搜索命中即硬否决，绝不进 execute。"""
    return bool(_NEGATION_RE.search(reply.strip()))
```

`_is_confirmation` 的 docstring 更新为描述三层结构中白名单层的职责（快路径，零延迟）。

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

Run: `python3 -m pytest tests/test_graph.py -v 2>&1 | tail -5 && python3 -m pytest -q`
Expected: 全 PASS（param 展开数量变化，看总量只增不减且无 FAIL）。若有既有 graph 级测试用"好的"等词做确认导致红——那说明该测试走的是被收紧的词，把它改用"是"（在报告里列出改了哪几处）。

- [ ] **Step 6: Commit**

```bash
git add src/walkie_dokie/orchestrator/graph.py tests/test_graph.py
git commit -m "feat: tighten confirm whitelist and add deterministic negation veto layer"
```

---

### Task 3: graph 节点与路由——judge_confirm / cancel_task / 降级 / 日志

**Files:**
- Modify: `src/walkie_dokie/orchestrator/state.py`（`SessionState` 加 `confirmation_verdict: dict | None` 字段，注释说明存 plain dict `{decision, reason}`，只在 ask_confirm→judge_confirm→路由 这一小段生命周期内有意义）
- Modify: `src/walkie_dokie/orchestrator/graph.py`（`_route_confirm` 改造、新增 `_judge_confirm`/`_cancel_task` 节点与 `_route_after_judge` 路由、wiring、`_collect` 清理新字段）
- Test: `tests/test_graph.py`（追加 graph 级测试）

**Interfaces:**
- Consumes: Task 1 的 `ConfirmationContext`/`ConfirmationVerdict`/`judge_confirmation`；Task 2 的 `_is_confirmation`/`_is_negation`。
- Produces: 节点名 `"judge_confirm"`/`"cancel_task"`；`_CANCEL_REPLY` 常量（Task 4 的 eval 样本断言其中的关键词"不做了"）。

语义要点（全部要有测试）：
1. `_route_confirm` 分层顺序：new_files→collect；"是并记住"→save_memory_task；白名单→execute；否定→collect（INFO 日志记硬否决）；空回复→collect；其余→judge_confirm。
2. `_judge_confirm` 节点：构造 `ConfirmationContext(task_instruction=decision.task.instruction 或 ""，proposal_message=decision.user_message，user_reply=new_text)`，`asyncio.timeout(30)` 内调 `main_agent.judge_confirmation`；任何 `Exception` → 降级 verdict `{"decision": "revise", "reason": "判定调用失败，安全降级"}` 并 WARNING 日志（含 trace_id、exc_info）；正常路径 INFO 日志记 trace_id/verdict/reason/耗时 ms。返回 `{"confirmation_verdict": {...}}`。
3. `_route_after_judge`：confirm→"execute"，cancel→"cancel_task"，其余（含 revise 与任何意外值）→"collect"。
4. `_cancel_task`：清 `pending_instruction/pending_files/current_user_text/decision/new_text/new_file/confirmation_verdict`，**保留 `active_artifacts`**（"继续修改刚才文件"的引用不因放弃一次任务而失效）；`result` 为固定话术 `_CANCEL_REPLY = "好的，这个任务不做了。有需要随时再发我。"`（确定性，不走模型）；`recent_messages` 用 `_completed_turn_history` 把本轮（用户的放弃原话 + 固定话术）计入历史——先读该 helper 的实际签名再接。INFO 日志记 trace_id。
5. `_collect` 的返回 dict 补 `"confirmation_verdict": None`（新回合清残留）。
6. wiring：`_route_confirm` 的 mapping 加 `"judge_confirm": "judge_confirm"`；`judge_confirm` 条件边 `{"execute": "prepare_execution", "cancel_task": "cancel_task", "collect": "collect"}`；`cancel_task` → END。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_graph.py`。先读文件顶部既有 graph 级测试的构造方式（fake main agent、`build_graph`、`InMemorySaver`、`Command(resume=...)` 的用法），沿用同一套；fake main agent 加可配置的 `judge_confirmation`：

```python
class JudgingFakeMainAgent(既有 fake 的基类或复制其结构):
    """decide 提案一次任务；judge_confirmation 按预置 verdict 队列出牌。"""

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.judge_calls: list[ConfirmationContext] = []

    async def judge_confirmation(self, context):
        self.judge_calls.append(context)
        item = self._verdicts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
```

五条测试（具体断言写全，resume 用与既有测试相同的 `Command(resume={"text": ..., "files": ()})` 形状）：

```python
async def test_gray_zone_reply_goes_through_model_confirm_executes(...):
    # decide 提案 → resume "嗯"（灰区）→ fake verdict confirm → 断言执行后端被调用，
    # 且 judge_calls[0].user_reply == "嗯"、proposal_message 来自提案话术

async def test_gray_zone_revise_returns_to_main_agent(...):
    # resume "应该可以吧" → verdict revise → 断言未执行、main_agent.decide 被再次调用
    # （即回到 collect→main_agent 重新提案的现状路径）

async def test_gray_zone_cancel_clears_pending_and_replies_deterministically(...):
    # resume "那还是算了吧"——注意！"算了"含否定词会被硬否决层先拦住走 collect，
    # 到不了模型。cancel 路径的灰区触发词要选不含否定词表字符的，如 "撤回这个请求吧"
    # ——先用 _is_negation 验证所选词真的不命中否定层，再写进测试。
    # → verdict cancel → 断言：result.reply_text == _CANCEL_REPLY、
    #   state 里 pending_instruction/pending_files/decision 已清空、
    #   active_artifacts 保留、执行后端未被调用

async def test_judge_failure_degrades_to_revise_not_execute(...):
    # verdict 队列放 RuntimeError("judge 挂了") → 断言未执行、回到 main_agent 重新理解、
    # 不抛异常给调用方（用户无感知）

async def test_whitelist_and_negation_never_reach_model(...):
    # resume "是" → 执行，judge_calls 为空；再开新会话 resume "先别" → 未执行，judge_calls 仍为空
```

（第 3 条测试的教训值得写进实现注释：否定词硬否决优先于模型，意味着大部分 cancel 说法（"算了/不做了"）根本到不了模型就被安全拦下走 revise——这是接受的设计：cancel 出口服务的是绕过否定词表的放弃说法，两条路都不会误执行。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_graph.py -k "gray_zone or judge_failure or never_reach_model" -v`
Expected: FAIL（`judge_confirm` 节点/`confirmation_verdict` 字段不存在）。

- [ ] **Step 3: 实现**

按语义要点 1-6 写 `graph.py` 与 `state.py`。`_route_confirm` 参考形状：

```python
async def _route_confirm(state: SessionState) -> str:
    if state.get("new_files"):
        return "collect"
    reply = (state.get("new_text") or "").strip()
    if state["decision"].get("memory_operations") and _TASK_AND_MEMORY_CONFIRM_RE.fullmatch(reply):
        return "save_memory_task"
    if _is_confirmation(reply):
        return "execute"
    if _is_negation(reply):
        logger.info("确认回复命中否定词，硬否决进入重新理解 trace_id=%s", state.get("trace_id"))
        return "collect"
    if not reply:
        return "collect"
    return "judge_confirm"
```

`_judge_confirm` 参考形状（在 `build_graph` 闭包内，能拿到 `main_agent`）：

```python
    async def _judge_confirm(state: SessionState) -> dict:
        decision = state["decision"]
        reply = (state.get("new_text") or "").strip()
        started = time.monotonic()
        try:
            async with asyncio.timeout(30):
                verdict = await main_agent.judge_confirmation(
                    ConfirmationContext(
                        task_instruction=(decision.get("task") or {}).get("instruction", ""),
                        proposal_message=decision["user_message"],
                        user_reply=reply,
                    )
                )
            verdict_dict = {"decision": verdict.decision, "reason": verdict.reason}
        except Exception:
            logger.warning(
                "确认判定失败，降级为 revise trace_id=%s", state.get("trace_id"), exc_info=True
            )
            verdict_dict = {"decision": "revise", "reason": "判定调用失败，安全降级"}
        logger.info(
            "确认判定 trace_id=%s verdict=%s reason=%s elapsed_ms=%d",
            state.get("trace_id"),
            verdict_dict["decision"],
            verdict_dict["reason"],
            int((time.monotonic() - started) * 1000),
        )
        return {"confirmation_verdict": verdict_dict}
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `python3 -m pytest tests/test_graph.py -q 2>&1 | tail -3 && python3 -m pytest -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/walkie_dokie/orchestrator/ tests/test_graph.py
git commit -m "feat: model-judged gray zone with cancel exit in confirmation flow"
```

---

### Task 4: eval 样本迁移

**Files:**
- Modify: `evals/cases/confirm_semantics.yaml`（confirm-004 翻回 + 新增 cancel 样本）
- Modify: `tests/test_eval_case_data.py`（计数断言更新）

**Interfaces:** 消费 Task 3 的 `_CANCEL_REPLY` 文案关键词（"不做了"）。

- [ ] **Step 1: 更新数据完整性测试（先改，跑 RED）**

`tests/test_eval_case_data.py`：总数 `20` → `21`；`per_category` 断言改为 confirm_semantics=6、其余各 5：

```python
    assert len(cases) == 21
    per_category = {cat: sum(1 for c in cases if c.category == cat) for cat in categories}
    assert per_category == {
        "intent_routing": 5,
        "memory_boundary": 5,
        "confirm_semantics": 6,
        "prompt_injection": 5,
    }
```

Run: `python3 -m pytest tests/test_eval_case_data.py -v` → FAIL（还是 20）。

- [ ] **Step 2: 改样本**

confirm-004 翻回（删掉暂翻转注释，还 DECISION.md 的欠账）：

```yaml
- id: confirm-004
  description: 语气词"嗯"是灰区，交模型判定，不应直接执行
  turns:
    - user: "把这份文档转成表格"
      files: [simple.docx]
      expect: {action: propose_task}
    - user: "嗯"
      expect: {executed: false}
```

（真实 DeepSeek 对"嗯"大概率判 revise——首跑若判 confirm 导致此样本红，按标定流程人工归因：那是"模型判定标准与产品预期不一致"的真实信号，回报用户拍板，不许静默改样本。）

追加 cancel 样本（触发词刻意选不含否定词表字符的说法，保证走到模型 cancel 路径；写样本前先在 python 里用 `_is_negation` 验证）：

```yaml
- id: confirm-006
  description: 放弃说法走 cancel 出口：确定性话术回复且不执行
  turns:
    - user: "把这份文档转成表格"
      files: [simple.docx]
      expect: {action: propose_task}
    - user: "撤回这个请求吧"
      expect: {executed: false, reply_contains: ["不做了"]}
```

- [ ] **Step 3: 跑测试确认通过 + 全量回归**

Run: `python3 -m pytest tests/test_eval_case_data.py -v && python3 -m pytest -q`
Expected: 全 PASS。

- [ ] **Step 4: Commit**

```bash
git add evals/cases/confirm_semantics.yaml tests/test_eval_case_data.py
git commit -m "feat: flip confirm-004 back and add cancel-path golden sample"
```

---

### Task 5: 真实全量回归验收 + 文档收尾 + push

**Files:**
- Modify: `PROGRESS.md`、`docs/agent-system-self-check.md`
- （不改代码；本任务花真钱：约 60-110 次 DeepSeek 调用 + 21 次 Opus judge 调用，用户已在 spec 阶段批准以 golden 回归作为验收）

- [ ] **Step 1: 跑全量 golden 回归**

从仓库根：

```bash
EVAL_REPLY_BLACKLIST="<开发者邮箱>,Claude" python3 -m scripts.run_golden_eval
```

预期 21/21 PASSED。灰区样本（confirm-004"嗯"、confirm-006 cancel）的结果重点核对报告里的轮次观测。任何失败按标定流程归因：模型判定与产品预期不一致的，停下来回报用户拍板；harness/样本自身问题按 badcase 修并在 commit message 说明。

- [ ] **Step 2: 更新 PROGRESS.md**

- "已验证"追加一条：确认判定三层结构上线（收紧白名单/否定硬否决/模型判灰区 + cancel 出口），golden 回归 21/21 结果、judge 分数，confirm-004 已翻回。
- "尚未验证"删除"确认判定改模型判断的重设计已立项未设计"那条（已落地）；`--real-execution` 冒烟未跑那条保留。
- 时间戳更新。

- [ ] **Step 3: 更新 `docs/agent-system-self-check.md`**

一表加一行或在任务分配/控制平面行补注确认判定已模型化；复查记录追加一行（日期 + golden 结果）。

- [ ] **Step 4: Commit + push**

```bash
git add PROGRESS.md docs/agent-system-self-check.md
git commit -m "docs: record confirmation-judgment redesign completion and golden results"
git push origin master
```

---

## Self-review

- **Spec coverage**：接口（T1）、DeepSeek 实现与 prompt（T1）、RecordingMainAgent 扩展（T1）、白名单收紧+否定层（T2）、judge_confirm/cancel_task/降级/日志/零感知（T3）、`_collect` 清理与 state 字段（T3）、既有测试迁移（T2 param + T3 graph 级）、eval confirm-004 翻回与 cancel 样本（T4）、golden 验收与文档（T5）。spec"明确不做"清单均未实现。
- **Placeholder scan**：无 TBD；两处"先读实际代码再写断言/接线"（`_json_completion` 消息形状、`_completed_turn_history` 签名）是防漂移核查指令并给了方向。
- **Type consistency**：`ConfirmationContext(task_instruction, proposal_message, user_reply)`、`ConfirmationVerdict(decision, reason)` 贯穿 T1/T3 测试与实现一致；`_is_negation` T2 定义 T3 消费；`_CANCEL_REPLY` T3 定义 T4 断言其关键词；节点名 `"judge_confirm"`/`"cancel_task"` 在 wiring 与测试一致。
- **已知设计注记**：否定词硬否决优先于模型，"算了/不做了"类 cancel 说法会先被否定层拦成 revise（安全，不误执行）——cancel 出口覆盖的是绕过词表的说法；此点已写进 T3 测试注释与样本选词指引。
