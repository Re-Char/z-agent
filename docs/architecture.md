# Z-Agent：基于 ACM / Scroll 思想的独立 Agent Runtime

**状态：** v1 实现基线  
**日期：** 2026-08-27  
**目标：** 自研一个能主动管理工作上下文、无损保存过程状态、按需恢复证据，并以中文与国产模型为一等公民、由桌面 GUI 交付、可安全接入主流扩展生态的长程 Agent。Hermes 仅作为能力对照，不作为宿主、依赖或封装对象。

## 1. 问题与设计目标

常见 Agent 的默认压缩器会在上下文接近模型窗口时，将早期消息和工具输出浓缩成摘要。它能控制 token，但有两个根本限制：

1. 是否压缩由框架阈值决定，不是由 Agent 对任务阶段的判断决定；
2. 压缩后的内容不再是 Agent 可定位、可验证、可计算的任务状态。

本项目实现一个核心运行时能力，而不是第三方插件：

- **Agent 主动上下文管理（ACM）**：Agent 可在任意推理阶段归档、检索与展开上下文。
- **无损事件历史（Scroll）**：所有会话事件进入追加式 Event Log，拥有稳定 ID、内容哈希与来源信息。
- **最小工作视图**：Prompt 只载入当前任务所需的工作集；大工具输出和历史轨迹保留在环境中。
- **零训练运行时**：所有能力通过本地上下文算法、显式工具协议和模型原生 tool calling 实现，不依赖微调、强化学习或服务端托管工具。

### 非目标（v1）

- 不复现 ACM 的 post-training / RL 训练流程，v1 及后续版本均不以训练作为功能前提。
- 不复用、包装或嵌入 Hermes 及其他现成 Agent 产品的运行时、权限系统或工具执行器。
- 不承诺跨会话人格记忆；这属于已有 memory-provider 的职责。
- 不使用 LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI 等 Agent 框架；模型 API 客户端仅负责网络协议。
- 不使用 API 服务端托管的 Code Interpreter、Files API 或远程文件工具；工具循环、解析、终止、重试和本地执行均由本项目实现。

## 2. 论文映射

| 论文 | 采用的思想 | 在本项目中的落点 |
| --- | --- | --- |
| ACM: Agentic Context Management (2026) | Agent 自主触发压缩、外置与检索 | `context_archive`、`context_retrieve`、`context_status` 工具与运行时策略 |
| Scroll: Context as an Environment (2026) | Event Log、持久执行环境、按投影进入 Prompt | `EventLog`、`WorkingSet`、稳定事件引用、受控查询接口 |

## 3. 总体架构

```text
                           ┌─────────────────────────────────────┐
                           │          Z-Agent Runtime             │
User / tools ─────────────▶│  Planner + tool-calling LLM loop     │
                           └──────────────┬──────────────────────┘
                                          │ active prompt
                         ┌────────────────▼────────────────┐
                         │        Context Orchestrator       │
                         │ budget / working set / projection │
                         └───────┬──────────────┬────────────┘
                                 │              │
                  context tools  │              │ append-only events
                                 ▼              ▼
                    ┌──────────────────┐  ┌──────────────────────┐
                    │ Context Policy   │  │ EventLog + BlobStore  │
                    │ archive/retrieve │  │ SQLite + file objects │
                    └────────┬─────────┘  └──────────┬───────────┘
                             │                         │
                             ▼                         ▼
                    ┌──────────────────┐    ┌─────────────────────┐
                    │ Evidence Index   │    │ Local Tool Runtime   │
                    │ FTS + metadata   │    │ process/fs policies  │
                    └──────────────────┘    └─────────────────────┘
```

## 4. 核心组件

### 4.1 EventLog（事实源）

`EventLog` 是每个 session 的追加式事实记录；不可就地覆盖或删除。每个事件拥有：

```text
event_id, session_id, sequence, parent_event_id, timestamp,
kind, role, payload_ref, payload_sha256, token_estimate,
tool_name, tool_call_id, tags, sensitivity, provenance
```

- 小文本 payload 存 SQLite；超过阈值的工具输出写入 BlobStore，仅将引用放入事件。
- `payload_sha256` 支持重放与审计；`parent_event_id` 维护压缩/派生关系。
- 原始工具输出从不被摘要覆盖。摘要也是事件，并以 `source_event_ids` 指向原始证据。

首版采用 SQLite（WAL 模式）+ 本地 blob 目录；FTS5 保留全历史精确检索，本地稀疏向量在查询时对有界候选集计算，不需要持久化向量索引。

### 4.2 WorkingSet（模型可见上下文）

`WorkingSet` 是每次 LLM 调用前临时构造的、受 token 预算限制的视图：

1. 稳定系统提示词、工作区权限和 context tool 使用说明；
2. 最近一次归档的结构化任务状态与 archive ID；
3. 未解决的 Runtime checkpoint（Core 生成的结构状态、证据 event ID 与文件 SHA）；
4. `context_pin` 固定的关键事件（跨归档优先保留）；
5. 尚未归档的最近对话尾部；
6. 完整的工具调用轮次（caller 与全部 tool result 要么一起保留，要么一起移除）。

