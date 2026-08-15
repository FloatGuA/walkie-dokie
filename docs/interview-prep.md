# 模拟面试：从后端/Agent 系统角度讲清楚 walkie-dokie

面向场景：后端/AI infra 岗位面试，面试官对 LangGraph、agent loop、tool calling 有实际经验，会追问设计取舍而不满足于“用了什么”。所有回答都对应仓库里的真实代码路径，方便你答完自己去翻。

---

## 一、30 秒电梯陈述

> "这是一个多平台（飞书为主）的文档办公助手。核心不是接了几个 LLM API，而是把一个容易踩坑的架构问题解决了：**谁是唯一对‘用户是谁、该记住什么、该对用户说什么’负责的模块**。我把系统拆成三层——LangGraph 只做可恢复的状态机（防抖、确认中断、checkpoint），MainAgent 是唯一的语义层（理解意图、维护长期记忆、组织话术），ExecutionAgent 是纯粹的黑盒执行单元（跑 Claude Agent SDK 的 agentic loop，只认文件、返回内部报告）。这个拆分是我从一次真实事故里反推出来的：旧版本里‘小帮’这个机器人身份被误写进了用户的长期记忆，根因就是没有唯一 owner。"

---

## 二、整体架构 & 设计取舍

**Q: 画一下你这个系统的数据流。**

```
飞书消息 → FeishuAdapter(WebSocket 长连接)
  → Debouncer(platform,user_id) 10 秒聚合多条消息
  → 附件先落盘到 var/inputs/，图里只传 JSON reference
  → UserLocks[platform:user_id] 保证同会话串行
  → graph.ainvoke(thread_id="platform:user_id")
      collect → main_agent → (reply | ask_confirm | ask_memory)
                                    ↓ 用户确认
                              prepare_execution → execute
                                    ↓
                              ExecutionAgent（Claude/Codex）
                                    ↓ ExecutionReport
                              MainAgent.finalize → 用户话术
  → 图返回 state，runner 按 __interrupt__/result 分支投递（先文件后文字）
```

**Q: 为什么不直接让一个 agent 用工具循环把这些事都干了？（最容易被挑战的问题）**

> "最早就是这么做的——见 DECISION.md 的演化史。问题是 coding agent 的 agentic loop 是为‘写代码/操作文件’设计的执行环境，把用户身份判断、长期记忆语义、对话话术也塞进同一个 loop 后，模型会把执行环境里的上下文（比如运行 Claude Code 的开发者账号信息、‘小帮’这个机器人名字）和用户对话上下文搞混。真实症状是：模型把‘你是小帮’误判成用户在自报姓名，写进了用户的长期档案。根因不是 prompt 写得不好，是**没有边界**——同一个模型调用既要理解语义又要有 shell/文件系统权限，职责耦合导致权限和判断力一起泄漏。拆开以后，MainAgent 是纯 chat API，物理上没有 shell/文件工具，它不可能把执行环境信息带进用户对话，因为它压根碰不到执行环境。"

**Q: 这样拆是不是牺牲了能力？比如 MainAgent 想读一下文件内容再决定怎么理解，做不到了。**

> "对，这是有意的能力削减。MainAgent 只能看到 `input_filename`（文件名）和 `known_facts`（结构化白名单档案），看不到文件内容。文件内容的理解让 ExecutionAgent 在自己的沙箱 workdir 里做，返回 summary。这是用‘弱一点的理解上下文’换‘语义判断权限收敛到一个可审计的模块’——对一个要长期维护记忆治理和用户身份边界的系统，这个 trade-off 我认为是对的。如果场景是纯粹的单轮文档问答，这个拆分就是过度设计。"

---

## 三、Agent Loop：这个项目里到底有几个“loop”

这是最容易被面试官抓住细节追问的地方——**MainAgent 和 ExecutionAgent 根本不是同一种意义上的 “agent”**。

**Q: 你说的 agent loop 指的是什么？**

