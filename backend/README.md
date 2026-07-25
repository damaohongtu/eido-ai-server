# Eido Backend

基于 FastAPI 的后端服务，提供技能执行、普通对话和技能管理 API。

---

## 目录结构

```
backend/
├── app/
│   ├── main.py                     # FastAPI 应用入口、启动事件
│   ├── api/v1/
│   │   ├── api.py                  # 路由注册
│   │   └── endpoints/
│   │       ├── chat.py             # POST /chat/chat — 对话与技能执行
│   │       ├── skills.py           # GET  /skills/   — 技能列表与详情
│   │       ├── mcp.py              # MCP 工具注册相关
│   │       └── workflow.py         # 健康检查
│   ├── core/
│   │   └── config.py               # Pydantic Settings（读取 .env）
│   ├── schemas/
│   │   └── chat.py                 # 请求/响应 Pydantic 模型
│   └── services/
│       ├── claude_skill_service.py # Claude SDK 长连接、原生 Skills 与 SSE
│       ├── open_code_service.py    # OpenCode CLI、原生会话续接与 SSE
│       ├── llm_service.py          # DeepSeek 普通对话
│       └── mcp_registry.py         # MCP 工具注册表
├── alembic/                        # 数据库迁移（保留备用）
├── scripts/                        # 辅助脚本
├── requirements.txt
├── run.py                          # 开发启动入口
└── .env.example                    # 环境变量模板
```

---

## 快速启动

```bash
conda activate eido
cd backend

# 首次安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，至少填入 ANTHROPIC_API_KEY

# 启动开发服务器（热重载）
python run.py
```

服务启动后：
- API 文档（Swagger）：http://localhost:8000/api/v1/docs
- 健康检查：http://localhost:8000/health

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ANTHROPIC_API_KEY` | Claude Agent SDK API Key | 必填* |
| `ANTHROPIC_MODEL` | Claude 模型 | provider 默认值 |
| `AGENT_HARNESS` | `claude_code` / `open_harness` / `opencode` | `claude_code` |
| `OPENCODE_MODEL` | OpenCode 可选模型，格式 `provider/model` | OpenCode 默认值 |
| `OPENCODE_CONFIG` | OpenCode JSON/JSONC 配置文件绝对路径 | 空 |
| `OPENCODE_CONFIG_CONTENT` | OpenCode JSON 配置；Docker 中可用于 provider 认证 | 空 |
| `EIDO_DATA_ROOT` | 会话和隔离的 OpenCode 数据根目录 | `{workspace}/.eido` |
| `SKILLS_DIR` | 技能目录路径 | `{workspace}/.claude/skills` |
| `WORKSPACE_ROOT` | 工作区根路径（传给 claude_agent_sdk） | 自动推断 |
| `LOG_LEVEL` | 日志级别 | `INFO` |

---

## API 说明

### POST `/api/v1/chat/chat`

统一对话入口，通过 `harness` 选择执行内核。

```json
{
  "session_id": "a1b2c3d4e5f6",
  "messages": [
    {"role": "user", "content": "@A股财报点评 分析中望软件2024年报"}
  ],
  "context": "（可选）上一步技能的输出，用于多技能流水线",
  "harness": "claude_code"
}
```

`harness` 可选 `claude_code`、`open_harness`、`opencode`；不传时使用
`AGENT_HARNESS`。响应固定为 SSE 流。

响应为 SSE 流，事件类型：

| type | 说明 |
|------|------|
| `thinking` | 执行状态提示 |
| `workflow_start` | 技能开始执行 |
| `content` | 正文增量内容 |
| `tool_use` | 工具调用信息 |
| `workflow_complete` | 执行完成 |
| `error` | 执行错误 |

## 技能服务（ClaudeSkillService）

核心服务位于 `app/services/claude_skill_service.py`：

- **`scan_skills()`** — 扫描 `SKILLS_DIR`，解析每个子目录的 `SKILL.md` frontmatter
- **`get_skill(skill_id)`** — 按目录名加载单个技能
- **`execute_stream(messages, context, session_id=...)`** — 优先复用 session 级 `ClaudeSDKClient`，以 SSE 格式流式返回

首轮由 Claude Code 原生 Skills 按需加载技能，并带入既有历史；续轮依赖原生
session，只发送最新用户请求。SDK、工具和 OpenCode 原始输出均完整写入带
`traceId`、`sessionId` 的日志。

## OpenCodeService

`app/services/open_code_service.py` 通过 OpenCode CLI 的 JSON 流接入统一 SSE：

- 请求指定 `"harness": "opencode"`，或配置 `AGENT_HARNESS=opencode`。
- 模型使用 `OPENCODE_MODEL=provider/model`；生产环境建议显式设置。
- 本机凭据可通过 `opencode auth login` 配置，并用 `opencode auth list` 检查。
- 同一 Eido `session_id` 在服务进程存活期间续接对应 OpenCode 原生 session。
- OpenCode 工作目录固定为当前会话工作区，读取 `uploads/`、写入 `outputs/`。
- 原始 NDJSON、推理、正文、工具输入/输出和执行汇总均完整写入日志。

完整操作步骤及排障说明见 [OpenCode 使用指南](../docs/opencode.md)。
