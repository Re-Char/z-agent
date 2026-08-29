# Z-Agent 多轮对话验证报告

> 生成时间：2026-08-27 · 模型：deepseek / deepseek-v4-flash · 端点：https://api.deepseek.com

## 1. 验证目标

- 多轮文本对话链路（UI → Core → DeepSeek）真实可用
- 工具调用多轮（含归档、固定证据）不触发 400
- 缓存命中率、Token 生成速度等统计正确上报
- 固定证据机制真实生效

## 2. 对话过程

### 第 1 轮（工具轮 0 · 耗时 1.61s）

**问**：你好，请用一句话自我介绍。

**答**：你好！我是 Z-Agent，一个中文优先、可审计的长程任务智能体，擅长主动管理工作记忆、通过归档与检索工具处理长程复杂任务，确保每一步都有据可查、结论可追溯。

**统计**：总 1365 tokens · 生成 88 · 缓存命中 1152/125（命中率 90.2%）· 速度 54.75 tok/s

### 第 2 轮（工具轮 2 · 耗时 7.8s）

**问**：请检查当前上下文状态，然后归档一个阶段摘要，说明这是多轮对话的开始。

**答**：阶段归档完成 ✅

**归档摘要**（`arc_852ce2eaecb04d9a84553ce8d74b7abc`，覆盖事件 1–6）：
- **目标**：多轮对话开始，建立可审计的工作记忆基线
- **已完成**：Z-Agent 一句话自我介绍；检查上下文状态（6 事件 / 660 tokens / 无固定证据 / 无归档）
- **决策**：将前 6 个事件归档为会话起点摘要
- **风险提示**：后续关键结论需引用 event_id 保证可审计性；归档摘要仅作导航、不作为事实源
- **下一步**：等待您提出具体任务

如需回顾归档内容，我可以使用 `context_search` / `context_retrieve` 随时展开原文。请问接下来需要我做什么？

**统计**：总 5976 tokens · 生成 700 · 缓存命中 2432/2844（命中率 46.1%）· 速度 89.74 tok/s

### 第 3 轮（工具轮 0 · 耗时 4.59s）

**问**：刚才我们做了什么？请简短总结。

**答**：简单回顾刚才的会话（可引用归档 `arc_852ce2eaecb04d9a84553ce8d74b7abc`，摘要事件 `evt_ac1dafd34ce94115923dbee68e53aa24`）：

1. **自我介绍**：说明我是 Z-Agent——中文优先、可审计的长程任务智能体。
2. **检查上下文状态**：确认会话仅有 6 个事件、660 tokens，无固定证据、无归档。
3. **归档阶段摘要**：将前 6 个事件归档为会话起点，明确了后续结论需引用 event_id 以保证可审计性的规则。

也就是说，我们刚刚完成了会话的"启动+基线建立"，现在可以随时开始正式任务。需要我做什么？

**统计**：总 2975 tokens · 生成 420 · 缓存命中 2304/251（命中率 90.2%）· 速度 91.58 tok/s

### 第 4 轮（工具轮 2 · 耗时 6.34s）

**问**：请把本会话最早的那条用户消息固定为关键证据，然后确认。

**答**：✅ 固定完成并确认：

- **固定事件**：`evt_daf746afdacf428e8bbe38c0036ab294`
- **内容**：`你好，请用一句话自我介绍。`（本会话第 1 条用户消息，sequence 1）
- **固定理由**：作为会话起点的关键证据，用于追溯对话起源与后续审计
- **当前固定列表**：`[evt_daf746afdacf428e8bbe38c0036ab294]`（1 项）

该事件已进入工作集且不会被归档清理，随时可作为审计锚点引用。还需要我做其他处理吗？

**统计**：总 20197 tokens · 生成 664 · 缓存命中 13824/5709（命中率 70.8%）· 速度 104.72 tok/s

## 3. 关键验证点