> "严格说这个系统里只有一个真正意义上的 tool-calling agentic loop，在 `ExecutionAgent` 里，是 Claude Agent SDK / Codex CLI 自带的：模型自己决定要不要读文件、写 Python 脚本（`python-docx`/`openpyxl`）、跑代码、看报错再改，直到它认为任务完成，通过 `output_format={"type": "json_schema", ...}` 吐一个结构化结果出来。这一层对我来说是**黑盒**——我不控制它内部具体调了几次工具，只控制输入（instruction + workdir + 可选输入文件）和输出契约（`summary/filename/warnings`）。

> MainAgent 不是这种 loop。它是**一次性结构化输出调用**：一次 `chat.completions.create(response_format=json_object)`，没有工具，没有多轮内部循环。它决定 `reply` 还是 `propose_task`，顺带给出记忆候选，一次调用就结束。真正的‘多轮’是靠 LangGraph 的 `collect → main_agent` 循环在**用户消息之间**推进的，不是模型在一次调用里自己循环。"

**Q: 为什么 MainAgent 不用工具？给它接个 file-read 工具不是更聪明吗？**

> "刻意不给。原因有两个：
> 1. 职责边界——一旦 MainAgent 能读文件，它就有了进入执行环境的入口，前面说的身份泄漏问题会卷土重来。
> 2. 可预测性——MainAgent 的输出要过 `MemoryRepository.validate()` 这种确定性校验（白名单字段、evidence 必须逐字来自当前用户消息），如果它是个多轮 agentic loop，每一步中间状态都可能产生我没审过的副作用。一次性结构化输出更容易做契约测试，`tests/test_main_agent.py` 直接喂 fake client 断言各种边界情况（比如‘你是小帮’不能被当成 name evidence）。"

**Q: ExecutionAgent 的黑盒 loop 你怎么约束它不跑偏？**

> "四层约束，都在 `agents/claude_agent.py`：
> 1. `permission_mode="bypassPermissions"` + 隔离 `cwd`：只能在自己的 workdir 里干活（这个 flag 本身是已知风险，见下面安全部分）。
> 2. `setting_sources=[]` + `exclude_dynamic_sections=True`：不读开发者本机 `~/.claude` 全局配置，不带 auto-memory/git status 这类和当前用户无关的动态段落——这是踩过坑之后加的，PITFALLS.md 记录了 `exclude_dynamic_sections` 挡不住开发者账号信息泄漏这个问题，靠 system prompt 显式禁止兜底。
> 3. `output_format` 用 JSON Schema 强约束返回结构，`required` 字段没给全直接在 `structured is None` 分支抛错，不会静默吞掉。
> 4. `asyncio.timeout(900)` 15 分钟硬超时，超时转成确定性异常，不让 graph 无限挂着。"

---

## 四、LangGraph：控制平面而不是主 Agent

**Q: 为什么用 LangGraph，不是自己写个状态机？**

> "本质我确实是要一个可恢复的状态机，LangGraph 提供了三个我不想自己重新造的东西：
> 1. **Checkpoint** — `AsyncSqliteSaver`，图在任意 node 之间的中间状态可以落盘、进程重启后从 `thread_id` 恢复。
> 2. **`interrupt()` / `Command(resume=...)`** — 原生支持‘暂停等用户确认，用户回复后从暂停点恢复’这种模式，不用我自己拿 pending state 拼一套。
> 3. **显式的图结构** — node/conditional edge 让状态机的合法转移一目了然，比一堆 if/else 判断‘现在处于哪个阶段’更不容易漏分支。

> 但我在文档和代码注释里反复强调一件事：**LangGraph 只是控制平面，不是主 Agent**。这是踩过的坑——旧架构曾经把‘有一个状态机在维护会话状态’误当成‘已经有主 Agent 层’，实际上语义判断（这句话是任务还是闲聊、这条证据算不算用户身份事实）散落在 graph 的路由逻辑里。现在 `graph.py` 里的每个 node 要么是纯粹的状态搬运（`_collect`），要么是调用 MainAgent/ExecutionAgent 拿到判断后落地为状态转移，它自己不发明业务默认值。"

**Q: 具体讲讲你的 interrupt/resume 是怎么工作的，中间状态存在哪。**