它不等同于完整聊天记录。构造器按 SQLite `sessions.context_version` 缓存相同投影；事件追加、固定、取消固定、归档、checkpoint 或工作区路径变化都在事务中递增版本。因此 Core 重启或另一个 Store 实例的写入也能使本地 WorkingSet 缓存失效。普通事件只能使用软预算，固定证据可以使用软预算与模型真实窗口之间的余量，但不能突破硬上限。任何裁剪都只改变投影，不会修改 EventLog。

### 4.3 Context Orchestrator（运行时控制面）

替换常见实现中“token 阈值 → 辅助模型摘要 → 覆盖 history”的单一路径。它负责：

- 在所有用户消息、模型输出和工具结果后写 EventLog；
- 估算 WorkingSet token，用硬上限保护主模型；
- 暴露 Context Tool schema；
- 生成可追踪的 evidence pack；
- 归档后将覆盖区间从活动 WorkingSet 外置，同时保留 EventLog 原文；
- 在接近上限时执行确定性裁剪，并通过 `context_status` 暴露占用和固定证据警告。

当前版本不会在没有可靠结构化状态时自动生成“保护性摘要”。归档由模型或用户显式触发；若模型没有归档，硬预算仍能避免请求超过 provider 上下文窗口，但被裁剪的旧事件需要通过 `context_search` / `context_retrieve` 找回。自动归档属于后续策略层能力，不能在实现前标记为完成。

### 4.4 Agent-facing Context Tools

| 工具 | 作用 | 关键返回值 |
| --- | --- | --- |
| `context_status()` | 查看预算、工作集组成与最近归档 | token 用量、活动锚点、可用范围 |
| `context_archive(reason, event_range, state_update)` | 主动结束一个子阶段并外置细节 | archive ID、摘要 ID、证据范围 |
| `context_search(query, filters)` | 在 EventLog 中检索 | 命中事件片段及稳定 ID |
| `context_retrieve(event_ids, detail)` | 按需分页展开原文 | 内容、来源、截断标记 |
| `context_pin(event_ids, rationale)` | 将关键证据固定到当前 WorkingSet | 更新后的预算影响 |
| `context_unpin(event_ids)` | 释放不再需要的证据 | 更新后的预算影响 |

工具由 Z-Agent 自研运行时直接注册到会话循环，而不是通过第三方 Agent 框架或插件注入。`context_archive` 的 `state_update` 必须是结构化 JSON（目标、已完成、决策、风险、下一步），方便可靠地构造任务锚点。

### 4.5 Evidence Index 与 Evidence Pack

`context_search` 当前采用两路可解释召回：SQLite FTS5/BM25，以及查询时构造的中文字符/词片段稀疏 TF-IDF 向量；两路通过 RRF 融合，完整短语命中拥有最终排序保护。向量通道只扫描最近的有界非敏感事件，不写入数据库；FTS5 仍负责全历史精确召回。

真正的中文/多语稠密 embedding、向量索引持久化和长期记忆写入策略属于 v2，见 [v2-roadmap.md](v2-roadmap.md)。

`Evidence Pack` 将多个命中组织为带引用的小型证据包：

```json
{
  "purpose": "验证数据库迁移是否成功",
  "items": [
    {"event_id": "evt_104", "excerpt": "migration finished", "source": "terminal"},
    {"event_id": "evt_108", "excerpt": "tests passed", "source": "terminal"}
  ],
  "omitted": 7,
  "expand_hint": "context_retrieve(['evt_101'...])"
}
```

### 4.6 Local Tool Runtime

本地工具运行时由项目自行实现工具注册、JSON Schema 校验、权限检查、超时、输出截断、错误分类和审计。模型只能请求工具调用；是否执行以及执行范围由本地运行时决定。v1 不调用模型厂商托管的代码执行或文件服务。

## 5. 一次长任务的执行序列

```text
1. 用户输入 → 追加 user event → 生成初始 WorkingSet
2. Agent 调用工具 → 原始结果写入 EventLog（大结果外置）
3. Agent 判断一个子阶段完成：context_archive(...)
4. Orchestrator 创建 archive + structured task-state event
5. 下一轮 Prompt 只保留任务锚点、最近尾部与 archive 地址
6. Agent 需要细节：context_search / context_retrieve
7. 任务结束：保存最终结论、引用和显式任务状态；不触发训练作业
8. 若本轮达到工具/时间上限：原子写 checkpoint 并暂停；GUI 续跑成功后以最终 event 解决该 checkpoint
```

## 6. 独立 Runtime 的模块边界

| Z-Agent 区域 | 职责 | 边界策略 |
| --- | --- | --- |
| session storage | EventLog、payload/blob 引用和查询 | repository 接口不依赖 API 与 GUI |
| agent run loop | 记录模型/工具边界事件，由 Orchestrator 构造 messages | 自行解析、执行、终止与错误处理 |
| prompt builder | WorkingSet 投影 | 稳定 system prompt、任务锚点和证据引用顺序 |
| tool registry | 内建注册 `context_*` 和受控本地工具 | 所有执行经本地 schema 校验、超时与审计 |
| compression | 仅生成可展开 archive 节点 | 永不以摘要覆盖 EventLog 原文 |
| provider gateway | 只做 HTTP 协议、能力适配和响应归一 | 无工具执行权，不持有 Agent 策略 |

