# Pitfalls

## Claude Agent SDK 不能用 claude.ai 订阅登录支撑对外产品，必须用 API key

**现象**：直觉上会觉得"我是唯一开发者，先用自己的 Claude Pro/Max 订阅登录（`claude login`）调试省钱"是无害的，尤其项目还在 MVP/单人调试阶段时看起来完全说得通。

**真因**：Anthropic 官方文档写明——Agent SDK 受 Commercial Terms of Service 约束，不允许第三方开发者为"自己提供给客户/终端用户的产品或服务"使用 claude.ai 登录（Free/Pro/Max 的 OAuth），必须用 API key 鉴权。来源：https://platform.claude.com/docs/en/agent-sdk/overview 。walkie-dokie 是通过企业微信/QQ/微信转发他人消息的机器人，只要背后转发的是别人（哪怕只是家人朋友）发来的消息，就构成"对外提供的产品"，不满足"纯个人使用"的豁免条件，从 MVP 阶段起就适用这条限制。

**正确做法**：从项目一开始就用 Anthropic API key（console.anthropic.com 申请）给 Claude Agent SDK 鉴权，不要指望开发者自己的 Pro/Max 订阅能覆盖这个用途。

**判据**：只要这个 Claude Agent SDK 会话的输出/产物最终会传递给"开发者本人之外的任何人"（不管是通过 bot、API、还是转发），就该用 API key，不是订阅登录。

**关联**：Codex（OpenAI）没查到同等明确的书面声明，但消费级 ChatGPT 订阅大概率也有类似的个人使用限制，只是未经确认。如果后续接 Codex 作为执行后端，按同样假设处理（用 API key），除非查到 OpenAI 明确允许订阅支撑对外产品的说法。

## Windows 下 `asyncio.create_subprocess_exec("codex", ...)` 直接报 `FileNotFoundError: [WinError 2]`

**现象**：`codex --version` 在终端（Git Bash / PowerShell）里跑得好好的，但 Python 用 `asyncio.create_subprocess_exec("codex", "exec", ...)` 调用同一个命令，直接抛 `FileNotFoundError: [WinError 2] 系统找不到指定的文件`（Windows 下 stderr 经常编码乱码，容易被误认成别的问题）。

**真因**：`codex` 在 Windows 上是 npm 全局安装生成的 shim，同名目录下实际有三份文件——`codex`（POSIX shell 脚本，给 Git Bash/WSL 用）、`codex.cmd`（cmd.exe 批处理，PowerShell/CMD 交互式终端解析裸命令名时默认会找到这个）、`codex.ps1`（PowerShell 脚本）。终端里输入 `codex` 时，shell 自己做了 PATHEXT 扩展名解析找到其中一个来执行；但 `asyncio.create_subprocess_exec`（底层是 Windows `CreateProcess`）不会做这层解析，传裸命令名 `"codex"` 找不到对应的 `.exe`，直接报错。

**正确做法**：调用前用 `shutil.which("codex")` 解析出真实路径（它会走 PATHEXT 逻辑，在这台机器上解析到 `codex.CMD`），把解析后的绝对路径传给 `create_subprocess_exec`，不要传裸命令名。任何用 `asyncio.create_subprocess_exec`/`subprocess.Popen(shell=False)` 调用 npm/pip 全局安装的 CLI 工具（本质是 shim 脚本而非原生 `.exe`）时，在 Windows 上都要过一遍这个坑。

**判据**：在 Windows 上用 `asyncio.create_subprocess_exec` 或 `subprocess.run(..., shell=False)` 调一个命令，命令本身在终端手动敲能跑，但 Python 里报 `WinError 2` 找不到文件——先怀疑这个命令是不是 npm/pip 装的 shim（`.cmd`/`.ps1`），不是原生 `.exe`。

**已由**：`src/walkie_dokie/agents/codex_agent.py` 里 `_CODEX_EXECUTABLE = shutil.which("codex")` 守门（模块加载时就地校验，解析不到会直接 `RuntimeError`，不会在运行时才发现）。

## Git Bash 里给 CLI 传以 `/` 开头的参数会被 MSYS 转换成 Windows 路径

**现象**：在这台机器的 Git Bash 里跑 `claude /status`（或任何 CLI 的斜杠子命令/参数），命令没有按预期执行子命令，而是莫名其妙地把参数解释成了别的东西，行为完全不像文档描述的那样。

**真因**：Git Bash 基于 MSYS2，会对形如 `/xxx` 的命令行参数做自动路径转换（当成 Unix 风格路径处理），`/status` 被转换成了 `C:/Program Files/Git/status` 这样的 Windows 路径再传给程序，程序收到的根本不是 `/status` 这个字符串。

**正确做法**：要么用 `//status`（双斜杠转义，MSYS 会跳过转换），要么在非 Git Bash 的终端（PowerShell、cmd）里跑这类命令。

**判据**：Git Bash 里调用任何 CLI，只要参数是 `/` 开头，且行为跟预期对不上（尤其看起来像是把参数当成了文件路径），先怀疑这条。

## Windows 上 Python `print()`/日志输出中文默认乱码