> "`ask_confirm` 和 `ask_memory` 两个 node 调 `interrupt({...})`，LangGraph 会把图在这个点暂停并把 payload 存进 checkpoint，`graph.ainvoke` 返回时 state 里带 `__interrupt__`。下一条用户消息来的时候，`run_mvp.py` 先 `graph.aget_state()` 拿到 `snapshot`，判断 `snapshot.interrupts` 非空且 `snapshot.next` 精确等于 `('ask_confirm',)` 或 `('ask_memory',)` 才允许 `Command(resume={"text":..., "file":...})`。

> 这里有个我踩过的真实 bug：LangGraph 的 `snapshot.next` 在**失败节点**上也会非空——它表示‘下一个该跑的节点’，不等于‘这是一个合法的用户确认点’。旧代码把 `next` 直接当 interrupt 标志，结果失败的 execute 节点会把用户下一条正常消息当成 resume 吞掉，还可能触发有副作用的 execute 重跑。现在的判断显式收窄成‘必须是真实 interrupt，且 next 恰好是这两个已知确认节点’，否则直接报错让用户知道会话状态异常，而不是静默做错事。"

**Q: `durability="sync"` 是什么，为什么要显式设？**

> "LangGraph 默认的异步 durability 不保证‘副作用发生前 checkpoint 已经落盘’这个顺序。举个例子：`prepare_execution` 节点创建了 workdir 并把 `execution_id` 写进 state，如果这一步的 checkpoint 是异步落盘的，进程刚好在这中间崩溃，恢复时可能都不知道这个 workdir 曾经存在过，或者反过来重复触发。所以生产入口所有 fresh/resume 的 `ainvoke` 都显式传 `durability='sync'`，保证 `prepare_execution` 的 checkpoint 落盘完成之后才真正进入 `execute` 打外部副作用。这是我在架构审阅里专门记录的一个坑，默认值在这个场景下不安全。"

---

## 五、Tool call 失败怎么处理 / 幂等性

这是我预期面试官会深挖的地方，因为这里最能看出对分布式系统失败模式的理解，而不只是接了个 SDK。

**Q: ExecutionAgent 调用失败了怎么办？**

> "先分清楚三种失败：
> 1. **调用本身失败/超时**（`ResultMessage.is_error` 或 `asyncio.timeout(900)` 触发）——这种转成 `RuntimeError`，`execute` 节点的 `except` 分支捕获，`error` 不为 None，`artifact` 强制置空，回复用户‘这次文档处理没有完成，请重新发起’。这一轮不会把 execute 状态挂起等重试，因为我不知道 coding agent 是不是已经在文件系统上留下了半成品。
> 2. **执行成功但 marker 落盘失败**（成功拿到 report，但写 `execution-report.json` 时进程崩了）——这是最麻烦的一类，属于**结果未知**状态，见下一题。
> 3. **finalize（最后一次措辞）失败**——报告已经产生、文件已经落盘，这时候不能因为‘怎么把结果说给用户听’这一步的 LLM 调用失败就让整个节点失败重跑，那样会重新执行一次已经成功的文档任务。所以这里单独 catch，降级成一句确定性文案（`已经处理完成，文件「xxx」已生成`），不重跑。"

**Q: 具体讲讲你的幂等设计，`execution-started.json` 和 `execution-report.json` 这套 marker 是干嘛的？**

> "这是在解决‘外部副作用（跑一次 Claude Agent SDK，可能已经写了文件、花了 token）不能随便重跑’这个问题，做法类似两阶段标记：
> - 真正调用 `execution_agent.run()` 之前，先原子写一个 `execution-started.json`（用 `tempfile.mkstemp` + `os.replace` 保证原子性，不会读到半写的文件）。
> - 调用成功后，把 `ExecutionReport` 写成 `execution-report.json`。
> - 下次这个 workdir 再被访问时（比如同一个 `execution_id` 因为某种重放触发了 execute 节点）：如果两个 marker 都在，直接读 `execution-report.json` 里的结果，**跳过重复执行**，走幂等读路径；如果只有 `started` 没有 `report`，说明上次执行的**结果未知**——可能成功了但没来得及写 report，也可能真的失败了——这时候我选择**拒绝自动重跑**，抛错转人工恢复，而不是赌一把。

