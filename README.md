# Z-Agent

Z-Agent 是一个中文优先、可审计的本地长程智能体。当前 `0.2.0` 正在开发 v2：核心自行实现模型/工具循环、无损 EventLog、中文检索、WorkingSet 投影、错误处理和终止条件，不依赖任何 Agent 框架或模型厂商托管的代码/文件工具。

## 当前能力

- 追加式 SQLite EventLog，大输出进入内容寻址 BlobStore；
- `context_status/search/retrieve/archive/pin/unpin` 原生上下文工具；中文搜索融合 FTS5/BM25 与本地稀疏 TF-IDF 向量，无需训练或向量数据库；
- 中文字符 unigram/bigram + 技术标识符的确定性混合检索；若环境安装 Jieba 会自动增加词级召回；
- OpenAI-compatible 模型网关，可配置 Qwen、DeepSeek、GLM、Kimi、MiniMax 等国产模型；
- 自研 tool-calling 循环、参数校验、最大轮次、任务超时和错误事件；
- Electron + React 中文桌面 GUI，包含任务时间线和上下文检查器；
- Z-Agent extension 目录/ZIP 安全导入、独立 Node/Python Extension Host、逐次权限审批、CycloneDX SBOM、本机安装签名与沙箱后端；
- 受管 MCP stdio 与 Streamable HTTP：显式授权、协议握手、工具发现/调用、OAuth PKCE、官方 Registry remote 导入和原生 Agent tool-calling 接入；
- 单元、集成、API 功能和前端组件测试；真实 DeepSeek 长程任务验收脚本。

## 环境

需要 Conda、Node.js 22+ 和 npm 10+。

```bash
npm run env:create
npm install
```

Conda 环境固定在仓库内 `.conda/envs/zagent`。更新依赖：

```bash
npm run env:update
```

## 开发

```bash
npm run dev
```

Electron 主进程会启动 Conda 环境中的 Python 核心服务，通过一次性 Bearer token 和 loopback HTTP 通信。也可以单独运行核心：

```bash
PYTHONPATH=src conda run --prefix .conda/envs/zagent \
  python -m zagent.server --port 8765 --auth-token local-dev-token
```

默认 provider 为 `echo`，不访问外部网络。在桌面的“模型设置”中填写国产模型的 OpenAI-compatible base URL、模型名与 API Key 后即可切换。

## 测试与质量

```bash
npm test
```

也可以分别运行：

```bash
npm run lint:core
npm run test:core
npm run test -w @zagent/ui
npm run typecheck -w @zagent/ui
npm run build:ui
```

测试不调用互联网；MCP 集成测试会启动真实本地 stdio 子进程并完成 JSON-RPC 握手、工具发现、调用和重启恢复，不以 mock 代替 transport。

真实 provider 长程验收会产生模型 API 调用费用，需显式指定桌面数据目录和临时工作区。脚本可直接运行，无需额外设置 `PYTHONPATH`；默认遇到 checkpoint 后停止，只有显式设置续轮预算才会自动继续：

```bash
.conda/envs/zagent/bin/python scripts/long_task_e2e.py \
  --data-dir "/path/to/Z-Agent/core" \
  --workspace /private/tmp/zagent-long-task \
  --phase build \
  --max-continuations 2
```

后续阶段使用输出中的 `session_id`，通过 `--session-id` 继续同一会话。脚本不会读取或打印 API Key；checkpoint 续跑仍保留每轮工具数和时间上限。

## 项目结构

```text
src/zagent/
  domain/       领域模型与错误
  storage/      SQLite EventLog 与 BlobStore
  context/      中文检索、WorkingSet、上下文工具
  providers/    模型 HTTP 协议和响应归一
  agent/        自研工具循环和终止条件
  extensions/   扩展安全安装、清单与 MCP transport
  api/          FastAPI 传输层
apps/
  desktop/      Electron 主进程和安全 IPC
  ui/           React GUI
tests/
  unit/
  integration/
  functional/
```

详细设计见 [docs/architecture.md](docs/architecture.md)。当前完成边界和真实长任务证据见 [docs/v1-acceptance.md](docs/v1-acceptance.md)，后续工作见 [docs/v2-roadmap.md](docs/v2-roadmap.md)。

## 安全边界

- Core API v1 只允许 loopback 地址；Renderer 不持有 Core token，所有请求经 Electron 主进程代理。
- 模型只能提出工具调用，本地 ToolExecutor 决定是否执行。
- 扩展和 MCP 来源默认不可信；可执行扩展只在独立 Python/Node Extension Host 中加载，绝不进入 Core/Electron 主进程。扩展启动与 MCP/扩展工具调用统一经过可撤销 Permission Broker。
- MCP stdio 使用参数数组直接启动、从不经过 shell；默认进入 macOS `sandbox-exec` 或 Linux bubblewrap，沙箱不可用时拒绝执行。Streamable HTTP 支持 JSON/SSE 响应、会话头与 OAuth PKCE；旧版 SSE transport 不执行。
- 扩展安装生成 CycloneDX 1.7 SBOM，并使用数据目录 trust key 的本机 Ed25519 安装签名；内容、SBOM 或签名变化都会阻断 Extension Host。
- API Key 与 OAuth token 以权限 `0600` 的本地文件保存；迁移到系统 Keychain/Credential Manager 仍属于发布加固项。

## 发布包状态

`v0.2.0` DMG 是开发验收用 prerelease：已提供校验和，但尚未内置可重定位 Python Core runtime，也未做 Apple Developer ID 签名与公证。源码/Conda 开发模式可运行；当前 DMG 不保证在未安装 Python 依赖的干净 macOS 上启动。
