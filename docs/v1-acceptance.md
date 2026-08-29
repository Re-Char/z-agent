# Z-Agent v1 验收结论

**验收日期：** 2026-08-29  
**结论：** v1 核心工程基线通过；扩展运行时、正式独立安装包、持久稠密向量和长期记忆不属于当前已完成范围，统一进入 v2。

## 1. 范围与完成状态

| 能力 | 状态 | 验收依据 |
| --- | --- | --- |
| 零训练、自研 Agent loop | 通过 | 自行实现消息投影、tool calling、参数校验、本地执行、轮次/截止时间、重试和错误事件；不依赖 Agent 框架或托管文件/执行工具 |
| EventLog / BlobStore | 通过 | SQLite WAL 追加事件、稳定 ID、SHA-256、外置大 payload、归档事务测试 |
| WorkingSet / 上下文工具 | 通过 | `status/search/retrieve/archive/pin/unpin`、完整工具轮、硬预算、归档外置、按 ID 恢复原文 |
| 中文检索 | 通过 v1 范围 | FTS5/BM25 + 本地中文稀疏 TF-IDF + RRF；无需训练、embedding 服务或向量数据库 |
| 国产模型 | 通过协议与实机验收 | OpenAI-compatible 网关；真实 DeepSeek 完成长任务、工具调用、归档与续轮 |
| 工作区代码工具 | 通过 | `overview/list/read/search/mkdir/write/replace`；SHA-256 乐观锁、原子写入、路径/符号链接/密钥/依赖目录防护 |
| Electron GUI | 通过开发基线 | React UI、流式与停止、Thinking 默认折叠、Markdown 消毒与代码块、工作区/模型/上下文界面；真实窗口烟测 |
| 扩展配置 | 通过发现边界 | Z-Agent manifest 与 MCP 配置发现/校验；第三方代码不会在 v1 中执行 |
| 扩展 host / marketplace 安装 | 未实现，转 v2 | 需要权限 broker、隔离 host、供应链记录与 registry adapter，不能用不安全的直接加载代替 |
| 独立生产安装包 | 未实现，转 v2 | DMG 可构建，但仍未内置 Python Core、未签名/公证、使用默认图标，不是干净机器发行包 |

这里的“v1 完成”指核心 Agent、上下文、中文/国产模型、受控文件工具和桌面交互形成可运行闭环，不把规划文档中的未来生态能力伪装为已完成。

## 2. 自动化质量门槛

2026-08-29 从干净测试进程重新执行：

- `npm test`：119 个 Python 单元/集成/功能测试 + 7 个 UI 测试全部通过；
- Python 覆盖率 84.79%，高于 80% 门槛；
- Ruff、TypeScript strict typecheck、Vite production build 通过；
- Electron `main.cjs` / `preload.cjs` 语法检查通过；
- electron-builder 生成 `dist/desktop/Z-Agent-0.1.0-arm64.dmg`；
- 已知非功能警告：Starlette TestClient 的 httpx 兼容层弃用提示。

## 3. 真实 DeepSeek 长程验收

验收脚本为 `scripts/long_task_e2e.py`。它复用 Electron 中已有的模型配置和 SecretStore，但不读取、打印或复制 API Key。目标工作区是新建的临时空目录 `/private/tmp/zagent-long-e2e.jIkvCr`。

### 3.1 任务

要求 Z-Agent 创建一个 Python 3.12 `taskboard` 项目：标准库实现依赖任务图、JSON 原子持久化、argparse CLI、单元测试与中文 README；随后根据外部测试失败修复，并在第二阶段增加稳定排序的 JSON/CSV 导出。

### 3.2 实际过程

1. Z-Agent 从空目录调用 `fs_mkdir` 和文件写入工具创建 `src/taskboard`、`tests`、`pyproject.toml` 与 README。
2. 首回合触发默认 8 工具轮上限。任务没有丢失，已写文件和 EventLog 均保留；同一 session 下一回合继续。
3. 首次外部运行 29 个测试，24 通过、5 失败。失败内容作为真实反馈交回 Z-Agent；它修正了测试假设，没有削弱“拒绝未知依赖”的核心规则。
4. 一次 `context_archive` 参数被 DeepSeek 生成为非法 JSON，暴露协议脆弱点；provider 现会在任何工具执行前进行一次受限协议修复，二次失败即停止。对应单元测试覆盖非流式与流式路径。
5. 修复后 33/33 通过；Z-Agent 搜索并固定初始需求，建立第一归档 `arc_570fd8dc199c47e9943ba9cbe975ae20`。
6. 在同一已归档 session 中增加 export 功能，外部测试达到 44/44，并通过手工 CLI 的 add/list/next/done/JSON/CSV 流程。
7. 最终回合修正 README 测试计数、取消不再需要的 pin，并建立第二归档 `arc_e66de42326624b05b810d79408e15436`。

最终 session 为 `ses_244d73f59bc54f86a7c35be8dd4b0b20`，累计 135 个事件、无 pinned event，最新 WorkingSet 估算 13,732 tokens。归档后原事件仍可按 event ID 检索和恢复。

### 3.3 独立验证

- `PYTHONPATH=src python -m unittest discover -s tests -v`：44/44 通过；
- 隔离 venv 中 `pip install --no-build-isolation -e .` 成功；
- 安装后的 `taskboard` console script 与 `python -m taskboard` 均成功；
- JSON/CSV 按任务 ID 稳定排序；依赖未完成时 `done` 非零退出，完成前置任务后后继任务可完成。

### 3.4 暴露出的真实限制

- 默认 8 工具轮适合单回合安全止损，但复杂项目需要用户/调度器发起续轮；v2 应提供 checkpoint 与自动续跑策略。
- 模型最终自然语言总结曾把 `models.py` 说成 `model.py`，而实际产物与测试正确；最终报告必须由事件/文件/测试结果生成证据表，不能只信模型自述。
- Z-Agent 没有 shell，能够安全编写和审计代码，但测试执行仍依赖外部受控 runner；引入 runner 前必须有权限、命令模板和隔离边界。
- v1 无删除工具，因此外部测试反馈文件保留在临时项目中；生产工作流需要受审批、可恢复的文件删除能力。

## 4. 初步优化结论

本轮已经落地：空工作区目录创建、`.git`/依赖/缓存目录的直接访问阻断、DeepSeek 非法 tool arguments 一次性协议修复、真实长任务可复用验收脚本，以及文档中的 v1/v2 边界校正。

下一步优先级见 [v2-roadmap.md](v2-roadmap.md)。
