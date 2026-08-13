# High-Precision Contract Intelligence Agent — 设计决策

> 状态：讨论中，边讨论边更新
> 首次记录：2026-08-13
> 约定：只有双方已经明确确认的内容进入“已确定”；尚未逐项讨论或仍需样例验证的内容进入“待验证/待讨论”，不把建议提前写成结论。

## 目标与硬约束（已确定）

- 建设长期合同知识库，合同和价目表可能持续更新，必须支持历史版本和当前发布版本。
- 第一版聚焦高精度事实问答、精确价格查询和确定性计算；暂不做风险审查、合同比较等分析型能力，但模块边界要允许后续扩展。
- 准确率优先于速度。关键事实必须有原始证据；证据不足时拒答，条件存在可消除的歧义时先澄清。
- 输出至少包含 `answer + evidence + document + page/clause + calculation + confidence`；PDF 引用同时返回查看器物理页码和正文印刷页码。
- LLM、Embedding、Reranker、Parser 都必须通过 Provider 接口可替换，以支持 AB Test。LLM 暂定使用 DeepSeek；这不意味着 Embedding 和 Reranker 绑定 DeepSeek。
- 第一版建立 100 条人工确认的高质量 Golden QA，包含可回答、需澄清和应拒答的样本。解析器另建页面/表格级 benchmark，不能只用最终 QA 指标代替解析质量评估。
- 面向可信使用，但按多用户设计。需要项目、查询上下文和长期 memory 的基础隔离，不建设复杂 RBAC。

## 开发策略：真实样例驱动的纵向 MVP（已确定）

- 当前设计只锁定跨数据形态仍成立的信任边界和模块边界；不得在没有真实 DOCX/XLSX/PDF 样例时实现想象中的通用平台。
- 先用一组代表性真实文件跑通“管理员上传 → 原生解析/PDF 对齐 → 可视检查 → 合同检索/价格查询 → Verifier → 飞书带证据回答或拒答”的最窄纵向闭环。
- MVP 不是一次性 throwaway：保留稳定 Provider、版本、Evidence 和 trace 契约，但每个 Provider 先只实现一个 baseline，数据 schema 只覆盖样例中真实存在的结构。
- 第一个价目表允许使用样例专用、版本化的 MappingSpec；至少观察 2～3 种真实格式后，才抽象通用 MappingSpec DSL 和管理 UI。
- MVP 先从真实文档人工标注约 20～30 条高价值 QA/拒答样本用于快速回归；第一版正式验收前再扩展到已经确定的 100 条 Golden Dataset。
- 每一轮新增能力必须由失败样例驱动，并加入回归集；不能仅因为某个框架支持某功能就提前接入。

暂缓到真实样例出现后再决定/实现的内容包括：

- 主 Parser 和 fallback Parser 的最终组合；
- chunk token 上限、短款合并和邻居扩展参数；
- 通用价目表维度超集、复杂定价 DSL 和数据库排斥约束；
- Gotenberg/LibreOffice 的正式采用；
- 多模型 AB UI、完整 cell 级复核 UI 和自动 confidence 公式；
- GraphRAG、通用知识图谱和多 Agent 架构。

### MVP 实施进度（2026-08-13）

已完成 Data Spike 第一批可运行能力：

