# Z-Agent 改动概览（for Codex）

> 面向后续接手者的快速导航。基线：`eda4ff1`（"merge remote repository initialization"）。
> 本分支相对基线已完成 v1 验收并进入 v2 首轮实施，覆盖后端核心、前端交互、上下文管理、工作区安全和真实长任务验收。

## 一分钟速览

| 主题 | 状态 | 关键文件 |
|---|---|---|
| DeepSeek 400 修复 | ✅ | `src/zagent/config.py`、`providers/openai_compatible.py`、`providers/parser.py` |
| 多模型配置与切换 | ✅ | `src/zagent/config.py`、`bootstrap.py`、`api/app.py`、`apps/ui/src/App.tsx` |
| 工作区（安全边界） | ✅ | `storage/schema.py`、`storage/sqlite_store.py`、`api/app.py`、`App.tsx` |
| 文件系统工具（安全读写） | ✅ | `src/zagent/agent/fs_tools.py` |
| 流式输出（SSE） | ✅ | `providers/openai_compatible.py`、`agent/runtime.py`、`api/app.py`、`apps/desktop/*` |
| 上下文管理修复 | ✅ | `context/working_set.py`、`context/orchestrator.py`、`storage/sqlite_store.py` |
| 前端交互打磨 | ✅ | `App.tsx`、`styles.css`、`Markdown.tsx`（新） |
| 扩展安全导入 + MCP stdio | ✅ v2 切片 | `extensions/manifest.py`、`extensions/mcp*.py`、`agent/mcp_tools.py` |
| 回归测试 + 验证报告 | ✅ | `tests/**`、`docs/verification-report.md` |

## 各主题详情

### 1. DeepSeek 400 修复（最早一轮，全部保留）
- **根因一**：配置里模型名是 `deepseek`（无效 ID），官方 `deepseek-chat`/`deepseek-reasoner` 已于 2026-07 停用 → 自动迁移为 `deepseek-v4-flash`、端点归一化为 `https://api.deepseek.com`（`config.py` 的 `ModelSettings` validator）
- **根因二**：思考模式工具轮续轮必须原样回传 `reasoning_content`，否则 400（`runtime.py`、`working_set.py`、`parser.py`、`domain/models.py`）
- **根因三**：`context_archive` 的 system 摘要事件插在 assistant tool_calls 与 tool 回复之间 → 工作集构建时排除 archive 事件
- **根因四**：窗口截断可能拆散工具轮配对（tool 消息缺 caller）→ `WorkingSetBuilder._drop_broken_tool_rounds` 保证轮次完整
- 错误透出：provider 捕获服务端 400 正文并透传到 UI（`_error_detail`）

### 2. 多模型配置
- `AppSettings` 增加 `models: List[ModelSettings]` + `active_model_id`，旧单 `model` 自动迁移
- API：`POST /v1/models`（创建并激活）、`PATCH/DELETE /v1/models/{id}`、`POST /v1/models/{id}/activate`；旧 `/v1/config/model` 保留兼容
- 每模型独立 API key 槽（`ZAGENT_MODEL_<id>`）；前端顶栏切换下拉 + 设置弹窗模型管理

### 3. 工作区（安全边界）
- 新表 `workspaces`（id/name/path），`sessions.workspace_id`（旧库自动迁移）
- API：`GET/POST /v1/workspaces`、`PATCH /v1/workspaces/{id}`
- **安全边界**：`workspace.path` 是 agent 文件工具的唯一可访问目录（resolve 后必须位于根内，含符号链接防护）
- 前端：侧边栏工作区切换 + 新建/编辑弹窗（Electron 原生文件夹选择器 `dialog:select-folder`）
- 注意：**Electron 不支持 `window.prompt/confirm`**（返回 undefined）——所有确认/输入框必须用自定义 Modal（`ConfirmDialog`/`CreateWorkspaceModal`）

### 4. 文件系统工具（`fs_tools.py`，安全读写）
- 读取：`fs_project_overview` / `fs_list` / `fs_read` / `fs_search`；修改：`fs_write` / `fs_replace`
- 路径穿越与符号链接逃逸防护；跳过 `.git`/依赖/构建目录；禁止 `.env`、凭据、私钥、二进制等敏感目标
- `fs_read` 返回完整内容 SHA-256；更新已有文件必须携带该版本指纹，过期写入会被拒绝
- 同目录临时文件 + `fsync` + `os.replace` 原子落盘；没有删除、命令执行或任意 shell 工具
- system prompt 注入当前工作区路径与工具能力说明（无路径时明确告知）
- `fs_search` 对 >512KB 文件只扫头部 32KB 并标注 `partial`

