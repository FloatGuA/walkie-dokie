# Admin 观测台（只读）Implementation Plan

> **状态：✅ 已于 2026-08-21 全部执行完毕**（subagent-driven；final review 后修 evidence 数组渲染/error 列/Host 校验等 7 项；真实验收通车，`pytest` 398 passed）。留档备查，不要重复执行；与实现不一致处以代码与 spec 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 本机只读 web 控制台：四板块（回合流/成本/记忆+摘要/eval 报告）收拢既有观测仪器，FastAPI + 单文件前端，127.0.0.1 无鉴权。

**Architecture:** `src/walkie_dokie/admin/data.py` 数据读取层（纯函数，坏行宽容 + skipped_lines 诚实计数；checkpoint 经只读 SQLite 连接 + langgraph 官方 serde 反序列化）；`app.py` FastAPI 只读端点绑定 data 层（路径为模块常量供测试 monkeypatch）；`index.html` 内联一切的 4-tab 前端 10s 轮询；`scripts/run_admin.py` uvicorn 入口 host 写死 127.0.0.1。fastapi/uvicorn 为 optional extra `admin`，import 全部限制在 admin 模块与入口内，未装 extra 时 bot 与标准 pytest 不受影响（admin 测试 `pytest.importorskip`）。

**Tech Stack:** FastAPI + uvicorn（新 optional extra，**本机尚未安装：Task 1 Step 0 先 `pip install -e ".[admin]"`**）；TestClient 走 httpx（已有）；langgraph-checkpoint-sqlite 的同步 `SqliteSaver`（已有依赖）。

**Spec:** `docs/superpowers/specs/2026-08-21-admin-console-design.md`

## Global Constraints

- **全只读**：无任何 POST/PUT/DELETE 端点；checkpoint 用 `sqlite3.connect("file:...?mode=ro", uri=True)` 只读连接，绝不经 graph、绝不写。
- `fastapi`/`uvicorn` 只能在 `src/walkie_dokie/admin/` 与 `scripts/run_admin.py` 内 import；admin 测试文件顶部 `pytest.importorskip("fastapi")`。
- host 写死 `"127.0.0.1"`，不提供改绑参数。
- 错误语义：数据源缺失/空 → 空态响应非 500；坏 JSON 行 → 跳过并计 `skipped_lines`；checkpoint 读取异常 → 该板块响应带 `checkpoint_error` 字段其余不受影响；端点自身 bug → 正常 500 不吞。
- `/api/evals/{name}` 的 `name` 必须 fullmatch `^\d{8}T\d{6}Z\.json$`，否则 404（防路径穿越，有专门测试）。
- 前端：无外部资源；固定 5 色序 `#2a78d6/#eb6834/#1baf7a/#eda100/#e87ba4` 按 purpose 固定映射；单 y 轴；文字用文本色 `#1f1f1e`/`#6b6a63`；背景 `#fcfcfb` 浅色单主题。
- 复用不复制：成本聚合必须调 `scripts.report_costs.aggregate(records, days, *, now=None)`（写代码前先读 `report_costs.py` 确认 records 的输入形状与现有 loader 是否可复用）。
- TDD。当前全量基线 **341 passed**（未装 fastapi 时 admin 测试应 skip 而非 fail）。
- commit trailer 按执行时 harness 规则。

---

### Task 1: 依赖 + data.py 回合流与成本读取

**Files:**
- Modify: `pyproject.toml`（optional-dependencies 加 `admin = ["fastapi", "uvicorn"]`）
- Create: `src/walkie_dokie/admin/__init__.py`（空）、`src/walkie_dokie/admin/data.py`
- Test: `tests/test_admin_data.py`（新；本任务的 data 函数不 import fastapi，无需 importorskip）

**Interfaces:**
- Produces（Task 4 依赖的精确签名）:

