# MCP 使用指南

## 当前支持结论

Eido AI Server 现在可通过一份只读 `mcp.json` 直接配置并调用 MCP：

| 执行器 | MCP 支持 | 实现方式 |
|--------|----------|----------|
| Claude Code | 支持 | 配置传入 Claude Agent SDK `mcp_servers`，工具允许规则为 `mcp__<server>__*` |
| OpenCode | 支持 | 自动转换并合并到 OpenCode 原生 `mcp` 配置 |

这不是独立的 Eido MCP 代理服务，也没有 MCP 管理 API。配置是单租户、服务级的，
所有 Eido session 共用同一组 MCP Server；每个 Agent 会话仍保持独立。

## 配置位置

本机默认读取项目根目录：

```text
./mcp.json
```

也可以设置：

```bash
export MCP_CONFIG_PATH=/absolute/path/to/mcp.json
```

Docker compose 默认映射：

```text
宿主机 ${MCP_CONFIG_FILE:-./mcp.json}
    → 容器 /etc/eido/mcp.json（只读）
```

自定义宿主机文件：

```bash
export MCP_CONFIG_FILE=/absolute/path/to/mcp.json
docker compose up -d --force-recreate
```

文件在容器启动时必须存在，否则 Docker 可能创建同名目录，导致服务报告
“MCP 配置路径不是文件”。仓库已提供空的 `mcp.json`，开箱时不会发生该问题。

## 配置格式

根节点固定使用 Claude 风格的 `mcpServers`：

```json
{
  "mcpServers": {
    "server-name": {
      "type": "http",
      "url": "https://example.com/mcp"
    }
  }
}
```

Server 名称只允许 1–64 位字母、数字、下划线和连字符，最多配置 64 个。

### Streamable HTTP

```json
{
  "mcpServers": {
    "research": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "{env:MCP_API_TOKEN}",
        "X-Tenant": "eido-server"
      }
    }
  }
}
```

`type` 可以写 `http` 或 `streamable-http`。

### SSE

```json
{
  "mcpServers": {
    "legacy-search": {
      "type": "sse",
      "url": "https://mcp.example.com/sse",
      "headers": {
        "Authorization": "Bearer fixed-token"
      }
    }
  }
}
```

Claude Code 保留 SSE transport；OpenCode 会按远程 MCP 接入。若目标服务同时提供
Streamable HTTP，优先使用 `/mcp` HTTP endpoint。

### 本地 stdio

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/workspace/.eido"
      ],
      "env": {
        "LOG_LEVEL": "info"
      },
      "cwd": "/workspace"
    }
  }
}
```

注意：

- `command` 必须安装在运行 Eido 的同一环境中。
- Docker 容器只包含镜像安装的命令和包。配置 `npx -y` 可能在首次调用时联网下载。
- `cwd` 仅传给 OpenCode；Claude SDK 当前 stdio 配置没有 `cwd` 字段。
- 宿主机路径在容器内不可见，必须先通过 compose volume 映射，并在 JSON 中使用
  容器内路径。

### 禁用 Server

以下两种写法都不会加载：

```json
{
  "disabled": true
}
```

```json
{
  "enabled": false
}
```

## 密钥与环境变量

字符串字段支持完整值环境变量引用：

```text
{env:MCP_API_TOKEN}
${MCP_API_TOKEN}
```

支持的位置包括 `command`、`args`、`env`、`cwd`、`url` 和 `headers` 的值。
当前只支持整个值替换，不支持 `"Bearer {env:TOKEN}"` 这样的字符串内插。Bearer
值应直接把环境变量设为完整内容：

```bash
export MCP_AUTHORIZATION='Bearer your-token'
```

```json
{
  "headers": {
    "Authorization": "{env:MCP_AUTHORIZATION}"
  }
}
```

Docker compose 需要显式把变量传入容器。在本地覆盖文件中增加：

```yaml
services:
  eido:
    environment:
      - MCP_AUTHORIZATION=${MCP_AUTHORIZATION}