### 5. 流式输出
- provider：`complete_stream`（SSE 解析，reasoning/tool_call delta 累积，`ModelResponse` 组装）
- runtime：`send_stream` 生成器（工具轮不转发 delta、最终回复轮逐块转发），与 `send` 共享内核（`_RoundState`/`_complete_round`/`_run_tool_round`/`_finalize`）
- API：`POST /v1/sessions/{id}/messages/stream`（SSE，错误以 `{"type":"error"}` 事件下发）
- Electron：`main.cjs` 的 `core:stream` IPC 逐块转发（`preload.cjs` 暴露 `requestStream`）
- 前端：fetch ReadableStream 解析，`streaming` 气泡逐块渲染 + 闪烁光标

### 6. 上下文管理
- **pinned 预算保护**（采纳外部 review）：
  - 硬上限：整个工作集（system+pinned+recent）≤ `context_window`，超限 pinned 进 `dropped_pinned_ids`
  - 入口拦截：`context_pin` 校验 token 总量 ≤ 预算 30%，超限返回可读错误
  - 可观测：`context_status` 返回 `pinned_tokens` + `warning`；前端 inspector 警告条
- **归档原子性**：`create_archive` 的 summary 事件 + archives 行同一事务（提取 `_insert_event`）
- **工作集缓存**：store 维护 per-session `context_version`（append/pin/unpin/archive 递增），builder 按版本缓存
- **检索排序**：强 token（词/bigram/技术符号）与单字分级加权，完整短语命中 -50 置顶
- 事件时间/会话相对时间显示、工作集列表可取消固定
- **归档真正压缩活动投影**：archive 覆盖区间不再进入最近事件尾部；固定事件会跨归档加回，原事件仍保留在 EventLog 并可搜索/取回
- **批量固定原子化**：事件去重、跨会话预校验、重复固定不重复计费，整批一次写入/失效缓存
- **统计修正**：裁掉不完整工具轮后重新计算工作集和固定 token；`context_status.archive_stats` 暴露已外置事件数与 token
- **缓存边界修正**：工作区路径变化会使关联会话的 system prompt 缓存失效
- **内部事件防注入**：`model_raw`、思考过程、archive 摘要和 `sensitivity=internal` 事件在 pin 入口被拒绝；WorkingSet 对旧库脏 pin 再做一次排除

### 7. Markdown 渲染
- `apps/ui/src/Markdown.tsx`：`marked` + `DOMPurify`（防 XSS），assistant 消息渲染；代码块语言栏、复制按钮、表格横向滚动和安全外链
- 新依赖：`marked@18`、`dompurify@3`（已写入 `apps/ui/package.json`）

### 8. 全面复验修正
- 修复 `context_status` 公共响应字段：内部 `token_estimate` 统一转换为前端契约 `working_set.tokens`，解决 inspector 明明列出工作集事件但占用仍显示 0 的问题。
- 修正 `fs_search` 大文件语义：超过 512KB 时只读取头部 32K 字符；命中项明确返回 `partial: true`，并补充“头部命中/尾部不误报”测试。
- Electron Core 启动改为单例 Promise、可配置 120 秒超时；超时或启动失败会终止子进程并清理状态，避免慢机器上的 15 秒误判和残留 Core。
- 前端首屏测试改为同步断言，移除对静态文本不必要的 `waitFor`。

### 9. 前端、思考折叠与工作区强化
- 重做 Electron 三栏布局、顶部栏、时间线、输入区、弹窗、错误条和响应式 Context Inspector；聊天内容固定在可读宽度，不再偏右或贴边。
- 模型 `reasoning_content` 会保留为可审计事件，但 UI 永远默认收起，只有用户点击“思考过程”才渲染正文；点击停止不会自动展开。实时 reasoning delta 不向 renderer 推送，工具记录也独立默认隐藏。
- 增加停止生成：renderer → preload → Electron main 的 AbortController 链路，取消不是错误且会刷新已持久化事件。
- 工作区切换立即清空旧会话状态；刷新始终携带当前 workspace ID；新增/编辑路径会在保存前验证为真实目录并规范化。
- OpenAI-compatible provider 对网络错误、429 和 5xx 做有限指数退避；400 等客户端配置错误不重试，直接展示服务端原因。
- Electron 固定为 `44.0.0`，electron-builder 可生成 macOS arm64 DMG。

