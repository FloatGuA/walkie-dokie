# walkie-dokie — Technical

三层分工（平台适配层 / 编排层 / 执行层）的概览见 [README.md](README.md) 的架构表格，这里不重复。本文档只记跨模块的稳定约定——半年后应该还成立、且读代码读不出全貌的那种。

## ExecutionAgent 契约：临时目录 + 结构化输出

所有执行后端（`agents/claude_agent.py` 的 `ClaudeAgentSDKBackend`、`agents/codex_agent.py` 的 `CodexBackend`）统一遵循同一个协议，接口定义见 `agents/base.py`：

1. 每次调用在一个独立的 `tempfile.TemporaryDirectory()` 里跑。有输入文件的话先写进这个目录，指令里明确要求后端在这个目录内用代码（python-docx/openpyxl）完成任务，产出文件也存这里。
2. 后端必须返回结构化 JSON：`{"reply_text": string, "filename": string}`，`filename` 为空字符串表示没有生成文件。两个后端各自的结构化输出机制不同——Claude Agent SDK 用 `ClaudeAgentOptions(output_format={"type": "json_schema", "schema": ...})`，Codex 用 `codex exec --output-schema`——但对上层暴露的都是同一个 `ExecutionResult` dataclass。
3. 调用方按 `filename` 去临时目录里读文件。**如果后端汇报的文件名在目录里找不到，直接抛错并把目录实际内容列出来**，不静默兜底——这条是踩出来的：Claude 曾经在 `reply_text` 里说完成了，`filename` 也给了，但实际保存的文件名跟汇报的不一致，早期实现直接 `read_bytes()` 导致裸 `FileNotFoundError`，现在改成先检查存在性、报错时带上目录实际内容，方便下次直接定位。

上层（orchestrator，等它接入之后）不需要知道、也不应该知道某个后端内部是怎么写代码完成任务的——只认"临时目录 + 结构化 JSON"这一层契约。加新的执行后端时，照这个协议实现 `ExecutionAgent.run()` 即可接入，不用改调用方代码。

## 平台适配层：回调风格 SDK 桥接到 async pull 接口

`platforms/base.py` 定义的 `PlatformAdapter` 接口是 pull 风格——调用方 `await adapter.receive()` 主动要下一条消息。但很多平台 SDK 提供的是回调风格的长连接/webhook（比如飞书 `lark-oapi` 的长连接客户端：注册一个同步回调函数，SDK 内部起一个阻塞的 `ws_client.start()`）。

`platforms/feishu.py` 的 `FeishuAdapter` 是这类桥接的参考实现：

- `ws_client.start()`（阻塞）丢进一个 daemon 线程里跑，不占用主 event loop
- 事件回调函数（`_on_message`）在收到消息时，用 `loop.call_soon_threadsafe(queue.put_nowait, event)` 把事件塞进一个 `asyncio.Queue`——`call_soon_threadsafe` 是必须的，因为回调本身跑在另一个线程，不能直接操作 asyncio 对象
- `loop` 引用要提前存好：`start()` 方法必须在 asyncio 事件循环内被调用一次，用 `asyncio.get_running_loop()` 拿到当前循环存下来，回调线程才有 loop 可用
- `receive()` 就是 `await queue.get()`

这个模式不是飞书专用的。以后任何"SDK 是回调/轮询风格，但我们的接口要求 async pull"的场景（比如后续如果接个人微信 `wxauto`，它的消息监听也不是天然 async 的）都可以照搬这个桥接方式，不用重新设计。
