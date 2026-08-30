# Z-Agent v2 路线图

本文收纳 v1 验收后尚未完成的产品能力。所有项目继续遵守：零训练、自研 Agent loop、不依赖 Agent 框架、不使用厂商托管代码/文件工具。

## 0. 实施快照（2026-08-30）

- ✅ `sessions.context_version` 已迁移到 SQLite，事件、pin、archive、checkpoint 和工作区路径更新在事务中递增；已通过旧库迁移、Core 重启和双 Store 实例可见性测试。
- ✅ 同一 Core 内的 FastAPI 并发请求通过 Store `RLock` 串行化共享 SQLite connection，修复 Electron 首屏同时请求 events/context 时的 `sqlite3.InterfaceError`；已加 12 线程压测。
- ✅ 工具轮次/时间达上限时，Runtime 会原子写入结构化 checkpoint：目标事件、已执行工具的证据 event ID、待办工具、文件 SHA、失败原因与 archive ID。
- ✅ GUI 能在重启后显示未解决 checkpoint，提供“继续任务”；成功续跑后记录 resolution event 并停止注入旧 checkpoint。
- ✅ 工具 invocation 以 `(session_id, call_id)` 持久化，工具名与规范化参数 SHA-256 参与判定；已完成的调用只回放原结果，参数冲突或“副作用后、结果落库前”崩溃状态会阻断自动重试。
- ✅ 确定性故障注入已在同一 session 连续产生 3 个 checkpoint 并最终完成；新 checkpoint 会以自己的 event ID supersede 旧暂停点，不会在完成后重新浮现。
- ✅ 扩展目录/ZIP 已实现安全导入：暂存校验、路径穿越/符号链接/压缩包限额防护、包 SHA-256、安装时间、原子替换回滚、启停与 Core 重启恢复；Electron 提供原生选择器。
- ✅ MCP stdio 已实现无 SDK 的 JSON-RPC 客户端：`2025-11-25` 初始化协商、换行帧限制、超时/取消、stderr 限额、工具分页发现/调用、按需启动和分级关闭；真实子进程及 Core HTTP 重启链路均通过。
- ✅ 明确批准的 MCP 工具会转换为命名空间化原生工具 schema，进入现有 Agent loop、call ID 幂等保护和 EventLog 工具结果；未批准配置不会执行。
- ✅ Python/Node 扩展在独立 Host 子进程加载；安装生成 CycloneDX 1.7 SBOM 与本机 Ed25519 签名，运行前重算内容摘要并验签。
- ✅ Permission Broker 持久保存 pending/once/session/always 决策、参数摘要、可撤销 grant 与审计；MCP 与扩展工具调用统一默认拒绝。
- ✅ MCP Streamable HTTP 支持 JSON/SSE response、`Mcp-Session-Id`、`MCP-Protocol-Version`、Bearer token 和会话 DELETE；OAuth 支持 RFC 9728/8414/OIDC discovery、PKCE S256、resource audience、state、refresh 与 DCR。
- ✅ 官方 MCP Registry v0.1 支持搜索、版本详情和 Streamable HTTP remote 导入；真实线上搜索与 remote 映射通过，导入保持未批准且不自动执行包安装脚本。
- ✅ macOS `sandbox-exec` 与 Linux bubblewrap backend 已实现，文件/网络按 manifest/config 收敛；引擎缺失或宿主禁止嵌套时 fail closed。
- ✅ 受控 Runner 已完成固定 `python_unittest` / `python_pytest` / `npm_test` 模板、逐次 Permission Broker、去敏快照、无网络 OS 沙箱、超时/输出/文件数/总大小上限和可引用 evidence event ID；沙箱不可用时 fail closed。
- ✅ WorkingSet 缓存键现包含 SQLite `context_version`、workspace 持久版本、模型配置 SHA-256 和工具 schema SHA-256。GUI 会将读到的 revision 作为消息写入前置条件，过期写入返回 HTTP 409；两个 SQLite Store 的 CAS 竞争测试通过。
- ✅ 真实 `deepseek-v4-flash` 在同一 session 中连续产生 10 个 checkpoint，然后续跑完成七步文件任务；最终文件为 `step=2\n`、SHA-256 为 `6224b8afa119441ab0a65db5d0896414779338cfc3085f281deb3b992b942dd4`，并且无 active checkpoint。
- ✅ macOS arm64 已生成可重定位 Core bundle；打包 Electron 仅使用 `Resources/core-runtime/bin/python`，真实启动和杀掉 Core 后恢复通过。正式图标、自动更新、崩溃诊断和自包含 Release CI 已配置；项目决定 `v0.2.1` 起跳过 Apple 签名/公证并明确发布 unsigned DMG。
- ⏳ 扩展生态余项：Open VSX/VSIX adapter、发布者公钥/透明日志信任链、Windows AppContainer backend 与项目 lockfile。当前 Ed25519 签名是本机安装证明，不冒充第三方发布者签名。

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

**当前边界：** checkpoint 数量与真实 provider 恢复已超过门槛；Runner 的快照、授权、沙箱与证据投影由自动化覆盖。还需在普通非嵌套 macOS 宿主上跑一次真实 `sandbox-exec` Runner 产物验收；当前 Codex 宿主禁止嵌套沙箱，因此只验证了拒绝执行而没有绕过。

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

**当前进度：** 本地目录/ZIP、独立 Host、Permission Broker、MCP stdio/Streamable HTTP/OAuth、官方 Registry remote、SBOM/本机签名与 macOS/Linux 沙箱 backend 已完成。Open VSX/VSIX 和完整发布者信任链仍未完成。

- 完成 Z-Agent Extension SDK、manifest、权限声明、独立 Worker/子进程 host、崩溃隔离和项目 lockfile。
- 实现 MCP stdio/HTTP transport、官方/自定义 registry adapter、工具权限审查、OAuth/secret 隔离与调用审计。
- Open VSX 只做发现、详情、下载与 VSIX 管理；运行兼容需 Code-OSS/Theia host，不能声称支持全部 VS Code Marketplace。
- npm/Git/本地包安装记录 publisher、许可、依赖图、哈希和 SBOM；升级显示权限 diff，默认不自动启用。
- marketplace 内容是不可信输入，安装、执行与高影响工具调用分别授权。

**验收：** 恶意/崩溃扩展不能访问未授权文件、密钥或主进程；每次安装、升级、授权和调用都有来源与哈希事件。

## 7. P1：生产桌面发布

- 将 Python Core 与依赖打成可重定位、离线可启动的 bundle；不要求目标机器安装 Conda/Python。
- 设置正式图标、bundle 元数据、Windows 与 Linux 包；CI 产出 SBOM 与校验和。macOS 当前采用明确标记的未签名发行，未来只在证书获取成本可接受时恢复签名/公证。
- Core 启动健康检查、迁移失败回滚、自动更新、崩溃报告与用户数据备份/恢复。
- Electron renderer 保持 contextIsolation、IPC allowlist 与 CSP；生产包不开放调试端口。

**验收：** macOS/Windows/Linux 干净虚拟机离线安装启动，创建工作区并完成 echo 模型任务；校验和可验证，卸载不破坏用户数据。

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