## 7. 分阶段实施

### Phase 0：技术验证（v1 已完成）

- 搭建独立 Python 原型：EventLog、FTS5、BlobStore、WorkingSet builder。
- 输入录制的长任务轨迹，验证归档后原文可恢复、引用稳定、token 可控。
- 交付：数据模式、CLI 调试工具、20 条以上单元测试。

### Phase 1：独立 Agent 核心（v1 已完成）

- 接入会话写入、Prompt Builder 和 `context_status/search/retrieve/archive`。
- 默认采用“显式 Agent 归档 + 确定性硬预算裁剪”；自动保护性归档仍是后续项。
- 增加显式轮次/截止时间限制和可审计错误事件。
- 交付：可运行的长任务 Agent、端到端测试、可观测日志。

### Phase 2：任务状态与多 Agent（约 1 周）

- 为 archive 建立结构化 task-state schema。
- 子 Agent 获取最小委托包：目标、限制、pinned evidence、可检索范围。
- 交付：父子会话证据追踪、上下文传递 token 对比。

### Phase 3：本地工具与扩展生态

- 扩展本地工具权限、MCP 连接和 Extension Host。
- 所有新增能力保持零训练、可离线测试和可回退。
- 交付：扩展合约测试、供应链审计与跨平台打包。

## 8. 可行性分析

### 8.1 可直接实现：高可行性

| 能力 | 依据 | 风险 | 结论 |
| --- | --- | --- | --- |
| EventLog + SQLite/BlobStore | 常规本地存储工程，不依赖外部 Agent 数据模型 | 迁移与磁盘增长 | 高 |
| 工作集投影与稳定事件引用 | Prompt Builder 的局部重构 | token 估算误差 | 高 |
| `context_*` 工具与 Agent 主动归档 | ACM 的推理时核心，无需训练 | 模型可能不主动使用工具 | 高；用 tool instruction + 硬阈值保护 |
| FTS5 + 稀疏向量融合 | 无额外模型/服务依赖，兼顾精确词与局部短语变体 | 不能替代真正的同义语义 embedding | 高；v1 已实现 |
| 子 Agent 最小上下文委托 | 有明确 task anchor/evidence 模型 | 初期需调整委托 prompt | 高 |

### 8.2 需要实验验证：中等可行性

| 能力 | 关键不确定性 | 缓解办法 |
| --- | --- | --- |
| Agent 自主选择正确归档时机 | 不同模型的工具规划能力差异大 | 记录决策、添加 few-shot、提供保护性归档 |
| archive 的结构化状态质量 | 可能遗漏未决条件 | 强制字段 + 提交前自检 + 来源事件引用 |
| 混合语义检索 | 错误召回可能误导推理 | 先 FTS/过滤，召回结果以 evidence 而非事实注入 |
| 持久 Python kernel | 状态泄漏、安全与恢复复杂 | v1 先不启用；使用现有 sandbox 并引入 checkpoint |

### 8.3 暂不建议首版实现：低可行性/高成本

| 能力 | 原因 |
| --- | --- |
| 任何 post-training / RL 上下文策略 | 超出时间与计算资源约束，且使运行时能力依赖特定训练产物 |
| 服务端 Code Interpreter / Files API | 关键工具逻辑无法本地审计、复现与控制 |
| 完整 Scroll 持久 kernel | 会扩大执行状态和安全边界；v1 只实现 EventLog 与确定性 WorkingSet |

## 9. 风险、数据治理与回退

- **敏感信息：** EventLog 会比摘要保留更多原文。必须在本地写入前检测 API key、token、私钥；BlobStore 加密策略由部署方配置。
- **磁盘增长：** 记录 payload 大小、按会话配额、提供显式导出/清理；清理必须生成 tombstone event，不能悄然破坏引用。
- **错误检索：** Prompt 中始终标示 retrieved evidence 为“历史证据”，不当作已验证事实；保留 event ID 供模型复核。
- **模型不使用管理工具：** 当前由确定性硬预算裁剪保护 provider，并提示模型使用上下文工具；不会凭空生成 archive。v2 只在结构化 checkpoint 完整时考虑自动归档，所有 context tool 调用率必须可观测。
- **回退：** 每个 session 可关闭主动归档，退回“近期消息 + 确定性预算裁剪”；原始事件始终保留，不依赖外部 compressor。

## 10. 验收指标

首版不以论文 benchmark 分数作为唯一成功标准，使用以下可审计指标：

| 指标 | 目标 |
| --- | --- |
| 原始事件可恢复率 | 100%（除明确清理且有 tombstone 的内容） |
| WorkingSet token 峰值 | 不超过配置硬预算 |
| 引用有效率 | `context_retrieve` 返回的 ID 必须可解析且哈希匹配 |
| 长任务完成率 | 与不启用主动上下文管理的内部 baseline 持平或更高 |
| 上下文管理开销 | 不显著增加 p95 轮次延迟；分别记录存储、检索、LLM 调用 |
| 子 Agent 首轮 token | 显著低于全量父上下文复制 |
| 可回退性 | 任一 session 可在不丢原始消息的条件下关闭主动上下文管理 |

