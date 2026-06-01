# Eido AI Server

基于 FastAPI 的 AI 对话与技能执行服务，支持 Docker 部署与会话工作区隔离。

## 快速启动（docker-compose.yml）

### 1. 构建镜像

```bash
docker build -f Dockerfile -t eido-server:latest .
```

### 2. 准备目录与环境变量

```bash
mkdir -p .claude/skills logs .eido/workspaces

# 必填：Anthropic 兼容 API（直连或自建代理）
export ANTHROPIC_BASE_URL=https://api.anthropic.com
export ANTHROPIC_API_KEY=your-api-key
# 或使用 auth token
# export ANTHROPIC_AUTH_TOKEN=your-token

export ANTHROPIC_MODEL=claude-sonnet-4-6
export AGENT_HARNESS=claude_code

# 可选
export EIDO_PORT=8000
export SKILLS_DIR=./.claude/skills
export LOG_DIR=./logs
export EIDO_DATA_DIR=./.eido
```

### 3. 启动服务

```bash
docker compose up -d
```

### 4. 查看状态与日志

```bash
docker compose ps
docker compose logs -f
```

### docker-compose.yml 完整示例

```yaml
services:
  eido:
    image: eido-server:latest
    container_name: eido-server
    restart: unless-stopped
    ports:
      - "${EIDO_PORT:-8000}:8000"
    environment:
      - ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN:-}
      - ANTHROPIC_MODEL=${ANTHROPIC_MODEL:-}
      - ANTHROPIC_SMALL_FAST_MODEL=${ANTHROPIC_SMALL_FAST_MODEL:-}
      - API_TIMEOUT_MS=${API_TIMEOUT_MS:-300000}
      - AGENT_HARNESS=${AGENT_HARNESS:-claude_code}
    volumes:
      - ${SKILLS_DIR:-./.claude/skills}:/workspace/.claude/skills:ro
      - ${LOG_DIR:-./logs}:/var/log/eido
      - ${EIDO_DATA_DIR:-./.eido}:/workspace/.eido
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://127.0.0.1:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s
```

宿主机目录映射说明：

| 宿主机路径 | 容器路径 | 用途 |
|-----------|----------|------|
| `./logs` | `/var/log/eido` | 应用日志 |
| `./.eido` | `/workspace/.eido` | 会话 DB、工作区（`workspaces/<session_id>/uploads|outputs`） |
| `./.claude/skills` | `/workspace/.claude/skills` | 技能定义（只读） |

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
      {\"role\": \"user\", \"content\": \"你好，请简要介绍你自己\"}
    ]
  }"
```

带技能上下文或流水线 `context` 字段：

```bash
curl -N -X POST http://localhost:8000/api/v1/chat/chat \
  -H "Content-Type: application/json" \
  -d "{
    \"session_id\": \"${SESSION_ID}\",
    \"messages\": [
      {\"role\": \"user\", \"content\": \"根据上传的财报继续分析\"}
    ],
    \"context\": \"上一步技能输出的摘要...\",
    \"harness\": \"claude_code\"
  }"
```

响应为 SSE（`text/event-stream`），每行形如 `data: {"type":"content",...}`，结束为 `data: [DONE]`。

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

## 其他部署方式

- **内置 LiteLLM 代理**：`docker compose -f docker-compose.litellm.yml up -d`（需先 `docker build -f Dockerfile.litellm -t eido-server-litellm:latest .`）
- **脚本启动**：`./appctl.sh`（见 `appctl.sh --help`）

后端开发说明见 [backend/README.md](backend/README.md)。