- 新增独立 `contract_intelligence` 领域模块和 `contract_admin` Django 管理入口，没有把合同事实问答塞入现有具备文件执行能力的 ExecutionAgent。
- 已落库 `KnowledgeProject / ProjectMembership / Document / DocumentVersion / SourceFile / ParserRun / EvidenceUnit / RetrievalUnit / RetrievalTrace / ComparisonReport / AuthorityReview / IndexBuild`；本地默认 SQLite，数据库配置边界允许切换 PostgreSQL。
- `DocumentVersion`、`SourceFile` 和已发布 `IndexBuild` 已有应用层不可变保护；源文件上传时计算 SHA-256。发布服务和 Golden Dataset 门禁尚未实现，因此管理端不开放发布动作。
- 已实现可替换 `ParserProvider` 契约和三个明确标记为 baseline 的 Provider：DOCX 原生结构、XLSX 单元格级结构、PDF 文字层。解析结果会显式报告未决修订、自动编号、隐藏区域、公式未受控重算、PDF 无文字层等限制。
- ingestion 会为每个 ParserRun 保存完整 Evidence/Retrieval 快照；稳定 Evidence ID 不依赖检索排名或 chunk 参数。
- Django Admin 已提供 Chunk/Evidence 检查页，可查看原文、条款、页码、source anchor、单元格 metadata、Provider warning 和 baseline 原件/PDF 差异报告。
- 已加入透明的本地中文 BM25 Retrieval Test，保存 Query token、Top-K 候选、分数和 Trace。它只用于样例诊断，当前没有 Dense、融合、Reranker、Evidence Verifier，不能称为已完成 Hybrid RAG。
- 非 superuser 管理用户的列表查询按 `ProjectMembership` 做基础项目过滤；飞书授权映射和查询 API 的强制授权仍待实现。

下一开发门槛是取得一组保持真实结构的 DOCX/PDF/XLSX，并在管理台运行后记录解析和召回失败。没有样例前不继续固化 MappingSpec、chunk 参数、OCR 路由或模型阈值。

### MVP 第二批实施（2026-08-13，暂定实现，待真实样例验收）

用户授权在 Data Spike 基础上继续形成纵向 MVP。本批实现不代表相关参数成为最终架构决策：

- 新增 `prepare → publish` 服务。最终稿人工声明、审核哈希、成功 ParserRun、Evidence Manifest 全部通过后才能原子切换项目当前发布版本；Manifest 漂移会阻止发布。
- 新增合同问答 LangGraph 子图：`retrieve → draft atomic claims → verify → rewrite/retrieve（最多一次）→ answer/refuse`。引用 ID 必须属于本次 Published IndexBuild 的召回集合；Verifier 失败后不能输出原答案。
- DeepSeek 分别实现 Tool Router、Answer Provider、Query Rewriter 和 Evidence Verifier 契约。虽然暂用同一模型服务，但调用、prompt、JSON schema 与 trace 独立；不能据此声称拥有独立模型级 verifier。
- 顶层 Agent 只有 `search_contract`、`query_price` 两个工具，不采用多 Agent 对话。`authorized_project_id` 由服务端注入，模型无法改变。
- 新增样例专用、版本化 `PriceMappingSpec`。它仅允许受支持字段映射到 Excel 列字母，不接受 Python/SQL/表达式；坏行使整次导入失败。导入记录先进入 Staging，管理员确认后才进入 Trusted。
- `PriceQuery` 通过固定 Repository 查询当前 Published IndexBuild；缺少会改变结果的维度时澄清，多事实冲突时拒答。数量乘法使用 `Decimal` 并生成 Calculation Ledger。
- 管理端新增项目问答页、QuestionRun/Trace、PriceRecord 复核、GoldenCase 与 EvaluationRun。当前评估可计算 Retrieval Recall@K、Answer/Citation/Numeric Accuracy、Hallucination Rate；Reranker 未接入时 Accuracy 明确为 `null`。
- 飞书采用独立入口进程：私聊用户可授权多个项目并显式选择一个；群聊按 chat_id 固定项目；回复目标与 actor identity 分离。现有 Office 执行图不承担合同问答。

本批仍未实现：Dense Embedding、Reranker、Docling/MinerU/RAGFlow/OCR、Phoenix、Celery/Redis、PDF.js bbox、价格复杂规则、对话式澄清 memory。必须在真实样例和首批 GoldenCase 到位后决定是否以及如何接入。

## 1. 系统架构和模块边界（已确定）

### 两个产品入口

1. **本地管理员 Web**：管理员手工管理知识库项目、文档、不可变版本、解析任务、质量问题、人工复核、发布/回滚以及飞书授权。
2. **飞书用户入口**：外部用户只查询已经发布且获得授权的知识库，不通过飞书上传、替换或发布知识库文档。

飞书私聊允许用户在已授权项目间选择当前项目；飞书群聊固定绑定一个项目。

### 部署形态

