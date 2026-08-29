# Z-Agent v2 路线图

本文收纳 v1 验收后尚未完成的产品能力。所有项目继续遵守：零训练、自研 Agent loop、不依赖 Agent 框架、不使用厂商托管代码/文件工具。

## 0. 实施快照（2026-08-29）

- ✅ `sessions.context_version` 已迁移到 SQLite，事件、pin、archive、checkpoint 和工作区路径更新在事务中递增；已通过旧库迁移、Core 重启和双 Store 实例可见性测试。
- ✅ 同一 Core 内的 FastAPI 并发请求通过 Store `RLock` 串行化共享 SQLite connection，修复 Electron 首屏同时请求 events/context 时的 `sqlite3.InterfaceError`；已加 12 线程压测。
- ✅ 工具轮次/时间达上限时，Runtime 会原子写入结构化 checkpoint：目标事件、已执行工具的证据 event ID、待办工具、文件 SHA、失败原因与 archive ID。
- ✅ GUI 能在重启后显示未解决 checkpoint，提供“继续任务”；成功续跑后记录 resolution event 并停止注入旧 checkpoint。
- ⏳ 尚未完成：幂等 invocation 去重、受控 Runner、3 次 checkpoint 的真实长任务验收、跨进程乐观锁、模型/工具 schema 版本纳入缓存键。因此 P0 总验收仍未标记完成。

## 1. P0：长任务可靠性与证据化验收

### 1.1 Checkpoint 与续跑

- 将“单回合最多 N 个工具轮”与“整个任务生命周期”分离；达到轮次/时间限制时写入结构化 checkpoint，而不是只有异常文本。
- checkpoint 至少包含目标、完成项、待办、最新文件 SHA、失败原因、建议下一步和可恢复 archive ID。
- GUI 提供明确的“继续任务”，并支持在预算/费用/权限策略内自动续跑；取消和崩溃后可恢复。
- 幂等工具调用记录 invocation ID，避免恢复时重复执行写入。

### 1.2 受控测试 Runner

- 仅执行项目/用户批准的命令模板，默认不提供任意 shell；工作目录锁定到 workspace。
- 环境、超时、网络、文件写入和输出大小均显式限制；测试结果作为带 provenance 的事件写入。
- Python、Node、Rust 等 runner 分适配器实现；危险命令、高影响副作用和外部网络必须单独审批。
- Agent 的“测试通过”结论必须引用 runner event ID，GUI 区分模型自述与可验证结果。

### 1.3 Provider 协议健壮性

- 建立国产模型 tool-call 合约集：非法 JSON、流式分片、空参数、重复 call ID、reasoning 续轮和中断恢复。
- 当前一次性 JSON 协议修复扩展为可观测策略：记录原错误类型、修复次数与最终结果，但永不执行未通过 schema 的参数。
- provider manifest 记录上下文窗口、tokenizer、工具/流式/reasoning 能力与版本；未经回归的模型不标记 `agent_ready`。

**P0 验收：** 同一 session 在至少 3 次 checkpoint 后完成多阶段代码任务；故障注入不重复写文件；所有测试结论可追溯到 runner 事件。

## 2. P0：数据库级上下文版本与持久状态

- 将进程内 `context_version` 迁移到 SQLite session 元数据并在事务中递增。
- WorkingSet 缓存键包含数据库版本、模型配置版本、workspace 版本和工具 schema 版本。
- 支持 Electron Core 重启、多客户端与未来多进程共享同一数据目录；并发更新使用明确的乐观锁。
- archive/checkpoint/task-state schema 做版本迁移，不依赖自由文本解析。

**验收：** Core 异常退出后重启，WorkingSet、pin、archive 与 checkpoint 一致；并发写入不会返回旧缓存。

## 3. P1：持久化稠密向量与中文评测

- 保留 v1 Exact/FTS5 与稀疏 TF-IDF 作为事实召回通道，embedding 不能单独决定证据。
- 提供可替换的本地或远程中文/多语 embedding provider，记录模型、维度、归一化与向量版本。
- 向量索引以 `event_id` 为主键，支持增量写入、批量重建、模型迁移、损坏回退和删除传播。
- 默认本地持久化；远程 embedding 前展示数据外发范围，敏感/Internal/Thinking 内容禁止发送。
- 建立中文同义词、简称、繁简、全半角、中英代码术语数据集，分别报告 lexical、sparse、dense 与 RRF 指标和延迟。