```python
def read_turns(path: Path, *, limit: int = 50, user: str | None = None) -> dict:
    # {"turns": [dict, ...], "skipped_lines": int}
    # turns 按文件顺序的倒序（最新在前）；user 非空时按 user_id 精确匹配过滤后再取 limit；
    # 文件缺失 → {"turns": [], "skipped_lines": 0}

def read_costs(path: Path, *, days: int = 7) -> dict:
    # {"aggregate": <scripts.report_costs.aggregate 的返回>, "skipped_lines": int,
    #  "disclaimer": "金额为保守上界估算，对账以控制台账单为准"}
```

- [ ] **Step 0: 安装 admin 依赖**

Run: `pip install -e ".[admin]"`（先在 pyproject 加 extra 再装）；`python3 -c "import fastapi, uvicorn; print('ok')"` 确认。

- [ ] **Step 1: 写失败测试**

`tests/test_admin_data.py`：

```python
import json
from pathlib import Path

from walkie_dokie.admin.data import read_costs, read_turns


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write((row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)) + "\n")


def test_read_turns_newest_first_with_limit_and_filter(tmp_path):
    path = tmp_path / "turns.jsonl"
    _write_jsonl(path, [
        {"timestamp": "t1", "user_id": "u1", "output_text": "a"},
        {"timestamp": "t2", "user_id": "u2", "output_text": "b"},
        {"timestamp": "t3", "user_id": "u1", "output_text": "c"},
    ])
    result = read_turns(path, limit=2)
    assert [t["timestamp"] for t in result["turns"]] == ["t3", "t2"]
    filtered = read_turns(path, limit=10, user="u1")
    assert [t["timestamp"] for t in filtered["turns"]] == ["t3", "t1"]
    assert result["skipped_lines"] == 0


def test_read_turns_skips_bad_lines_and_reports_count(tmp_path):
    path = tmp_path / "turns.jsonl"
    _write_jsonl(path, [{"timestamp": "t1", "user_id": "u1"}, "不是 JSON{", {"timestamp": "t2", "user_id": "u1"}])
    result = read_turns(path)
    assert len(result["turns"]) == 2
    assert result["skipped_lines"] == 1


def test_read_turns_missing_file_is_empty_state(tmp_path):
    result = read_turns(tmp_path / "absent.jsonl")
    assert result == {"turns": [], "skipped_lines": 0}


def test_read_costs_reuses_aggregate_and_reports_disclaimer(tmp_path):
    path = tmp_path / "model_calls.jsonl"
    _write_jsonl(path, [
        {"timestamp": "2026-08-21T10:00:00", "provider": "deepseek", "model": "deepseek-chat",
         "purpose": "decide", "platform": "test", "user_id": "u1",
         "prompt_tokens": 100, "completion_tokens": 20, "duration_ms": 500},
    ])
    result = read_costs(path, days=7)
    assert result["aggregate"]["totals"]["calls"] == 1
    assert "上界" in result["disclaimer"]
    assert read_costs(tmp_path / "absent.jsonl")["aggregate"]["totals"]["calls"] == 0
```

（`aggregate` 的 totals 键名以 `scripts/report_costs.py` 实际返回为准——写断言前先读它；`read_costs` 缺文件时传空 records 给 aggregate 得到零值聚合，而不是自造空壳。测试里 timestamp 若 aggregate 按当前时钟过滤会跨日失效——用 `aggregate(records, days, now=...)` 的 now 参数注入固定时钟，或测试里动态生成今天的 timestamp，二选一并保持确定性。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_admin_data.py -v` → FAIL（ModuleNotFoundError walkie_dokie.admin）。

- [ ] **Step 3: 实现**

`data.py` 顶部只 import 标准库 + `scripts.report_costs`（不 import fastapi）。`read_turns`/`read_costs` 共用一个内部 `_read_jsonl(path) -> tuple[list[dict], int]`（逐行 json.loads，异常行计数跳过）。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `python3 -m pytest tests/test_admin_data.py -v && python3 -m pytest -q` → 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/walkie_dokie/admin/ tests/test_admin_data.py
git commit -m "feat: admin data layer for turns and costs panels"
```

