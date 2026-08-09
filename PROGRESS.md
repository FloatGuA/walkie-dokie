# walkie-dokie — Progress

## 状态快照

项目方向：面向中老年人群的多平台机器人办公助手，核心场景是 Word/Excel 文档的生成/编辑/问答（原「物业家政多Agent平台」方向已搁置，见 [DECISION.md](DECISION.md)）。

**MVP 端到端闭环已跑通并验证，orchestrator 也已接入，防抖+确认环节也跑通了，用户 memory 两层都做了**：飞书发消息（文字/文件）→ LangGraph 状态机（`orchestrator/`：防抖攒消息 → 生成任务草稿（带已知用户信息）→ 列出缺失信息等用户确认 → 确认后强制执行不再追问，执行完被动提取新事实并回显给用户）→ `ClaudeAgentSDKBackend` 用 python-docx 生成/编辑/总结文档 → 飞书把文件和文字回复发回用户。会话状态用 `AsyncSqliteSaver` 落盘（`var/checkpoints.db`），能扛进程重启（防抖缓冲区除外，见下方"待处理"）。每条消息独立工作目录（`var/workspaces/`，持久化不清理）+ 结构化留痕（`var/logs/turns.jsonl`）+ 项目本地日志（`var/logs/walkie-dokie.log`，DEBUG 粒度）+ `pytest tests/` 自检套件。平台选型定为飞书自建应用（长连接，见 DECISION.md），执行后端目前只有 Claude 这一条能跑，Codex 在 Windows 上因上游沙箱 bug 不可用。

### 数据流 / prompt 注入快照（2026-08-10，随实现变化，不保证长期准确——稳定的跨模块结构见 TECHNICAL.md）

```
飞书消息 → FeishuAdapter（file 类型先下载）→ InboundEvent
  → run_mvp.handle_event()：图卡在 ask_confirm？→ Command(resume=文字) 直接恢复
                             否则 → Debouncer 攒 10 秒 → dispatch_fresh()
  → graph.ainvoke(thread_id=user_id)

  【collect】合并 new_text/new_file 进 pending_instruction/pending_file

  【draft】（pending_instruction 非空才进；调用 generate_draft_task_prompt）
      system_prompt：preset="claude_code" + append（draft.py._SYSTEM_PROMPT）
        + exclude_dynamic_sections=True
        append 内容：反账号信息泄漏的强制指令 + "判断 is_task，是的话给 task_summary
        （给执行agent看，客观）/missing_info，不管是不是任务都要给 user_message
        （给用户看，对话口吻，跟 task_summary 不能混用同一套措辞）"
      user 输入：pending_instruction 原文
        + （有文件）"工作目录下有输入文件：xxx"
        + （有已知档案）memory.load_facts() 读到的用户信息
      配置：allowed_tools=[]、max_turns=6、setting_sources=[]、output_format=JSON schema
      产出：{is_task, task_summary, missing_info, user_message}

  【draft 之后按 is_task 分流】
      is_task=false → 【reply_directly】：发 user_message，清空 pending_*，END
                       （闲聊/寒暄从这里直接退出，不进 confirm，也是死循环的出口）
      is_task=true  → 【ask_confirm】：interrupt() 暂停，把 user_message 发给用户
                       回复匹配确认词前缀 → execute；不匹配 → 回 collect
                       （回 collect 后文字被合并、重新过一次 draft，可能被
                       重新分类成 is_task=false 从而跳出循环）

  【execute】（确认通过才跑）
      system_prompt：preset="claude_code" + append（claude_agent.py._SYSTEM_PROMPT_APPEND）
        + exclude_dynamic_sections=True
        append 内容："你是 walkie-dokie 文档处理执行单元，只做文档相关的事，不要提无关能力"
        + 同一条反账号信息泄漏指令
      user 输入：task_prompt = draft_task_prompt["task_summary"]
        + （missing_info 非空）"这些信息用占位符直接完成，不要再问"
        + （有已知档案）memory.load_facts() 第二次独立读取，"涉及这些字段用真实值"
      配置：cwd=独立工作目录（var/workspaces/）、bypassPermissions、setting_sources=[]、
        output_format=JSON schema
      执行完 → memory.extract_facts(pending_instruction 原文) 用 DeepSeek 提取新事实并存盘
        （这次调用没有专门的 system_prompt 隔离，走的是 openai SDK 直连 DeepSeek，
        不涉及 Claude Agent SDK 的账号泄漏问题）

  → deliver_graph_output：发文件 + reply_text +（有新事实）"顺便记住了..."
```