## 11. v1 最小里程碑

v1 已完成以下最小切片：

1. 定义 SQLite schema 与 EventLog append API；
2. 实现 `context_archive/search/retrieve` 的本地 CLI；
3. 以一段真实长工具调用轨迹验证“归档 → 搜索 → 原文恢复”；
4. 输出 token/延迟/磁盘占用报告；
5. 接入自研 Agent loop，并以 FakeProvider 做离线闭环测试。

这样先验证“无损可寻址状态”这一核心假设，再逐步承担真实模型行为和扩展执行带来的不确定性。

## 12. 参考

- ACM: Agentic Context Management for Long Horizon Tasks (2026), https://arxiv.org/abs/2607.23809
- Scroll: Context as an Environment: Programmatic Context Management for Long-Horizon Agents (2026), https://arxiv.org/abs/2608.21690

## 13. 中文优先与国产模型适配

### 13.1 设计结论

中文支持不能通过“将 UI 和提示词翻译成中文”解决。长程 Agent 的实际质量取决于：模型是否能稳定地发出结构化工具调用、上下文预算是否按照**该模型的 tokenizer**计数、历史检索能否理解中文词语边界，以及中英混合的代码/路径/API 参数是否被完整保留。CToolEval 的中文 Agent 评测已经表明，中文模型在复杂工具理解和调用上存在明显不稳定性；因此运行时不能假设任一 OpenAI-compatible API 的 tool-call 行为完全一致。

架构选择如下：

1. Agent 内部状态、事件 ID、工具参数和 archive schema 一律采用语言中立的 JSON；**不翻译 JSON key、路径、命令或 API 参数**。
2. 面向模型的说明、任务锚点、archive 摘要和 GUI 默认采用中文；保留用户原文语言与 `language` 元数据，避免不必要的翻译造成信息损失。
3. 预算由 provider 适配器提供 tokenizer / token-count API；缺失时使用保守估算并降低上下文水位，不能仅按字符数判断。
4. 中文检索采用分词索引与字符 n-gram 的混合召回，不能只使用 SQLite FTS5 默认 tokenization。
5. 每个模型版本都必须通过中文工具调用回归集，才能被标记为 `agent_ready`。

### 13.2 China Model Gateway

`Model Gateway` 是外部模型 API 与 Context Orchestrator 之间的唯一边界：

```text
GUI / Agent loop
       │ Canonical ChatRequest + ToolSchema
       ▼
┌────────────────────────────────────────────┐
│ Model Gateway                               │
│ capability negotiation / token accounting   │
│ tool-call normalization / retry policy      │
└───┬───────────────┬──────────────┬─────────┘
    │               │              │
 Qwen API       DeepSeek API    GLM API ...
 OpenAI-compatible / native adapter / local vLLM
```

每个 `ModelProfile` 必须显式声明，而不是从模型名猜测能力：

```yaml
id: qwen-agent-primary
provider: qwen
model: <configured-by-user>
locale: zh-CN
capabilities:
  native_tool_calls: true
  parallel_tool_calls: false
  reasoning_channel: optional
  structured_output: json_schema
  context_window: provider-reported
tokenizer: provider-or-local-exact
context_policy:
  soft_limit_ratio: 0.70
  hard_limit_ratio: 0.82
tool_policy:
  malformed_call: repair_once_then_ask_model
  max_schema_bytes: 24576
```

适配器负责将各厂商的 function call / reasoning 字段归一为内部 `ToolCall`，同时保存**原始响应**到 EventLog，确保解析错误可诊断。对于只支持文本工具调用的模型，提供受约束的 JSON action envelope；解析失败时由一次“只修复 JSON、不继续推理”的重试处理，禁止执行模糊匹配得到的命令。

初期覆盖目标是 Qwen、DeepSeek、GLM、Kimi、MiniMax、Baichuan/本地 OpenAI-compatible 服务；这里的“支持”表示统一请求、流式响应、工具调用、token 预算和诊断，而不是保证所有模型在所有 Agent 任务上的等效表现。模型和能力由可更新的 provider manifest 描述，API 变化不会扩散到核心 Agent loop。

### 13.3 中文检索、摘要与上下文预算

`Evidence Index` 拆为三个索引，并保留统一 event ID：

| 索引 | 用途 | 中文策略 |
| --- | --- | --- |
| Exact / metadata | event ID、文件路径、命令、工具名、时间过滤 | 不做语言处理，保证可重现 |
| Lexical BM25 | 用户问题、摘要、终端文本 | Jieba/THULAC 可配置分词 + 原始字符 2-gram 双字段 |
| Sparse vector（v1） | 短语变体、局部重叠和长度归一化 | 本地 TF-IDF、CJK 2/3-gram、技术标识符分量，无训练、无数据外发 |
| Dense semantic（v2） | 同义表达和中英混合语义召回 | 只使用经过中文/多语验证的 embedding provider，保留 lexical 结果作证据 |