第一版采用**模块化单体代码库 + 独立运行进程**，暂不拆微服务：

```text
Django Web             管理项目、版本、复核、发布和授权
Feishu Bot             用户查询入口和会话协调
Celery Worker          OCR、解析、清洗、Embedding、建索引和质量检查
PostgreSQL             业务事实来源
Redis                  Celery broker
RAGFlow                解析/chunk/retrieval 调试和可替换检索后端
Phoenix                线上 trace、失败追因、数据集和实验
```

- 三个应用入口位于同一仓库并复用 Python 领域模块；不为了“服务化”让本机模块互相调用 HTTP。
- OCR/解析必须运行在独立 Worker 中，不能占用或拖垮飞书问答进程。
- Celery 负责任务投递、重试和 Worker；PostgreSQL 中的业务状态才决定文档是否可发布，Celery result 不是业务事实来源。
- 出现独立团队、独立 GPU 集群、多个产品复用解析能力或独立部署要求后，再评估把 ingestion 拆成微服务。

### 管理与调试工作台

- 管理入口使用 **Django Admin + Unfold**，不从零开发管理后台、认证、CRUD 和常规筛选页面。
- PDF 展示和定位使用 **PDF.js**，只增加 bbox 覆盖层及复核交互。
- **RAGFlow UI 是正式的 RAG 调试工作台**：必须支持查看解析结果、chunk、页面截图、人工修改和 retrieval test；不是可选的高级功能。
- **Phoenix 是正式的线上运行观测台**：查看真实问题的 Query 改写、BM25/Dense 召回、融合、Reranker、关联条款扩展、Evidence Grader、Verifier、重试和最终终态。
- 第一版接受 Django、RAGFlow、Phoenix 三个内部页面，通过 Django 项目页深链接打开；暂不 iframe 嵌入或重做 RAGFlow/Phoenix UI。

### 领域所有权

- PostgreSQL 是项目、逻辑文档、不可变文档版本、发布版本、权限、价格、标准化证据和 Golden Dataset 的唯一业务事实来源。
- RAGFlow/Phoenix 的内部 ID 作为外部系统映射保存，但二者都不拥有业务版本和证据政策。
- 原始文件通过 `ObjectStore` 边界访问；第一版可以用本地持久目录，未来可换 MinIO/S3。
- 新合同智能能力作为独立只读领域模块/ LangGraph 子图接入现有 MainAgent，不进入当前拥有 Bash/文件执行能力的 `ExecutionAgent`。
- MainAgent 只负责 `contract_qa` 意图和用户交互，不能把自身知识当成合同事实来源。
- **Agentic RAG 是正式的技术选型维度**：Contract Intelligence LangGraph 必须能在证据不足时做有预算的 Query 改写、检索策略切换、关联条款/附件扩展和再次验证；但保持单个受控工作流，不为了形式拆成多个互相讨论的 Agent。
- 检索后端/SDK 必须暴露候选文本、稳定 evidence ID、文档版本、页码/条款元数据、各阶段分数及过滤条件，允许重复检索和完整 trace；只返回最终自然语言答案的黑盒 RAG API 不满足要求。

### 隔离与上下文

- 文档属于 `KnowledgeProject`，不属于某个飞书查询用户。
- 所有服务端查询强制注入 `authorized_project_id`；不能让模型自行生成或改变权限过滤条件。
- 第一版使用简单映射：飞书用户可访问多个项目，群聊固定绑定一个项目。
- 短期查询上下文、长期用户 memory、访问权限和文档证据是四类不同状态，必须分开存储和治理。
- 查询上下文保存已明确的商品、地区、数量、日期等结构化条件；追问仍需重新查询原始数据，上一轮答案不能作为新一轮事实证据。
- 项目发布 revision 变化后，旧 evidence 不得默认作为当前事实继续使用；显式追问历史回答时可按旧版本复核。

### 不可变版本和发布

```text
上传原件
→ 创建不可变 DocumentVersion
→ 后台解析和质量检查
→ 必要时人工复核
→ 创建 Draft IndexBuild
→ Retrieval Test + Golden Dataset
→ 管理员发布
→ 原子切换当前 PublishedRevision
```