> 这不是完整的 exactly-once，我在 PROGRESS.md 里明确写了这一点：这套 marker 能防止‘明知已经成功还再跑一次’，但没法让系统自动判定‘一个结果未知的执行到底该不该重跑’——那需要 execution backend 自己提供幂等键（比如让 Claude Agent SDK 侧也能‘续接’一个未完成的 session），目前没有。诚实地说这是当前架构里最大的一块技术债。"

**Q: 为什么不干脆做自动重试？**

> "因为 ExecutionAgent 的操作不是幂等的——它是‘调用一个可能已经修改了文件系统、消耗了 token/配额的 agentic loop’，不是‘发一个 HTTP GET’。盲目重试在‘上次可能已经成功’的场景下，最坏情况是重复生成文件、重复扣费，而且没有办法在应用层区分‘这次重试和上次是不是同一个逻辑操作’，因为 coding agent backend 本身不暴露幂等键。宁可保守地转人工，也不做静默重跑。"

---

## 六、并发与一致性

**Q: 同一个用户连续发好几条消息，会发生什么？**

> "先过 `Debouncer`：同一个 `(platform, user_id)` 10 秒内的消息会被聚合成一条再交给图，这是为了不让‘帮我写一封...’和‘...请假条，理由是感冒’这种被打字速度拆开的自然语言触发两次决策。防抖 ready 之后进 `UserLocks[session_key]`，保证同一个会话的图调用是串行的——不然‘查询当前 state → resume/fresh invoke’之间会有 TOCTOU 窗口，比如两条并发消息同时看到‘没有 pending confirm’然后都发起 fresh invoke，会产生并发写同一个 `thread_id` 的 checkpoint。"

**Q: `UserLocks` 是进程内锁，多实例部署会有什么问题？**

> "会失效——如果后面要多进程/多实例部署（比如做水平扩展或者蓝绿部署），同一个 `session_key` 可能被路由到两个进程，各自的内存锁互相看不见，又会出现刚才说的并发写同一 `thread_id` 的问题。这是我在 PROGRESS.md P1 里明确列出的债务，需要换成按 session 分区的分布式锁或者队列，目前只支持单进程部署。"

---

## 七、长期记忆治理

**Q: 为什么记忆这块要单独讲，不就是个 key-value store 吗？**

> "存储本身简单，难点是**什么时候允许写入**。这里的设计原则是‘模型只能提候选，不能自己决定写’：
> 1. **白名单字段**——只有 `name/department/job_title/preferred_address` 四个字段，模型返回别的字段直接在 `DeepSeekMainAgent.decide()` 里被过滤丢弃，不进入候选。
> 2. **evidence 必须逐字来自当前用户消息**——`MemoryRepository.validate()` 会拿 `evidence` 去比对 `current_user_text`，不是‘看起来合理’就行，必须是原文子串，而且明确排除历史消息和 assistant 的话被当成证据（对应‘你是小帮’不能被当成用户在说自己叫小帮这个真实踩过的坑）。
> 3. **候选 ≠ 落盘**——校验通过只是‘展示给用户看’，任务型对话要回复‘是并记住’，纯聊天要回复‘记住’，单独回复‘是’只执行任务不存记忆。这是为了防止模型误判之后没有人工兜底就直接改变了长期状态。
> 4. **确认词是完整匹配**——用正则 `fullmatch`，‘好像不对’‘可以先别做’‘是，不过先改’都不会被当成确认，这也是从真实误触发案例里加的正则。"

**Q: 这套东西怎么测？你不可能每次都调真实 DeepSeek 断言。**

> "`tests/test_main_agent.py` 用 fake client 把 `decide()`/`finalize()` 的输出行为做成确定性的，测的是 `MemoryRepository` 这层校验逻辑和 graph 路由，不测 DeepSeek 模型本身的语义质量好不好——那是两件事。目前 84 个测试全是这种离线契约测试，真实 DeepSeek 的多轮对抗样例/黄金集还没做，这个我在 PROGRESS.md 里也标成待办，不会把‘校验逻辑测过了’混同成‘模型语义质量测过了’。"

---

## 八、安全边界（诚实暴露风险，别回避）