关键点：`draft`（判断+草稿）、`execute`（真正干活）、`memory.extract_facts`（事实提取）是三次完全独立的模型调用，互不知道对方存在，全靠 `SessionState` 传递；前两次都是 Claude Agent SDK（同一套隔离/反泄漏机制要分别配置，已经在这两处各踩过一次坑，见 PITFALLS.md），第三次是 DeepSeek。

## 待处理 / 下一步

- **`bypassPermissions` 提示词注入敞口**：`ClaudeAgentSDKBackend` 一直用 `bypassPermissions` 跑，现在又接了"读用户文件"的功能，存在真实的提示词注入风险，但现在收益太低不值得处理。**硬性条件：项目公开给除开发者之外的人用之前必须先处理**，详细取舍见 DECISION.md
- **文件问答/总结的效果不太好**：实测让 Claude 总结一份 `worklog.md`，流程全部走通了（收文件→确认→执行→发回结果），但用户反馈总结内容质量不够好。用户明确说"效果问题另说"，先不查，工作流本身通了就行——如果要认真做，大概率要在 draft/执行阶段的 prompt 上下功夫，不是这次的重点
- **Codex 后端在 Windows 上不可用**：不是订阅额度问题（额度问题已过去），是 Codex CLI `workspace-write` 沙箱在 Windows 上的已知上游 bug，拦掉几乎所有命令执行。用户拍板不开 `--dangerously-bypass-approvals-and-sandbox` 绕过（风险太大，见 DECISION.md）。MVP 阶段只用 Claude 这条后端，Codex 等上游修复或者考虑换 harness / 挪到非 Windows 环境再验证
- `lark_oapi` 自己的 logger 会被我们的 root logger 重复打印一遍（不影响功能，未处理）
- `lark_oapi` 报了个不影响功能的噪音错误：`processor not found, type: im.message.reaction.created_v1`（用户在飞书给消息点了个表情反应，触发了一个我们没注册处理器的事件类型），不影响主流程，未处理
- 针对"文档办公"场景重新梳理适老化交互设计（旧方向的语音优先设计不完全适用，具体怎么做还没讨论）
- 周计划/路线图还没细化，留到下一步单独讨论
- **进程常驻/自动重启**：现在是手动敲命令跑的开发脚本，关终端/重启/崩溃都没人管。用户拍板"这个好做，以后再做"，方案已讨论过（Windows 服务化包装，或挪到云主机 + 进程管理器），暂不实现
- **部署目标机器未定**：本地先跑通，以后要挪云主机，但云主机是 Linux/macOS/Windows 都还没定，用户明确说"现在还不知道"——先不依赖任何特定 OS 的实现细节
- **日志粒度**：websocket 帧级噪音（PING/PONG）、aiosqlite 每次 checkpoint 写入的 SQL 语句都已经调粗，其余（`walkie_dokie.*`、urllib3 等）还是 DEBUG 粒度，用户明确说了等过了高频调试阶段再整体调粗，不用现在处理
- **服务不可用时用户端完全无感知**——**用户拍板不做通知机制，只记日志**，接受飞书长连接是黑盒这个现实，详见 DECISION.md，不再是待办
- **防抖缓冲区不持久化，进程重启会丢消息**：orchestrator 的会话状态已经用 `SqliteSaver` 落盘，但 `Debouncer` 的缓冲区（`_buffers`/`_files`/`_tasks`）是纯内存状态，不在 checkpoint 里。用户发消息后如果正好卡在 10 秒防抖窗口内、这时候进程被杀/崩溃，这条消息会丢，跟会话状态有没有持久化无关。（注：2026-08-09 一度怀疑一次真实丢消息是这个原因导致，后来查日志确认那次其实是正常处理成功了，是用户自己隔 44 分钟发了两次一模一样的话——这条缺口本身依然存在，只是那次不是它导致的）
- **提取 prompt 会把"用户怎么称呼机器人"误当成"用户自己的姓名"**：实测"你是小帮，是我的智能助手"被提取成了用户的第二个姓名字段，不是幻觉，是 prompt 没有把"用户自己的身份信息"和"用户对机器人的称呼"分开，需要在 `orchestrator/memory.py` 的 `_EXTRACT_SYSTEM_PROMPT` 里明确排除后者

## 进行中

（无）

## 已完成