- **缓存命中率**：第 1/3 轮达 90.2%（prompt 前缀命中），第 2 轮因新增上下文降到 46.1%，第 4 轮 70.8% —— 多轮上下文缓存真实生效
- **固定证据**：第 4 轮模型调用 `context_pin`，固定事件为 `evt_daf746afdacf428e8bbe38c0036ab294`（第 1 条用户消息），工作集中 `pinned_event_ids` 确认存在
- **归档链路**：第 2 轮完成 `context_status` + `context_archive`（arc_852ce2...），第 3 轮模型能正确引用归档 ID
- **消息序列**：`assistant_tool_calls` 后紧跟 `tool_result`，无中间 system 消息（此前 400 的根因已修复）

## 4. 复现方式

```bash
# 无网络、可入测试套件：
PYTHONPATH=src conda run --prefix .conda/envs/zagent pytest tests/functional/test_multi_turn_dialogue.py -v

# 真实 DeepSeek 多轮（需要 ZAGENT_API_KEY）：
# 运行 /tmp/za-dialogue/run.py（临时数据目录 + 用户真实配置）
```

## 5. 相关指标接口

`POST /v1/sessions/{id}/messages` 响应新增 `stats` 字段：

```json
{"total_tokens": 20197, "completion_tokens": 664, "cache_hit_tokens": 13824,
 "cache_miss_tokens": 5709, "cache_hit_rate": 70.8, "elapsed_seconds": 6.34, "tokens_per_second": 104.72}
```
## 6. 补充验证（第二轮迭代）

### 6.1 乐观更新时序（真实 DeepSeek，思考 5.3s）

发送消息后立即采样（~300ms）：`events=1, role=user, thinking=true` —— 用户消息**先上屏**、思考动画显示中；完成后 `events=2, role=assistant`，动画消失。符合"先显示输入内容让 AI 想，再输出结果"。

### 6.2 Markdown 渲染（真实模型输出）

模型回复的 markdown 完整渲染：h2 标题×1、粗体×6、行内代码×3、无序列表×1、代码块×1（`marked` + `DOMPurify`，无 innerHTML 注入风险）。

### 6.3 上下文/归档管理审计（tests/functional/test_context_management.py）

| 验证点 | 结论 |
|---|---|
| 归档摘要含 event_range + source_event_ids + state | ✅ 正确落库 |
| 归档状态注入 system prompt（"当前任务状态"） | ✅ 后续轮次模型可见 |
| archive 事件不打断 assistant tool_calls → tool 序列 | ✅（此前 400 根因） |
| 事件超出 recent 窗口：未固定旧事件被挤出工作集 | ✅ 对照测试证明 |
| 被挤出事件仍可通过 context_search/context_retrieve 取回 | ✅ 可审计性成立 |
| 固定事件在挤出 + 归档后仍保留在工作集 | ✅ 固定真正有效 |
| 归档后继续对话，工作集角色序列合法 | ✅ system/user/assistant/tool |

### 6.4 固定证据的用途（对照测试）

- 不固定：事件超出 recent_event_limit 后从工作集消失（不再发送给模型）；
- 固定：该事件始终在 `included_event_ids` 中，跨归档、跨挤压保留；
- 原文永远可从 SQLite + FTS 检索取回（审计底账）。

### 6.5 对齐与自适应（CDP 实测）

- 输入框 `field-sizing: content`：空输入 346px 宽，长输入增长到 642px（上限 660px），不再恒宽；
- checkbox 垂直偏移全部 0px；input/select 高度统一 39px；
- MCP/扩展列表项独立网格（.model-item.plain），无错位占位列。

## 7. 补充验证（第三轮迭代）

### 7.1 工作区（安全边界）

- 首次启动自动创建"默认工作区"；UI 可新建工作区（名称 + 可选路径 `/tmp/project-x`）并自动切换；
- 会话按工作区隔离：项目工作区 0 会话、默认工作区 1 会话（`GET /v1/sessions?workspace_id=` 过滤）；
- v1 数据库自动迁移：旧 `sessions` 表追加 `workspace_id` 列，旧会话归入默认工作区；
- 工作区 path 作为 agent 未来文件工具的安全边界字段存储。

### 7.2 Markdown 代码块复制

代码块渲染为 `pre > code`，悬停显示"复制"按钮（右上角），点击复制源码到剪贴板并显示"已复制"（1.4s 后还原）。实测点击后按钮文案变化正确。

