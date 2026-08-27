# Z-Agent 改动概览（for Codex）

> 面向后续接手者的快速导航。基线：`eda4ff1`（"merge remote repository initialization"）。
> 本分支相对基线包含 7 轮迭代，覆盖后端核心、前端交互、上下文管理三块。

## 一分钟速览

| 主题 | 状态 | 关键文件 |
|---|---|---|
| DeepSeek 400 修复 | ✅ | `src/zagent/config.py`、`providers/openai_compatible.py`、`providers/parser.py` |
| 多模型配置与切换 | ✅ | `src/zagent/config.py`、`bootstrap.py`、`api/app.py`、`apps/ui/src/App.tsx` |
| 工作区（安全边界） | ✅ | `storage/schema.py`、`storage/sqlite_store.py`、`api/app.py`、`App.tsx` |
| 文件系统工具（只读） | ✅ | `src/zagent/agent/fs_tools.py`（新） |
| 流式输出（SSE） | ✅ | `providers/openai_compatible.py`、`agent/runtime.py`、`api/app.py`、`apps/desktop/*` |
| 上下文管理修复 | ✅ | `context/working_set.py`、`context/orchestrator.py`、`storage/sqlite_store.py` |
| 前端交互打磨 | ✅ | `App.tsx`、`styles.css`、`Markdown.tsx`（新） |
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

### 4. 文件系统工具（`fs_tools.py`，只读）
- `fs_project_overview` / `fs_list` / `fs_read` / `fs_search`
- 路径穿越防护 + 跳过 `.git`/`node_modules`/`__pycache__` 等
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

### 7. Markdown 渲染
- `apps/ui/src/Markdown.tsx`：`marked` + `DOMPurify`（防 XSS），assistant 消息渲染；代码块悬停复制按钮
- 新依赖：`marked@18`、`dompurify@3`（已写入 `apps/ui/package.json`）

## 验证状态

- Python：**95 个测试通过**（`PYTHONPATH=src conda run --prefix .conda/envs/zagent pytest -q`）
- 前端：typecheck（`tsc -b`）+ vitest + build 全绿
- E2E（真实 DeepSeek）：读文件 → 流式输出 → 折叠展示 → 归档/固定 → 无 400/500
- 详细验证记录：`docs/verification-report.md`（7 轮迭代逐项记录）

## 接手注意事项

1. **运行实例需重启 Electron 才能加载新代码**（用户本地应用跑的是旧构建）
2. 工作区必须设置路径后 agent 才能读文件（侧边栏 ✎ 编辑）
3. `WorkingSetBuilder` 缓存是进程内按版本失效；若未来多进程共享存储需迁移到 DB 版本号
4. 测试基础设施（CDP 浏览器驱动、SSE 代理桥）是临时脚本，未入库；`tests/` 内的都是持久化测试
5. `.conda/envs/zagent` 是本地 conda 环境；`estimate_tokens` 中文 1 字符 ≈ 1 token（口径已在工具描述注明）
