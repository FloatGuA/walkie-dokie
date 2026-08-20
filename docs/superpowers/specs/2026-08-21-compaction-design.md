# 短期历史压缩（Compaction）设计

日期：2026-08-21
状态：已与用户逐项对齐定稿（6 个决策点：4 个用户拍板 + 2 个技术细节由设计给定并经确认）
关联：DECISION.md「短期历史压缩（compaction）设计稿」（2026-08-17，架构方向与被否方案）与本次定稿条目；`docs/agent-system-self-check.md` 一表「记忆压缩」行。

## 目的

`recent_messages` 现状是硬截断（12 条/单条 2,000 字/总计 12,000 字符），超出部分在 `_completed_turn_history` 里静默丢弃。compaction 在丢弃前把被挤出的批次压成带逐字 evidence 的摘要条目，随 checkpoint 持久（同一用户永远同一 thread，天然跨天/跨重启），持续喂给 MainAgent——个人助手的"记住之前聊过什么"由此闭环。

## 已拍板决策

| # | 决策点 | 拍板 | 被否方案及原因 |
|---|--------|------|----------------|
| 1 | 模型 client | **用户拍板**：抽象 `Summarizer` 接口；v1 用 Claude CLI（Agent SDK，model=haiku，订阅额度零现金），后续换 Claude/DeepSeek API 时只加实现类不动接口 | 直接 deepseek-chat（用户选择先走订阅省现金）；其他供应商小模型（新依赖新 key，无收益） |
| 2 | 一级触发 | **用户拍板**：被挤出消息进 `pending_compaction` 缓冲，攒满 6 条（≈3 轮）压一次 | 每有挤出就压（调用多 3 倍、单条语境窄）；字符阈值（不如条数直观） |
| 3 | 二级触发 | **用户拍板**：条目 >20 触发 merge，目标 ≤10 | 字符阈值（同上，可后续换） |
| 4 | 执行位置 | **用户拍板**：回合内同步、投递之后、同一把 session 锁内——用户零感知延迟（回复已发出），状态机保持串行 | main_agent 前同步压（秒级延迟直接加在用户等待路径上）；完全异步后台（与下一轮 ainvoke 并发写同 thread checkpoint，正是 PITFALLS 已知坑，需额外锁/队列不成比例） |
| 5 | 数据结构/注入（技术细节，经确认） | 条目 `{fact: str, evidence: [str, ...]}`；`DialogueContext.conversation_summary: tuple[str, ...]` 只注入 facts，evidence 留 state 审计不占 prompt | 注入完整 evidence（prompt 膨胀，审计价值在日志/state 已覆盖） |
| 6 | 校验规则（技术细节，经确认） | `SummaryValidator` 纯代码机械校验（见下）；**二级"只合并不新增"从模型指令升级为机械校验**——新条目每条 evidence 必须逐字来自被合并条目 evidence 并集（把 2026-08-17 决策预告的补强直接做进 v1） | 只靠 prompt 约束二级不新增（2026-08-17 已标为已知局限，机械化成本低） |

**澄清（用户问询后确认）**："跨会话记住"由 checkpoint 持久机制天然满足（无"新会话"概念）；本设计排除的是把摘要条目自动晋升进 4 字段长期档案（`known_facts`）——那道白名单+当前消息逐字 evidence 的门是身份混淆 bug 专门收紧的，两者生命周期分开（2026-08-17 决策原文），如需晋升另行立项。

## Summarizer 接口（`main_agent/summarizer.py` 新文件）

```python
class Summarizer(ABC):
    @abstractmethod
    async def summarize(self, messages: tuple[dict, ...]) -> tuple[dict, ...]: ...
    # 一级：pending 批次原始消息（{role, content}）→ 候选条目（未验证）

    @abstractmethod
    async def merge(self, entries: tuple[dict, ...]) -> tuple[dict, ...]: ...
    # 二级：现有已验证条目 → 合并后的候选条目（未验证）
```

`ClaudeAgentSummarizer`：Agent SDK `query()`，`model="haiku"`、`allowed_tools=[]`、`max_turns=6`（PITFALLS output_format 轮数坑）、`output_format` JSON schema、隔离参数照 judge/_execution_options 的 fail-closed 约定（`setting_sources=[]`/`mcp_servers={}`/`strict_mcp_config`/`skills=[]`/`env` 清洗）、lazy import。prompt 核心指令：抽取对后续对话有用的事实，每条带被压缩消息中的逐字片段作 evidence，拿不准就少抽不硬编；merge 模式额外声明只许合并精简、不许新增事实。消息内容按不可信数据声明（用户原文可能含注入）。

