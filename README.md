# Eido AI Server

基于 FastAPI 的 AI 对话与技能执行服务。执行器为 Claude Agent SDK（内置 Claude Code CLI），
支持 Docker 部署与会话工作区隔离，通过 Anthropic 兼容端点连接任意 Model Provider。

## 架构

- **执行器**：`claude-agent-sdk` Python 包（内置原生 Claude Code CLI，无需 Node.js / npm）。
- **多轮续接**：同一 `session_id` 复用 `ClaudeSDKClient`，客户端重建时按原生会话 ID `resume`。
- **技能**：技能库挂载到会话工作区 `.claude/skills/`，由 Claude Code 原生 Skills 按需加载。
- **MCP**：一份只读 `mcp.json` 配置全部 MCP Server，新建或重建客户端时读取。

## 镜像构建（基础镜像 + 应用镜像）

基础镜像包含 OS 依赖与 Python 依赖，与代码分离，代码变更时无需重建：

```bash
# 1. 构建基础镜像（仅依赖变更时需要重建）
docker build -f Dockerfile.base -t eido-base:latest .

# 2. 构建应用镜像（代码变更时重建）
docker build -f Dockerfile -t eido-server:latest .
```

## 快速启动

### 1. 准备目录与环境变量

```bash
mkdir -p .claude/skills logs .eido/workspaces
cp mcp.json mcp.local.json

# 必填：Anthropic 兼容端点与凭据
export ANTHROPIC_BASE_URL=https://api.anthropic.com
export ANTHROPIC_API_KEY=your-api-key
# 或使用 auth token
# export ANTHROPIC_AUTH_TOKEN=your-token

export ANTHROPIC_MODEL=claude-sonnet-4-6

# 可选
export EIDO_PORT=8000
export SKILLS_DIR=./.claude/skills
export LOG_DIR=./logs
export EIDO_DATA_DIR=./.eido
```

### 2. 启动服务

```bash
docker compose up -d
```

### 3. 查看状态与日志

```bash
docker compose ps
docker compose logs -f
```

### 宿主机目录映射说明

| 宿主机路径 | 容器路径 | 用途 |
|-----------|----------|------|
| `./logs` | `/var/log/eido` | 应用日志 |
| `./.eido` | `/workspace/.eido` | 会话工作区 |
| `./.claude/skills` | `/workspace/.claude/skills` | 技能定义（只读） |
| `./mcp.json` | `/etc/eido/mcp.json` | MCP 配置（只读） |

---

## API 与 curl 示例

默认地址：`http://localhost:8000`（可通过 `EIDO_PORT` 修改映射端口）。

### 健康检查

```bash
curl -s http://localhost:8000/health | jq .
```

```json
{"status":"healthy","version":"1.0.0"}
```

### 创建会话

```bash
curl -s -X POST http://localhost:8000/api/v1/sessions/ \
  -H "Content-Type: application/json" \
  -d '{}' | jq .
```

```json
{"id":"a1b2c3d4e5f6"}
```

将返回的 `id` 记为 `SESSION_ID`：

```bash
export SESSION_ID=a1b2c3d4e5f6
```

### 列出会话

```bash
curl -s http://localhost:8000/api/v1/sessions/ | jq .
```

### 上传文件到会话工作区

支持 `.md` `.pdf` `.csv` `.xls` `.xlsx`，单文件最大 20MB。

```bash
curl -s -X POST "http://localhost:8000/api/v1/chat/upload" \
  -F "session_id=${SESSION_ID}" \
  -F "file=@/path/to/report.pdf" | jq .
```

```json
{"path":"/workspace/.eido/workspaces/a1b2c3d4e5f6/uploads/xxxxxxxx_report.pdf","name":"report.pdf"}
```

### 流式对话

```bash
curl -N -X POST http://localhost:8000/api/v1/chat/chat \
  -H "Content-Type: application/json" \
  -d "{
    \"session_id\": \"${SESSION_ID}\",
    \"messages\": [
      {\"role\": \"system\", \"content\": \"始终使用中文回答\"},
      {\"role\": \"user\", \"content\": \"你好，请简要介绍你自己\"}
    ]
  }"
```

