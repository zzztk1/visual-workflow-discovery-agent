# 餐饮行业 Workflow Discovery Agent 交付说明

## 1. 项目概述

本项目搭建了一个面向餐饮行业的 **Workflow Discovery Agent**。它的目标不是直接替餐馆生成某一个固定方案，而是从公开互联网资料中发现真实存在的人工工作流，提取公开证据，生成候选工作流，并在人工锁定其中一个候选后，继续自动完成流程拆解、痛点分析、Agent 介入设计、产品化方案生成、风险审核和可观测结果导出。

这个 Agent 聚焦餐饮行业中真实存在、可验证、可拆解、可被 Agent 改造的人工流程，例如：

- 小餐馆视觉营销物料制作与发布工作流
- 餐厅客诉与退款处理工作流
- 餐厅库存盘点与采购补货工作流

项目核心价值在于：把“从公开资料中找到一个真实人工工作流，并形成 Agent 产品化方案”这件事，从人工搜索、人工判断、人工写方案，转变成一个可运行、可复盘、可观测的标准化 Agent 工作流。

每次运行都会生成完整产物，包括：

- 候选工作流列表
- 公开证据链接
- 人工锁定后的目标工作流
- 原始人工流程图
- Agent 改造流程图
- 痛点与低效点分析
- Agent 介入后的产品化方案
- 风险审核结果
- trace、metrics、health、causal audit 等可观测文件
- 最终报告草稿

因此，本项目不是静态方案生成器，而是一个具备“发现、判断、锁定、推导、审核、导出”能力的完整 Agent 系统。

## 2. Agent 搭建说明

### 2.1 用了什么工具

本项目采用轻量但完整的工程实现，优先保证比赛现场可运行、可展示、可复盘。主要工具如下：

- **Python 标准库**：实现 Agent 主流程、状态管理、HTTP 服务、文件导出和公开网页抓取。
- **本地状态机工作流**：用固定节点链路实现 Agent 的分阶段执行，避免黑盒一键生成。
- **本地 JSON 输出与 checkpoint**：记录每次运行状态、候选、证据、指标和报告，第一版不强依赖数据库。
- **公开搜索与证据池 fallback**：优先尝试实时搜索；当现场网络或搜索不可用时，回退到预置公开证据池，并在 `source_mode` 中如实记录。
- **HTML / CSS / JavaScript 前端工作台**：提供任务配置、搜索发现、候选锁定、深度拆解、指标追踪和报告导出页面。
- **MetricsContext**：记录每个节点的耗时、token 消耗、工具调用次数、重试次数、错误次数和模型成本。
- **trace.json / metrics.json / health.json / causal_audit.json**：提供完整可观测证据，证明 Agent 真实运行。
- **Mermaid 流程图**：导出原始人工流程图和 Agent 改造后的流程图。
- **unittest 自动测试**：验证候选切换、证据绑定、模板泄漏、风险审核和输出完整性。

第一版没有强依赖外部商业搜索 API 或云端链路，是为了降低比赛现场的不确定性。后续进入生产环境时，可以将搜索执行器替换为正式 Search API，将本地 JSON checkpoint 升级为 PostgreSQL Checkpointer，将本地 trace 同步到 LangSmith 或同类可观测平台。

### 2.2 怎么搭建的

Agent 采用分阶段状态机方式搭建。每个节点只负责一类明确任务，节点之间通过共享状态传递结构化数据。每个节点执行时都会进入统一的 `_run_node` 包装器，由包装器自动记录输入摘要、输出摘要、耗时、token 消耗、工具调用次数、重试次数和错误摘要。

整体链路分为两个阶段。

第一阶段是 **发现阶段**：

- 接收任务约束
- 规划搜索方向
- 执行公开搜索或证据池回退
- 抽取证据
- 构建候选工作流
- 对候选进行排序和筛选

这个阶段只生成候选工作流，不直接生成最终方案。

第二阶段是 **深度拆解阶段**：

- 人工锁定一个候选
- 只围绕被锁定候选继续推导
- 拆解原始人工流程
- 分析痛点和低效点
- 设计 Agent 介入后的新流程
- 生成产品化方案
- 执行风险审核
- 执行因果审计
- 导出报告和可观测文件

中间的 `human_lock` 是关键设计。它避免 Agent 自动选择错误方向后继续扩散成本，也保证最终方案来自明确选中的真实工作流，而不是固定模板或模型想象。

### 2.3 Agent 的流程是什么

Agent 的固定流程如下：

```text
brief_intake
→ query_planner
→ search_executor
→ evidence_extractor
→ candidate_builder
→ candidate_scorer
→ human_lock
→ workflow_decomposer
→ painpoint_analyzer
→ intervention_designer
→ product_solution_generator
→ risk_reviewer
→ causal_auditor
→ export_report
```