---

### Task 2: data.py 记忆与 checkpoint 读取

**Files:**
- Modify: `src/walkie_dokie/admin/data.py`
- Test: `tests/test_admin_data.py`（追加）

**Interfaces:**
- Produces:

```python
def read_memory(memory_dir: Path, checkpoint_db: Path) -> dict:
    # {"users": [{"platform": str, "user_id": str, "profile": dict,
    #             "summary": [{"fact","evidence"}...], "pending_compaction": int}],
    #  "checkpoint_error": str | None}
```

要点：
- 档案：扫 `memory_dir` 下 `v2_*.json`（形状先读 `main_agent/memory.py` 的 `_path`/存储格式确认；platform/user_id 从文件名段还原可能有 hash——若无法逆向还原，users 键以文件内容或文件名原样展示并在报告里说明取舍）。
- checkpoint：`sqlite3.connect(f"file:{checkpoint_db}?mode=ro", uri=True)`；thread 列表 `SELECT DISTINCT thread_id FROM checkpoints`；每 thread 用**同步 `SqliteSaver`（langgraph-checkpoint-sqlite 已有依赖）包住这个只读连接**调 `get_tuple({"configurable": {"thread_id": ...}})`，从 `checkpoint["channel_values"]` 取 `conversation_summary`/`pending_compaction`——用官方 serde 反序列化而非手撸 msgpack（理由：不脆；spec 允许的二选一，报告里记录）。API 形状以已装 langgraph 版本实际为准，写代码前先在 python REPL 试一遍读真实 `var/checkpoints-v2.db`。
- db 缺失/表缺失 → users 里只有档案信息、`checkpoint_error=None`（这不是错误是空态）；连接/读取真异常 → `checkpoint_error=str(exc)`，档案部分照常返回。
- 档案与 checkpoint 的用户并集展示：有档案没摘要、有摘要没档案都要出现。

- [ ] **Step 1: 写失败测试**

追加（真实 SqliteSaver 写 tmp db 再只读读出；构造 checkpoint 的最省路径：用 `build_graph` + `AsyncSqliteSaver`（`langgraph.checkpoint.sqlite.aio`）跑一轮 fake reply（fake main agent 照 test_graph 模式），预置 `conversation_summary` 经 invoke 输入注入——参考 test_graph 里预置 state 的既有做法；本机非受限 sandbox，aiosqlite 可用）：

```python
async def test_read_memory_merges_profiles_and_checkpoint_summary(tmp_path):
    # 1) JsonMemoryRepository(tmp_path/"memory").apply(...) 写一个用户档案（照 test_run_mvp 既有用法）
    # 2) build_graph(fake main agent, fake exec, memory, checkpointer=AsyncSqliteSaver(tmp db)) 跑一轮
    #    reply，invoke 输入带 conversation_summary=[{"fact": "孙女叫小雨", "evidence": ["我孙女小雨"]}]
    #    与 pending_compaction=[2 条消息]
    # 3) read_memory(tmp_path/"memory", tmp db 路径)
    # 断言：该用户 profile 含写入字段；summary == 预置条目；pending_compaction == 2；checkpoint_error is None

def test_read_memory_without_db_returns_profiles_only(tmp_path):
    # 只有档案无 db → users 有档案条目、summary 空、checkpoint_error None

def test_read_memory_db_error_is_reported_not_raised(tmp_path):
    # checkpoint_db 指向一个非 sqlite 的垃圾文件 → checkpoint_error 非 None、档案照常
```

（断言写全，不许空转；AsyncSqliteSaver 的用法先读其 docstring/签名。）

- [ ] **Step 2: RED** → AttributeError（read_memory 不存在）。

- [ ] **Step 3: 实现**（按要点）。