### 10. 归档语义与检查器可观测性
- Context Inspector 的归档卡片显示已外置事件数与估算 token，并明确“结构化状态进入系统提示词、原文按 event ID 恢复”
- 固定证据提示改为“跨归档优先保留，超过真实上下文硬上限会警告”，不再暗示无限保留
- 架构文档区分当前的显式归档/硬预算裁剪与尚未实现的自动保护性归档，消除实现状态的过度承诺

### 11. 无训练混合向量召回
- 新增 `HybridRetriever`：SQLite FTS5/BM25 + 中文稀疏 TF-IDF 向量 + RRF 融合
- 稀疏向量包含 CJK 单字低权重、2/3-gram、规范化词片段与路径/代码标识符分量；无需训练、模型下载、NumPy 或向量数据库
- exact phrase 最终排序保护，结果暴露 `channels`、`fusion_score`、双路 rank 和 vector similarity，便于审计
- 向量通道只读取最近 1000 个非敏感事件；内部响应和 Thinking 不参与向量化，FTS5 保持全历史回退
- 数据库持久化向量索引、稠密多语 embedding 和长期记忆明确进入 `docs/v2-roadmap.md`

### 12. 空工作区项目创建与路径防护
- 新增 `fs_mkdir`，允许 Agent 在工作区内递归创建项目目录，从空目录构建标准 `src/`、`tests/` 结构
- `.git`、依赖目录、缓存和构建产物不仅从列表/搜索隐藏，直接路径读取和写入也会被核心拒绝
- `fs_mkdir` 复用工作区 resolve 与符号链接边界，不能创建工作区外或敏感目录

### 13. DeepSeek 工具参数协议修复
- 若 provider 在工具执行前发现 `tool_calls[].function.arguments` 不是合法 JSON，追加严格修复指令并只重试一次
- 第二次仍非法立即返回协议错误；任何未经 JSON 解析和工具 schema 校验的参数都不会执行
- 流式路径遇到非法参数时停止本轮并走同一受限修复，不再把原始文本包装成 `_raw` 交给工具
- 新增非流式修复、二次失败终止和流式修复三类单元测试

### 14. v1 验收与真实长任务
- 新增 `scripts/long_task_e2e.py`，使用现有 SecretStore 中的真实 provider 配置运行 build/audit/extend/finalize 多阶段任务，不读取或输出密钥
- 真实 DeepSeek 从空目录创建 `taskboard` 项目；首轮 24/29 通过，经外部失败反馈修复为 33/33，再扩展为 44/44
- 同一 session 建立两次 archive、完成 pin/unpin，并在归档后继续修改最新代码；最终 135 个事件、无固定事件
- 隔离 venv editable install、console script、`python -m` 和手工 CLI 流程通过
- `docs/v1-acceptance.md` 明确已完成边界；`docs/v2-roadmap.md` 收纳 checkpoint、runner、记忆、稠密向量、扩展 host 和生产发布

### 15. 代码高亮与工作区初始页
- Markdown fenced code 接入 highlight.js，已知语言按声明高亮、无语言代码自动识别；保留 DOMPurify、语言标签和复制按钮
- 新建工作区时使旧异步请求失效并清空旧 sessions/events/context/streaming，显示独立的“开始一个新任务”初始页
- 工作区下拉新增可访问名称；新增语法 token 和“从活跃会话创建工作区”交互回归

### 16. v2 首轮：持久缓存版本与可恢复 checkpoint

- `sessions.context_version` 改为 SQLite 持久列；旧库自动迁移，Core 重启与多 Store 实例能观察同一单调版本。
- 新增 `checkpoints` 结构表与审计 event；达到工具轮次/时间上限时记录目标、已执行工具 event ID、待办、文件 SHA、失败原因与 archive ID。
- 未解决 checkpoint 只作为系统状态注入，不插入 provider 工具轮；成功续跑后以最终 event ID 标记解决。
- Electron GUI 显示持久恢复条和“继续任务”，Context Inspector 显示已完成/待办数；checkpoint 原始 event 不作为聊天气泡重复显示。
- highlight.js 从整套 common 语言改为 15 种常用开发语言，JS 从约 467KB/150KB gzip 降到 390KB/123KB gzip。
- Python Core 版本元数据与 Electron 统一为 `0.1.1`，API/health 从单一 `__version__` 读取。
- 真实 Electron 启动烟测暴露并修复共享 SQLite connection 的并发读取竞态：events/context 同时请求不再出现 `InterfaceError` 或错误空态。

### 17. v2 工具 invocation 幂等保护

