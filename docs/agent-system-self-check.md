# Agent 系统自查清单

创建：2026-08-20（Asia/Shanghai）。用于定期自查 walkie-dokie 这类 agent 系统是否"代码级别合格"，覆盖状态调度层与工程能力层两部分。状态标注只反映创建时的代码事实（测试数量来自 `pytest --collect-only`），复查时应重新核实，不要直接信任本文件的数字。

## 怎么用

对每一项确认三件事：**有没有实现 / 有没有测试 / 测的是正常路径还是边界+并发场景**。debounce 模块的教训是：有测试不等于测对了地方——上次静默丢文件的 race bug 就是从"有 7 个测试"的模块里漏出来的。

---

## 一、状态与调度层面

| 模块 | 代码位置 | 测试数 | 状态 | 风险/备注 |
|------|---------|--------|------|-----------|
| 长期记忆 | `main_agent/memory.py` | 20 | 已实现，白名单字段+evidence 校验+确认落盘 | 相对成熟；真实 DeepSeek 对抗性 eval 仍未跑（见下方 Evaluation harness） |
| 短期历史 | `orchestrator/state.py` / `graph.py` | 含在 graph 41 个测试里 | 已实现（原文/历史直接带入 prompt） | 窗口外历史已由下一行的 compaction 压缩承接；仍在的既有行为是单条超长消息尾部被硬截断 |
| 记忆压缩（compaction） | `main_agent/summarizer.py` + `orchestrator/graph.py` compact 节点 | 35+ | ✅ 已实现（2026-08-21）：攒批 6 条→haiku 抽取→机械校验→注入 decide，二级合并 >20→≤10，失败重试 3 次丢弃 | 真实标定一次通过（3 事实零编造）；长会话质量分布待 badcase 观察 |
| 任务分配/控制平面 | `orchestrator/graph.py` | 86 | 已实现，测得最重的模块 | 全项目相对最扎实的部分；2026-08-20 确认判定升级为四层结构（白名单/放弃层/否定否决/模型判灰区+cancel 出口），审计日志全程可归因 |
| 时间窗口/debounce | `orchestrator/debounce.py` | 9 | 已实现 | 2026-08-20 补了两个用 `asyncio.gather` 真并发验证的回归测试（`handle_event` 双发、`dispatch_fresh` vs `handle_event` 竞态），确认现有 `UserLocks` 确实序列化了这两个场景，无需生产代码改动 |

## 二、工程能力层面

| 能力 | 现状 | 优先级 | 备注 |
|------|------|--------|------|
| 可观测性 / Trace | ✅ 已实现（2026-08-20）：`Debouncer` 窗口触发时生成 trace_id，随 `SessionState` checkpoint 落盘并贯穿 debounce→main_agent→execute→投递；confirm-race resume 沿用原 id 不重开。与 `execution_id`（幂等身份）并存不合并 | 已闭环 | debounce+graph 并发场景测试已于 2026-08-20 补齐（见第一张表 debounce 行） |
| 幂等与失败语义 | started marker 已挡住部分场景，未区分可重试 vs 永久失败，无 backoff/熔断 | 高（已在 PROGRESS.md P0） | 外部调用（DeepSeek/Claude/Codex/飞书）都需要 |
| Evaluation harness（golden set 回归） | ✅ 已实现（2026-08-20）：四类 20 样本端到端回归（真实 DeepSeek + fake 执行）+ Opus judge 话术评分（只报告）+ judge 校准集（一致率 100%），报告存 `var/evals/`，手动 `python3 -m scripts.run_golden_eval` | 已闭环（样本随 badcase 增长） | 22 样本；`--real-execution` 冒烟 2026-08-21 完成（真实 Claude 执行 2 样本 PASSED；DeepSeek 瞬时超时触发 FAILED_INFRA 属防线正常工作） |
| Guardrails（输入侧） | 输出侧已有 Office 主动内容校验 + 执行 agent 最小权限沙箱；输入侧（用户原文喂进 main_agent 决策前）无注入检测 | 中 | 输出侧方向是对的，输入侧是缺口。已有第一条基线证据：eval 首跑 inject-002 显示"索要系统提示词"会让模型把内部设定原文抛给用户（确定性黑名单没拦住，judge 抓到的） |
| 成本与延迟预算 | 无 token 用量/单用户成本埋点 | 中（开放前必须） | 按量计费 API，异常重试或超长文件可能打飞成本 |
| 人工兜底路径 | 有 ask_confirm 交互，但无"多次失败后转人工/明确说不会"的出口 | 中 | PROGRESS.md P2 已提到中老年用户体验，这是其中一角 |

---

## 复查记录

- 2026-08-20：首次建立，随本次进度汇报会话产出。
- 2026-08-20：可观测性/Trace 项已实现（`orchestrator/debounce.py` + `SessionState.trace_id`），TDD 全程覆盖，`pytest` 140 passed。
- 2026-08-20：debounce+graph 并发场景补了两个真并发回归测试（Task 1/2），确认现有锁机制已正确工作，无需修复。
- 2026-08-20：eval harness 全量实现并完成首次真实运行（20/20 PASSED，judge 校准 100%），结果见 `var/evals/20260820T111441Z.json`；顺带修复 temperature 未固定与 `_DELETE_TERMS` 缺"删掉"两个生产问题，并立项"确认判定改模型判断"重设计（见 DECISION.md）。
- 2026-08-20：确认判定四层结构落地并验收（golden 21/21，`var/evals/20260820T150924Z.json`）；final review 发现并经用户拍板修复 cancel 出口错位（补确定性放弃词层）。
- 2026-08-21：compaction 全链路上线并完成真实 haiku 标定（3 条事实全忠实零编造），`pytest` 282 passed；final review 前置任务里抓住粘滞旗标劫持用户轮的隐患并已修复。
- 2026-08-21：--real-execution 冒烟达成目标（真实执行 2 样本 PASSED，瞬时超时触发 FAILED_INFRA 属正常）；确认判定 prompt 补不可信数据条款并新增确认轮注入样本 inject-006（22/22 PASSED，判定理由"系统通知非用户本人同意"实证条款生效）。