- [ ] **Step 4: 跑测试确认通过 + 全量回归** → 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/walkie_dokie/admin/data.py tests/test_admin_data.py
git commit -m "feat: admin data layer reads memory profiles and checkpoint summaries read-only"
```

---

### Task 3: data.py eval 报告读取

**Files:**
- Modify: `src/walkie_dokie/admin/data.py`
- Test: `tests/test_admin_data.py`（追加）

**Interfaces:**
- Produces:

```python
_EVAL_NAME_RE = re.compile(r"^\d{8}T\d{6}Z\.json$")

def list_eval_reports(evals_dir: Path) -> dict:
    # {"reports": [{"name","status","mode","summary","git_commit"}...]} 按 name 倒序（即时间倒序）
def read_eval_report(evals_dir: Path, name: str) -> dict:
    # 非法 name（不 fullmatch _EVAL_NAME_RE）→ raise ValueError；不存在 → raise FileNotFoundError
```

- [ ] **Step 1: 写失败测试**

```python
def test_list_eval_reports_newest_first(tmp_path): ...
    # 写两个合法名字的报告 json（status/mode/summary/git_commit 字段），断言倒序与字段透传
def test_read_eval_report_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        read_eval_report(tmp_path, "../../etc/passwd")
    with pytest.raises(ValueError):
        read_eval_report(tmp_path, "20260820T111441Z.json.bak")
def test_list_eval_reports_empty_dir(tmp_path): ...
```

- [ ] **Step 2: RED** → **Step 3: 实现** → **Step 4: 全量回归 PASS** → **Step 5: Commit**

```bash
git add src/walkie_dokie/admin/data.py tests/test_admin_data.py
git commit -m "feat: admin data layer lists and reads eval reports safely"
```

---

### Task 4: FastAPI app + 入口脚本

**Files:**
- Create: `src/walkie_dokie/admin/app.py`、`scripts/run_admin.py`
- Test: `tests/test_admin_app.py`（新，顶部 `pytest.importorskip("fastapi")`）

**Interfaces:**
- Consumes: Task 1-3 的全部 data 函数。
- Produces:

```python
# app.py 模块常量（测试 monkeypatch 这些）：
TURNS_PATH / MODEL_CALLS_PATH / MEMORY_DIR / CHECKPOINT_DB / EVALS_DIR / INDEX_HTML_PATH
def create_app() -> "FastAPI":
    # GET /            → FileResponse(INDEX_HTML_PATH)；文件缺失 404
    # GET /api/turns   → read_turns(TURNS_PATH, limit=query, user=query)
    # GET /api/costs   → read_costs(MODEL_CALLS_PATH, days=query)
    # GET /api/memory  → read_memory(MEMORY_DIR, CHECKPOINT_DB)
    # GET /api/evals   → list_eval_reports(EVALS_DIR)
    # GET /api/evals/{name} → read_eval_report(...)；ValueError→404、FileNotFoundError→404
```

`run_admin.py`：docstring 写用法；argparse `--port`（默认 8788）；`uvicorn.run(create_app(), host="127.0.0.1", port=args.port)`；`__main__` guard；确认 pytest 不收集。

- [ ] **Step 1: 写失败测试**

`tests/test_admin_app.py`（TestClient；monkeypatch 六个常量到 tmp；每端点一条正常 + 空态；`/api/evals/../x` 与非法名 404；断言响应 JSON 形状与 data 层一致；`GET /` 在写入临时 index.html 后返回 200 且 content-type text/html）。测试代码按上述语义写全。

- [ ] **Step 2: RED** → **Step 3: 实现** → **Step 4: 全量回归**（另跑一次 `python3 -m pytest -q -p no:cacheprovider` 确认 scripts/run_admin.py 未被收集）→ **Step 5: Commit**

```bash
git add src/walkie_dokie/admin/app.py scripts/run_admin.py tests/test_admin_app.py
git commit -m "feat: read-only admin API and localhost-only entrypoint"
```

---

### Task 5: index.html 前端

**Files:**
- Create: `src/walkie_dokie/admin/index.html`
- Test: `tests/test_admin_app.py`（追加 smoke）

**要求（dataviz 规范照 Global Constraints）：**
- 4 tab：回合（表格：时间/用户/输入输出截断/耗时/成败徽标/trace_id，user 筛选框 + limit）；成本（stat 行：估算金额/总调用/总 tokens + 按日×purpose 堆叠柱状图 + 免责一行）；记忆（每用户卡片：4 字段档案表 + 摘要条目列表 fact 加粗 evidence 灰字 + pending 计数 + checkpoint_error 展示）；Eval（运行列表表格 + passed 率与 clarity 两张分开的单系列折线小图 + 点开单报告的逐样本表）。
- 10s `setInterval` 轮询当前 tab 的端点；顶部"最后刷新 HH:MM:SS"；fetch 失败在页内显示错误条不白屏。
- 堆叠图：柱细、段间 2px 背景间隔（从段顶让出——项目已踩过从底裁的坑）、图例 + 柱顶总量直接标签；`skipped_lines > 0` 时在板块角落显示"跳过 N 行损坏数据"。
- 空态：每板块"暂无数据"。
- smoke 测试：读文件断言含 4 个 tab 标记、5 个色号字符串、无 `http://`/`https://` 外部引用（`file.read()` 字符串断言即可）。