- **用户 memory 两层 + 会话持久化**（2026-08-09，已验证：`memory.extract_facts()` 单测过，能正确提取姓名/职位；`AsyncSqliteSaver` 落盘位置和启动日志确认过；飞书端到端链路——防抖→执行→回复——也在 2026-08-09 19:40 那次真实交互里确认跑通了（查日志才确认的，一度误判成"卡在防抖缓冲区丢消息"，见下方"待处理"的更正说明）。但截至记录时，"顺便记住了"这条主动回显消息本身还没有在真实飞书交互里亲眼看到过，只单测过 `extract_facts` 和图内部的状态流转，最后一步的真实观感还没验证）
  - 层 1（会话持久化）：`scripts/run_mvp.py` 的 checkpointer 从 `InMemorySaver()` 换成 `AsyncSqliteSaver.from_conn_string(var/checkpoints.db)`，启动时 `await checkpointer.setup()` 建表
  - 层 2（用户事实记忆）：新增 `src/walkie_dokie/orchestrator/memory.py`——`load_facts`/`save_facts` 按 `{platform}_{user_id}.json` 存取（`var/memory/`），`extract_facts()` 执行完成后被动提取个人身份类事实
  - 提取模型选型走了一圈弯路：先试本地 Ollama（`qwen2.5:7b`/`qwen3:8b`），两次都严重跑题、完全答非所问，改用 DeepSeek `deepseek-chat`（`openai` SDK 换 `base_url`），效果明显可靠，详细经过见 DECISION.md、PITFALLS.md
  - `orchestrator/graph.py`：`_draft`/`_execute` 都会先 `memory.load_facts()` 拿已知信息注入 prompt（"涉及这些字段用真实值，不要用占位符"）；`_execute` 成功后提取新事实、存下来、通过新增的 `SessionState.new_facts` 字段带出去
  - `scripts/run_mvp.py` 的 `deliver_graph_output`：`new_facts` 不为空时，主动发一条"顺便记住了：...如果记错了，跟我说一声就能改"——被动记忆不能悄悄发生，这条是用户明确要求加的，测过要一行一条信息，别挤成一句话（面向中老年用户）
  - 顺手把 `draft`/`ask_confirm` 里 missing_info 列表的展示也从"、"密集拼接改成一行一条，跟上面同一个诉求
  - `tests/test_graph.py` 补了 `test_newly_extracted_facts_surface_in_final_state`/`test_no_new_facts_leaves_new_facts_none`/`test_known_facts_flow_into_draft_and_execute` 三个用例
  - `pyproject.toml` 新增依赖 `langgraph-checkpoint-sqlite`、可选依赖组 `memory`（`openai`）

- **搭起 pytest 自检套件**（2026-08-09，已验证：`pytest tests/` 22 项全过，1.76 秒跑完，不依赖网络/真实 API/`claude login`）
  - `tests/test_debounce.py`：`Debouncer` 的窗口触发、多消息合并+重置计时器、文件文字分开到达再合并、不同用户互不干扰
  - `tests/test_locks.py`：`UserLocks` 同用户拿同一把锁、不同用户互不阻塞、并发访问确实被串行化——这条直接对应昨天验证过的并发竞态修复，以后回归就靠它守
  - `tests/test_graph.py`：`collect→draft→ask_confirm→execute` 全流程（用 `FakeAgent` + monkeypatch 掉 `create_workspace_dir`/`log_turn`/`generate_draft_task_prompt`，不碰真实文件系统和 API）、文件单独到达不触发执行、拒绝确认会循环回草稿不执行、`_is_confirmation` 前缀匹配的参数化测试（含"是的"这个之前修过的 bug）
  - `pyproject.toml` 新增 `dev` extra（`pytest`/`pytest-asyncio`）和 `[tool.pytest.ini_options]`
  - 测试怎么 mock 图的三个真实依赖，写进了 TECHNICAL.md，以后加新测试直接抄
  - 顺手更新了 README.md 几处过时内容（"orchestrator 还没接入"、"临时目录"、"Codex 卡订阅额度"，都已经不是事实了）

