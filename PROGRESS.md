# walkie-dokie — Progress

## 状态快照

骨架阶段。目录结构、包配置、核心 State 定义、README/DECISION 已就位；尚无任何 Agent/图的实际逻辑代码，尚未 git init。

## 待处理 / 下一步

- 从「第 1-2 周」路线图中选定第一个可验证的最小单元开始写代码（具体切入点待定）
- git init + 首次提交
- 路线图（来自规划对话 [存档](https://claude.ai/share/14e91185-54c3-4b48-b860-dc11d6dfc690)，按周压缩）：
  - **第 1-2 周**：企业微信接入 + ASR + LangGraph 单图跑通"报修工单"一条闭环 + 3 个工具，能用手机对着说话就出单
  - **第 3-4 周**：澄清 Agent（记忆补全 + 单轮最大信息增益提问 + ≤2 轮追问）+ 三层记忆 + HITL 审批 + 知识 RAG（保洁规范/家电说明书）
  - **第 5-6 周**：A2A 拆出家政平台 Agent + n8n 定时任务（排班推送/超时告警/结算导出）+ Langfuse trace
  - **第 7 周**：eval 数据集（150-200 条模拟语音，含方言/错字/多意图/省略主语）+ 指标表（意图准确率/槽位F1/澄清轮数/任务完成率/危险操作误触发率/成本/P95延迟）+ README + 2 分钟录屏 demo

## 进行中

（无）

## 已完成

- **项目骨架搭建**（2026-08-07，未验证：仅本地文件与 `pip install -e .` 可用，无功能可测）
  - 目录结构：`src/walkie_dokie/{graphs,agents,tools,memory,integrations}`，`eval/dataset`，`n8n`，`docs`，`tests`
  - `src/walkie_dokie/state.py`：`WorkOrderState`（TypedDict，字段 raw_input/normalized/missing_slots/clarify_rounds/order_draft/risk_level/approval）
  - `pyproject.toml`（src layout 配置）—— 已验证 `pip install -e . --no-deps` 成功且 `import walkie_dokie` 通过
  - `.gitignore`、`README.md`、`DECISION.md`
