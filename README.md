# Z-Agent

Z-Agent 是一个中文优先、可审计的本地长程智能体。v1 自行实现模型/工具循环、无损 EventLog、中文检索、WorkingSet 投影、错误处理和终止条件，不依赖任何 Agent 框架或模型厂商托管的代码/文件工具。

## v1 能力

- 追加式 SQLite EventLog，大输出进入内容寻址 BlobStore；
- `context_status/search/retrieve/archive/pin/unpin` 原生上下文工具；中文搜索融合 FTS5/BM25 与本地稀疏 TF-IDF 向量，无需训练或向量数据库；
- 中文字符 unigram/bigram + 技术标识符的确定性混合检索；若环境安装 Jieba 会自动增加词级召回；
- OpenAI-compatible 模型网关，可配置 Qwen、DeepSeek、GLM、Kimi、MiniMax 等国产模型；
- 自研 tool-calling 循环、参数校验、最大轮次、任务超时和错误事件；
- Electron + React 中文桌面 GUI，包含任务时间线和上下文检查器；
- Z-Agent extension manifest 与 MCP server 配置发现；扩展代码默认不执行；
- 单元、集成、API 功能和前端组件测试。

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

所有 Python 测试使用临时 SQLite 和 FakeProvider，不调用真实模型或互联网。

## 项目结构

```text
src/zagent/
  domain/       领域模型与错误
  storage/      SQLite EventLog 与 BlobStore
  context/      中文检索、WorkingSet、上下文工具
  providers/    模型 HTTP 协议和响应归一
  agent/        自研工具循环和终止条件
  extensions/   扩展清单与 MCP 配置
  api/          FastAPI 传输层
apps/
  desktop/      Electron 主进程和安全 IPC
  ui/           React GUI
tests/
  unit/
  integration/
  functional/
```

详细设计见 [docs/architecture.md](docs/architecture.md)。

## 安全边界

- Core API v1 只允许 loopback 地址；Renderer 不持有 Core token，所有请求经 Electron 主进程代理。
- 模型只能提出工具调用，本地 ToolExecutor 决定是否执行。
- 扩展和 MCP 来源默认不可信；v1 仅发现配置，不自动执行扩展代码。
- v1 API Key 以权限 `0600` 的本地文件保存；生产版将替换为系统 Keychain/Credential Manager。
