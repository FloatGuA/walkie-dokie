# Agent 系统自查清单

创建：2026-08-20（Asia/Shanghai）。用于定期自查 walkie-dokie 这类 agent 系统是否"代码级别合格"，覆盖状态调度层与工程能力层两部分。状态标注只反映创建时的代码事实（测试数量来自 `pytest --collect-only`），复查时应重新核实，不要直接信任本文件的数字。

## 怎么用

对每一项确认三件事：**有没有实现 / 有没有测试 / 测的是正常路径还是边界+并发场景**。debounce 模块的教训是：有测试不等于测对了地方——上次静默丢文件的 race bug 就是从"有 7 个测试"的模块里漏出来的。

---

## 一、状态与调度层面

| 模块 | 代码位置 | 测试数 | 状态 | 风险/备注 |
|------|---------|--------|------|-----------|
| 长期记忆 | `main_agent/memory.py` | 20 | 已实现，白名单字段+evidence 校验+确认落盘 | 相对成熟；真实 DeepSeek 对抗性 eval 仍未跑（见下方 Evaluation harness） |
| 短期历史 | `orchestrator/state.py` / `graph.py` | 含在 graph 41 个测试里 | 已实现（原文/历史直接带入 prompt） | 未压缩，长对话会持续膨胀 token |
| 记忆压缩（compaction） | 无 | 0 | **未实现**，仅 DECISION.md 有设计稿（2026-08-18 那条） | 不是"代码合格与否"的问题——先决定做不做，再谈质量 |
| 任务分配/控制平面 | `orchestrator/graph.py` | 41 | 已实现，测得最重的模块 | 全项目相对最扎实的部分 |
| 时间窗口/debounce | `orchestrator/debounce.py` | 9 | 已实现 | 2026-08-20 补了两个用 `asyncio.gather` 真并发验证的回归测试（`handle_event` 双发、`dispatch_fresh` vs `handle_event` 竞态），确认现有 `UserLocks` 确实序列化了这两个场景，无需生产代码改动 |

## 二、工程能力层面

| 能力 | 现状 | 优先级 | 备注 |
|------|------|--------|------|
| 可观测性 / Trace | ✅ 已实现（2026-08-20）：`Debouncer` 窗口触发时生成 trace_id，随 `SessionState` checkpoint 落盘并贯穿 debounce→main_agent→execute→投递；confirm-race resume 沿用原 id 不重开。与 `execution_id`（幂等身份）并存不合并 | 已闭环 | debounce+graph 并发场景测试已于 2026-08-20 补齐（见第一张表 debounce 行） |
| 幂等与失败语义 | started marker 已挡住部分场景，未区分可重试 vs 永久失败，无 backoff/熔断 | 高（已在 PROGRESS.md P0） | 外部调用（DeepSeek/Claude/Codex/飞书）都需要 |
| Evaluation harness（golden set 回归） | DECISION.md 已拍板"fail-fast"策略，但真正的 golden set 尚未建立 | **高** | 最容易被拖欠——prompt 改坏了不会报错，只会悄悄变差 |
| Guardrails（输入侧） | 输出侧已有 Office 主动内容校验 + 执行 agent 最小权限沙箱；输入侧（用户原文喂进 main_agent 决策前）无注入检测 | 中 | 输出侧方向是对的，输入侧是缺口 |
| 成本与延迟预算 | 无 token 用量/单用户成本埋点 | 中（开放前必须） | 按量计费 API，异常重试或超长文件可能打飞成本 |
| 人工兜底路径 | 有 ask_confirm 交互，但无"多次失败后转人工/明确说不会"的出口 | 中 | PROGRESS.md P2 已提到中老年用户体验，这是其中一角 |

---

## 复查记录

- 2026-08-20：首次建立，随本次进度汇报会话产出。
- 2026-08-20：可观测性/Trace 项已实现（`orchestrator/debounce.py` + `SessionState.trace_id`），TDD 全程覆盖，`pytest` 140 passed。
- 2026-08-20：debounce+graph 并发场景补了两个真并发回归测试（Task 1/2），确认现有锁机制已正确工作，无需修复。
