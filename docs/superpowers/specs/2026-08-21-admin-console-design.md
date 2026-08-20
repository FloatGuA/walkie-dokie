# Admin 观测台（只读运维后台）设计

日期：2026-08-21
状态：已与用户逐项对齐定稿（5 个决策点用户拍板）
关联：DECISION.md 2026-08-21 定稿条目；数据源为既有仪器（turns.jsonl / model_calls.jsonl / var/memory / checkpoints-v2.db / var/evals）。

## 目的

把散落的观测仪器（回合留痕、成本记账、记忆档案、对话摘要、eval 报告）收进一个本机 web 控制台，供开发者日常监控与排障。类比 OpenClaw 的后台形态，但 V1 严格只读。

## 已拍板决策

| # | 决策点 | 拍板 | 被否方案及原因 |
|---|--------|------|----------------|
| 1 | 目的定位 | 开发者自己的运维观测台 | 家人/管理员配置面板（依赖尚未存在的多用户体系与对外前置项，建在沙上）；分期预留（YAGNI，二期真需要时再扩） |
| 2 | V1 范围 | 四板块全要：对话回合流 / 成本仪表 / 记忆与摘要 / eval 报告；**纯只读** | V1 带可写配置——可写配置是 golden 回归纪律的旁路（UI 改词表/阈值/prompt 没有 TDD/review/回归），"哪些可写 + 改完强制过回归"的机制值得单独二期设计，不预先猜配置项 |
| 3 | 配置与回归关系 | 随只读拍板整体挪二期 | — |
| 4 | 技术形态 | **用户拍板**：FastAPI + uvicorn（optional extra `admin`，不影响 bot 本体安装）；单文件内联前端，fetch 轮询 10s | stdlib http.server（零依赖但二期扩展别扭，用户选生态）；静态生成 HTML（无后台体验，与诉求不符） |
| 5 | 安全边界 | host 写死 `127.0.0.1`、无鉴权——本机开发工具 | 对外暴露/鉴权体系（对外开放前置项，与 API key/enforcement 同期） |

## 结构

```text
src/walkie_dokie/admin/
  app.py        # FastAPI 实例、只读 JSON 端点、GET / 返回内联 index.html
  data.py       # 数据读取层：纯函数为主，独立可测，不 import app
  index.html    # 单文件前端：4 tab、内联 CSS/JS、fetch 轮询 10s、无外部资源
scripts/run_admin.py   # uvicorn 入口：host=127.0.0.1 写死、--port 默认 8788、__main__ guard
pyproject: [project.optional-dependencies] admin = ["fastapi", "uvicorn"]
```

`fastapi`/`uvicorn` 只能在 admin 模块内 import（optional extra 纪律，照 claude_agent_sdk 的 lazy import 先例：不装 admin extra 时 bot 与标准 pytest 完全不受影响；admin 相关测试在依赖缺失时 skip——用 `pytest.importorskip`）。

## 端点（全只读；无任何 POST/PUT/DELETE）

| 端点 | 数据源 | 返回 |
|---|---|---|
| `GET /` | index.html | 控制台页面 |
| `GET /api/turns?limit=50&user=` | turns.jsonl 尾部 | 回合列表（倒序）：timestamp/platform/user_id/record_type/输入输出（截断展示长度）/duration_ms/success/error/run_id(trace_id)；`user` 为空不筛 |
| `GET /api/costs?days=7` | model_calls.jsonl | **复用 `scripts.report_costs.aggregate` 纯函数**（DRY，不复制聚合逻辑）：totals/按日×purpose/按用户 + 金额上界估算与免责说明 |
| `GET /api/memory` | var/memory/*.json + checkpoints-v2.db | 每用户：4 字段档案；对应 thread 最新 checkpoint 里的 conversation_summary 条目（fact+evidence）与 pending_compaction 条数 |
| `GET /api/evals` | var/evals/*.json 文件列表 | 历次运行摘要（文件名/status/mode/summary/git_commit），按时间倒序 |
| `GET /api/evals/{name}` | 单报告 | 逐样本结果与 judge 明细；`name` 必须匹配 `^\d{8}T\d{6}Z\.json$`（防路径穿越，机械校验） |

## checkpoint 只读访问（关键技术点）

- 用 `sqlite3.connect("file:...checkpoints-v2.db?mode=ro", uri=True)` 只读连接；**绝不经过 graph、绝不写**。
- thread 列表：`SELECT DISTINCT thread_id FROM checkpoints`；每 thread 取最新 checkpoint 行，反序列化出 `conversation_summary` / `pending_compaction` 字段。反序列化格式以 langgraph `SqliteSaver` 实际存储为准（JSON/msgpack 由实现时读表结构确认，spec 不猜；若 langgraph 提供同步只读读取 API 且不加新依赖，允许用它替代裸 SQL——实现时二选一并记录理由）。
- bot 同时运行时的并发：只读连接对 WAL/journal 模式安全；db 文件不存在或表不存在时返回空列表（观测台对"还没跑过 bot"的机器要能空态展示）。

## 前端

- 单 index.html，4 个 tab（回合/成本/记忆/eval），fetch 对应端点，10s 轮询刷新，顶部显示最后刷新时间。
- 成本板块图表遵循项目 dataviz 规范：固定 5 色序（#2a78d6/#eb6834/#1baf7a/#eda100/#e87ba4）按 purpose 固定映射、单 y 轴、堆叠段 2px 间隔、图例+柱顶直接标签、文字用文本色；eval 趋势为两张分开的单系列小图（passed 率、clarity 均值），不做双轴。
- 浅色单主题（本机开发工具，刻意单一外观），背景显式 #fcfcfb；无外部资源（字体/CDN 一律不引）。

## 错误语义

- 数据源文件缺失/空 → 对应板块空态（"暂无数据"），不是 500——数据源是外部产物，这是系统边界，宽容处理合理。
- 数据行损坏（坏 JSON 行）→ 跳过该行并在响应里带 `skipped_lines` 计数（观测台要诚实报告自己跳过了多少）。
- `checkpoints-v2.db` 读取异常（锁/格式）→ 该板块返回错误说明字段，其余板块不受影响。
- 端点自身 bug → 正常 500（fail fast，不吞）。

## 测试

- `data.py` 纯函数离线单测：正常/空文件/缺文件/坏行（skipped_lines）/user 筛选/limit；checkpoint 读取用真实 `SqliteSaver` 写一个 tmp db 再只读读出（不联网，langgraph 已是核心依赖）。
- 端点：FastAPI TestClient（httpx 已有），monkeypatch 数据路径常量到 tmp；`/api/evals/{name}` 的路径穿越拒绝有专门测试。
- admin 依赖缺失环境：`pytest.importorskip("fastapi")` 保证套件在未装 extra 时仍全绿。
- 验收：真实启动 `python3 -m scripts.run_admin`，curl 四个端点 + 用户浏览器目检。

## 明确不做（YAGNI）

- 任何写端点/配置改动能力（二期，连同"改配置强制过 golden 回归"机制一起设计）。
- 鉴权、HTTPS、非 localhost 绑定（对外开放前置项）。
- websocket/实时推送（轮询够用）、用户管理、dark mode、持久化服务（手动起停）。

## 已知代价

- 轮询读文件全量解析（turns/model_calls 增长后接口变慢）——单人数据量下可忽略，文件超过量级再谈索引/增量。
- checkpoint 反序列化耦合 langgraph 存储格式——langgraph 升级可能破坏记忆板块（只影响观测台，不影响 bot；测试用真实 SqliteSaver 写读，升级时测试会红）。