**现象**：脚本本身没报错，但终端（包括 Bash 工具捕获的输出、后台任务写的 log 文件）里所有中文全变成类似 `����` 的乱码，看起来像编码损坏，容易怀疑是数据本身的问题。

**真因**：这台机器的 Windows 控制台默认代码页是 GBK（`cp936`），Python 的 `sys.stdout`/`sys.stderr` 默认继承这个代码页而不是 UTF-8，`print()`/`logging` 往里写中文字符串时就用 GBK 编码输出，但终端/日志查看工具按 UTF-8 解释，于是乱码。内容其实没坏——用 `.decode('gbk')` 或者干脆换个正确编码的终端能看清。

**正确做法**：入口脚本启动时显式把 `sys.stdout`/`sys.stderr` reconfigure 成 `utf-8`（`sys.stdout.reconfigure(encoding="utf-8")`），或者设置环境变量 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`。项目里统一放在 `src/walkie_dokie/logging_config.py` 的 `setup_logging()` 里做，所有入口脚本启动时调一次，不用每个脚本各自处理。

**判据**：Windows 上跑 Python 脚本，终端或日志文件里中文变问号/乱码方块，但脚本逻辑本身跑通了（没抛异常）——先怀疑这条，不要去查业务逻辑。

## `codex exec` 的 `workspace-write` 沙箱在 Windows 上几乎拦掉所有 PowerShell 命令，且这是上游未修复的 bug，不是我们能配置绕开的

**现象**：`codex exec --sandbox workspace-write` 明确开了写权限，但只要 Codex 试图执行任何 PowerShell 命令（包括完全无害的，比如单纯写一个文件），都会收到 `rejected: blocked by policy`，最终 Codex 只能回复"工作区是只读的，没法执行"，即使任务本身清楚明确、不缺任何信息。表面上像是"意图理解不到位在反复追问"，实际上是执行层面被拦了，Codex 只是把"拒绝原因"包装成了自然语言解释。

**真因**：这是 Codex CLI 在 Windows 上的已知开放问题（GitHub `openai/codex` issue #11885、`openai/codex-plugin-cc` issue #57）——`workspace-write` 沙箱策略检查器在 Windows/PowerShell 路径下几乎拒绝所有命令执行，跟本地有没有自定义 config.toml/AGENTS.md/rules 完全无关（本项目已经排除过：换了全新隔离的 `CODEX_HOME`、用 `-c sandbox_permissions=...` 显式覆盖配置，问题依旧存在）。文档提到的另一个说法是 `config.toml` 的 `sandbox_permissions` 只有"direct CLI path"生效、"app-server path"不生效，但即使按"direct CLI path"配置也没能绕开。

**正确做法**：目前没有能在不牺牲安全性的前提下修复的办法——唯一的绕过方式是 `--dangerously-bypass-approvals-and-sandbox`（完全跳过审批和沙箱），官方文档明确警告这只该用于"外部已经做好沙箱隔离"的环境，而我们的执行只是换了个当前工作目录，不构成真正的操作系统级隔离，开这个 flag 等于让 Codex 生成的命令对整台机器有无限制的读写和网络权限，存在真实的提示词注入风险。**本项目决定不开这个 flag**，Codex 后端在 Windows 上暂不可用，见 DECISION.md。

**判据**：Windows 上用 `codex exec --sandbox workspace-write` 执行任何命令，只要看到 stderr 里出现 `rejected: blocked by policy`（用 `--json` 才能看到这条，不加 `--json` 只会看到 Codex 把拒绝原因转述成自然语言的"信息不全"式回复），直接对应这条已知问题，不用再花时间排查自己的 prompt/config 写得对不对。

## 飞书发消息不能文件+文字一起发，只能分开发，"收到文件没指令"是必然会发生的正常状态

**现象**：设计消息处理流程时，很自然会假设用户发文件的同时能带一句说明（"这是我的简历，帮我改一下格式"这种一条消息搞定），结果实测发现飞书客户端发文件就是单独一条消息，没有"配文字"这个选项，用户只能先发文件、再单独发一条文字说明（或者反过来）。

**真因**：飞书 IM 的文件消息（`msg_type: file`）本身就不支持附带文本内容，这是飞书客户端和协议层面的限制，不是我们能通过 API 参数绕开的。

**正确做法**：处理消息的状态机必须把"只收到文件、没收到指令"和"只收到指令、没收到文件"都当成正常的中间状态来设计，不能假设两者会在同一条消息里一起到达；而且收到单独一条文件消息时必须主动给用户反馈（哪怕只是"收到了，请告诉我要做什么"），不能因为"信息还不全"就沉默不回复——沉默会让用户以为发送失败了。

**判据**：设计任何"文件+指令"两部分输入的交互流程之前，先确认目标 IM 平台的客户端到底支不支持"发文件时同时带文字"，不要想当然。

## Claude Agent SDK 用 `output_format` 要结构化输出时，`max_turns=1` 偶尔会被判超限报错

**现象**：`query()` 配了 `output_format={"type": "json_schema", ...}` 和 `max_turns=1`，绝大多数调用正常返回结构化结果，但偶尔会报错，`ResultMessage.result` 是 `None`（没有诊断价值，容易一头雾水）。打印 `subtype`/`stop_reason`/`terminal_reason`/`errors` 这些字段才能看清：`subtype='error_max_turns'`、`stop_reason='tool_use'`、`errors=['Reached maximum number of turns (1)']`。

**真因**：`output_format` 要求的结构化输出，内部是靠模型调用一次工具来交付最终答案实现的（不是直接在文本里返回 JSON）。这次工具调用本身要占一轮，如果模型在给出这次工具调用之前还想先"思考"一下（哪怕只是很短的一步），就需要 2 轮才能完成，`max_turns=1` 这时候会把这次调用直接判定超限失败，而不是等它继续。

**正确做法**：给结构化输出的调用留够余量——实测 `max_turns=2` 依然撞见过超限报错，轮数波动比预期大，不值得为了省一点 token 精确调这个数字。`allowed_tools=[]` 不能防住这个问题——结构化输出用的那次工具调用是 SDK/CLI 内部机制，不受 `allowed_tools` 白名单约束。既然工具本来就被 `allowed_tools=[]` 卡死了，多给几轮不会导致失控探索，直接给够（比如 6）就行，不用来回试小数字。

**判据**：`output_format` + 较小的 `max_turns` 组合，只要看到 `ResultMessage.is_error=True` 且 `result=None`，先打印 `subtype`/`errors` 字段确认是不是 `error_max_turns`，不要凭空猜测是 prompt 写得不对。

## 同一用户的消息在 execute 节点还没跑完时又发一条，会对同一个 LangGraph thread 触发并发 `ainvoke()`，导致 checkpoint 状态错乱（不报错，更隐蔽）

**现象**：用 `asyncio.create_task` 让不同用户互不阻塞是对的，但同一个用户在"图正在跑 `_execute`"这段时间又发一条新消息，会被当成一次全新请求，对**同一个 `thread_id`** 再发起一次 `ainvoke()`。两次调用各自独立完成、都不报错，但最终 checkpoint 状态是错的——先完成的那次执行结果可能在最终状态里丢失，用户会先收到"完成了"，紧接着又收到一个跟原始任务对不上的新确认问题，状态还卡在这个新问题上。

**真因**：`aget_state().next` 只在图真正暂停在 `interrupt()` 时才非空——`_execute` 节点正在跑（哪怕跑很久）不算暂停，所以判断"这个用户是不是正卡在等确认"这个检查会漏掉"正在执行中"这个窗口，把这期间的新消息误判成全新请求处理。LangGraph 的 checkpointer 不会替你把同一个 thread 的并发调用排队或加锁，两次 `ainvoke()` 各自读、各自写，后写的会覆盖/丢失先写的部分字段。

**正确做法**：按 `user_id` 加一把 `asyncio.Lock`（见 `orchestrator/locks.py` 的 `UserLocks`），所有对同一个 `thread_id` 的 `ainvoke()`/`Command(resume=...)` 调用都要先拿到这个用户的锁才能发起，不同用户的锁互不相干，不影响跨用户并发。

**判据**：任何用 LangGraph checkpointer 按 `thread_id` 隔离多用户状态的场景，只要允许"用户在一次调用还没返回时又发起新的一次调用"，就要检查有没有对同一个 thread 做互斥——这不是 LangGraph 自动帮你做的事。

## 本地量化小模型（7B-8B）做严格结构化提取，会大范围跑题，不是精度问题

**现象**：让本地 Ollama 模型（实测 `qwen2.5:7b`、`qwen3:8b`）从一句话里提取指定的几个字段，输出内容跟输入完全不相关——不是"提取得不准"，是答非所问：问"从这句话里提取姓名部门"，模型能输出一段关于"脑卒中"的医学科普，或者"三顾茅庐"的英文翻译，输入里压根没有这些词。换更严格的反幻觉 prompt（"逐字核对，不要推测，不要编造"）之后，问题依然存在，没有改善。

**真因**：不确定具体机制，但现象很一致——两个不同模型、两次不同的严格 prompt，都出现了"完全脱离输入内容"级别的跑题，而不是"多编了一两个字段"这种轻度幻觉。这提示这类量化到 Q4 左右的 7B-8B 模型，在"严格约束 + JSON schema 结构化输出"这个组合任务上可靠性可能有系统性问题，不是靠调 prompt 能稳定解决的。

**正确做法**：涉及"必须忠于原文、不能编造"的提取类任务，先别默认本地小模型能顶上，实测验证过再决定要不要用；效果不确定时换一个更大/非量化的模型，或者直接用云端 API（本项目最终换成了 DeepSeek `deepseek-chat`，同样的输入表现明显更可靠，见 DECISION.md）。

**判据**：本地小模型在结构化提取任务上，如果输出内容跟输入主题完全不沾边（不是"编错了几个字段"而是"整个答非所问"），先别怀疑是自己 prompt 写得不够严格，先怀疑是模型本身在这类任务上不够可靠——加更强的约束语言未必能解决系统性的跑题问题。