v1 排序采用 RRF（Reciprocal Rank Fusion）融合 BM25 与稀疏向量结果，不让向量通道单独决定证据。对于“银行卡号、产品名、代码标识符、法律条款、路径”等精确词，exact 命中优先于向量相似度。

archive 摘要使用中文字段名和短句，但固定保留：原始 event 范围、关键原文引用、未决问题、决策依据、命令/文件路径和英文技术术语。不可把中文“润色”放在原始日志之前；原文永远是事实源。

### 13.4 中文 Agent 评测门槛

建立 `zh-agent-evals`，包含：

- 中文单/多工具调用、缺失参数追问、JSON schema 约束；
- 中文需求与英文 shell、文件路径、SQL、API 参数混合；
- 长上下文检索：同义词、简称、全角/半角、繁简、日期和数字；
- 归档后取回：必须引用正确 event ID，不能将摘要臆测当事实；
- 模型故障注入：空 tool call、reasoning 中夹带 JSON、错误参数类型、流式中断。

基线参考 SuperCLUE-Agent 与 CToolEval，但上线准入以项目自有、可重放的真实轨迹集为准。每个 provider 运行同一批 case，记录 tool-call AST 正确率、任务完成率、检索精度、中文乱码率、上下文峰值和成本；不能依据通用中文问答排行榜选择 Agent 默认模型。

### 13.5 可行性

| 项目 | 可行性 | 主要风险 | 决策 |
| --- | --- | --- | --- |
| 中文 GUI、中文任务锚点与 archive schema | 高 | 术语一致性 | 首版实现 |
| 国产 API 的 OpenAI-compatible 适配 | 高 | 字段与限额不断变化 | provider manifest + 合约测试 |
| 实际 tokenizer 预算 | 中 | 云端 tokenizer 不公开或版本漂移 | provider 返回优先；保守估算与在线校准 |
| 中文混合检索 | 高 | 分词歧义、专名漏召回 | 分词 + 2-gram + exact 三路融合 |
| 任何国产模型均可靠自主工具调用 | 低 | 模型本身差异和供应商变更 | 按模型评测分级；必要时降级为确认式执行 |

## 14. 桌面 GUI 与打包架构

### 14.1 技术决策

采用 **Electron + TypeScript + React + Vite + npm workspace**，而不直接把 Hermes TUI 嵌入 WebView。理由是 Agent 需要启动本地 Python/Node 子进程、管理 PTY、文件权限、扩展 host 和本地数据库；Electron 的主进程可作为受控桌面宿主，Renderer 只承担界面。它也匹配 DeepSeek Harness 常见的“npm harness + 本地服务 + 桌面包装”交付方式。

Tauri 是可选的体积优化方向，但会把 Python/Node 子进程、PTY、扩展 host 的跨平台治理转到 Rust 边界；对于 v1 的扩展目标，Electron 的工程风险更低。

### 14.2 进程边界

```text
┌────────────────── Z-Agent Desktop (Electron) ──────────────────┐
│ Main process                                                    │
│  - process supervisor / secure credential vault / auto-update   │
│  - permission broker / extension host controller                │
│       │ IPC (typed, allow-listed, no shell passthrough)         │
│ Preload bridge ────────────────────────────────────────────────│
│ Renderer (React)                                                │
│  - Chat / task timeline / context inspector / extension store   │
└───────────────────────┬────────────────────────────────────────┘
                        │ localhost authenticated RPC
┌───────────────────────▼────────────────────────────────────────┐
│ Z-Agent Core service                                            │
│ Agent loop / Model Gateway / Context Orchestrator / EventLog    │
└────────────────────────────────────────────────────────────────┘
```

Core service 是独立可执行进程，GUI 崩溃或重启不应破坏任务和 EventLog。只监听随机本地端口或 Unix domain socket，并使用单次启动令牌；不得将未鉴权的 Agent RPC 暴露到局域网。

### 14.3 GUI 必须支持的上下文能力

- 任务时间线：用户、模型、工具、archive、检索和审批事件都有稳定 event ID；
- Context Inspector：显示当前 WorkingSet 的 token 占用、pinned evidence、被归档范围与可展开原文；
- Evidence drill-down：点击引用直接打开原始工具输出/文件差异，而非只显示摘要；
- Model Console：切换 provider profile、显示 tool-call 原始/标准化结果、token 和错误原因；
- Permission Center：工具、MCP server、extension 的权限、来源、哈希、网络域名和最后使用时间；
- 中文优先：完整 CJK 字体回退、中文日期/数字格式，以及中文/英文不互相破坏的 code block 与 Markdown 渲染。

模型思考内容不应抢占主对话，也不得默认展开。Core 保存厂商协议续轮所必需的
`reasoning_content`，最终轮 reasoning 另存为 `assistant_reasoning` 审计事件；前端仅显示默认收起的
“思考过程”披露栏，用户点击后才渲染正文。实时 reasoning delta 不经 Electron IPC 推送，
`assistant_reasoning` 也不进入后续 WorkingSet，避免重复注入模型上下文。工具调用与结果使用另一套
默认隐藏的审计开关。

### 14.4 工作区代码工具安全边界