- **验证并修复"同一用户并发写同一 checkpoint thread"的竞态**（2026-08-09，已用脚本验证：假执行后端 + 真实 `graph.ainvoke()` 制造竞态场景，加锁前确认会复现状态错乱，加锁后确认干净——不同任务的 result 不再互相覆盖，脚本验证完已删除）
  - `src/walkie_dokie/orchestrator/locks.py`：新增 `UserLocks`，按 `user_id` 分 `asyncio.Lock`，`scripts/run_mvp.py` 的 `dispatch_fresh`/`resume_pending` 两个发起 `ainvoke()` 的地方都改成先拿锁。TECHNICAL.md 记了这条规则，以后新增别的调用图的入口也要遵守
  - 顺带定位并修了另一个之前遗留的问题：`orchestrator/draft.py` 的 `max_turns=1`（后来 2 也不够）会偶尔被结构化输出内部的工具调用撞上"轮数超限"报错，之前那次神秘的"draft 生成失败：None"就是这个——诊断信息升级后（打 `subtype`/`errors` 等字段）这次直接看清了根因，改成 `max_turns=6`，见 PITFALLS.md
  - 顺手修了 TECHNICAL.md 里一处过时内容：`ExecutionAgent` 契约那段还写着 `tempfile.TemporaryDirectory()`，但工作目录早就改成持久化的 `create_workspace_dir()` 了，一直没同步

- **执行 agent 显式 system_prompt + websocket 日志降噪**（2026-08-09，已冒烟测试：`test_claude_backend.py` 正常生成文件，没再出现无关内容；websocket PING/PONG 不再进日志，`Lark` SDK 自己的 connected/disconnected/reconnecting 这些连通性日志还在）
  - `src/walkie_dokie/agents/claude_agent.py`：之前从没给 `ClaudeAgentOptions` 设过 `system_prompt`，实际跑的是 Claude Code 默认的通用助手人设——这很可能就是之前"回复里混进 Gmail/Calendar 连接器提示"那次异常的根因。改成 `{"type": "preset", "preset": "claude_code", "append": ..., "exclude_dynamic_sections": True}`：保留 Claude Code 自带的代码能力，追加 walkie-dokie 自己的任务框定，用 `exclude_dynamic_sections` 去掉 auto-memory/git status 这类跟单个用户绑定、这里用不上的动态段落
  - `src/walkie_dokie/logging_config.py`：新增 `_QUIET_LOGGERS`，把 `websockets` logger 单独调到 INFO，不影响 `walkie_dokie.*` 或其他第三方库的 DEBUG 粒度——这是按 logger 名字精确降噪，不是笼统调整全局级别

- **文件接收链路打通 + 两个交互 bug 修复**（2026-08-09，已验证：真实发 `worklog.md` 给 bot 让它总结，`var/workspaces/feishu_ou_.../20260809/f643acf3/` 下同时存了输入文件 `worklog.md` 和输出文件 `worklog_summary.docx`，`turns.jsonl` 里对应的 `run_id` 能直接定位到这个目录——按 session 找回输入输出的诉求确认可行）
  - `src/walkie_dokie/platforms/feishu.py`：`_on_message` 收到 `message_type == "file"` 时，调用飞书"获取消息中的资源文件" API（`client.im.v1.message_resource`，按 `message_id` + `file_key` 下载）拿到真实字节，填进 `InboundEvent.file`
  - 修了一个真实存在、用户实测发现的 bug：**飞书发消息文件和文字不能一起发，只能分开**，所以"只收到文件、没收到指令"是必然会发生的正常情况，但原来的 orchestrator 在这种情况下直接静默结束、不回复用户，跟"什么都没收到"没区别。`scripts/run_mvp.py` 的 `deliver_graph_output` 现在检测到"有 `pending_file` 但没有 `result`"时，主动回一句"收到文件「xxx」了，请告诉我需要我做什么"
  - 修了另一个真实 bug：`orchestrator/graph.py` 的 `_is_confirmation` 原来是精确匹配一个词表，用户回"是的"（词表里只有"是"）没被识别成确认，被当成"还在补充说明"重新生成了一遍草稿。改成前缀匹配（`str.startswith(tuple)`），"是的""好的呢"这类自然说法都能过
  - `src/walkie_dokie/orchestrator/draft.py`：draft 生成失败时的日志从只打一个 `message.result`（经常是 `None`，没有诊断价值）扩展成打 `subtype`/`stop_reason`/`terminal_reason`/`api_error_status`/`errors`/`result` 全部字段