```

不要把真实密钥直接提交到 `mcp.json`。配置加载错误不会打印已解析的 secret。

## 启动与验证

### 检查 Docker 挂载

```bash
docker compose config
docker compose up -d
docker compose exec eido sh -lc 'test -f /etc/eido/mcp.json && echo MCP_CONFIG_OK'
```

不要使用 `cat` 输出含密钥的 MCP 配置。

### 验证 OpenCode 识别配置

Eido 会在运行时把统一配置转换到 OpenCode。直接调用 Eido 最可靠：

```bash
curl -N -X POST http://localhost:8000/api/v1/chat/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Trace-Id: mcp-opencode-test' \
  -d '{
    "session_id": "e087dd1dee3e",
    "messages": [
      {"role": "user", "content": "列出当前可用的 MCP 工具，并调用 research 中与此请求匹配的工具"}
    ],
    "harness": "opencode"
  }'
```

### 验证 Claude Code 识别配置

```bash
curl -N -X POST http://localhost:8000/api/v1/chat/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Trace-Id: mcp-claude-test' \
  -d '{
    "session_id": "e087dd1dee3e",
    "messages": [
      {"role": "user", "content": "使用 research MCP 工具完成查询，并说明调用了哪个工具"}
    ],
    "harness": "claude_code"
  }'
```

模型会根据工具描述和请求语义决定是否调用。需要强制验证时，在请求中明确指定
Server 名称和用途。

## 配置热更新行为

服务每轮请求前读取 `MCP_CONFIG_PATH`：

- Claude Code：配置内容发生变化后，下一轮会使用新的配置签名；旧的 warm client
  不再复用。
- OpenCode：下一轮重新生成并注入 OpenCode `mcp` 配置。
- 不需要重建 Docker 镜像，也不要求重启 Eido。

如果修改了 compose 的挂载来源 `MCP_CONFIG_FILE` 或新增环境变量，则需要重新创建
容器；只修改已挂载文件的内容不需要。

## 日志排查

按请求 traceId 查询：

```bash
grep 'traceId=mcp-opencode-test' logs/app.log
grep 'traceId=mcp-claude-test' logs/app.log
```

关键日志：

```text
✓ MCP 配置已加载: path=/etc/eido/mcp.json servers=research
[ClaudeMCP] path=/etc/eido/mcp.json servers=research
[OpenCode/MCP] path=/etc/eido/mcp.json servers=research
[Claude/Tool/Call] mcp__research__...
[OpenCode/Tool] name=... status=completed
```

常见错误：

- `MCP 配置文件不存在`：路径或 volume 错误。
- `MCP 配置路径不是文件`：宿主机源文件不存在，Docker 创建成了目录。
- `根节点必须包含 mcpServers`：误把 OpenCode 原生 `{"mcp": ...}` 配置直接作为
  统一文件。
- `引用的环境变量未设置`：变量没有进入 Eido 进程或容器。
- 连接失败：容器网络不可达、URL endpoint/transport 不匹配或鉴权失败。

## 容器网络注意事项

- MCP 服务运行在同一个 compose：URL 使用服务名，例如
  `http://mcp-service:3000/mcp`。
- MCP 服务运行在宿主机：macOS/Windows Docker Desktop 通常使用
  `http://host.docker.internal:3000/mcp`，不能使用容器内的 `127.0.0.1`。
- MCP 服务运行在公网：确认容器 DNS、代理、TLS 证书和出口策略允许访问。
- stdio MCP 由 Eido/OpenCode 子进程在容器内启动，不需要额外暴露端口。

## 安全边界

- `mcp.json` 以只读方式挂载，Agent 无法通过正常文件写入工具修改它。
- MCP 工具由 Agent 自动选择调用；只配置可信 Server，并限制其权限。
- 启用的 MCP 越多，工具描述占用的上下文越多。只启用会话实际需要的 Server。
- 远程 MCP 会接收查询内容和工具参数，其数据治理由对应 MCP 服务决定。
- 当前配置是全局单租户配置，不提供按 session 隔离的 MCP 密钥或 Server 列表。