**Q: `bypassPermissions` 是什么，安全吗？**

> "这是 Claude Agent SDK 的一个权限模式，让执行单元在自己的 workdir 里操作时不用逐次询问权限确认。当前用的是本机 `claude login` 缓存的订阅鉴权，不是走合规的 API key + 独立沙箱。这是 DECISION.md 里明确记录、用户知情接受的 MVP 阶段风险，**不适合在开放给开发者本人以外的用户之前保留**。真正对外之前需要换成 API key 鉴权 + 真正的 OS/容器级沙箱隔离，而不是靠 prompt 约束加 cwd 限制。如果面试官问‘生产就绪吗’，答案是明确的‘没有，这是已知且写在文档里的 P0 阻塞项’。"

---

## 九、可观测性与故障恢复现状

**Q: 出了问题你怎么排查？**

> "`turn_log.py` 写结构化 JSONL 到 `var/logs/turns.jsonl`，每一轮 execute 记 `run_id/input/output/backend/duration/success/error`，而且这个写入本身失败了也不会让已经成功的执行变成 pending（`_execute` 里单独 catch，只记日志不改变业务结果，这也是吃过一次亏才加的：turn log 失败曾经把成功执行拖回 pending 状态触发重跑）。"

**Q: 现在最大的可靠性缺口是什么，你会怎么排优先级？**

> "按 PROGRESS.md 的 P0 顺序：
> 1. 还没在真实部署环境上跑通完整闭环（这个我们今天正在做）。
> 2. 没有持久 inbox/outbox：飞书重投会不会重复处理、图跑成功了但发送失败会不会丢结果、文件发成功文字发失败的半投递——都还没有端到端的故障注入测试。
> 3. Execution idempotency 只到‘不会明知成功还重跑’，没到真正 exactly-once。
> 4. 安全隔离没做完。

> 我会先做 1（不然后面的都是纸上谈兵），然后按‘会不会丢用户数据/重复扣费’的严重度做 2 和 3，安全隔离在真正对外开放前必须完成但不阻塞当前单开发者自用阶段。"

---

## 十、快速追问库（一句话版，用来查漏补缺）

- **为什么 checkpoint 里不存文件 bytes？** → 图状态只存 `var/inputs/`、`var/workspaces/` 下的 JSON reference，附件/产物落盘和图的生命周期解耦，SQLite 不会因为大文件膨胀，也避免文件 bytes 被重复写进 checkpoint（真实修过的 bug）。
- **`active_artifact` 是干嘛的？** → 让“继续修改刚才那份文件”能拿到真实文件，而不是只有文字历史没有文件引用；且只有 MainAgent 显式设置 `use_previous_artifact=true` 才会被执行器使用，控制平面不猜测。
- **为什么所有 node 是 `async def`？** → 当前受管环境里同步 node 会被扔进线程池，线程池 worker 完成后没法用 asyncio 的 socketpair 唤醒主 event loop，图会假死；全异步节点绕开这条有问题的跨线程唤醒路径（`graph.py` 顶部注释里写明了）。这也是为什么今天要专门在 WSL 下重新冒烟——之前的沙箱限制在真实 Linux 环境下可能已经不存在了。
- **飞书文件+文字为什么要分开发？** → 飞书 API 本身文件消息不能带文字 caption，这是记录在 PITFALLS.md 里的平台限制，`deliver_graph_output` 里先发文件再发文字。
- **怎么防止两个不同用户的文件名冲突？** → `artifacts.py` 里输入/输出都按 `platform:user_id` + 时间戳分目录，文件名清洗过程本身也修过一个“不同用户 ID 清洗后碰撞”的 bug。

---

## 十一、如果被问“这个项目最想重做的一个决定是什么”

> "把 `run_mvp.py` 里的 debounce/locks/delivery 收进正式的 `SessionCoordinator`。现在这个入口脚本承担了太多 application-service 职责——防抖窗口回调里直接拿锁调图、投递失败只能记日志兜底，测试起来也不方便单独验证‘会话协调’这一层的行为。这是我在 P1 里排的第一项，如果重新设计我会一开始就把它当一个独立组件写，而不是从一个 MVP 脚本慢慢长出来。"