带流水线 `context` 字段：

```bash
curl -N -X POST http://localhost:8000/api/v1/chat/chat \
  -H "Content-Type: application/json" \
  -d "{
    \"session_id\": \"${SESSION_ID}\",
    \"messages\": [
      {\"role\": \"user\", \"content\": \"根据上传的财报继续分析\"}
    ],
    \"context\": \"上一步技能输出的摘要...\"
  }"
```

响应为 SSE（`text/event-stream`），每行形如 `data: {"type":"content",...}`，结束为 `data: [DONE]`。

首轮请求中的非空 `system` message 会按顺序合并，并原生追加到 Claude Code system
prompt。同一 `session_id` 的后续请求可以重复传入完全相同的 system message，也可以省略；
为保证原生会话语义一致，中途变更 system message 会返回 SSE `error`，此时应创建新会话。

`messages` 支持 `user`、`assistant`、`system` 三种角色。冷启动时，最后一条 `user`
作为当前请求，此前的 `user`/`assistant` 作为带角色的历史上下文恢复；正常续轮由 Claude
Code 原生会话保存历史，不会重复注入调用方再次携带的历史消息。

每个 HTTP 响应都会返回 `X-Trace-Id`。也可以由调用方传入合法的
`X-Trace-Id` 以串联上游请求。默认日志记录请求结果和 Claude 单轮执行摘要，
并附带 `traceId` 与 `sessionId`。需要查看 Claude 原始消息时，可设置 `LOG_LEVEL=DEBUG`。

同一 `session_id` 的后续轮次复用客户端，由 Claude Code 管理对话历史。删除会话时，
服务先结束当前执行并断开客户端，再删除工作区。

### MCP 服务

Server 通过一份只读 `mcp.json` 配置 MCP。默认文件：

```json
{
  "mcpServers": {
    "example": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "{env:MCP_API_TOKEN}"
      }
    }
  }
}
```

修改后，新建或重建的客户端会读取最新配置。完整格式见
[MCP 使用指南](docs/mcp.md)。

### 列出会话工作区文件

```bash
curl -s "http://localhost:8000/api/v1/workspace/files?session_id=${SESSION_ID}" | jq .
```

### 下载工作区文件

```bash
curl -s "http://localhost:8000/api/v1/workspace/file?session_id=${SESSION_ID}&path=outputs/result.md" \
  -o result.md
```

### 删除会话（含工作区中间文件）

```bash
curl -s -X DELETE "http://localhost:8000/api/v1/sessions/${SESSION_ID}" | jq .
```

```json
{"deleted":true}
```

### Chat 服务健康

```bash
curl -s http://localhost:8000/api/v1/chat/health | jq .
```

### OpenAPI 文档

- Swagger UI: http://localhost:8000/api/v1/docs
- OpenAPI JSON: http://localhost:8000/api/v1/openapi.json

---

## 宿主机定时清理（日志与会话）

脚本内已写死项目相对路径，按**修改时间超过 7 天**清理：

| 脚本 | 清理路径 |
|------|----------|
| `scripts/cleanup-logs.sh` | `<项目根>/logs/` |
| `scripts/cleanup-sessions.sh` | `<项目根>/.eido/workspaces/` |

### 手动执行

```bash
chmod +x scripts/cleanup-logs.sh scripts/cleanup-sessions.sh

# 预览（不实际删除）
DRY_RUN=1 ./scripts/cleanup-logs.sh
DRY_RUN=1 ./scripts/cleanup-sessions.sh

# 执行清理
./scripts/cleanup-logs.sh
./scripts/cleanup-sessions.sh
```

### 宿主机 crontab 示例

将 `/path/to/eido-ai-server` 替换为实际项目路径后执行 `crontab -e`：

```cron
0 3 * * * /path/to/eido-ai-server/scripts/cleanup-logs.sh >> /path/to/eido-ai-server/logs/cron-cleanup.log 2>&1
30 3 * * * /path/to/eido-ai-server/scripts/cleanup-sessions.sh >> /path/to/eido-ai-server/logs/cron-cleanup.log 2>&1
```

---

后端开发说明见 [backend/README.md](backend/README.md)。
