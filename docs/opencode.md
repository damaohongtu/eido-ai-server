# OpenCode 使用指南

本文说明如何在 Eido AI Server 中配置和使用 OpenCode。当前集成面向纯 Server、
单租户和单次会话场景，不引入 Eido Project 概念。

OpenCode 官方资料：

- [CLI](https://opencode.ai/docs/cli/)
- [Providers](https://opencode.ai/docs/providers/)
- [Models](https://opencode.ai/docs/models/)
- [Config](https://opencode.ai/docs/config/)

## 1. 集成方式

客户端仍然调用 Eido 的统一接口：

```text
POST /api/v1/chat/chat
```

请求中指定 `"harness": "opencode"` 后，服务端会：

1. 根据 `session_id` 创建或打开独立会话工作区。
2. 将 OpenCode 的工作目录设置为该会话目录。
3. 首轮通过 `opencode run --format json --thinking --auto` 启动执行。
4. 记录 OpenCode 返回的原生 session ID；同一 Eido `session_id` 的后续请求通过
   `--session` 续接。
5. 将 OpenCode NDJSON 转换为 Eido SSE，同时完整记录原始与语义化执行日志。

会话目录结构：

```text
.eido/workspaces/<session_id>/
├── uploads/    # 用户上传文件
└── outputs/    # OpenCode 生成的产物
```

OpenCode 会收到明确的工作区说明：读取 `uploads/`，并将生成文件写入 `outputs/`。
`SKILLS_DIR/*/SKILL.md` 会作为可用技能索引加入首轮提示词。

## 2. 本机快速开始

### 2.1 安装与检查

Docker 镜像已安装 OpenCode；本机运行后端时需要先安装 CLI：

```bash
node --version
npm install -g opencode-ai
opencode --version
```

建议使用 Node.js 22。查看当前 OpenCode 支持的模型：

```bash
opencode models
```

模型 ID 必须使用 `provider/model` 格式，例如：

```text
zai/glm-5.1
deepseek/deepseek-chat
anthropic/claude-sonnet-4-6
```

实际可用模型取决于当前 OpenCode 版本、Provider 账户权限和认证状态，请以
`opencode models` 的输出为准。

### 2.2 配置 Provider 凭据

推荐使用 OpenCode 自带的交互式认证：

```bash
opencode auth login
opencode auth list
```

选择 Provider 后输入 API Key。OpenCode 默认将凭据保存在：

```text
~/.local/share/opencode/auth.json
```

Eido 本机模式默认保留 OpenCode 的标准数据目录，因此能直接复用这些凭据。

认证后先脱离 Eido 做一次最小验证：

```bash
opencode run \
  --model zai/glm-5.1 \
  --format json \
  --thinking \
  --auto \
  --dir "$PWD" \
  "只回复：OpenCode认证成功"
```

该命令应至少产生 `text` 事件，并在 `step_finish` 中出现非零 token。若这里只得到
`reason=unknown`、零 token 或没有 `text/tool_use`，应先修复 Provider 或模型配置，
而不是排查 Eido API。

### 2.3 配置 Eido

如果在项目根目录通过环境变量启动：

```bash
export AGENT_HARNESS=opencode
export OPENCODE_MODEL=zai/glm-5.1
```

如果进入 `backend/` 运行 `python run.py`，也可以写入 `backend/.env`：

```dotenv
AGENT_HARNESS=opencode
OPENCODE_MODEL=zai/glm-5.1
```

修改配置后需要重启后端。检查服务：

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8000/api/v1/chat/health
```

`AGENT_HARNESS=opencode` 将 OpenCode 设为默认执行器。也可以保留其他默认值，
在每个请求中单独指定 `"harness": "opencode"`。

## 3. 完整 API 调用流程

### 3.1 创建会话

```bash
export EIDO_BASE_URL=http://localhost:8000
SESSION_JSON=$(curl -s -X POST "$EIDO_BASE_URL/api/v1/sessions/" \
  -H "Content-Type: application/json" \
  -d '{}')
export SESSION_ID="$(printf '%s' "$SESSION_JSON" | jq -r '.id')"

echo "$SESSION_ID"
```

`session_id` 是 Eido 对外会话 ID，不需要也不应该由调用方生成 OpenCode 原生
session ID。

### 3.2 上传文件

```bash
curl -s -X POST "$EIDO_BASE_URL/api/v1/chat/upload" \
  -F "session_id=$SESSION_ID" \
  -F "file=@/absolute/path/report.pdf" | jq .
```

上传接口支持 `.md`、`.pdf`、`.csv`、`.xls`、`.xlsx`，单文件最大 20 MB。文件会被
重命名后写入当前会话的 `uploads/`，OpenCode 可以直接枚举该目录，不必把返回的
容器绝对路径再次拼进提示词。

### 3.3 发起 OpenCode 对话

```bash
curl -N -X POST "$EIDO_BASE_URL/api/v1/chat/chat" \
  -H "Content-Type: application/json" \
  -H "X-Trace-Id: opencode-report-001" \
  -d "{
    \"session_id\": \"$SESSION_ID\",
    \"messages\": [
      {
        \"role\": \"user\",
        \"content\": \"分析 uploads 中的文件，并将完整 HTML 报告写入 outputs/report.html\"
      }
    ],
    \"harness\": \"opencode\"
  }"
```

常见 SSE：

```text
data: {"type":"thinking","content":"正在通过 OpenCode 分析请求..."}
data: {"type":"workflow_start","skill_name":"auto"}
data: {"type":"thinking","content":"OpenCode 开始第 1 个推理步骤..."}
data: {"type":"thinking","content":"✓ 工具完成: read ..."}
data: {"type":"content","content":"报告已生成..."}
data: {"type":"workflow_complete","data":{"references":[]}}
data: [DONE]
```

说明：

- `thinking`：推理、步骤状态和工具执行进度。
- `content`：模型正文增量。
- `workflow_complete`：执行成功。
- `error`：认证、模型、CLI 或执行错误；错误请求不会再发送
  `workflow_complete`。
- `: ping`：长任务心跳，不是业务事件。

客户端应持续读取到 `data: [DONE]`，不能把普通 HTTP 200 当作 Agent 已完成。

### 3.4 在同一会话中继续

后续请求继续使用相同 `session_id`：

```bash
curl -N -X POST "$EIDO_BASE_URL/api/v1/chat/chat" \
  -H "Content-Type: application/json" \
  -H "X-Trace-Id: opencode-report-002" \
  -d "{
    \"session_id\": \"$SESSION_ID\",
    \"messages\": [
      {\"role\": \"user\", \"content\": \"检查刚生成的 HTML，修复排版问题\"}
    ],
    \"harness\": \"opencode\"
  }"
```

服务进程未重启时，只发送最新用户消息即可，Eido 会续接 OpenCode 原生会话。

如果 Eido 服务重启，当前版本不会持久化“Eido session ID → OpenCode session ID”的
映射。此时会创建新的 OpenCode 会话；如需恢复对话语义，客户端应在 `messages`
中重新发送必要历史。`uploads/` 和 `outputs/` 文件仍然保留。

同一 `session_id` 不应并发发送多个 Agent 请求；应等待上一条 SSE 到 `[DONE]`
后再发送下一条。

### 3.5 查看和下载产物

```bash
curl -s \
  "$EIDO_BASE_URL/api/v1/workspace/files?session_id=$SESSION_ID" | jq .

curl -s \
  "$EIDO_BASE_URL/api/v1/workspace/file?session_id=$SESSION_ID&path=outputs/report.html&download=true" \
  -o report.html
```

### 3.6 删除会话

```bash
curl -s -X DELETE \
  "$EIDO_BASE_URL/api/v1/sessions/$SESSION_ID" | jq .
```

删除会话会清理 Eido 会话工作区，并回收内存中的 OpenCode 续接状态。

## 4. 配置参考

| 变量 | 作用 | 建议 |
|------|------|------|
| `AGENT_HARNESS` | 默认执行器 | 设为 `opencode`，或每次请求传 `harness` |
| `OPENCODE_MODEL` | 模型 ID，格式 `provider/model` | 生产环境显式设置 |
| `OPENCODE_CONFIG` | OpenCode JSON/JSONC 配置文件路径 | 使用绝对路径；Docker 中必须是容器内路径 |
| `OPENCODE_CONFIG_CONTENT` | 内联 JSON 配置 | 适合 Docker/CI 注入运行时配置 |
| `EIDO_DATA_ROOT` | Eido 数据根目录，同时决定隔离的 OpenCode 数据目录 | Docker 固定为 `/workspace/.eido` |
| `LOG_LEVEL` | 控制控制台日志级别 | 默认 `INFO` 已包含完整执行过程 |

OpenCode 的 MCP Server 不写在 `OPENCODE_CONFIG_CONTENT` 中，而是统一配置在
`MCP_CONFIG_PATH` 指向的 `mcp.json`；Eido 会自动转换并合并。详见
[MCP 使用指南](mcp.md)。

模型选择顺序在本服务中是：

1. `OPENCODE_MODEL` 非空时，Eido 将其作为 `opencode run --model` 传入，优先级最高。
2. 否则使用 OpenCode 配置中的 `model`。
3. 再否则由 OpenCode 按最近使用模型和内部默认顺序选择。

生产环境不建议依赖“最近使用模型”，否则升级或切换凭据后可能选中不同模型。

### 4.1 使用独立配置文件

示例 `/absolute/path/opencode.json`：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "anthropic/claude-sonnet-4-6",
  "share": "disabled",
  "autoupdate": false,
  "provider": {
    "anthropic": {
      "options": {
        "apiKey": "{env:ANTHROPIC_API_KEY}"
      }
    }
  }
}
```

配置：

```bash
export OPENCODE_CONFIG=/absolute/path/opencode.json
export ANTHROPIC_API_KEY=your-api-key
```

OpenCode 支持 `{env:VARIABLE_NAME}` 和 `{file:path}` 变量替换。建议通过环境变量或
只读 secret 文件提供 API Key，不要把密钥提交到 Git。

如果使用 Anthropic 兼容网关，可以在 Provider options 中增加：

```json
{
  "baseURL": "https://your-gateway.example.com/v1"
}
```

### 4.2 使用内联配置

```bash
export OPENCODE_CONFIG_CONTENT='{"$schema":"https://opencode.ai/config.json","model":"anthropic/claude-sonnet-4-6","share":"disabled","autoupdate":false,"provider":{"anthropic":{"options":{"apiKey":"{env:ANTHROPIC_API_KEY}"}}}}'
```

`OPENCODE_CONFIG_CONTENT` 会作为 OpenCode 的运行时配置参与合并。若同时设置
`OPENCODE_MODEL`，Eido 传入的 `--model` 仍优先。

## 5. Docker 使用

镜像内已包含 Node.js 22 和 OpenCode CLI。

### 5.1 使用现有 Anthropic 环境变量

```bash
export AGENT_HARNESS=opencode
export OPENCODE_MODEL=anthropic/claude-sonnet-4-6
export ANTHROPIC_API_KEY=your-api-key
export ANTHROPIC_BASE_URL=https://api.anthropic.com
export OPENCODE_CONFIG_CONTENT='{"$schema":"https://opencode.ai/config.json","share":"disabled","autoupdate":false,"provider":{"anthropic":{"options":{"apiKey":"{env:ANTHROPIC_API_KEY}"}}}}'

docker compose up -d --build
```

这里通过 `OPENCODE_CONFIG_CONTENT` 明确让 OpenCode 的 Anthropic Provider 读取
compose 已注入的 `ANTHROPIC_API_KEY`。

### 5.2 在容器中交互式认证其他 Provider

OpenCode 数据目录固定为：

```text
/workspace/.eido/opencode-data
```

该目录位于 compose 的 `.eido` 挂载卷中，因此容器重建后凭据仍然存在。

```bash
docker compose run --rm eido opencode auth login
docker compose run --rm eido opencode auth list
docker compose run --rm eido opencode models
```

认证和模型确认完成后：

```bash
export AGENT_HARNESS=opencode
export OPENCODE_MODEL=zai/glm-5.1
docker compose up -d --force-recreate
```

也可以对已启动的容器执行：

```bash
docker compose exec eido opencode auth list
docker compose exec eido opencode models
```

### 5.3 Docker 中使用配置文件

宿主机配置文件必须挂载到容器，然后把 `OPENCODE_CONFIG` 设置为容器内路径：

```yaml
services:
  eido:
    environment:
      - OPENCODE_CONFIG=/etc/opencode/opencode.json
      - OPENCODE_MODEL=anthropic/claude-sonnet-4-6
    volumes:
      - ./opencode.json:/etc/opencode/opencode.json:ro
```

## 6. 日志与追踪

请求可以显式传入 `X-Trace-Id`：

```bash
curl -N -X POST "$EIDO_BASE_URL/api/v1/chat/chat" \
  -H "Content-Type: application/json" \
  -H "X-Trace-Id: opencode-debug-001" \
  -d "{\"session_id\":\"$SESSION_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"检查 uploads 目录\"}],\"harness\":\"opencode\"}"
```

本机查看：

```bash
grep 'traceId=opencode-debug-001' backend/logs/app.log
# 如果 LOG_DIR=./logs，则改为：
grep 'traceId=opencode-debug-001' logs/app.log
```

Docker 查看：

```bash
docker compose logs -f eido
grep 'traceId=opencode-debug-001' logs/app.log
```

每条应用日志均包含 `traceId` 和 `sessionId`。OpenCode 日志包括：

- 完整原始 NDJSON：`OpenCode/stdout`。
- 模型推理：`OpenCode/Assistant/Reasoning`。
- 模型正文：`OpenCode/Assistant/Text`。
- 工具输入与完整输出：`OpenCode/Tool/Input`、`OpenCode/Tool/Output`。
- 步骤 token、费用与停止原因：`OpenCode/Step/Finish`。
- 本轮事件数、工具数、正文长度、总 token、退出码和耗时。

超长内容按带有 `chunk=x/y` 的有序日志块记录，所有分块均包含同一个
`traceId`、`sessionId`，不会只保留预览或丢失尾部。

## 7. 常见问题

### 7.1 `reason: unknown`、零 token、没有正文

依次检查：

```bash
opencode auth list
opencode models
opencode run --model "$OPENCODE_MODEL" --format json --thinking --auto "只回复：测试成功"
```

Docker 则在容器内执行：

```bash
docker compose exec eido opencode auth list
docker compose exec eido opencode models
```

确认：

- `OPENCODE_MODEL` 是 `provider/model`，且确实出现在 `opencode models` 中。
- 模型对应的 Provider 已认证。
- API Key 有效、余额和模型权限正常。
- 修改环境变量后已重启或重建服务。

服务端检测到 OpenCode 成功退出但没有 `text` 或 `tool_use` 时，会返回明确的
`error` SSE，不会误报 `workflow_complete`。

### 7.2 本机 CLI 正常，Eido 中认证失败

如果显式设置了 `EIDO_DATA_ROOT`，Eido 会把 OpenCode 的 `XDG_DATA_HOME` 指向：

```text
<EIDO_DATA_ROOT>/opencode-data
```

这时 OpenCode 不再读取默认的 `~/.local/share/opencode/auth.json`。请在相同
`XDG_DATA_HOME` 下重新认证，或取消该设置：

```bash
XDG_DATA_HOME=/your/eido-data/opencode-data opencode auth login
```

### 7.3 Docker 配置文件找不到

`OPENCODE_CONFIG` 必须是容器内可见的绝对路径。仅设置宿主机路径不会自动挂载，
需要同时添加 volume。

### 7.4 第二轮没有续接上下文

确认：

- 两次请求的 `session_id` 完全一致。
- 第一条 SSE 已读取到 `[DONE]` 后才发送第二条。
- 两轮之间 Eido 服务没有重启。
- 没有调用 `DELETE /api/v1/sessions/<session_id>`。

服务重启后如需继续语义上下文，应在 `messages` 中重发必要历史；会话工作区文件
不会因此丢失。

### 7.5 任务完成但找不到文件

明确要求 OpenCode 将产物写入 `outputs/<filename>`，然后调用 workspace files API
确认实际路径。不要要求写入项目目录或会话工作区之外。

## 8. 安全注意事项

Eido 以 `--auto` 启动 OpenCode，适合无人工确认的 Server 执行。请注意：

- 仅向可信调用方开放 API，并在外层增加鉴权和访问控制。
- 优先使用 Docker 隔离，不要让服务进程持有不必要的宿主机权限。
- Skills 目录建议只读挂载。
- Provider 密钥使用环境变量、secret 或只读文件，不要写入提示词和 Git。
- 生产环境建议设置 `share: "disabled"`，避免误共享会话。
- 不要对同一 `session_id` 并发执行多个 OpenCode 请求。