- 新增 `tool_invocations` 持久表，记录 `session_id + call_id + tool_name + arguments_sha256 + status + result_event_id`。
- 首次 claim 才能执行本地工具；已完成的相同调用会追加带原始 result event ID 的 replay 工具回复，不再执行工具。
- 重用 call ID 但改变工具/参数时返回 conflict；调用已 claim 但未持久结果时返回 uncertain，要求先读当前文件再用新 call ID。
- 工具 result event 与 invocation completed 在同一 SQLite 事务落库；覆盖 replay、参数冲突和崩溃窗口三类回归。
- 新 checkpoint 会 supersede 同 session 的旧未解决 checkpoint；故障注入测试连续 3 次暂停后完成，旧恢复点不会重新浮现。

### 18. v0.2.0 扩展安全导入与受管 MCP stdio

- 项目版本统一提升到 `0.2.0`；这表示进入 v2 开发，不表示 v2 全部完成。
- 扩展支持真实目录与 ZIP 导入：拒绝路径穿越、符号链接、加密 ZIP、超大文件/包和多根 manifest；在安装根暂存校验后原子落盘，覆盖升级失败可恢复旧版本。
- 每个安装记录内容 SHA-256、来源类型/名称与 UTC 安装时间；启用状态、安装元数据和包内容均可在 Core 重启后恢复。
- 自研 MCP stdio JSON-RPC 客户端实现 `initialize`、版本协商、`notifications/initialized`、分页 `tools/list`、`tools/call`、超时取消、4 MiB 帧限制、stderr 限额和分级进程关闭，不依赖 MCP/Agent SDK。
- MCP 配置区分 `enabled` 与 `approved`；命令以参数数组直接启动且不经过 shell，环境变量只按名称 allowlist 传入。批准的工具以命名空间别名加入原生 Agent tool-calling loop。
- Electron 增加扩展目录/ZIP 原生选择器、包 SHA 展示、启停、MCP 授权、连接测试和工具清单；HTTP/SSE 会明确显示当前只保存配置。
- 真实验收使用运行中的 Core HTTP API 导入中文扩展，启动独立 MCP server 子进程，协商 `2025-11-25`，发现并调用 `echo`；Core 重启后再次恢复扩展与 MCP 工具。

### 19. v0.2.0 扩展 Host、权限、HTTP/OAuth 与供应链

- 新增独立 Python/Node Extension Host；扩展入口只在子进程加载，以 MCP 风格 JSON-RPC 暴露工具，Core 与 Electron 不导入第三方模块。
- 新增 SQLite Permission Broker：请求绑定 subject/action/session/参数 SHA-256，支持 once/session/always、撤销与完整 audit；MCP 和扩展工具统一逐动作检查。
- MCP Streamable HTTP 实现 JSON/SSE response、session/protocol headers、Bearer 与 DELETE；OAuth 实现 protected-resource/authorization-server discovery、PKCE S256、state/resource、refresh 与动态客户端注册。
- 接入官方 MCP Registry v0.1 搜索、版本详情和 Streamable HTTP remote 导入；只读线上搜索和真实 remote 映射通过，包命令不自动执行。
- 扩展导入生成 CycloneDX 1.7 SBOM，并用数据目录 trust key 做 Ed25519 安装签名；内容/SBOM/签名篡改阻断执行。
- stdio/Extension Host 默认使用 macOS sandbox-exec 或 Linux bubblewrap；引擎不可用或宿主禁止嵌套时拒绝启动，用户必须显式关闭 sandbox 才能承担风险。
- Electron 增加 Permission Center、Registry 搜索/导入、Extension Host 工具发现、Streamable HTTP/OAuth 配置和系统浏览器 OAuth 回调。
- 门禁结果：154 个 Python 测试、80.95% 分支覆盖率、12 个 Vitest、Ruff、TypeScript strict、Vite production build 和 Electron main/preload 语法检查全部通过。

## 验证状态

- Python：**139 个测试通过**，分支覆盖率 **82.65%**（门槛 80%），Ruff 全绿
- 前端：**11 个 Vitest 测试** + strict typecheck + Vite production build 全绿
- 扩展/MCP E2E：真实 Core HTTP → 安全导入 → MCP stdio 子进程 → 工具调用 → Core 重启恢复通过
- E2E（真实 DeepSeek）：从空工作区创建项目 → 外部测试反馈修复 → 归档/固定 → 二阶段扩展 → 44/44 项目测试与安装级验证
- 详细验证记录：`docs/verification-report.md`（12 轮迭代逐项记录）

## 接手注意事项