各节点职责如下：

1. `brief_intake`：读取行业、目标、必须包含项、排除项、候选数量和 MVP 周期。
2. `query_planner`：根据餐饮行业和人工工作流目标生成搜索 query。
3. `search_executor`：执行实时搜索；如果失败则回退到公开证据池。
4. `evidence_extractor`：从搜索结果或证据池中提取证据链接、证据摘要、流程线索和低效类型。
5. `candidate_builder`：按工作流类型聚合证据，生成候选工作流。
6. `candidate_scorer`：对候选进行评分，标记证据不足、流程不连续或 Agent 介入价值不足的问题。
7. `human_lock`：由使用者选择一个候选，后续节点只能围绕该候选继续执行。
8. `workflow_decomposer`：把被选中的工作流拆解为原始人工步骤，并生成 Mermaid 流程图。
9. `painpoint_analyzer`：分析每个步骤中的人工重复、信息搬运、判断成本、沟通审核成本等低效点。
10. `intervention_designer`：设计 Agent 在流程中的介入点，包括自动提取、分类判断、建议生成、人工复核和结果沉淀。
11. `product_solution_generator`：生成产品化方案初稿，包括产品定位、核心功能、用户流程、MVP 验证方式和可观测指标。
12. `risk_reviewer`：检查过度自动化、虚假信息、价格修改、自动发布等风险，并给出人工确认点。
13. `causal_auditor`：检查下游产物是否都引用了被锁定候选，防止串题或固定模板泄漏。
14. `export_report`：导出最终报告、证据文件、流程图、trace、metrics、health 和测试报告。

### 2.4 每一步输入输出是什么

| 节点 | 输入 | 输出 | 作用 |
| --- | --- | --- | --- |
| `brief_intake` | 用户配置：行业、目标、必须包含项、排除项、候选数量、MVP 周期 | 标准化 brief | 明确本次 Agent 运行边界 |
| `query_planner` | 标准化 brief | 搜索 query 列表 | 把任务目标转成可执行搜索方向 |
| `search_executor` | query 列表、live search 开关、证据池 | 原始搜索结果、`source_mode` | 获取公开互联网证据，并记录来源模式 |
| `evidence_extractor` | 原始搜索结果 | `EvidenceItem` 列表 | 抽取链接、标题、摘要、流程线索和低效类型 |
| `candidate_builder` | 证据列表 | `CandidateWorkflow` 列表 | 将证据聚合成若干真实候选工作流 |
| `candidate_scorer` | 候选工作流 | 带评分和标记的候选列表 | 识别更适合 Agent 改造和 7 天 MVP 的流程 |
| `human_lock` | 候选列表、人工选择索引 | `SelectedWorkflow` | 锁定唯一目标流程，阻断后续跑偏 |
| `workflow_decomposer` | 被锁定工作流、原始步骤和证据 | 原始流程结构、`original_workflow.mmd` | 还原人工工作流的连续步骤 |
| `painpoint_analyzer` | 被锁定工作流、原始流程、证据 | 痛点列表 | 标出重复、搬运、判断、沟通、审核等低效点 |
| `intervention_designer` | 原始流程、痛点、工作流类型 | Agent 介入点、`agent_workflow.mmd` | 设计 Agent 改造后的新流程 |
| `product_solution_generator` | 被锁定工作流、介入点、风险约束 | 产品化方案 | 生成可落地的 Agent 产品方案初稿 |
| `risk_reviewer` | 产品化方案、工作流类型、证据 | 风险审核结果 | 标记必须保留人工确认的高风险环节 |
| `causal_auditor` | 全部下游产物 | `causal_audit.json` | 检查方案是否真实绑定所选候选 |
| `export_report` | 当前完整状态 | Markdown、JSON、Mermaid 等文件 | 导出可提交、可复盘、可录屏展示的结果 |

### 2.5 设计理念是什么

本 Agent 的设计理念可以概括为五点。

第一，**先发现真实工作流，再生成方案**。比赛题目要求从公开互联网发现真实存在的人工工作流，因此 Agent 不能一开始就写产品方案，而必须先完成搜索、证据抽取、候选生成和人工锁定。

第二，**证据优先，而不是模型想象优先**。每个候选都必须绑定公开证据链接。证据不足时，系统会降低候选可信度，并在报告中标记 `evidence_insufficient`。

第三，**人机协同，而不是完全自动化**。Agent 负责发现、拆解、推导和生成初稿；人负责锁定候选和确认高风险环节。这种设计更接近真实产品落地，也能避免 Agent 搜偏后继续消耗成本。

