# 可直接导入的 Extension 与 MCP 示例

这些文件只使用 Python 标准库，不联网、不读取工作区，也不需要安装第三方包。

## 1. 导入 Extension ZIP

在 Electron 中打开“扩展与 MCP” → “导入扩展包”，选择：

`zagent-demo-extension.zip`

勾选“导入后启用 manifest”，然后点击“安全导入”。安装结果应显示：

- ID：`com.zagent.demo`
- 签名：`verified`（这是本机安装签名，不是第三方发布者签名）
- Host 工具：`hello`、`sum_numbers`

第一次点击“启动独立 Host”会立即弹出 Codex 式 `host:start` 审批框。选择“仅此一次”后应用会自动重试，不需要再次点击，随后应显示 2 个工具。

随后在对话中输入：

> 请调用 com.zagent.demo 扩展的 hello 工具，name 使用“中文用户”。

工具真正执行前还会出现一次 `tool:hello` 审批。批准后，工具结果中应包含 `你好，中文用户！`。

## 2. 导入本地 MCP 配置

在“扩展与 MCP” → “导入本地 MCP 配置”中选择：

`mcp-demo/zagent-demo-mcp.json`

点击“安全导入 MCP”。导入器会把 `${ZAGENT_PYTHON}` 替换为当前 Core 使用的 Python，并把 `./server.py` 限定解析到配置文件所在目录。配置中的授权状态不会被信任，导入后始终是“未授权”。

点击卡片上的“连接并读取工具”即构成一次明确授权和协议握手，成功后应显示：

- MCP protocol：`2025-11-25`
- Server：`zagent-demo-mcp`
- 工具：`echo`、`sum_numbers`

然后在对话中输入：

> 请调用 zagent-demo-mcp 的 echo 工具，text 使用“真实 MCP 已连通”。

工具调用前会弹出逐次 Permission Broker。批准后，返回的 `structuredContent.echo` 应等于输入文本。

## 边界验证

- 两种可执行集成都在独立子进程中运行，不会加载进 Electron Renderer 或 Core 主进程。
- Extension ZIP 导入时生成 CycloneDX SBOM、包 SHA-256 和本机安装签名；改动安装后的代码会使状态变成 `package_modified` 并阻止启动。
- MCP 配置最大 1 MiB，只允许一个 `schema_version: 1` 的 Server；相对路径不得逃出配置目录，且导入时强制撤销授权。
- 两个示例都默认启用 OS 沙箱且禁止网络；系统不支持沙箱时会 fail closed。

## 3. 真实市场格式兼容

`marketplace/official-mcp-docs.json` 使用 Claude Desktop 的标准 `mcpServers` 配置，连接 MCP 官方文档 Streamable HTTP Server：

- 官方地址：`https://modelcontextprotocol.io/mcp`
- 来源：MCP 官方 Registry/参考服务器项目
- 导入后保持未授权，点击“连接并读取工具”才会联网

`marketplace/official-hello-world-node.mcpb` 是从 `modelcontextprotocol/mcpb` 官方仓库的 `examples/hello-world-node` 原始 manifest 和 server 构建的 MCPB 0.3 包，不是 Z-Agent 自定义 manifest。生产依赖在打包时升级到无已知 npm audit 漏洞的兼容版本，并保留官方 MIT License。

Z-Agent 当前真实支持：

- Claude Desktop `mcpServers` 单 Server JSON；
- VS Code `servers` 单 Server JSON；
- MCP Streamable HTTP 与 stdio；
- MCPB/DXT manifest 0.1–0.4 的 Node、Python、Binary runtime；
- 官方 MCP Registry 的 remote endpoint。

MCPB `uv` runtime 尚不自动安装依赖，会明确拒绝。VSIX 依赖完整 VS Code Extension Host API，也不属于当前兼容范围；项目不会把“能解压 VSIX”冒充为“能运行 VS Code 扩展”。