- 新版本失败、待复核或尚未发布时，飞书继续查询旧的当前发布版本。
- 发布后的 `DocumentVersion` 和 `IndexBuild` 不允许原地修改；调整解析结果、chunk 或模型配置时创建新 Draft。
- 管理员在 RAGFlow 中完成修订后，发布前必须同步标准化证据快照到 PostgreSQL并计算 manifest hash。
- 每次回答引用具体 `DocumentVersion/IndexBuild` 或内容哈希，确保文档更新后旧回答仍可复核。

## 2. 文档 ingestion / parsing（部分已确定）

### 原生结构优先，PDF/OCR 兜底

合同和价目表可以取得 Word、Excel 等原件，因此 ingestion 的总原则改为：

```text
DOCX/XLSX 原生结构解析
        ↓
确定性清洗、标准化和质量检查
        ↓
必要时与官方 PDF/渲染快照对齐

只有 PDF 或页面缺少可靠文字层
        ↓
PDF parser / OCR / layout / table recognition
```

- 不应把可用的 Word/Excel 原件先栅格化再 OCR；OCR 是信息有损的 fallback。
- DOCX 优先保留段落、样式、标题层级、编号、表格、页眉页脚、批注/修订状态和 OOXML 原始关系。
- XLSX 优先保留 workbook/sheet、表头、行列、合并单元格、隐藏行列、公式、缓存值、单位和单元格坐标；价格进入可信表前必须经过确定性清洗和校验。
- 原始值和标准化值并存；清洗不能覆盖原始证据。
- PDF 仍然重要：它提供稳定的视觉版式、物理页码、bbox、签章页和最终可视证据。若同时存在原生文件和正式 PDF，必须登记它们的版本关系并检测内容差异，不能默认二者完全一致。

### 原生文件与正式 PDF 的权威性和一致性（已确定）

同一逻辑版本可以包含不同角色的文件：

```text
DocumentVersion
├─ structured_source：用于高质量结构抽取的 DOCX/XLSX 原件
├─ executed_copy：正式签署或正式发布的 PDF
├─ attachment：附件
└─ derived_render：系统从原生文件生成的受控 PDF 快照
```

- 同时存在 DOCX/XLSX 与正式 PDF 时，默认由原生文件提供结构抽取，PDF 作为最终业务权威和视觉证据；发布前先做自动一致性检查。
- 一致性检查至少覆盖标题/编号、主体、日期、金额、百分比、条款编号、关键段落、表格关键单元格和附件清单。排版、分页和换行差异与实质内容差异分开报告。
- 自动检查只能表述为“未检测到实质差异”，不能声称已经证明两份文件在法律上完全一致。
- 管理端提供明确的人工审核：管理员声明 DOCX/XLSX、PDF 是否为同一最终稿，并确认哪个文件角色具有权威性。发布动作不能靠文件名或相似度自动推断“最终稿”。
- 机器校验结果与管理员声明分别保存并留痕，建议至少记录：`comparison_status`、差异报告、审核人、审核时间、权威文件、说明和所审核文件的 SHA-256。
- 检测到关键差异时阻断普通发布。管理员应上传正确的一组文件，或明确选择单一权威文件；不能继续把不一致的两份文件混合作为同一事实来源。
- 若选择 PDF 为权威文件，DOCX/XLSX 只能辅助结构抽取；任何无法对齐回 PDF 的条款、数值或表格行不能进入已发布证据。
- 若只有 DOCX/XLSX，管理员可将原生文件确认为最终稿；系统生成的 `derived_render` 只用于稳定页码和 bbox，不冒充正式签署原件。

### DOCX/XLSX 原生解析组件与审核政策（已确定）

不从头实现完整的 Office 解析器，采用“成熟解析组件 + 薄的格式审计/归一化层”：

