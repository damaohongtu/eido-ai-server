# Eido Backend

基于 FastAPI 的后端服务，通过 Claude Agent SDK 执行技能与对话，以 SSE 流式返回。

---

## 目录结构

```
backend/
├── app/
│   ├── main.py                     # FastAPI 应用入口、启动事件
│   ├── api/v1/
│   │   ├── api.py                  # 路由注册
│   │   └── endpoints/
│   │       ├── chat.py             # POST /chat/chat、/chat/upload
│   │       ├── sessions.py         # 会话 CRUD
│   │       └── workspace.py        # 工作区文件管理
│   ├── core/
│   │   ├── config.py               # Pydantic Settings（读取 .env）
│   │   └── mcp_config.py           # 只读 MCP 配置加载与校验
│   ├── schemas/
│   │   └── chat.py                 # 请求/响应 Pydantic 模型
│   └── services/
│       ├── agent_service.py         # Claude Agent SDK 执行与 SSE
│       └── session_workspace.py    # 单次会话工作区隔离
├── requirements.txt                # 运行时依赖（进基础镜像）
├── requirements-dev.txt            # 开发/测试依赖
├── run.py                          # 开发启动入口
└── .env.example                    # 环境变量模板
```

---

## 快速启动

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，至少填入 ANTHROPIC_API_KEY

# 启动开发服务器（热重载）
python run.py
```

服务启动后：

- API 文档（Swagger）：http://localhost:8000/api/v1/docs
- 健康检查：http://localhost:8000/health

### 运行测试

```bash
python -m pytest ../tests
```

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ANTHROPIC_BASE_URL` | Anthropic 兼容端点 | 空（官方端点） |
| `ANTHROPIC_API_KEY` | API Key | 必填* |
| `ANTHROPIC_AUTH_TOKEN` | Auth Token（与 API_KEY 二选一） | 空 |
| `ANTHROPIC_MODEL` | Claude 模型 | provider 默认值 |
| `ANTHROPIC_SMALL_FAST_MODEL` | 小模型 | provider 默认值 |
| `API_TIMEOUT_MS` | API 超时 | `300000` |
| `MCP_CONFIG_PATH` | MCP JSON 文件路径；空表示禁用 MCP | `{workspace}/mcp.json` |
| `EIDO_DATA_ROOT` | 会话工作区数据根目录 | `{workspace}/.eido` |
| `SKILLS_DIR` | 技能目录路径 | `{workspace}/.claude/skills` |
| `WORKSPACE_ROOT` | 工作区根路径（传给 claude_agent_sdk） | 自动推断 |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `LOG_DIR` | 日志目录 | `logs` |

---

## API 说明

### POST `/api/v1/chat/chat`

统一对话入口，执行器为 Claude Agent SDK。

```json
{
  "session_id": "a1b2c3d4e5f6",
  "messages": [
    {"role": "user", "content": "@A股财报点评 分析中望软件2024年报"}
  ],
  "context": "（可选）上一步技能的输出，用于多技能流水线"
}
```

响应为 SSE 流，事件类型：

| type | 说明 |
|------|------|
| `thinking` | 执行状态提示 |
| `workflow_start` | 技能开始执行 |
| `content` | 正文增量内容 |
| `workflow_complete` | 执行完成 |
| `error` | 执行错误 |

## Agent 服务（AgentService）

核心服务位于 `app/services/agent_service.py`：

- **`execute_stream(messages, context, session_id=...)`** — 通过 SDK 一次性
  `query()` 执行，以 SSE 格式流式返回。
- **多轮续接**：SDK 原生 `resume`。首轮记录 `ResultMessage.session_id`，
  同一 Eido session 的后续请求自动续接；resume 失败时自动回退重建。
- **技能加载**：`SKILLS_DIR` 以目录符号链接挂到会话工作区 `.claude/skills/`，
  由 Claude Code 原生 Skills 机制按需加载，无需扫描解析。
- **并发保护**：同一 session 同时只允许一轮执行。
- SDK 消息（正文、推理、工具输入/输出、执行汇总）均完整写入带
  `traceId`、`sessionId` 的日志。

## MCP 配置

`MCP_CONFIG_PATH` 指向标准 `{"mcpServers": {...}}` JSON。每轮执行前重新读取，
修改后无需重启。完整说明见 [MCP 使用指南](../docs/mcp.md)。