### 7.3 最近归档显示

归档由模型调用 `context_archive` 触发后，inspector"最近归档"区块显示归档卡片：archive_id + 完整 state JSON + 说明"归档摘要已注入系统提示词"。此前用户看不到是因为 echo 演示模型不会调用归档工具；真实 DeepSeek 实测（arc_4ede0fba...）显示正常。占位文案也改为明确说明触发方式。

### 7.4 固定证据 + 右侧直接取消

- 固定后 inspector"当前工作集"中对应项显示 📌 + 内容预览 + "取消"按钮；
- 实测：固定 → pinnedItems=1 → 点击右侧"取消" → pinnedItems=0、统计卡归零；
- 缓存稳定性：相同状态两次构建工作集消息字节级一致（tests/functional/test_context_management.py），确保 DeepSeek 前缀缓存不被无谓失效。

### 7.5 缓存命中率分析（实测数据）

| 轮次 | 命中 | 未命中 | 命中率 | 说明 |
|---|---|---|---|---|
| 1 | 1152 | 125 | 90.2% | 首次请求，命中模型侧常见前缀 |
| 2 | 2432 | 2844 | 46.1% | 注入大量新内容（工具结果+归档），必然 miss |
| 3 | 2304 | 251 | 90.2% | 稳定前缀完全命中，仅新消息 miss |
| 4 | 13824 | 5709 | 70.8% | pin 操作 + 长回复，reasoning 回传增大请求 |

**结论**：DeepSeek 为自动前缀缓存；命中率低只出现在"大量新内容注入"的轮次，稳定前缀轮次均为 90%+，属正常水平。已保证请求字节级稳定（两个新增测试）。提升建议：同一会话连续提问、控制归档频率（归档会更新 system prompt 中的状态段）、了解思考模式工具轮需回传 reasoning_content 会增大请求体。

## 8. 补充验证（第四轮迭代：工作区文件工具 + 流式输出）

### 8.1 文件系统工具（工作区安全边界落地）

Agent 现在具备只读文件工具，**严格限定在工作区路径内**：
- `fs_project_overview` / `fs_list` / `fs_read` / `fs_search`
- 路径穿越防护：`resolve()` 后必须位于工作区根内（含符号链接），越界一律拒绝（8 个单元测试覆盖）
- 跳过 .git / node_modules / __pycache__ / dist 等噪声目录
- 实测（真实 DeepSeek + 项目路径 `/Users/rechar/work/software/预推免/z-agent`）：模型自主调用 overview → list → read 多轮，读完 README.md / package.json 后给出 markdown 优化意见（P0/P1 分级、跨平台兼容性等真实内容分析）

### 8.2 文件夹选择器

Electron 原生 `dialog.showOpenDialog`（`dialog:select-folder` IPC → preload `selectFolder()`），新建工作区时"选择文件夹…"按钮直接弹系统目录选择器，不再手输路径。

### 8.3 思考过程与工具事件折叠

- 工具调用折叠为一行 `🔧 fs_read 参数摘要`，点击展开完整 JSON
- 工具结果折叠为 `⚙️ fs_read 结果摘要`
- 思考过程独立折叠块 `🧠 思考过程（N 字）`（E2E 实测 6 个思考块）
- 实测 34 个折叠项全部可展开

### 8.4 流式逐行输出（SSE）

- 后端：provider `complete_stream`（SSE 解析，含 reasoning/tool_call 增量）→ runtime `send_stream`（工具轮不转发，最终回复轮逐块转发）→ `POST /v1/sessions/{id}/messages/stream`（StreamingResponse）
- Electron：main.cjs 真流式转发（getReader 逐块 → IPC 通道 → renderer）
- 实测（真实 DeepSeek）：流式气泡文字长度采样 [20 → 65 → 141] 逐块增长；工具轮期间显示思考动画；完成后气泡被真实事件替换
- 修复了两个联调 bug：SSE 代理必须逐块透传（.json() 解析 SSE 会 500）；工作集截断必须保持工具轮配对完整（否则 400 "tool 消息必须跟在 tool_calls 后"）