- DOCX 以 Docling 的原生 Office 解析和无损结构化输出作为统一结构入口；额外的 OOXML 审计层只检查合同领域不能静默丢失的特性，例如自动编号、修订、批注、隐藏文本、字段和嵌入关系。
- XLSX 使用 `openpyxl` 做只读、单元格级原生解析，保留 workbook/sheet、Excel Table/命名区域、单元格坐标、值、公式、样式/显示格式、合并区域、隐藏 sheet/行/列、批注和外部链接信息。Docling/RAGFlow 可辅助生成统一预览，但不能作为价格 ETL 的唯一数据来源。
- Office 原始文件永久只读保存；清洗、人工修正和结构化结果都以新版本/overlay 形式记录，不能反写覆盖上传原件。

DOCX 发布政策：

- 存在未接受/拒绝的修订时默认阻止发布。管理员必须上传清稿，或明确声明“以当前显示稿为最终内容”，并记录审核人和文件哈希。
- 批注保留供审核，默认不进入检索和事实证据。
- 隐藏文本默认不进入检索，并在发布前提示；不能在清洗阶段静默删除。
- 自动编号必须还原为可验证的显示条款号；无法稳定恢复时进入人工复核。

XLSX 发布政策：

- 同时保存公式文本、文件中缓存值、受控重算值、显示值/格式以及依赖的源单元格。Python 读取库不负责证明公式计算正确。
- 使用固定版本、隔离运行的 LibreOffice 对需要信任的公式进行受控重算；公式缓存与重算结果不一致时进入人工复核。
- 外部工作簿链接、无法重算的函数、循环引用或错误值不能直接进入可信价格表；需要管理员处理或确认固定值。
- 隐藏 sheet、隐藏行和隐藏列必须被发现并展示。默认不进入可信价格记录，除非管理员明确纳入。
- 合并表头和多行表头可以展开成规范字段路径，但每个规范值必须保留回到原始单元格/合并区域的 provenance。
- 批注、筛选和分组状态保留为审核元数据；批注默认不是价格事实。
- 人工修正细化到 cell/字段级，以 overlay 及审核记录保存，不能直接改写原始 workbook。

### Office 受控渲染与公式重算（暂定，待真实样例验证）

- 候选方案是使用固定镜像版本的自托管 Gotenberg/LibreOffice 生成 `derived_render`，用于 DOCX 的稳定视觉快照以及 XLSX 的管理员预览；需要固定中文字体、locale、时区、转换参数和镜像 digest。
- DOCX 可尝试将原生条款对齐到 `derived_render` 的页码/bbox；无法稳定对齐时进入人工复核。快照页码必须标明是“系统渲染页码”，不能冒充正式 PDF 页码。
- XLSX 的主要证据暂定为“文件版本 + 工作表 + 单元格/命名区域”，渲染快照页码仅作为可选辅助。
- 公式受控重算暂定使用隔离的 LibreOffice job，重算副本只用于比较，不覆盖原始 XLSX；具体可行性、格式保真和公式兼容性需用真实价目表验证。
- 上述方案只有在取得代表性 DOCX/XLSX 后完成版式、分页、公式和性能测试，才升级为正式决策。

### 多解析器和统一 IR

- RAGFlow 作为解析器 bake-off 和可视化工作台；同一份代表性文档可以分别运行 DeepDoc、MinerU、Docling 后对比。
- RAGFlow、MinerU、Docling 都通过 `ParserProvider` 接入，后续 chunk、索引和问答只消费统一 `DocumentIR`。
- 中文扫描件、复杂排版、印章、旋转文字和跨页表格重点比较 MinerU/DeepDoc；Docling 重点验证原生结构、表格和 provenance。最终默认路由由样例 benchmark 决定，当前不提前指定唯一赢家。
- 高风险页才按需二次解析；关键数字发生解析器分歧时进入人工复核，不自动选择看似合理的结果。
- 必须保存 Parser 名称/版本、配置哈希、原始输出、页码、bbox、内容哈希和质量指标，保证结果可复现。

统一 `DocumentIR` 至少表达：

- 文档版本和 ParserRun；
- 页面、物理页码、印刷页码、页面尺寸；
- block 类型、原文、标准化文本、阅读顺序、bbox、置信信息和 source reference；
- 标题/条款层级；
- 表格、行列、合并单元格、cell 原始值/标准化值及 cell provenance；
- 图片、图表、印章、签名等视觉资产。

