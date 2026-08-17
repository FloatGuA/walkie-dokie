# 多文件执行会话设计

日期：2026-08-18
状态：已批准，待实现
关联：[PROGRESS.md](../../../PROGRESS.md)（"尚未验证"第 2 项：文件目前仍为单槽处理）、[TECHNICAL.md](../../../TECHNICAL.md)

## 问题

`orchestrator/debounce.py` 的 `Debouncer._files` 是单槽 `dict[key, IncomingFile]`：同一防抖窗口内用户连发多个文件，晚到的会静默覆盖早到的。这个单文件假设贯穿整条链路——`SessionState.pending_file`/`new_file`、`DialogueContext.input_filename`、`ExecutionAgent.run()` 的 `input_path: Path | None`、`ExecutionReport.artifact_path/result_filename`——全部是单值而非列表。

## 范围决策：本次只做多文件会话，不做"重任务"独立 Agent

用户最初提出两件事：(1) 多文件打包给执行 Agent 的编排逻辑；(2) 把"闲聊/文件处理/重任务"拆成不同 Agent。这两者是独立子系统。(1) 有明确缺口驱动、边界清晰；(2) 中"重任务"具体指什么尚未定义——现有 `ExecutionAgent`（Claude Agent SDK / Codex CLI）本身已是全功能 agentic 执行引擎，若"重"只是需要更长超时/更多轮次，调参即可，不需要新 Agent 类型。按 YAGNI 原则，**(2) 推迟到"重任务"的具体需求明确后再单独走一轮 brainstorming**，本次 spec 只覆盖 (1)。

## 已否决方向

- **AutoGen / CrewAI 作为编排框架**：AutoGen 的核心原语是 agent 间自由消息传递（group chat/debate），这正是本项目 v2 重构要替代掉的模式——旧架构自由消息传递导致"机器人身份误写入用户记忆"的线上问题。CrewAI 的 human-in-the-loop 是同步阻塞式，两者都没有本项目依赖的"进程重启后从 checkpoint 恢复确认中断状态机"能力（`AsyncSqliteSaver` + `interrupt()`/`Command(resume=...)` + `durability="sync"`）。换框架等于重新解决已攻克的难题，且沙箱安全边界与编排框架选择无关。保留 LangGraph 不变，只借鉴其"supervisor 路由到专职 worker"的思路（LangGraph 官方本身也有对应参考架构，不需要额外依赖）。
- **工作区创建提前到 collect/debounce 阶段**（文件一到就落工作区，而非先存 `var/inputs/` 暂存引用）：会在"确认前"产生不受 checkpoint 覆盖的外部副作用目录，正是 `durability="sync"` + started/report marker 当初要防的"checkpoint 落盘与外部副作用顺序竞争"问题。调研 AutoGen/CrewAI/OpenHands 后确认：三者都不需要应对"确认前/确认后"这种阶段区分（要么同步交互，要么工作区与容器生命周期直接绑定），本项目的可恢复确认中断状态机是自身差异化点，没有更成熟模式可抄。
- **用挂载（bind-mount）替代拷贝**（参考 OpenHands 的 `SANDBOX_VOLUMES`）：挂载会让 agent 能看到跟 host 共享/可寻址的路径结构，在"文档内容本身不可信、需要防 prompt injection"的场景里，"先校验、拷贝进隔离 workdir、不给 agent 任何指向 staging 区路径能力"比挂载更安全。继续用拷贝。

## 已批准设计

### 1. 输出基数：支持多输出

`ExecutionReport` 从 `artifact_path: Path | None` + `result_filename: str | None` 的哨兵对，改为：

```python
@dataclass(frozen=True)
class ExecutionArtifact:
    path: Path
    filename: str

@dataclass(frozen=True)
class ExecutionReport:
    summary: str
    artifacts: tuple[ExecutionArtifact, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # summary 非空字符串校验不变
        # warnings 是字符串 tuple 校验不变
        # 新增：每个 artifact.path.name == artifact.filename 且 path.is_file()
        # 新增：artifacts 内 filename 不允许重复
```