## 数据结构与校验

```text
SessionState 新增（均 plain dict/list，checkpoint 安全）:
  pending_compaction: list[dict]        # 被挤出的原始消息 {role, content}
  compaction_failures: int              # 当前批次连续失败计数
  conversation_summary: list[dict]      # 已验证条目 {fact, evidence}
```

`SummaryValidator`（纯代码）：
- `fact` 非空 str 且 ≤200 字符；`evidence` 非空 list[str]，每条非空。
- 一级：每条 evidence 必须是本批 pending 消息某条 `content` 的逐字子串。
- 二级：每条 evidence 必须是被合并条目 evidence 并集中某条的逐字子串（含相等）。
- 单次压缩产出 ≤6 条（防膨胀）。
- 不合格条目丢弃 + WARNING（保守拒绝，照 memory repository 哲学）；**整批全拒 = 本次压缩失败**（见失败语义）。

## graph 集成与触发链路

1. `_completed_turn_history` 改造：被 12 条窗口挤出的消息追加进 `pending_compaction`（不再静默丢）。
2. 新节点 `compact`：取 pending 批 → `summarizer.summarize` → validator → 追加 `conversation_summary`、清 pending 与失败计数；若条目 >20 → 同节点内 `summarizer.merge` → validator（二级规则）→ 替换为合并结果（目标 ≤10；merge 后仍 >20 不递归，留待下次触发）。
3. 触发：`run_mvp` 在 `deliver_graph_output` 完成后、仍持 session 锁时，读 snapshot；`len(pending_compaction) >= 6` 则 `graph.ainvoke({"new_compaction_request": True}, config, durability="sync")`。`_has_instruction` 路由见该 flag 直接进 `compact` → END，无用户输出（deliver 对 result=None 静默，复用既有分支）。不用 `aupdate_state`（隐式 as_node 坑已在项目中弃用）。
4. `DialogueContext` 加 `conversation_summary: tuple[str, ...]`（facts only），`_main_agent` 节点组装时从 state 读取。

## 失败语义

Summarizer 异常/超时/非法输出/整批被拒 → pending 批**保留**、`compaction_failures += 1`、WARNING（含 trace_id 与原因）；下次达到触发条件时重试。`compaction_failures >= 3` → 丢弃该批、清计数、WARNING 记录丢弃。方向安全：丢弃的内容在旧逻辑下本来就被静默丢掉，行为不劣于现状。压缩失败绝不影响已投递的回复（触发在投递之后）。外部 API 调用是真正的系统边界，此处重试不违反 fail-fast 铁律。

## 可观测性

`compact` 节点 INFO 记：trace_id（沿用触发回合的）、批大小、产出/拒绝条目数、是否触发二级、耗时 ms、token 用量（Summarizer 返回或日志内记）——满足 2026-08-17 决策"成本独立核算"目标的最小实现（日志级核算，不建仪表盘）。

## 测试

- `SummaryValidator` 全部机械规则离线单测（含二级 evidence ⊆ 并集）。
- graph 级（fake Summarizer）：挤出→pending 累积；满 6 触发 compact；条目追加；>20 触发 merge；失败保留重试；3 次丢弃；`new_compaction_request` invoke 无用户输出。
- `ClaudeAgentSummarizer` 注入 query_fn 测解析与 options 字段（照 judge 的测法）。
- `run_mvp` 集成：投递后锁内触发、阈值不满不触发。
- **不加 golden 长会话样本**：现有样本 ≤4 轮触发不了窗口，长会话样本成本高断言弱；摘要质量靠首次真实运行人工看日志标定 + badcase 驱动回填。

## 明确不做（YAGNI）

- 不把摘要自动晋升长期档案（见澄清；如需另行立项）。
- 不做摘要用户可见回显 / `/long-term-memory` 展示（用户确认跨会话持久即可）。
- 不做压缩结果缓存、不做二级递归合并、不做仪表盘级成本核算。

## 已知代价

- Claude CLI 实现绑定本机登录态；登录失效时压缩持续失败→按失败语义丢批，行为退化为现状（硬截断），不影响主流程。
- 二级合并即使有机械 evidence 校验，"合并后 fact 措辞是否忠实"仍靠模型；evidence 可追溯性保住了审计底线。
- 摘要质量未经真实验证（首跑标定）。