工作区路径在保存时必须是实际目录，并规范化为绝对路径。所有文件工具再次执行
`resolve`/父目录校验，符号链接也不能逃逸。文件工具只提供有界文本能力：

- 读取、目录概览和关键词检索；二进制、大文件、依赖/构建目录和常见密钥文件受限；
- 在工作区内创建目录和文本文件，或用 `fs_read` 返回的 SHA-256 乐观锁更新已有文件；
- 精确文本替换默认只允许唯一匹配，写入使用同目录临时文件原子替换；
- 不提供删除、任意命令、shell、提权、网络上传或工作区外访问。

工作区文本工具自身不是 OS 沙箱。扩展 Host、MCP stdio 和受控 Runner 均经过独立
Permission Broker 和 OS 沙箱启动器。Runner 不接受 shell 或任意参数，而是将去掉密钥、依赖、
Git 对象与构建目录的项目快照放入临时执行目录，按预定义模板运行，并返回快照 SHA、超时、
截断状态和可引用 event ID。OS 沙箱不可用时默认拒绝执行。

### 14.5 打包与发布

使用 npm workspace 管理 Electron 和 React UI。Python Core 由独立 `environment-runtime.yml` 构建，
`pip install --no-deps` 安装本项目后用 `conda-pack` 生成可重定位 runtime；生产主进程仅启动
`Resources/core-runtime/bin/python`，缺失时 fail closed，不回退系统 Python。

Electron Builder 负责将 runtime、正式图标和 UI 打包进 DMG。主进程用1/2/4 秒退避最多恢复
Core 三次，将最后崩溃原因以 `0600` 写入用户数据目录；`electron-updater` 消费同一 Release 中的
`latest-mac.yml` 和 blockmap。当前项目选择未签名发行：tag CI 显式禁用签名自动发现，
改为强制验证包内 Core、DMG 校验和和版本/tag 一致性，Release 页明确标注 unsigned。

| 项目 | 可行性 | 说明 |
| --- | --- | --- |
| Chat/任务/上下文查看 GUI | 高 | 主流 React/Electron 工程 |
| 本地核心服务守护与断线重连 | 高 | 明确进程监督边界 |
| PTY 与完整终端体验 | 中 | Windows 兼容和权限提示需要专项测试 |
| 一键跨平台签名与自动更新 | 中 | 需要 Apple/Windows 签名身份和 CI 秘密管理 |
| 直接复刻 DeepSeek Harness GUI | 不建议 | 借鉴交互，不复制其实现、品牌或 API 假设 |

## 15. 扩展系统与 Marketplace 兼容策略

### 15.1 结论与边界

“接入常见 marketplace”不是单一技术能力。MCP server、VS Code extension、npm package、Agent Skill 的执行模型和信任模型不同；将它们都当作可在主进程任意执行的代码，会使桌面 Agent 变成高风险的远程代码执行宿主。

因此采用 **统一发现，分生态适配，隔离执行** 的方案：

| 生态 | 目标支持方式 | 市场/来源 | 明确边界 |
| --- | --- | --- | --- |
| MCP | 一等支持，安装为受管 server，stdIO / HTTP transport | 官方 MCP Registry + 自定义 registry | 工具调用经权限 broker；默认不信任 |
| Z-Agent Extension | 一等支持，manifest + Worker/子进程 host | 自建 registry、文件或 Git 安装 | 只暴露稳定 Extension SDK |
| VS Code | 可选 IDE compatibility layer | **Open VSX**、本地 VSIX | 需要 Code-OSS/Theia extension host；不承诺完整 API 覆盖 |
| npm | 作为 Z-Agent extension 的分发来源，不直接任意加载 | npm registry / 私有 registry | lockfile、签名/哈希、allowlist、子进程隔离 |
| Skills | 声明式包，可从 Git/registry 导入 | 自建/兼容 skills 目录 | 文本指令不等于受信任代码 |

Microsoft 的 Visual Studio Marketplace 不能被第三方 Code-OSS 衍生产品默认当作可自由使用的扩展源；Open VSX 是开源、厂商中立的替代 registry。故产品默认连接 Open VSX，并允许用户显式导入本地 `.vsix`；不能把“兼容 VS Code 扩展”宣传为“能安装全部微软 Marketplace 扩展”。部分扩展还依赖微软专有服务或 proposed API，必须列为不兼容或降级。

### 15.2 Z-Agent Extension Contract

扩展不直接获得 Node/Electron/文件系统的完全权限。安装包必须含 `zagent.extension.json`：

```json
{
  "id": "com.example.calendar",
  "version": "1.0.0",
  "entry": "dist/worker.js",
  "contributes": ["tools", "views", "skills"],
  "permissions": ["network:https://api.example.com", "secrets:calendar"],
  "integrity": "sha256-...",
  "minHostVersion": "0.1.0"
}
```

运行模型：

1. Marketplace adapter 拉取 manifest、签名/哈希、许可、发布者和依赖图；
2. 用户在 GUI Permission Center 批准所请求的能力；
3. Extension Host 在受限 Worker/独立子进程中启动扩展；
4. 扩展只经 RPC 请求已批准的 `tools`、`storage`、`network`、`ui` 能力；
5. 每次 Agent 发起的高影响操作仍由核心 permission broker 确认，而非由扩展自行放行。