### 8.5 工具轮配对完整性（新修复）

多轮工具调用后事件超出最近窗口时，截断可能把 `assistant_tool_calls` 与其 `tool_result` 拆散 → 400。`WorkingSetBuilder._drop_broken_tool_rounds` 保证：窗口内的工具轮要么完整（caller + 全部回复）要么整体剔除。回归测试 `test_truncation_never_splits_tool_rounds` + 全量 85 测试通过。

## 9. 补充验证（第五轮：工作区路径引导 + 输入框对齐）

### 9.1 输入框/发送按钮对齐

- composer 增加 `gap: 8px`（输入框与按钮间距）；textarea 内边距调整为 11px，单行时文字在输入框内垂直居中
- 实测几何：单行 textarea 46px / 按钮 40px / 底部对齐 / 间距 8px；多行时按钮贴底部（与 ChatGPT 一致）

### 9.2 工作区路径引导（解决"依旧检查不了内容"）

根因：用户会话所在工作区**未设置路径**，且旧代码的 system prompt 未告知模型有文件工具。改进：
- system prompt 注入工作区信息：有路径时告知"当前工作区（可读取）：<path>，有 fs_list/fs_read/fs_search/fs_project_overview 工具"；无路径时明确"未设置路径，请先说明需要设置"
- 侧边栏工作区下拉显示"· 未设置路径"提示；新增 ✎ 编辑按钮 → 编辑工作区弹窗（名称/路径 + 文件夹选择器）→ `PATCH /v1/workspaces/{id}`
- 实测（真实 DeepSeek）：默认工作区"未设置路径" → 编辑选择项目目录 → 下拉更新 → 发"阅读项目文件" → agent 主动调用 fs 工具 28 次（overview/list/read/search），基于真实文件内容给出优化意见（甚至识别出仓库里的孤儿测试文件），无错误

### 9.3 其他

- `PATCH /v1/workspaces/{id}` 路由 + 单元测试
- 全量 86 测试通过

## 10. 补充验证（第六轮：pinned 预算保护 + runtime 重构 + UI 打磨）

### 10.1 pinned 事件预算保护（采纳优化意见）

原缺陷：pinned 事件无条件保留，可击穿 context_window 导致 400；且 context_pin 无数量/体量限制。已实现三层防护：
- **硬上限**：整个工作集（system + pinned + recent）不得超过 context_window；超限的 pinned 事件被丢弃并暴露在 `WorkingSet.dropped_pinned_ids`，`pinned_tokens` 如实上报
- **入口拦截**：context_pin 校验固定事件 token 总量，超过预算 30% 返回可读 ToolExecutionError（含当前/新增/上限数字）
- **可观测性**：context_status 新增 `pinned_tokens` + `warning`（dropped 或 over-budget 时给出明确中文警告）；前端 inspector 显示警告条 + 固定 token 数
- 回归测试：硬上限不击穿（2 个）、pin 拦截（1 个）、over-budget 警告（1 个）

### 10.2 runtime 重构（采纳次要观察）

`send`/`send_stream` 提取共享内核：`_RoundState`（usage 聚合 + stats）、`_complete_round`、`_store_raw`、`_run_tool_round`、`_finalize`、`_check_deadline`。两个公开方法仅保留 provider 调用方式（阻塞 vs 流式）与输出方式的差异，约 200 行重复消除。

### 10.3 前端 UI 打磨

- inspector 上下文警告条（pinned 超限可视化）
- 事件 meta 显示时间（HH:MM）
- 会话列表显示相对时间（刚刚/N 分钟前/N 小时前/N 天前）
- 修复一个崩溃 bug：meter 中 `context?.working_set.tokens.toLocaleString()` 的取值方式导致首次渲染 TypeError（页面空白），改为安全取值
- E2E 回归：fs 工具、折叠、流式、时间显示全部正常，模型基于真实 package.json 输出 monorepo 优化意见

## 11. 补充验证（第七轮：按优化清单逐项处理）

### 核对结论（清单备注项）

