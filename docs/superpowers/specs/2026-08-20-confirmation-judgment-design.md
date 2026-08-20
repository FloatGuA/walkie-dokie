# 确认判定重设计：三层结构 + MainAgent 模型判灰区

日期：2026-08-20
状态：已与用户逐项对齐定稿（4 个决策点用户拍板 + 1 条补充约束）
关联：DECISION.md「确认判定将从正则匹配重设计为 MainAgent 模型判断」（2026-08-20 立项条目）及本次定稿条目；eval 样本 confirm-004 的暂翻转注记；`docs/superpowers/specs/2026-08-20-eval-harness-design.md`（验收仪器）。

## 目的

现状里"这句话是不是无条件放行执行"由 `_CONFIRM_RE` 正则独占判定，语义边界词（"嗯""好""行"）永远覆盖不全，且每补一个词都是在做没有依据的产品猜测。把灰区判定交给模型（真正的判断类任务），同时用确定性规则守住误执行的安全边界。

## 已拍板决策

| # | 决策点 | 拍板 | 被否方案及原因 |
|---|--------|------|----------------|
| 1 | 判定者形态 | `MainAgent` 新增独立方法 `judge_confirmation`（专用小 prompt，同一 DeepSeek client） | 复用 `decide`（大 prompt 塞确认判定互相干扰，且确认路径多带无关上下文）；graph 内直接起判定函数（破坏"MainAgent 是唯一面向用户语义的 Agent"这条 v2 架构约束） |
| 2 | 误判兜底 | 确定性否定词硬否决：回复命中否定信号时无论模型判什么都不得进 execute，模型只能在未被否决的空间里判 confirm | 置信度双际检（模型自报 confidence 不可靠、阈值难定、多一轮交互成本高）；不设兜底（误执行有真实副作用，与"宁可多澄清一轮"的既定哲学相悖） |
| 3 | 快路径 | 保留收紧版白名单：仅无歧义词（是/是的/确认/没错）完整匹配直接执行；"嗯/好/行/可以"等语气歧义词移出白名单进灰区 | 一律过模型（对零歧义回复纯浪费 1-3s 延迟和一次调用，中老年用户对延迟敏感） |
| 4 | cancel 出口 | 新增真正的丢弃任务出口：cancel → 清 pending 状态 → 确定性话术回复 → END | cancel 归入 revise 回 main_agent 重新理解（"算了"会被当新输入反问"那您想做什么"，对老人不友好；pending 上下文残留） |
| 5 | 用户感知（补充约束） | **灰区判定对用户零感知**：无任何中间话术/提示，判定过程只体现为该轮回复稍慢；判定细节（verdict/reason/耗时）记 trace 日志 | — |

## 新接口（`main_agent/base.py`）

```python
@dataclass(frozen=True)
class ConfirmationContext:
    task_instruction: str      # 待确认 TaskContract.instruction
    proposal_message: str      # 提案时给用户看的话术（decision.user_message）
    user_reply: str            # 用户本轮原文

ConfirmationDecision = Literal["confirm", "revise", "cancel"]

@dataclass(frozen=True)
class ConfirmationVerdict:
    decision: ConfirmationDecision
    reason: str                # 只进 trace 日志，不给用户

class MainAgent(ABC):
    @abstractmethod
    async def judge_confirmation(self, context: ConfirmationContext) -> ConfirmationVerdict: ...
```

DeepSeek 实现：专用小 system prompt（角色：判断中老年用户对一个已提案任务的回复是确认执行、还是补充/质疑、还是放弃；强调"拿不准判 revise，绝不猜 confirm"），JSON 输出 `{decision, reason}`，`temperature=0`，非法 decision 值抛 RuntimeError（fail fast）。

## graph 流转

模型调用不放路由函数（路由保持纯函数），放新节点 `judge_confirm`；verdict 写入 state 后由第二段路由读取。

```text
ask_confirm resume
  → _route_confirm（纯确定性，顺序即优先级）:
      1. 有新文件 → collect                     （现状保留）
      2. "是并记住" 正则 → save_memory_task      （现状保留）
      3. 收紧白名单完整匹配 → execute            （零延迟快路径）
      4. 否定词硬命中 → collect                  （硬否决；宽匹配可接受——
         误把"不错"否决只是多澄清一轮，安全方向）
      5. 其余（灰区）→ judge_confirm 节点
  → judge_confirm: verdict = main_agent.judge_confirmation(...)
      ├─ confirm → execute
      ├─ revise  → collect（main_agent 重新理解，现状路径）
      └─ cancel  → cancel_task 节点
  → cancel_task（确定性，不走模型）:
      清 pending_instruction / pending_files / decision / current_user_text，
      active_artifacts 保留（"继续修改刚才文件"的引用不因放弃一次任务而失效），
      result = 固定话术（如"好的，这个任务不做了。有需要随时再发我。"）→ END
```

白名单与否定词表的具体词项在 plan 中定稿并逐词测试；原则：白名单只收零歧义词，否定词表宁宽勿漏（误否决的代价只是多一轮澄清）。`ask_memory` 遗留路径（`_MEMORY_CONFIRM_RE` 等）不动——只服务旧 checkpoint 恢复。

## 错误语义（方向不对称）

`judge_confirmation` 抛异常（超时/API 故障/非法输出）→ graph 的 `judge_confirm` 节点内**降级为 revise**（回 main_agent 再澄清），绝不降级为 confirm；降级记 warning 日志（含 trace_id 与异常）。宁可多问一轮，不带着故障执行。

eval 侧配套：`RecordingMainAgent` 扩展覆盖 `judge_confirmation`（记录异常并 re-raise），保证 eval 运行中判定层的基础设施故障仍触发 FAILED_INFRA，不被 revise 降级掩盖成"多了一轮澄清"的假绿。

## 可观测性

`judge_confirm` 节点以 INFO 记：trace_id、verdict、reason、耗时 ms。快路径与硬否决层也各记一行命中原因（DEBUG 或 INFO），使"这轮为什么执行了/没执行"全程可从日志归因。

## 测试与 eval 迁移

- 既有确认测试（"好像不对""可以先别做""是，不过先改"）迁移到否定词硬否决层断言，保持确定性、不需要 fake 判定。
- 灰区/异常降级/cancel 清状态：fake `judge_confirmation` 离线测（graph 级 + 接口 contract 级）。
- DeepSeek 实现的 prompt/解析：fake client 测（沿用 `FakeCompletions` 风格）。
- eval golden set：confirm-004（"嗯"）翻回 `executed: false`（还 DECISION.md 暂翻转注记的欠账）；新增 cancel 样本（"算了，不做了" → 断言不执行且后续轮任务上下文已被丢弃）；实现完成后跑一次全量 golden 回归 + judge 报告作为验收。

## 明确不做（YAGNI）

- 不做置信度阈值/双际检；不做判定结果缓存。
- 不改 `decide`/`finalize` 的既有契约；不动 `ask_memory` 遗留路径。
- 不给灰区判定加用户可见的等待提示（决策 5：零感知）。
- 不在本次引入"部分确认"（"先做第一页"）语义——revise 回 main_agent 重新提案已覆盖。

## 已知代价

- 灰区回复多一次 DeepSeek 调用（几厘、1-3s），用户仅感知为该轮稍慢。
- 否定词硬否决层保留了一小块正则——这是刻意的安全网（code decides 的否决权），不是对模型判断的不信任回退；词表仍会有漏网，但漏网方向是"进模型判"而非"直接执行"，安全。