MCP server 也进入同一许可模型：记录 server 二进制/包哈希、transport、声明工具、网络端点和最后一次工具调用；升级必须重新解析权限差异。

### 15.3 VS Code 兼容性分级

完整移植 VS Code extension host 是大型 IDE 项目，不应阻塞 Agent 产品。采用三档：

| 等级 | 能力 | 可行性 |
| --- | --- | --- |
| A：Open VSX 发现与本地 VSIX 管理 | 搜索、详情、下载、许可/安全信息、导入 | 高 |
| B：Code-OSS/Theia workspace 容器 | 在内嵌或独立 IDE workspace 内运行兼容扩展 | 中；需要维护 extension host |
| C：把任意 VSIX 当 Z-Agent 内核扩展运行 | 直接调用 VS Code API 并改 Agent 行为 | 低；不能作为承诺 |

`0.2.0` 现已实现 Z-Agent 目录/ZIP 安全安装、独立 Python/Node Extension Host、受管 MCP stdio/Streamable HTTP、OAuth 与官方 MCP Registry remote 导入。需要编辑器生态时再实施 B；Open VSX/VSIX adapter 仍未实现。C 仅针对少量目标扩展做适配器，不提供“任意 VSIX”承诺。

### 15.4 扩展安全与供应链

- 默认禁用未签名/未记录来源的扩展自动更新；支持 registry allowlist 与项目级 lockfile。
- 安装前展示 publisher、许可证、权限 diff、依赖、二进制哈希和网络域名；高风险权限需要每次确认或管理员策略。
- 为 npm/VSIX/MCP 生成 SBOM 和安装审计事件；EventLog 记录工具真正由哪个 extension/server 提供。
- 绝不因扩展来自公共市场而视为可信。公共 VS Code/Open VSX 市场均出现过恶意扩展事件，Agent 的文件与命令执行权限使风险更高。
- MCP 只规定工具互操作，不自动解决 server 信任；采用“发现、审查、授权、沙箱、审计”五步入库流程。

### 15.5 可行性

| 项目 | 可行性 | 前置条件/风险 | 排期 |
| --- | --- | --- | --- |
| MCP Registry 搜索、受管安装与调用 | 高 | transport、OAuth 与工具权限治理 | v2 |
| 自有 extension SDK / registry | 高 | 先冻结最小 SDK，避免 API 漂移 | v2 |
| npm 私有/公有包分发 | 高 | lockfile、依赖扫描、子进程隔离 | v2 |
| Open VSX 检索和 VSIX 导入 | 高 | 仅是分发/管理，不等价于运行兼容 | v2 |
| Code-OSS / Theia extension host | 中 | 包体积、升级维护、扩展权限 | v2 |
| 官方 VS Code Marketplace 直接接入 | 不可作为默认方案 | 使用授权与技术兼容性限制 | 不排期 |

## 16. 实施顺序与验收

### v1.0：核心与中文基线（本次实现）

1. Phase 0 EventLog / WorkingSet 原型；
2. Model Gateway 接入国产 OpenAI-compatible 模型与本地 echo/FakeProvider；
3. 中文 lexical + 稀疏 TF-IDF 混合检索与单元/功能回归；
4. 通过“归档后恢复证据”和真实 DeepSeek 多阶段代码任务端到端测试。

### v2.0：生产桌面产品

1. 将现有 Electron shell 与 Core service 打为自包含 runtime，补齐迁移恢复和自动更新；
2. 在现有 Chat、Timeline、Context Inspector、模型设置上补齐证据报告和 Permission Center；
3. macOS 优先签名打包，Windows/Linux 以 CI artifact 验证。

### v2.1：开放生态

1. MCP registry adapter、受管 MCP server 与审批流；
2. Z-Agent Extension SDK、manifest、Worker host、项目 lockfile；
3. Open VSX 发现与 VSIX 导入；不启动完整 VS Code extension host。

**更新后的核心验收指标：**

| 指标 | v2 目标 |
| --- | --- |
| 中文工具调用成功率 | 在固定 `zh-agent-evals` 中按 provider 分别报告；未达阈值不得标为 `agent_ready` |
| 归档后中文证据恢复 | 原文、event ID、哈希 100% 一致 |
| GUI 核心恢复 | Renderer 重启不丢失正在进行的任务事件 |
| 扩展可审计性 | 每个安装/升级/权限批准/工具调用都有来源与哈希记录 |
| 默认供应链风险 | 未授权扩展、MCP 与网络权限均不得自动启用 |

## 17. 新增参考

- CToolEval: A Chinese Benchmark for LLM-Powered Agent Evaluation in Real-World API Interactions, ACL Findings 2024, https://aclanthology.org/2024.findings-acl.928/
- SuperCLUE / SuperCLUE-Agent, https://github.com/CLUEbenchmark/SuperCLUE
- Official MCP Registry, https://registry.modelcontextprotocol.io/docs
- MCP Specification, https://modelcontextprotocol.io/specification/
- Open VSX Registry FAQ, https://www.eclipse.org/legal/open-vsx-registry-faq/
- Using Open VSX in VS Code / Code-OSS products, https://github.com/eclipse-openvsx/openvsx/wiki/Using-Open-VSX-in-VS-Code