1. **运行实例需重启 Electron 才能加载新代码**（用户本地应用跑的是旧构建）
2. 工作区必须设置路径后 agent 才能读文件（侧边栏 ✎ 编辑）
3. `WorkingSetBuilder` 投影仍保存在进程内，但缓存键已同时绑定 SQLite context/workspace revision、模型配置 SHA 和工具 schema SHA；GUI 使用 context revision 做乐观并发控制
4. 测试基础设施（CDP 浏览器驱动、SSE 代理桥）是临时脚本，未入库；`tests/` 内的都是持久化测试
5. `.conda/envs/zagent` 是本地 conda 环境；`estimate_tokens` 中文 1 字符 ≈ 1 token（口径已在工具描述注明）

### 20. 自包含桌面发布、受控 Runner 与跨进程一致性

- 新增独立 `environment-runtime.yml` 与 `scripts/build_core_runtime.sh`；构建脚本在临时 Conda 环境中安装运行时依赖和 Z-Agent，再用 `conda-pack` 生成可重定位 `dist/core-runtime`。
- 生产 Electron 只启动包内 `Resources/core-runtime/bin/python`，runtime 缺失即拒绝启动；不再回退到目标机的 Conda/系统 Python。
- 加入正式应用图标、hardened runtime/entitlements、`@electron/notarize`、`electron-updater`、Core 1/2/4 秒退避恢复与 `0600` 崩溃诊断。tag CI 强制 Apple 凭据，并验证 codesign/stapler/spctl/DMG 后上传更新元数据、SBOM、源码归档与校验和。
- 新增 `runner_execute`：只允许三种固定测试模板，每次经 Permission Broker，仅在去敏快照内运行，禁止网络并限制超时、输出、文件数和总大小；结果携带 provenance、快照 SHA 和 event ID。
- `WorkingSet` 缓存键加入模型配置与工具 schema 版本；workspace revision 独立持久。消息 API 支持 expected context revision，并发冲突返回 409，Electron GUI 已传递该 revision。
- 真实 `deepseek-v4-flash` 同一 session 连续写入 10 个 checkpoint 后续跑完成，最终文件 SHA 由外部校验，数据库无 active checkpoint。

### 21. v0.2.2 未签名自包含 Release

- 根据项目发布决策取消 Apple Developer ID 和公证凭据门禁；tag CI 显式设置 `CSC_IDENTITY_AUTO_DISCOVERY=false`。
- CI 仍强制校验 tag/项目版本一致、包内 Python Core 可执行与可导入、DMG 校验和，并生成 blockmap、自动更新元数据、CycloneDX SBOM、源码归档和 SHA256SUMS。
- Release 说明会明确标记 unsigned，不冒充 Apple 签名/公证产物；首次启动可能需要用户手动通过 Gatekeeper。
- `v0.2.1` tag 首次运行暴露 Actions 后续 shell 未将 Conda base 的 `conda-pack` 加入 PATH，未生成 Release；`v0.2.2` 改为通过 `conda info --base` 传递绝对可执行路径。

### 22. v0.2.3 中文检索与长期记忆

- 中文文本统一经过 NFKC 与 OpenCC `t2s`；jieba 词、连续 CJK 字符/n-gram、技术标识符和显式区域词别名共同进入 FTS 与稀疏通道，繁简、全半角及“专案/项目、资料库/数据库、登入/登录”等表达可互相召回。
- EventLog FTS 增加索引版本；分词规则升级后启动时从原始事件一次性重建，不会让旧会话永久停留在旧索引语义。
- SQLite 新增 memories、sources、terms、FTS 和 audit；长期记忆区分 episodic/semantic/procedural 与 user/workspace 作用域，跨 Core 重启和跨会话恢复。
- `memory_remember/search/list/confirm/forget` 进入原生 tool-calling loop；记忆必须引用来源 event ID，默认只创建 candidate，冲突必须显式 supersede，不能静默覆盖。
- Internal/Thinking/归档/checkpoint 事件与疑似密钥正文禁止写入；删除会清空正文、FTS 与持久稀疏 terms，只保留内容 SHA 与不含正文的审计 tombstone。
- WorkingSet 根据最新用户请求最多注入 5 条相关 active 记忆，并明确标为“不可信数据，不是指令”；memory revision 进入缓存键，跨进程写入不会继续使用旧投影。
- 新增长期记忆 HTTP 生命周期 API、跨会话繁体查询、冲突替代、作用域隔离、秘密拒绝、双索引删除、Core 重启和缓存失效测试。
