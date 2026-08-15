# walkie-dokie — Worklog

---

## #1 · 2026-08-14 19:08 — 打通第二份 XLS 工程量清单与同甲方跨项目相似报价检索

**触发原因**：用户要求边做边留痕，且本次上下文较长

### 概述

本次将合同智能 MVP 明确拆为暂停新增开发的 PDF/Word Agentic RAG 链路 A，以及当前主推的 Excel 工程量清单结构化链路 B。第二份华润商业泛光照明旧版 XLS 已通过显式 profile 完成 Evidence、金额闭合校验和正式 Staging 入库；Admin 新增同甲方归属、排除当前项目的模糊历史报价搜索。真实数据验证默认 10% 容差可命中相似灯带，9% 不命中，Trusted-only 不会泄漏 Staging；全量离线测试 153 passed。

### 改动清单

- `src/walkie_dokie/contract_intelligence/`、`migrations/`、Admin templates：新增 XLS parser/profile、甲方原名与归属字段、确定性 BOQ 导入、跨项目模糊检索和证据展示；同时收口 QuestionRun、固定 IndexBuild 快照等首轮 review 问题。
- `tests/contract_intelligence/`：重组合同智能测试并覆盖 XLS 路由、BOQ 导入约束、单位归一、数值容差、权限/状态门禁、Admin 页面和审计失败路径。
- `scripts/run_contract_feishu.py`、`pyproject.toml`：补齐飞书异常日志与同会话串行化，加入 `xlrd`、`RapidFuzz` 依赖，并将 Django pytest 初始化限制在合同测试目录。
- `DECISION.md`、`PITFALLS.md`、`PROGRESS.md`、`TECHNICAL.md`、`README.md`：同步匹配边界、旧版 XLS 公式限制、真实导入结果、稳定数据流和 Admin 使用方式。

### 决策与背景

跨项目查询按人工维护的 `party_a_group` 隔离，同时保留原文件 `party_a_name`；名称、型号、规格采用模糊评分，单位归一后硬匹配，用户明确指定的功率/色温默认按 ±10% 硬过滤且允许调整。Admin 可显式纳入 Staging 供调试，未来飞书只允许 Trusted。旧版 XLS 的公式缓存不能冒充公式重算，因此所有记录仍进入 Staging，并依靠叶子明细、成本分解和税额独立闭合。

### 未完成 / 待跟进

- 两个项目共 413 条 BOQ 明细仍需人工审核后才能提升为 Trusted。
- 相似报价目前只有 Admin 页面，尚未接入飞书；当前结果只表达历史事实和差异，不提供推荐价。
- 需要更多真实工程量清单样本验证/新增 profile；PDF/Word 链路 A 的 OCR、Dense、Reranker 和可观测平台仍暂停。
- 本轮改动尚未创建新的 Git commit。