## 18. v1 工程约束与质量门槛

Python 使用仓库内 Conda 环境（`.conda/envs/zagent`），依赖由 `environment.yml` 固定；Node 使用 npm workspace 和 lockfile。核心代码按以下边界组织：

```text
src/zagent/
  domain/       # 语言中立的数据模型和错误类型
  storage/      # EventLog、BlobStore 与 SQLite repository
  context/      # 中文检索、token 预算、WorkingSet、context tools
  providers/    # 厂商 API 协议与响应归一，不包含 Agent 策略
  agent/        # 自研工具循环、终止条件、重试和本地工具执行
  extensions/   # manifest、权限与 MCP 配置
  api/          # FastAPI schema 与 route；不承载业务逻辑
apps/
  desktop/      # Electron 主进程与安全 IPC
  ui/           # React renderer
tests/
  unit/         # 纯函数、repository、解析器和权限规则
  integration/  # EventLog + Context + FakeProvider 的 Agent 闭环
  functional/   # 真实本地 HTTP API 与桌面 bridge 合约
```

v1 合并门槛：

- `pytest` 单元、集成和 API 功能测试全部通过，核心分支覆盖率目标不低于 80%；
- 前端 TypeScript strict typecheck 和组件测试通过；
- 禁止业务层 import FastAPI/Electron，禁止 provider 层执行工具；
- 所有模型输出先归一和 schema 校验，再进入自研 Agent loop；
- 最大工具轮次、单工具超时、总任务截止时间和可重试错误必须显式配置；
- 测试使用 FakeProvider 和本地临时数据库，不依赖外部模型 API 或互联网。

## 19. v1 实现状态

截至 2026-08-29，仓库已实现可运行的 v1 基线：

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 自研 Agent loop | 已实现 | 自行完成输出解析、本地工具调度、轮次/截止时间终止和错误映射，不依赖 Agent SDK |
| EventLog / BlobStore | 已实现 | SQLite WAL、追加式事件、稳定 ID、SHA-256 与大内容外置 |
| WorkingSet / context tools | 已实现 | 归档区间从活动投影外置、原文可寻址恢复、固定证据跨归档保留、工具轮完整性与硬上限保护；中文 BM25 + 稀疏 TF-IDF 向量融合无需训练 |
| v2 checkpoint / DB cache version | 已实现 | SQLite 持久 context/workspace version；模型与工具 schema SHA 参与 WorkingSet 缓存；GUI 按 revision CAS 写入，过期返回 409；checkpoint 可续跑，invocation 幂等回放 |
| 国产模型接入 | 已实现协议层 | 支持 OpenAI-compatible endpoint；API 客户端只负责 HTTP，不托管工具执行 |
| 工作区代码工具 | 已实现 | 安全读取/检索/创建目录、SHA-256 版本锁写入与精确替换；敏感文件、`.git`、依赖/缓存目录、二进制、路径逃逸、删除与执行均拒绝 |
| 桌面 GUI | 已实现可测试基线 | Electron + React，包含响应式会话/聊天/检查器、停止生成、模型与扩展配置、Markdown 安全渲染；Thinking 与工具记录分别默认收起 |
| 扩展生态 | v2 可执行切片 | 独立 Python/Node Host；逐次 Permission Broker；MCP stdio/Streamable HTTP、OAuth PKCE、官方 Registry remote 导入；CycloneDX SBOM、本机 Ed25519 安装签名与 fail-closed OS 沙箱。Open VSX/VSIX 与发布者公钥信任链仍待完成 |
| 受控 Runner | 已实现 | 固定 Python/Node 测试模板、逐次授权、去敏快照、禁网 OS 沙箱、超时/输出/快照限额和 EventLog 证据引用；沙箱不可用时 fail closed |
| 测试 | 已实现 | 164 个 Python 单元/集成/功能测试，13 个前端交互/Markdown 测试，核心覆盖率超过 80%，Ruff、类型检查与生产构建通过；真实 DeepSeek 同会话 10 次 checkpoint 后完成 |

本版明确不含任何训练流程，也不把 Hermes 或其他现成 Agent 产品作为运行依赖。真实厂商 API 的联网验收需要由用户提供 endpoint、model 与 API key。第三方执行采用三道门：server/extension 启用、独立 Host/transport、逐动作 Permission Broker；stdio/extension 还需 OS 沙箱可用。当前 macOS 测试宿主禁止嵌套 `sandbox-exec`，自动化验证了 fail-closed 路径；在普通桌面宿主上会先探测再运行。

当前源码构建的 arm64 DMG 已内置可重定位 Python Core runtime 与正式图标，并通过实际启动、崩溃恢复和磁盘镜像校验。`v0.2.1` 起的发布策略为未签名自包含 DMG：无需 Apple 账号，但需要用户承担 Gatekeeper 手动放行和自动更新可能受 macOS 安全策略限制的产品取舍。