清单假设"运行实例已有 dropped_pinned_ids/pinned_tokens/warning 而工作区源码没有"——**核对后不成立**：当前工作区源码 `domain/models.py`（WorkingSet 字段）、`context/working_set.py`（_hard_cap 逻辑）、`context/orchestrator.py`（pin 拦截 + warning）已包含全部实现（上一轮落地），运行实例与源码一致，无需回填。清单 P0 与 P1#2 在上一轮已实现，本轮起于 P1#3。

### 本轮实现

| 项 | 实现 | 测试 |
|---|---|---|
| P1#3 归档原子性 | append_event 提取 `_insert_event`（无事务管理），create_archive 的 summary 事件 + archives 行放入同一事务；任何一步失败整体回滚 | 强制 DROP archives 表 → create_archive 抛错 → 无孤儿 archive 事件 |
| P2#4 工作集缓存 | store 维护 per-session `context_version`（append_event/pin/unpin/create_archive 时递增），WorkingSetBuilder 按版本缓存 build 结果 | 两次 build 返回同一对象；append/pin 后重建 |
| P2#5 检索排序 | 强 token（jieba 词/bigram/技术 token）与弱 token（单字）分级；完整查询短语命中 -50、强词 -5/个、弱字 -1/个，bm25 基础上加权重排 | "数据库迁移"精确命中排第一 |
| P3#6 口径说明 | fs_read/context_retrieve 工具描述注明"按字符计，中文 1 字符 ≈ 1 token" | 描述文案 |
| P3#7 fs_search partial | 文件 >512KB 只扫头部 32KB（性能权衡）在返回项标注 `partial` 字段 | partial 字段断言 |

### 回归

- 全量 95 测试通过（新增原子性 1、缓存 2、排序 1、partial 1）
- 真实 DeepSeek E2E：fs 工具读 package.json 给出具体优化意见（"去掉 test:core 的 PYTHONPATH 前缀"）、流式输出、无警告无错误

## 12. 全面复验（第八轮：DeepSeek 链路、契约与启动健壮性）

### 12.1 真实 Electron → DeepSeek 链路

- 普通流式请求实测：输入“请只回复：链路正常”，返回“链路正常”，无 400/500。
- thinking + tool calling 实测：模型先调用 `context_status`，工具结果返回后继续生成“工具链路正常”；说明 assistant tool round 的 `reasoning_content` 已被正确保存并回传。
- 当前有效模型为 `deepseek-v4-flash`，官方端点归一化为 `https://api.deepseek.com`。

### 12.2 本轮发现并修复

| 问题 | 根因 | 修复与回归 |
|---|---|---|
| inspector 工作集占用显示 0 | 后端返回 `token_estimate`，前端读取 `tokens` | orchestrator 在公共响应边界转换字段；API、orchestrator、超预算测试均断言 `tokens` |
| `fs_search` 的 partial 文档与实现不一致 | 大文件此前被直接跳过，返回项永远 `partial: false` | >512KB 只读头部 32K；头部命中返回 `partial: true`，尾部不误报 |
| 慢环境 Electron Core 15 秒误超时 | 固定超时过短，失败后启动状态/子进程清理不完整 | 默认 120 秒且支持 `ZAGENT_CORE_START_TIMEOUT_MS`；共享启动 Promise；失败时终止并清理 |
| 前端首屏测试慢环境伪超时 | 静态首屏断言使用了不必要的异步轮询 | 改为同步断言 |

### 12.3 最终回归（2026-08-28）

- Python 全量：96 passed，0 failed（0.46s）；覆盖率上一轮为 84.14%，门槛 80%。
- Python 定向：34 passed（fs/context/API/working set）。
- Ruff：通过。
- UI Vitest：1 passed（45ms）；TypeScript typecheck：通过。
- Vite production build：通过，JS 295.90KB（gzip 94.37KB），CSS 16.49KB（gzip 4.45KB）。
- Electron `main.cjs` / `preload.cjs` 语法检查：通过。
- 已知非功能警告：FastAPI TestClient 依赖触发 Starlette 的 httpx 弃用提示，建议升级测试栈。

## 13. 前端与工作区安全复验（第九轮）

### 13.1 已实现修正