`artifacts=()` 直接表示"没有产出文件"，取代原来 `None`/`None` 哨兵对，是同一语义的自然推广，不引入新的空值约定。

### 2. Session 边界：严格等于一个防抖窗口，确认前追加合并进已有工作区

- `Debouncer._files: dict[key, list[IncomingFile]]`，`add()` 用 `append` 取代覆盖。
- 窗口到期即"封口"：`_on_ready` 回调收到的是这一窗口内的完整文件列表，不再等待更多文件。
- 若用户在确认前又发新文件（新一轮防抖触发新一次 `collect`），且当前仍是同一个未确认任务（`pending_instruction` 累积语义下的同一轮），则新文件 **合并进已有 `pending_files`**（不新建工作区概念，因为工作区本身要到 `prepare_execution` 才真正创建）；若是独立新任务，则是全新的 `pending_files`。
- 这与现有 `pending_instruction` 跨多条消息累积文字的机制是同一模式，只是把"文字累积"扩展到"文件累积"，不引入新的生命周期概念。

### 3. 校验失败策略：排除坏文件，其余继续

`agents/base.py` 新增共享函数（两个 backend 都调用，不再各自重复实现单文件校验+拷贝循环）：

```python
def stage_execution_inputs(
    refs: tuple[ArtifactReference, ...], workdir: Path
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """校验并拷贝输入到 workdir，返回 (拷贝后的路径列表, 排除文件的 warning 列表)。
    refs 非空但全部未通过校验时抛 RuntimeError，不进入 backend 调用。
    refs 为空（任务本身不带输入文件，如"从零生成一份请假条"）是正常情况，
    直接返回 ((), ())，不视为失败。
    """
```

- `refs` 为空（任务不需要输入文件，纯生成场景）：直接返回空结果，正常进入 backend 调用，不是错误路径——这与今天"无输入文件的 document_task"完全一致，不受本次改动影响。
- `refs` 非空时逐个文件 `validate_office_artifact`：通过的拷进 workdir（目标名见第 4 条），不通过的跳过并记一条 warning（说明文件名与原因）。
- `refs` 非空但全部未通过校验 → 直接 `RuntimeError`，不调用 backend/query。这个异常复用现有机制——业务异常被节点转换成完成态错误结果，不需要新的错误处理路径。

### 4. 文件名碰撞：`display_filename` 字段去重

`ArtifactReference` 新增可选字段：

```python
class ArtifactReference(TypedDict):
    kind: Literal["input", "output"]
    path: str
    filename: str                    # 物理文件名，与 path.name 强绑定，resolve_artifact_reference 的不变量不变
    display_filename: str | None     # None 表示等于 filename；仅同批次内文件名碰撞时才赋值去重（如 "报价单-2.xlsx"）
    mime_type: str
```

- 去重逻辑发生在 `collect` 节点合并 `pending_files` 时：新文件的（有效）文件名若与当前批次内已有文件相同，则赋值 `display_filename` 并按到达顺序追加递增数字后缀（第二个重复文件为 `报价单-2.xlsx`，第三个为 `报价单-3.xlsx`，以此类推；后缀基于扩展名前插入，不改变扩展名）。
- `MainAgent` 看到的 `DialogueContext.input_filenames` 与 execute 阶段拷入 workdir 的目标名，统一取 `display_filename or filename`。
- `filename` 字段本身不变，继续满足 `resolve_artifact_reference` 里 `path.name == filename` 的不变量，不影响物理存储。

### 5. 已知缺口：多文件投递部分失败，本次不处理