**验收：** 所有召回引用能恢复原事件与 SHA；向量库损坏可降级到 v1 检索；中文评测显示 dense 的实际增益后才默认启用。

## 4. P1：长期记忆

- 区分 episodic（任务经历）、semantic（稳定事实）和 procedural（用户确认的偏好/流程）。
- 记忆必须包含来源 event ID、置信度、作用域、创建原因、最后验证时间和过期策略。
- 写入采用“候选 → 去敏/冲突检测 → 用户策略 → 提交”流程，不能把每条聊天自动向量化成记忆。
- 敏感内容、密钥、Thinking、provider 原始响应不进入长期记忆；工作区/用户/项目作用域严格隔离。
- 用户可以查看、固定、纠正、过期、导出和删除记忆；删除同步清理派生向量并留下审计 tombstone。

**验收：** 跨会话任务能召回已确认事实；冲突不静默覆盖；删除后 lexical/dense 两条路径均不可再召回正文。

## 5. P1：安全权限与文件操作完善

- Permission Broker 统一文件、网络、MCP、扩展和未来 runner 权限；决策按一次/会话/项目持久化并可撤销。
- 增加可恢复的文件删除、移动、补丁预览和批量变更事务；高风险路径永不因模型请求自动放行。
- SecretStore 迁移到 macOS Keychain / Windows Credential Manager / Linux Secret Service。
- 增加 symlink race、TOCTOU、超大输出、压缩炸弹、Git 对象和嵌套依赖目录安全测试。

## 6. P1：扩展 Host 与 Marketplace

- 完成 Z-Agent Extension SDK、manifest、权限声明、独立 Worker/子进程 host、崩溃隔离和项目 lockfile。
- 实现 MCP stdio/HTTP transport、官方/自定义 registry adapter、工具权限审查、OAuth/secret 隔离与调用审计。
- Open VSX 只做发现、详情、下载与 VSIX 管理；运行兼容需 Code-OSS/Theia host，不能声称支持全部 VS Code Marketplace。
- npm/Git/本地包安装记录 publisher、许可、依赖图、哈希和 SBOM；升级显示权限 diff，默认不自动启用。
- marketplace 内容是不可信输入，安装、执行与高影响工具调用分别授权。

**验收：** 恶意/崩溃扩展不能访问未授权文件、密钥或主进程；每次安装、升级、授权和调用都有来源与哈希事件。

## 7. P1：生产桌面发布

- 将 Python Core 与依赖打成可重定位、离线可启动的 bundle；不要求目标机器安装 Conda/Python。
- 设置正式图标、bundle 元数据、macOS 签名与公证、Windows 签名、Linux 包；CI 产出 SBOM 与校验和。
- Core 启动健康检查、迁移失败回滚、自动更新、崩溃报告与用户数据备份/恢复。
- Electron renderer 保持 contextIsolation、IPC allowlist 与 CSP；生产包不开放调试端口。

**验收：** macOS/Windows/Linux 干净虚拟机离线安装启动，创建工作区并完成 echo 模型任务；签名验证和卸载不破坏用户数据。

## 8. P2：上下文策略、多任务与可观测性

- 在不训练的前提下尝试规则/状态机驱动的高水位 checkpoint；只有结构化状态完整时才自动归档。
- 子任务使用最小委托包：目标、限制、pinned evidence 和检索范围；父子事件保留 provenance。
- GUI 增加任务阶段、checkpoint、费用/token/延迟、工具失败率和检索命中解释。
- 建立真实任务回放集，比较无归档、显式归档、自动 checkpoint 三种策略的完成率和成本。
- 对模型自述进行证据校验：文件列表、测试数、命令结果和版本号由系统事件生成，不由模型自由填写。

## 9. 建议实施顺序

1. Checkpoint + 受控 runner + 证据化报告；
2. 数据库 context version 与恢复一致性；
3. Permission Broker 与可恢复文件操作；
4. 稠密向量评测，确认收益后再持久化；
5. 长期记忆；
6. Extension Host / MCP / Marketplace；
7. 自包含签名发行与跨平台 CI；
8. 多任务与自动上下文策略。

优先顺序刻意先解决本次长程实测暴露的“能写代码但不能自主验证、达到回合上限后需要人工续轮、最终总结可能偏离证据”，再扩大记忆和第三方执行面。