- [ ] **Step 1: smoke 测试先行（RED：文件不存在）** → **Step 2: 写 index.html** → **Step 3: 全量回归 PASS** → **Step 4: Commit**

```bash
git add src/walkie_dokie/admin/index.html tests/test_admin_app.py
git commit -m "feat: single-file admin console frontend"
```

---

### Task 6: 真实验收 + 文档 + push

**Files:**
- Modify: `README.md`（安装段提 `.[admin]`、新"运行 Admin 观测台"一节）、`PROGRESS.md`、`docs/agent-system-self-check.md`（复查记录）

- [ ] **Step 1: 真实启动验收**

```bash
python3 -m scripts.run_admin --port 8788 &   # 或另终端
curl -s localhost:8788/api/turns | head -c 300
curl -s localhost:8788/api/costs | head -c 300
curl -s localhost:8788/api/memory | head -c 300
curl -s localhost:8788/api/evals | head -c 300
curl -s localhost:8788/ | head -c 200
```

四个 API 返回真实数据（本机已有 turns/model_calls/evals 数据；memory 板块读真实 `var/checkpoints-v2.db`——eval 跑过的 thread 应能看到 summary/pending）。确认后关掉服务。浏览器目检留给用户。

- [ ] **Step 2: 文档**（README 两处；PROGRESS 已验证条目 + 时间戳；自查表复查记录一行。措辞按既有风格。）

- [ ] **Step 3: Commit + push**

```bash
git add README.md PROGRESS.md docs/agent-system-self-check.md
git commit -m "docs: record admin console v1 (read-only) completion"
git push origin master
```

---

## Self-review

- **Spec coverage**：结构与 extra（T1）、六端点（T1-T4）、checkpoint 只读+官方 serde（T2）、路径穿越校验（T3）、前端四板块与 dataviz 规范（T5）、错误语义（T1 坏行/T2 checkpoint_error/T3 ValueError→404/T4 500 不吞）、importorskip（T4/T5 测试）、127.0.0.1 写死（T4）、验收与文档（T6）。"明确不做"未越界。
- **Placeholder scan**：T2/T4/T5 部分测试以语义清单给出（依赖现场 API 形状与既有测试模式，plan 已给核查指令与断言语义）；无 TBD。
- **Type consistency**：data 层五个函数签名 T1-T3 定义、T4 消费；六个路径常量 T4 定义、测试 monkeypatch；`_EVAL_NAME_RE` T3 定义 T4 经异常映射消费。
- **风险注记**：T2 的 checkpoint 读取是全 plan 最不确定点（langgraph API 形状），已内置"先 REPL 试真实 db 再写"指令；aiosqlite 在本机（WSL 非受限 sandbox）可用，PITFALLS 的 self-pipe 坑不适用。