- **执行后端配置隔离排查**（2026-08-09，已验证隔离本身生效：`ClaudeAgentSDKBackend`/draft 加 `setting_sources=[]` 后不再受影响；`CodexBackend` 切到独立 `CODEX_HOME` 后确认不再读取开发者个人的 `~/.codex` 内容——但排查过程中发现 Codex 在 Windows 上还有一个隔离之外的、更深的沙箱执行 bug，未解决，见上方"待处理"和 DECISION.md）
  - 起因：`CodexBackend` 测试时回复混入无关前缀"FGuA"，查到是开发者本机 `~/.codex/AGENTS.md` 的个人全局约束泄漏
  - `src/walkie_dokie/agents/claude_agent.py`、`src/walkie_dokie/orchestrator/draft.py`：`ClaudeAgentOptions` 都加了 `setting_sources=[]`
  - `src/walkie_dokie/agents/codex_agent.py`：新增 `CODEX_HOME_DIR`（`var/codex_home/`，自动创建），子进程调用传 `env={**os.environ, "CODEX_HOME": ...}`，不再需要 `--ignore-user-config`/`--ignore-rules` 这类单项 flag
  - `scripts/test_claude_backend.py`、`scripts/test_codex_backend.py`：修了个遗留问题——两个脚本还在用 `ExecutionAgent.run()` 的旧签名（没传 `workdir`），跟不上早前的接口改动，顺手改成用 `create_workspace_dir()`
  - 排查过程中用 `codex exec --json` 才能看到真实的执行拒绝原因（`rejected: blocked by policy`），不加 `--json` 只能看到 Codex 把拒绝包装成的自然语言回复，容易被误判成"意图理解不到位"

- **防抖 + 任务草稿确认闭环**（2026-08-09，已验证：真实在飞书测试——发"我写一份请假条" → 10 秒防抖后收到列明 7 项缺失信息的确认消息 → 回"是" → 派发执行时自动加上"用占位符直接完成、不要再问"的限定 → Claude 真的用合理默认值生成了 `请假条.docx`，不再卡在反复追问）
  - `src/walkie_dokie/orchestrator/debounce.py`：新增 `Debouncer`，按 `user_id` 缓冲消息，每条新消息重置一个可取消的 10 秒 `asyncio.Task`，到期把窗口内消息合并派发
  - `src/walkie_dokie/orchestrator/draft.py`：新增 `generate_draft_task_prompt()`，轻量 Claude Agent SDK 调用（`allowed_tools=[]`、`max_turns=1`，不跑代码），结构化输出 `{task_summary, missing_info}`——第一版只输出一句纯文本草稿，实测发现草稿如果自带"应该先问用户"的判断，会在用户确认后被原样喂给执行 agent 导致执行 agent 也去追问，改成结构化拆开 `task_summary`/`missing_info` 两个字段后，confirm 消息才能直接把缺什么列清楚，且执行时能针对性地加"别再问了"的限定
  - `src/walkie_dokie/orchestrator/graph.py`：图从两节点扩成四节点 `collect → draft → ask_confirm → execute`，`ask_confirm` 用 `langgraph.types.interrupt()` 暂停等用户回复，`_route_confirm` 机械判断回复是不是确认词（`是/对/确认/ok/yes` 等固定集合，不是 NLU 意图分类），不是就并回 `pending_instruction` 重新生成草稿；`execute` 节点如果 `draft.missing_info` 非空，会把"这些信息用占位符直接完成，不要再问"拼进喂给执行 agent 的最终指令
  - `scripts/run_mvp.py`：`handle_event` 先查 `graph.aget_state().next` 判断这个用户是不是正卡在 `ask_confirm`——是的话直接 `ainvoke(Command(resume=文本))` 恢复，不走防抖；不是的话才丢给 `Debouncer`
  - 用一个玩具两节点图（`scripts/_scratch_interrupt_test.py`，验证完删了）单独确认了 `interrupt()`/`Command(resume=...)`/`aget_state().next` 的实际行为再动手写正式代码：中断后 `ainvoke()` 返回值带 `__interrupt__` 字段，恢复时节点从头重跑