runner "先发文件、再发文字"的现有逻辑对 N 个文件顺序发送。若第 2/N 个文件投递失败，属于 PROGRESS.md P0 已排期的"持久 inbox/outbox、delivery ack/retry"缺口在多文件场景下的自然放大，不是新问题。本次不新增 ack/outbox 机制，避免与已排期项重复造轮子。

## 接口改动清单

| 文件 | 改动 |
|---|---|
| `orchestrator/debounce.py` | `_files` 单值→`list`；`add()` 覆盖→`append`；`on_ready` 回调签名文件参数改为列表 |
| `artifacts.py` | `ArtifactReference` 新增 `display_filename: str \| None` |
| `main_agent/base.py` | `DialogueContext.input_filename: str \| None` → `input_filenames: tuple[str, ...] = ()`；`TaskContract` 不变（`instruction` 仍自包含自由文本，不记录对应文件列表，执行时直接从 graph state 的 `pending_files` 取） |
| `agents/base.py` | `ExecutionReport.artifact_path/result_filename` → `artifacts: tuple[ExecutionArtifact, ...]`；`ExecutionAgent.run()` 的 `input_path: Path \| None` + `input_filename: str \| None` → `input_paths: tuple[Path, ...]` + `input_filenames: tuple[str, ...]`（一一对应，保持现有"path/filename 分开传"风格）；新增 `stage_execution_inputs()` 共享函数 |
| `agents/claude_agent.py` / `agents/codex_agent.py` | 改调用 `stage_execution_inputs()`，删除各自重复的单文件校验+拷贝循环；prompt 里说明工作目录下有 N 个输入文件 |
| `orchestrator/graph.py` | `SessionState.pending_file`/`new_file` → `pending_files: tuple[ArtifactReference, ...]`；`collect` 节点做合并+去重；`use_previous_artifact` 语义扩展为拉取前一轮全部产出（可能是 M 个文件） |
| 平台投递（`scripts/run_mvp.py` 或对应 runner） | 文件发送从单文件循环改为对 `artifacts` 列表循环，逻辑不变只是基数变化 |

## 测试策略

沿用 `superpowers:test-driven-development`——每个改动点先写失败测试，再写实现代码，不保留测试前预写的代码；延续本项目现有的 110 个离线契约测试路数（fake agent/临时目录，不联网，不依赖真实飞书/DeepSeek/Claude/Codex）。覆盖面：

1. **Debouncer**：多文件累积（断言 `append` 而非覆盖）；窗口重置行为不受影响。
2. **collect 节点**：`pending_files` 合并语义（同任务追加 vs 新任务新建）；`display_filename` 去重触发条件（含"不碰撞时不设置"的负向用例）。
3. **`stage_execution_inputs()`**：全部通过 / 部分失败排除+warning / 全部失败抛 `RuntimeError` 且不调用 backend，三条路径独立测试；碰撞去重后的目标名正确落地到 workdir。
4. **`ExecutionReport` 新不变量**：`artifacts` 内 filename 不重复、每个 `path.name == filename`、`artifacts=()` 合法代表无产出。
5. **两个 backend 的 fake 契约测试**：签名改多文件后，fake query（Claude）/fake subprocess（Codex）用例覆盖 N 输入 → M 输出组合，包括 N=1（回归现有单文件场景）、N>1 全通过、N>1 部分排除。
6. **`use_previous_artifact` 拉取多个前次产物**：前一轮产出 M 个文件时，"继续修改刚才的文件"应把 M 个全部带入下一轮 `pending_files`。
7. **runner 多文件顺序投递**：现有单文件发送测试模式扩展为循环断言，不新增 ack/outbox 相关测试（该机制本身不在本次范围）。

## 明确排除的范围

- "闲聊/文件处理/重任务"三分 Agent 架构——推迟到"重任务"具体需求明确后单独 brainstorming。
- 多文件投递的 ack/outbox 机制——已在 PROGRESS.md P0 排期，本次不重复设计。
- 工作区挂载（mount）方案——已否决，见上文。