### 质量与人工复核边界

- Parser 返回成功不代表 ingestion 成功；页数、文本覆盖、乱码、条款编号、阅读顺序、关键数字和表格结构均须经过质量门禁。
- 金额、小数点、百分号、正负号、日期、数量单位、币种、商品编码和条款编号属于高风险 token。
- 低置信页可以进入人工复核队列。
- 价目表必须支持细到单元格的人工确认/修正；只修改整个 chunk 不足以保证价格准确。
- 图片、印章和签名保存并可定位，也可做 OCR；第一版不允许模型仅凭视觉描述自动断言“已盖章/已签字”，除非管理员人工确认。
- VLM 生成的图片描述只能作为 retrieval hint，不能自动成为事实证据。
- 默认在本地解析完整原件。把完整合同页面发送到第三方 OCR/VLM 服务必须由管理员显式开启；检索到的少量证据可以按既定策略发送给 DeepSeek。

## 3. 合同 chunk 策略（已确定）

### 四层模型

合同不使用一个通用 chunk 对象同时承担结构、证据、召回和生成上下文。采用：

```text
Clause Tree
    ↓
Evidence Unit       最小可验证的原文和引用边界
    ↓
Retrieval Unit      为 BM25/Dense 优化的检索表示
    ↓
Context Package     本次回答所需的完整上下文
```

- `Clause Tree` 按章、条、款、项、附件和条款内表格表示合同层级。信号优先使用 DOCX 自动编号/标题样式、明确的中文或数字编号、PDF 目录/书签/版式；模型只能提出低置信结构候选，不能独自决定最终条款号。
- `Evidence Unit` 是一条、一款、一个列举项、一项定义、表格逻辑行或附件条目等最小可验证原文单元，保存文档版本、条款路径、原文、物理/印刷页码、bbox/source anchor 和内容哈希。
- `Retrieval Unit` 可以在原文前确定性加入文件名、完整标题路径、条款号和原始主题标题，以提高 BM25/Dense 召回；增强文本本身不自动成为证据。一个 Retrieval Unit 可以关联一个或多个连续 Evidence Unit。
- `Context Package` 在召回后确定性扩展父条款引导句、必要相邻项、标题路径、表头、定义以及明确引用的条款/附件。最终回答只引用其中真实 Evidence Unit。

### 切分规则

- 结构边界优先，token 长度只作兜底，不对合同默认使用固定 token 滑动窗口。
- 一条未超限时尽量保持完整；超长时依次按款、列举项、句子拆分，最后才做 tokenizer-aware 切分。
- 子项必须携带共同主句或表头作为 `context_prefix`，但重复上下文需明确标记，不能伪装成当前 Evidence Unit。
- 相邻短款是否合并、token 上限和 context window 大小留给真实合同与 Golden Dataset 实验决定，并作为版本化 IndexBuild 参数。

### 轻量关系图与 Agentic Retrieval

- 建立定义词到定义证据、条款显式引用、条款到附件/价目表的轻量关系图，不在第一版建设通用知识图谱。
- 解析“根据第 X 条”“除第 X 款外”“详见附件”等显式引用；目标不能唯一解析时进入人工复核。
- Agentic Retrieval 在首次证据不足时可沿定义/引用边扩展，或重新改写 Query；不能无方向无限重搜。

### 表格和稳定证据

- 合同内表格保留标题、层级表头、行列、合并单元格、cell provenance 以及表前/表后的限制性文字，不能先压平成普通 Markdown 段落再切分。
- 检索表示可以向逻辑行重复表头，但最终证据必须回到原始单元格/区域。
- 若合同附件表格同时进入价格 ETL，合同 Evidence 与结构化 PriceRecord 通过共同的 source row/cell ID 对齐。
- Evidence ID 不使用易漂移的“第几个 chunk”，而由文档版本、结构路径、source anchor 和内容哈希构成。调整 Retrieval Unit 参数不应改变原始 Evidence ID。

### 与通用组件的边界

