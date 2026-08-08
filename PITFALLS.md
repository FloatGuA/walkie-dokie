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