- Electron 桌面布局重构：聊天列居中并限制可读宽度，composer 与正文同轴；检查器在窄屏变为带遮罩的右侧抽屉。
- 思考折叠：事件历史保留 `reasoning_content` 供用户审计，但默认只出现“思考过程”折叠栏；实时 reasoning SSE 不转发，工具记录独立默认隐藏，工作集预览只显示安全摘要。
- Markdown：DOMPurify 消毒，代码块语言栏/复制按钮，表格滚动容器，外链使用 `noopener noreferrer`。
- 工作区：切换时清除旧事件；会话刷新按 workspace 过滤；路径保存前验证；文件读取返回 SHA-256，已有文件更新采用版本锁与原子替换。
- 操作体验：支持停止生成、错误提示关闭、Escape/遮罩关闭弹窗、目录未设置时提供明确 CTA。
- 稳定性：429/5xx/网络错误有限重试，400 不重试；Electron Core 单例启动与 120 秒上限；Electron 版本固定以保证可打包。

### 13.2 自动化结果（2026-08-28）

- `npm test`：通过。
- Python：102 passed，覆盖率 83.63%（门槛 80%）；Ruff 通过。
- UI：6 passed；TypeScript strict typecheck 通过。
- Vite production build：JS 300.54KB（gzip 95.74KB），CSS 20.26KB（gzip 5.46KB）。
- Electron `main.cjs` / `preload.cjs` 语法检查通过。
- electron-builder：成功生成 `dist/desktop/Z-Agent-0.1.0-arm64.dmg`；当前本地包未签名，使用默认图标。
- 该 DMG 仍是开发分发产物：尚未内置 Python runtime，目标机器需通过 `ZAGENT_PYTHON` 或系统 Python 提供 Core 依赖；发布版需增加可重定位 Core bundle、签名与公证。

### 13.3 实机与响应式检查

- Electron 真实窗口中 Thinking 只显示“思考过程 / 默认收起”，无正文泄露；工具调用与结果默认隐藏，但可通过独立开关审计。
- 用户消息改为右对齐的内容自适应气泡，最大宽度 76%，长文本不再从主内容区最左边铺满。
- 1440×900：侧栏 248px、主区 884px、检查器 308px，总宽精确等于 viewport，无横向溢出。
- 1024×768：检查器默认移出视口，点击“上下文”后成为 330px 抽屉；页面 `scrollWidth` 等于 viewport。
- 完成后已停止本次启动的 Vite、Electron 与 Core 进程。

## 14. 上下文归档语义复验（第十轮，2026-08-29）

### 14.1 发现与修正

- 原实现的 archive 只写摘要与结构化状态，覆盖区间仍可能作为“最近事件”进入下一轮 WorkingSet；现改为覆盖区间从活动投影外置，真正降低后续 prompt 占用。
- EventLog 原文没有删除：FTS5 搜索仍返回稳定 event ID，`context_retrieve` 仍按 ID 返回原 payload；固定事件会跨归档重新加入 WorkingSet。
- 批量固定改为去重、跨会话预校验和单事务写入；重复固定不再重复计算 token，失败不会留下部分 pin。
- 不完整工具轮被整体移除后重新统计 token，避免 inspector 显示裁剪前的虚高值。
- 工作区路径变化现在会使关联会话的 system prompt 缓存失效。
- Context Inspector 新增 archive 外置事件数/token，并澄清固定证据是“优先保留但不突破模型硬上限”。
- 内部 provider 原始响应、思考事件与归档摘要不能被固定；WorkingSet 也会排除旧数据库中的内部脏 pin，避免敏感内部 payload 被重新注入模型。

### 14.2 自动化结果

- `npm test`：Ruff、110 个 Python 测试、7 个 UI 测试和 TypeScript strict typecheck 全部通过。
- Python 覆盖率 83.86%，高于 80% 门槛。
- Vite production build 通过：JS 300.68KB（gzip 95.81KB），CSS 20.26KB（gzip 5.46KB）。
- 新增覆盖：归档外置/固定恢复/原文检索取回、重复固定计费、跨会话批量固定原子性、重复归档拒绝、工具轮裁剪后 token 重算、工作区缓存失效、前端归档指标展示、API 契约。