- 可以复用 Docling Hierarchical/Hybrid Chunker 的结构化和 tokenizer-aware 能力，但合同编号、共同主句、定义和交叉引用由薄的合同规则层补齐。
- Clause Tree、Evidence Manifest、关系图和发布版本由自己的 PostgreSQL 持有。
- RAGFlow 保存/索引 Retrieval Unit，并承担 chunk 可视检查和 retrieval test；它是可替换的检索/调试后端，不是原始证据的唯一 owner。

## 4. 价目表结构化（原则已确定，schema 由样例驱动）

- 采用 `Raw → Staging → Trusted` 三层。Raw 原样保存 workbook/sheet/cell、公式、合并区域和说明；Staging 承载候选映射、清洗和复核；只有已发布的 Trusted 记录可被飞书查询。
- 第一份真实价目表先采用样例专用、版本化的 MappingSpec。DeepSeek可以提出受白名单约束的映射候选，但不能生成任意 Python/SQL、不能直接修改原件或发布数据。
- Trusted 记录使用真实样例需要的固定核心字段，并允许少量经 schema 验证的扩展维度；在观察多种价目表前不建设假想的字段超集。
- 金额使用 PostgreSQL `NUMERIC`/Python `Decimal`，同时保留币种、单位、税口径、数量/日期适用范围和原始值。
- 一个价格事实绑定完整 Evidence Set：价格行、表头、单位、地区、生效期、税务说明和限制性备注都可成为必要证据，不能只引用价格 cell。
- 第一版只执行能够确定性编译和计算的价格规则。不能稳定结构化的复杂自由文本保留为证据并标记 `not_machine_queryable`，不能让 Agent 临场解释后报价。
- DeepSeek只输出经过 schema 验证的 `PriceQuery`；后端通过固定、参数化 Repository 查询并强制注入项目和发布版本，不开放 Text-to-SQL。
- 零结果、商品/条件歧义、多结果和数据冲突都是显式终态：分别拒答/提示、澄清或阻断发布，不能通过放松过滤条件猜一个价格。
- 价格计算由确定性 Calculator 生成 Calculation Ledger；LLM只负责呈现，不重新心算。
- 具体商品标识、地区/客户/渠道维度、`price_kind`、阶梯边界、公式和冲突规则必须从真实价目表提取后再定。

## 已确认但尚未进入逐项设计的约束

- 合同正文使用 BM25 + Dense Embedding + Reranker 的 Hybrid Retrieval，并支持受限的 Agentic Retrieval。
- 后续 Hybrid Retrieval/Reranker 技术选型必须同时评估 Agentic RAG 可控性：Query 改写、多轮检索、metadata filter、关联证据扩展、重试预算、可观测性和确定性拒答，而不能只比较一次性 Top-K 指标。
- 价目表尽量进入 PostgreSQL，通过受约束的结构化查询和确定性计算回答，不能依赖纯 RAG 查价格。
- Evidence Grader/Verifier 按原子 claim 检查；只有关键事实全部获得支持才能回答。
- `confidence` 不能由模型随口生成，后续需基于解析质量、证据覆盖、冲突状态、检索稳定性和 verifier 结果定义。

## 待讨论/待验证

以下内容尚未定稿：

1. 用真实 DOCX/XLSX 验证 Gotenberg/LibreOffice 的受控渲染、中文字体、分页、公式重算和格式保真，再决定是否正式采用。
2. Excel 证据的最终引用格式：暂定以 `工作表 + 单元格/命名区域` 为主，是否附加渲染页码由样例验证。
3. Office 格式范围：第一版只接受 DOCX/XLSX，还是还接受 DOC/XLS/XLSM 等旧格式或宏格式。
4. 基于第一份真实价目表确定 MVP PriceRecord 字段、MappingSpec、价格规则和冲突政策。
5. 基于真实合同与种子 Golden Dataset 确定 Hybrid Retrieval、Reranker、Embedding 和索引后端 baseline。
6. 基于实际失败路径确定 Contract Intelligence LangGraph 的状态、Agentic Retrieval 动作和重试预算。
7. Golden Dataset 的详细 schema、标注流程、指标阈值和发布门禁。