第四，**结构化推导，而不是固定模板套用**。下游节点只能读取被锁定候选的步骤、低效类型、证据和工作流类型。不同候选会进入不同 profile，例如视觉营销、客诉退款、库存采购、菜单维护或通用餐饮流程，避免所有结果都变成同一个视觉营销模板。

第五，**可观测、可复盘、可审核**。每次运行都会导出 trace、metrics、health 和 causal audit，用来证明 Agent 真实运行、成本可记录、链路可复盘、错误可定位。

## 3. 使用的技术与工具

本项目使用的技术与工具分为六类。

### 3.1 Agent 编排

- Python 状态机工作流
- 节点式函数编排
- 共享状态对象
- `run_id` / `request_id` 链路标识
- discovery 与 deep dive 两阶段运行模式

### 3.2 搜索与证据

- 实时公开搜索尝试
- 预置公开证据池 fallback
- `source_mode` 来源模式记录
- 证据链接、证据摘要、证据 ID 和工作流绑定
- 证据不足标记

### 3.3 数据结构

- `EvidenceItem`
- `CandidateWorkflow`
- `SelectedWorkflow`
- `WorkflowDecomposition`
- `Painpoint`
- `AgentIntervention`
- `ProductSolution`
- `RiskReview`
- `RunAudit`

这些结构用于保证下游产物都能回溯到选中候选、原始步骤和公开证据。

### 3.4 前端演示

- HTML / CSS / JavaScript 单页工作台
- 任务配置页
- 搜索发现页
- 候选工作流页
- 公开证据页
- 深度拆解页
- 指标与 Trace 页
- 报告导出页

前端不是静态展示页，而是用于演示完整链路：运行搜索发现、查看候选、人工锁定、继续深度拆解、查看风险审核和导出结果。

### 3.5 可观测与工程质量

- `trace.json`：记录完整节点链路、输入摘要、输出摘要和错误。
- `metrics.json`：记录节点耗时、token 消耗、工具调用、重试次数和错误数。
- `health.json`：记录模型配置、搜索模式、输出目录、fallback 状态。
- `causal_audit.json`：检查下游产物是否真实绑定被选候选。
- `test_report.md`：记录自动测试和验收结果。

### 3.6 测试工具

- Python `unittest`
- 候选切换测试
- 模板泄漏测试
- 证据绑定测试
- 风险审核测试
- 输出完整性测试

## 4. 设计理念

本项目的整体设计不是追求“看起来像 Agent”，而是追求“能证明 Agent 真的完成了发现、判断、拆解和方案生成”。

### 4.1 发现型 Agent 优先于生成型 Agent

餐饮行业里有大量看似适合 AI 的任务，但并不一定是真实、连续、可落地的工作流。因此本项目先做 workflow discovery，而不是直接做一个餐馆海报生成器、菜单生成器或客服机器人。只有先发现真实人工流程，后续产品方案才有依据。

### 4.2 证据链是产品方案的基础

每个候选工作流都需要公开证据链接。Agent 不只输出结论，还要能回答“这个流程为什么真实存在”“证据在哪里”“流程中的低效点来自哪里”。这让最终方案更像产品调研和产品设计，而不是纯文本生成。

### 4.3 人工锁定是必要的控制点

Agent 可以发现多个候选，但最终选哪个流程做深度拆解，应该由人确认。这个人工锁定点可以同时控制方向、成本和风险。锁定后，下游节点只围绕该候选继续推导，避免 Agent 在多个主题之间漂移。

### 4.4 成本控制嵌入流程本身

系统把任务拆成发现阶段和深度拆解阶段。发现阶段只生成候选，不生成完整方案；只有人工锁定后才进入高成本的拆解和方案生成。这样可以减少无效 token、无效工具调用和无效报告生成。

### 4.5 工程质量服务于比赛可证明性

评分不仅看结果是否完整，也看 Agent 是否真实搭建、流程是否清楚、是否有证据、是否可观测、是否能说明成本和效率。因此本项目从第一版就加入了 trace、metrics、health、causal audit 和测试报告，确保录屏和文档都能证明系统真实运行。

### 4.6 第一版轻量，生产版可升级

第一版选择本地 JSON、轻量 HTTP 服务和内置证据池，是为了比赛现场稳定运行。生产版可以复用既有项目经验升级为：

- LangGraph StateGraph 编排
- PostgreSQL Checkpointer 持久化
- LangSmith 或 OpenTelemetry 全链路追踪
- 正式搜索 API 与网页抓取服务
- 更细的评测集和人工审核工作台

这种设计兼顾了比赛当天的可交付性和后续真实产品化空间。