- **orchestrator 接入 + 日志/留痕基础设施**（2026-08-08，已验证：真实在飞书发消息，工作目录/结构化留痕/项目日志三者都确认落盘正确，见 `var/workspaces/feishu_ou_.../20260808/96025e01/请假条.docx` 和对应的 `var/logs/turns.jsonl` 记录）
  - `src/walkie_dokie/orchestrator/graph.py`：`build_graph()` 用 LangGraph `StateGraph` 实装，两个节点 `collect`（把新消息并入 pending_* 状态）/`execute`（调用执行 agent，记结构化留痕），按 `user_id` 做 checkpoint thread 隔离
  - `src/walkie_dokie/orchestrator/state.py`：`SessionState` 加回 `platform` 字段（工作目录命名要用）；`result` 字段从直接存 `ExecutionResult` dataclass 改成存 plain dict——碰到 LangGraph checkpointer 对未注册自定义类型的 deprecation 警告，改存 dict 更省心
  - `src/walkie_dokie/workspace.py`：新增 `create_workspace_dir(platform, user_id)`，执行目录改为 `var/workspaces/{platform}_{user_id}/{日期}/{run_id}/`，不再用 `tempfile.TemporaryDirectory()` 自动销毁——项目早期要能复盘，生成过程要留得住
  - `src/walkie_dokie/turn_log.py`：新增 `log_turn()`，每轮"输入→输出"结构化记一行 JSONL 到 `var/logs/turns.jsonl`（时间戳/平台/用户/输入/输出/后端/耗时/成功与否），跟人读的日志分开，供以后写脚本查询统计
  - `src/walkie_dokie/logging_config.py`：加了落盘到 `var/logs/walkie-dokie.log` 的 `RotatingFileHandler`（10MB×5 份），文件里存 DEBUG 粒度、控制台保持 INFO，不再依赖会话临时目录存日志
  - `src/walkie_dokie/agents/base.py`/`claude_agent.py`/`codex_agent.py`：`ExecutionAgent.run()` 加了 `workdir: Path` 参数，两个后端都不再自己起临时目录，改用调用方传入的工作目录
  - `src/walkie_dokie/platforms/base.py`：顺手修了一个遗留 bug——`Platform` 类型标注还是 `Literal["wecom", "qq", "wechat"]`，没有 `"feishu"`，跟 `feishu.py` 实际赋的值对不上
  - `scripts/run_mvp.py`：`asyncio.create_task()` 改成每条消息真正并发派发（之前是线性 `while` 循环）；发现新问题——同一用户连续快发消息现在会并发执行成多个独立请求，不是排队/合并，见下方"待处理"

- **MVP 端到端闭环跑通**（2026-08-08，已验证：真实在飞书里发消息、真实收到生成的 docx 文件和文字回复，日志全链路确认——收消息→执行→上传文件→发文件→发文字回复全部成功）
  - `src/walkie_dokie/platforms/feishu.py`：`FeishuAdapter`，用 `lark-oapi` 官方 SDK 长连接收消息（`P2ImMessageReceiveV1` 事件桥接到 `asyncio.Queue`），REST API 发消息/传文件（`file_key` 机制）。删除了过时的 `wecom.py` 占位
  - `src/walkie_dokie/agents/claude_agent.py`：`ClaudeAgentSDKBackend` 实装完成，用 `query()` + `ClaudeAgentOptions(output_format={"type": "json_schema", ...})` 拿结构化输出（`reply_text`/`filename`），在临时目录里跑，走本机 `claude login` 订阅鉴权
  - `src/walkie_dokie/agents/codex_agent.py`：`CodexBackend` 实装完成（`codex exec --sandbox workspace-write --output-schema`），代码可运行但**未实测成功过**，卡在 Codex 订阅额度
  - `src/walkie_dokie/agents/base.py`：`ExecutionAgent.run()` 签名加了 `input_filename` 参数（原来漏了，写 Codex 后端时发现必须要有文件名才能正确处理输入文件）
  - `scripts/run_mvp.py`：不经过 orchestrator 的最小胶水脚本，`platform.receive()` → `backend.run()` → `platform.send()` 线性串联
  - `src/walkie_dokie/logging_config.py`：新增 `setup_logging()`，统一 logging 格式，顺带修了 Windows 控制台 GBK 编码导致中文乱码的问题（见 PITFALLS.md）。`feishu.py`/`claude_agent.py`/`codex_agent.py`/`run_mvp.py` 全部接入
  - 过程中踩了两个环境坑，已记入 PITFALLS.md：Windows 下 `asyncio.create_subprocess_exec` 传 npm CLI 裸命令名找不到文件（要 `shutil.which()`）；Git Bash 里 `/` 开头参数被 MSYS 转成路径
  - `pyproject.toml` 新增依赖：`httpx`、`python-dotenv`、`lark-oapi`
  - 平台选型过程：企业微信自建应用（缺资质）→ 企业微信智能机器人/微信公众号（不支持发文件）→ QQ 官方机器人（缺资质）→ 一度拍板个人微信 `wxauto`（用户接受封号风险）→ 最终定为飞书自建应用（长连接、零封号风险、原生支持发文件），详细取舍见 DECISION.md

- 更早的骨架搭建历史（2026-08-07，物业家政方向作废 + 按新方向重建）搬到了 [docs/progress-archive.md](docs/progress-archive.md)
