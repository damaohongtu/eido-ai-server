"""Claude Agent SDK 执行服务：原生 resume 多轮续接 + 原生 Skills + SSE 流式输出。"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncGenerator, List, Optional
from urllib.parse import urlsplit

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, query
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SEC = 12.0
_HEARTBEAT_FRAME = ": ping\n\n"
CLAUDE_LOG_CHUNK_CHARS = 4000


def _serialize_log_value(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return repr(value)


def _log_complete_value(
    event_name: str, value: object, level: int = logging.INFO
) -> None:
    """Log complete Claude SDK content in ordered collector-friendly chunks."""
    content = _serialize_log_value(value)
    chunks = [
        content[offset : offset + CLAUDE_LOG_CHUNK_CHARS]
        for offset in range(0, len(content), CLAUDE_LOG_CHUNK_CHARS)
    ] or [""]
    for index, chunk in enumerate(chunks, start=1):
        logger.log(
            level,
            "  [Claude/%s chunk=%d/%d chars=%d] %s",
            event_name,
            index,
            len(chunks),
            len(content),
            chunk,
        )


def extract_latest_user_text(messages: list) -> str:
    def _role(m: object) -> str:
        return (
            getattr(m, "role", None)
            or (m.get("role") if isinstance(m, dict) else "")
            or ""
        )

    def _content(m: object) -> str:
        c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        return (c or "").strip()

    for msg in reversed(messages or []):
        if _role(msg) == "user":
            return _content(msg)
    return ""


def build_conversation_history_before_latest_user(
    messages: list, *, max_chars: int = 12_000
) -> str:
    latest_user_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if (
                getattr(messages[index], "role", None)
                or (
                    messages[index].get("role")
                    if isinstance(messages[index], dict)
                    else ""
                )
            )
            == "user"
        ),
        len(messages),
    )
    history = messages[:latest_user_index]
    selected: list[str] = []
    used = 0
    for message in reversed(history):
        role = (
            getattr(message, "role", None)
            or (message.get("role") if isinstance(message, dict) else "")
            or ""
        )
        content = (
            getattr(message, "content", None)
            or (message.get("content") if isinstance(message, dict) else "")
            or ""
        )
        if not role or not content:
            continue
        block = f"**{role}**: {content[:4000]}"
        if selected and used + len(block) > max_chars:
            break
        if len(block) > max_chars:
            block = block[:max_chars]
        selected.append(block)
        used += len(block)
    selected.reverse()
    return "\n\n".join(selected)


class _ResumeUnavailable(RuntimeError):
    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause


class _AgentAuthenticationError(RuntimeError):
    pass


class AgentService:

    AUTO_ALLOWED_TOOLS = [
        "Bash",
        "Glob",
        "Grep",
        "Read",
        "Write",
        "Edit",
        "WebFetch",
        "WebSearch",
    ]

    def __init__(self, skills_dir: Path, workspace_root: Path):
        self.skills_dir = skills_dir
        self.workspace_root = workspace_root
        self._claude_session_ids: dict[str, str] = {}
        self._busy_sessions: set[str] = set()

    def _ensure_native_skills_link(self, cwd: Path) -> None:
        """将技能库以单条目录符号链接挂到会话工作区，交给 Claude Code 原生发现。"""
        native_root = cwd / ".claude" / "skills"
        target = self.skills_dir.resolve()
        if native_root.is_symlink():
            current = (native_root.parent / os.readlink(native_root)).resolve()
            if current == target:
                return
            native_root.unlink()
        elif native_root.exists():
            logger.warning("原生技能目录已存在且非托管链接，保留并跳过: %s", native_root)
            return
        native_root.parent.mkdir(parents=True, exist_ok=True)
        native_root.symlink_to(target, target_is_directory=True)

    def reset_session(self, session_id: str) -> None:
        self._claude_session_ids.pop(session_id, None)
        self._busy_sessions.discard(session_id)

    async def execute_stream(
        self,
        messages: list,
        context: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        logger.info(
            f"▶ execute_stream 开始 | 消息数: {len(messages)}"
            + (f" | session={session_id}" if session_id else "")
            + (f" | 含上下文 {len(context)} 字符" if context else "")
        )

        yield self._sse(
            {"type": "thinking", "content": "正在分析请求，自动规划执行..."}
        )
        yield self._sse({"type": "workflow_start", "skill_name": "auto"})

        if session_id:
            if session_id in self._busy_sessions:
                yield self._sse(
                    {
                        "type": "error",
                        "message": f"会话 {session_id} 正在执行中，请等待上一轮完成",
                    }
                )
                yield "data: [DONE]\n\n"
                return
            self._busy_sessions.add(session_id)

            from app.services.session_workspace import get_session_workspace_manager

            try:
                cwd = get_session_workspace_manager().session_root(session_id)
            except ValueError as e:
                self._busy_sessions.discard(session_id)
                yield self._sse({"type": "error", "message": f"非法 session_id: {e}"})
                yield "data: [DONE]\n\n"
                return
        else:
            cwd = self.workspace_root

        try:
            try:
                self._ensure_native_skills_link(cwd)
            except Exception:
                logger.exception("原生技能目录映射失败，本轮继续执行（技能不可用）")

            latest_user_text = extract_latest_user_text(messages)
            if not latest_user_text:
                yield self._sse({"type": "error", "message": "未找到用户输入"})
                yield "data: [DONE]\n\n"
                return

            claude_sid = self._claude_session_ids.get(session_id or "")
            agent_env = self._build_agent_env(session_id)
            auth_error = self._agent_auth_error(agent_env)
            if auth_error:
                logger.error("Claude Agent SDK 认证配置缺失: %s", auth_error)
                yield self._sse({"type": "error", "message": auth_error})
                yield "data: [DONE]\n\n"
                return
            from app.core.config import settings
            from app.core.mcp_config import load_mcp_config

            mcp_environment = {**os.environ, **agent_env}
            try:
                mcp_config = load_mcp_config(
                    settings.MCP_CONFIG_PATH, environment=mcp_environment
                )
            except ValueError as exc:
                logger.error("Claude MCP 配置加载失败: %s", exc)
                yield self._sse(
                    {"type": "error", "message": f"MCP 配置错误: {exc}"}
                )
                yield "data: [DONE]\n\n"
                return
            auth_mode, provider = self._agent_auth_summary(agent_env)
            logger.info("  [ClaudeAuth] mode=%s provider=%s", auth_mode, provider)
            logger.info(
                "  [ClaudeMCP] path=%s servers=%s",
                mcp_config.path,
                ",".join(mcp_config.server_names) or "(none)",
            )

            async def _run_once(resume_sid: Optional[str]) -> AsyncGenerator[str, None]:
                prompt = self._build_prompt(
                    cwd=cwd,
                    latest_user_text=latest_user_text,
                    conversation_history=(
                        build_conversation_history_before_latest_user(messages)
                        if not resume_sid
                        else ""
                    ),
                    context=context,
                    resume=bool(resume_sid),
                )
                available_tools = list(self.AUTO_ALLOWED_TOOLS)
                available_tools.append("Skill")
                allowed_tools = list(available_tools)
                allowed_tools.extend(
                    f"mcp__{name}__*" for name in mcp_config.servers
                )
                options = ClaudeAgentOptions(
                    allowed_tools=allowed_tools,
                    tools=available_tools,
                    cwd=str(cwd),
                    setting_sources=["project"],
                    skills="all",
                    permission_mode="acceptEdits",
                    env=agent_env,
                    include_partial_messages=False,
                    max_buffer_size=10 * 1024 * 1024,
                    resume=resume_sid,
                    mcp_servers=mcp_config.servers,
                    strict_mcp_config=True,
                )
                message_count = 0
                saw_result = False
                run_started = time.perf_counter()
                try:
                    messages_iter = query(prompt=prompt, options=options)
                    logger.info(
                        "  [ClaudeRun] mode=%s prompt_chars=%d tools=%d mcp=%d cwd=%s",
                        "resume" if resume_sid else "fresh",
                        len(prompt),
                        len(available_tools),
                        len(mcp_config.servers),
                        cwd,
                    )
                    async for message in messages_iter:
                        message_count += 1
                        self._log_message(message)
                        if self._is_not_logged_in_message(message):
                            raise _AgentAuthenticationError(
                                self._agent_auth_failure_message()
                            )
                        if isinstance(message, ResultMessage):
                            saw_result = True
                            sid = getattr(message, "session_id", None)
                            if session_id and sid:
                                self._claude_session_ids[session_id] = sid
                        for event in self._convert_message(message):
                            yield event
                except ClaudeSDKError as exc:
                    if resume_sid and not message_count:
                        raise _ResumeUnavailable(exc) from exc
                    raise
                finally:
                    logger.info(
                        "  [ClaudeRun/Summary] mode=%s status=%s elapsed_ms=%.1f "
                        "messages=%d",
                        "resume" if resume_sid else "fresh",
                        "ok" if saw_result else "incomplete",
                        (time.perf_counter() - run_started) * 1000,
                        message_count,
                    )

            queue: asyncio.Queue = asyncio.Queue()
            _SENTINEL = object()
            had_error = {"v": False}

            async def producer() -> None:
                tried_resume = bool(claude_sid)
                try:
                    if tried_resume:
                        try:
                            async for ev in _run_once(claude_sid):
                                await queue.put(ev)
                            return
                        except _ResumeUnavailable as exc:
                            logger.warning(
                                "resume(%s) 在本轮输出前失败，清 sid 并回退到重建模式: %s",
                                claude_sid,
                                exc.cause,
                            )
                            if session_id:
                                self._claude_session_ids.pop(session_id, None)
                            await queue.put(
                                self._sse(
                                    {
                                        "type": "thinking",
                                        "content": "原会话已失效，重建中...",
                                    }
                                )
                            )
                        except Exception as exc:
                            logger.error("resume 模式执行异常: %s", exc, exc_info=True)
                            had_error["v"] = True
                            await queue.put(
                                self._sse(
                                    {"type": "error", "message": f"执行失败: {exc}"}
                                )
                            )
                            return
                    async for ev in _run_once(None):
                        await queue.put(ev)
                except asyncio.CancelledError:
                    raise
                except _AgentAuthenticationError as e:
                    logger.error("Claude Agent SDK 认证失败: %s", e)
                    had_error["v"] = True
                    await queue.put(self._sse({"type": "error", "message": str(e)}))
                except Exception as e:
                    logger.error(f"技能自动执行失败: {e}", exc_info=True)
                    had_error["v"] = True
                    await queue.put(
                        self._sse({"type": "error", "message": f"执行失败: {e}"})
                    )
                finally:
                    await queue.put(_SENTINEL)

            async def heartbeat() -> None:
                try:
                    while True:
                        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
                        await queue.put(_HEARTBEAT_FRAME)
                except asyncio.CancelledError:
                    pass

            prod_task = asyncio.create_task(producer())
            hb_task = asyncio.create_task(heartbeat())

            try:
                while True:
                    ev = await queue.get()
                    if ev is _SENTINEL:
                        break
                    yield ev
                if not had_error["v"]:
                    yield self._sse(
                        {"type": "workflow_complete", "data": {"references": []}}
                    )
                logger.info("◀ execute_stream 完成")
            finally:
                hb_task.cancel()
                if not prod_task.done():
                    prod_task.cancel()
                for t in (hb_task, prod_task):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
        finally:
            if session_id:
                self._busy_sessions.discard(session_id)

        yield "data: [DONE]\n\n"

    def _build_prompt(
        self,
        *,
        cwd: Path,
        latest_user_text: str,
        conversation_history: str,
        context: Optional[str],
        resume: bool = False,
    ) -> str:
        context_section = ""
        if context and context.strip():
            bounded_context = context.strip()[:4000]
            context_section = (
                f"\n\n---\n\n## 上一步执行结果（供参考）\n\n{bounded_context}\n"
            )

        if resume:
            return f"## 用户最新请求\n\n{latest_user_text}{context_section}"

        skills_root_abs = Path(self.skills_dir).resolve()
        workspace_section = (
            f"**当前会话工作区（你的 cwd）**: `{cwd}`\n"
            f"  - 用户上传文件位于: `{cwd / 'uploads'}`\n"
            f"  - 你生成的所有产物请写入: `{cwd / 'outputs'}`\n"
            f"**技能库根目录（绝对路径，仅可读取）**: `{skills_root_abs}`\n"
        )
        history_section = ""
        if conversation_history:
            history_section = f"\n\n## 对话历史\n\n{conversation_history}\n\n---\n\n"
        skills_section = (
            "## 技能使用\n\n"
            "可用技能已由 Claude Code 原生 Skills 机制注册。根据请求按需调用 Skill；"
            "技能正文会在命中后加载，无需先读取或枚举 SKILL.md。\n\n---\n\n"
        )
        return (
            f"{workspace_section}\n"
            f"{skills_section}"
            f"## 执行说明\n\n"
            f"- 所有写文件操作请落在 `{cwd / 'outputs'}` 目录下；不要写到工作区之外。\n"
            f"- 用户上传文件已在消息中提供绝对路径，可直接 Read。\n\n"
            f"---\n"
            f"{history_section}"
            f"## 用户最新请求\n\n{latest_user_text}"
            f"{context_section}"
        )

    @staticmethod
    def _build_agent_env(session_id: Optional[str]) -> dict[str, str]:
        from app.core.config import settings

        env = {
            **settings.claude_agent_env,
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        }
        if session_id:
            env["EIDO_SESSION_ID"] = session_id
            # CLI 会将其解析为 Anthropic 请求体的 metadata 字段，
            # 供 litellm 等网关读取并上报 langfuse 做会话追踪
            env["CLAUDE_CODE_EXTRA_METADATA"] = json.dumps(
                {"session_id": session_id}, ensure_ascii=False
            )
        return env

    @staticmethod
    def _agent_auth_error(agent_env: dict[str, str]) -> Optional[str]:
        keys = (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_ANTHROPIC_AWS",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        )
        if any(agent_env.get(key) for key in keys):
            return None
        return AgentService._agent_auth_failure_message()

    @staticmethod
    def _agent_auth_failure_message() -> str:
        return (
            "Claude Agent SDK 未配置可用的非交互式凭据。请配置 ANTHROPIC_API_KEY，"
            "或配置受支持的 Anthropic 兼容网关/云平台凭据，然后重启服务。"
        )

    @staticmethod
    def _agent_auth_summary(agent_env: dict[str, str]) -> tuple[str, str]:
        if agent_env.get("ANTHROPIC_API_KEY"):
            auth_mode = "api_key"
        elif agent_env.get("ANTHROPIC_AUTH_TOKEN"):
            auth_mode = "auth_token"
        else:
            auth_mode = next(
                (
                    key.removeprefix("CLAUDE_CODE_USE_").lower()
                    for key in (
                        "CLAUDE_CODE_USE_BEDROCK",
                        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
                        "CLAUDE_CODE_USE_VERTEX",
                        "CLAUDE_CODE_USE_FOUNDRY",
                    )
                    if agent_env.get(key)
                ),
                "missing",
            )
        base_url = agent_env.get("ANTHROPIC_BASE_URL", "").strip()
        provider = urlsplit(base_url).hostname if base_url else "api.anthropic.com"
        return auth_mode, provider or "custom"

    @staticmethod
    def _is_not_logged_in_message(message: object) -> bool:
        if not isinstance(message, AssistantMessage):
            return False
        return any(
            isinstance(block, TextBlock)
            and "not logged in" in block.text.lower()
            and "/login" in block.text.lower()
            for block in message.content
        )

    def _log_message(self, message: object) -> None:
        """Write complete SDK messages; no preview/truncation is used in logs."""
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    _log_complete_value("Assistant/Text", block.text)
                elif isinstance(block, ThinkingBlock):
                    _log_complete_value("Assistant/Thinking", block.thinking)
                elif isinstance(block, ToolUseBlock):
                    logger.info("  [Claude/Tool/Call] %s", block.name)
                    _log_complete_value(f"Tool/Input:{block.name}", block.input)

        elif isinstance(message, UserMessage):
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        raw = block.content
                        content_str = raw if isinstance(raw, str) else str(raw or "")
                        status = "ERROR" if block.is_error else "OK"
                        _log_complete_value(
                            f"Tool/Result:{status}",
                            content_str,
                            logging.ERROR if block.is_error else logging.INFO,
                        )

        elif isinstance(message, SystemMessage):
            _log_complete_value(f"System/{message.subtype}", message.data)

        elif isinstance(message, ResultMessage):
            cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "N/A"
            duration = f"{message.duration_ms / 1000:.1f}s"
            status = "ERROR" if message.is_error else "OK"
            usage = getattr(message, "usage", None) or {}
            logger.info(
                f"  [Result/{status}] 用时={duration} | 费用={cost} | "
                f"轮次={message.num_turns} | session={message.session_id} | "
                f"terminal={getattr(message, 'terminal_reason', None) or '-'} | "
                f"api_status={getattr(message, 'api_error_status', None) or '-'} | "
                f"input={usage.get('input_tokens', 0)} | "
                f"cache_read={usage.get('cache_read_input_tokens', 0)} | "
                f"cache_create={usage.get('cache_creation_input_tokens', 0)} | "
                f"output={usage.get('output_tokens', 0)}"
            )

    def _convert_message(self, message: object) -> List[str]:
        events: List[str] = []

        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    if block.text:
                        events.append(
                            self._sse({"type": "content", "content": block.text})
                        )
                elif isinstance(block, ThinkingBlock):
                    preview = block.thinking[:300].strip()
                    if preview:
                        events.append(
                            self._sse(
                                {
                                    "type": "thinking",
                                    "content": f"[深度思考] {preview}{'…' if len(block.thinking) > 300 else ''}",
                                }
                            )
                        )
                elif isinstance(block, ToolUseBlock):
                    hint = self._tool_hint(block.name, block.input)
                    events.append(self._sse({"type": "thinking", "content": hint}))

        elif isinstance(message, UserMessage):
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        raw = block.content
                        content_str = (
                            raw
                            if isinstance(raw, str)
                            else (str(raw) if raw is not None else "")
                        )
                        preview = content_str[:200].strip()
                        status = "✗ 工具出错" if block.is_error else "✓ 工具完成"
                        hint = f"{status}: {preview}" if preview else status
                        events.append(self._sse({"type": "thinking", "content": hint}))

        elif isinstance(message, SystemMessage):
            if message.subtype == "init":
                tools = message.data.get("tools", [])
                if tools:
                    tool_list = ", ".join(tools[:6]) + ("…" if len(tools) > 6 else "")
                    events.append(
                        self._sse(
                            {"type": "thinking", "content": f"已加载工具: {tool_list}"}
                        )
                    )

        elif isinstance(message, ResultMessage):
            cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "N/A"
            duration = f"{message.duration_ms / 1000:.1f}s"
            events.append(
                self._sse(
                    {
                        "type": "thinking",
                        "content": (
                            f"执行完成 | 用时: {duration} | "
                            f"费用: {cost} | 轮次: {message.num_turns}"
                            + (" | ⚠️ 出错" if message.is_error else "")
                        ),
                    }
                )
            )

        return events

    def _tool_hint(self, tool_name: str, tool_input: dict) -> str:
        hints = {
            "Read": lambda i: f"读取文件: {i.get('file_path', '')}",
            "Bash": lambda i: f"执行命令: {str(i.get('command', ''))[:120]}",
            "Glob": lambda i: f"查找文件: {i.get('pattern', '')}",
            "WebFetch": lambda i: f"获取网页: {i.get('url', '')}",
            "WebSearch": lambda i: f"搜索: {i.get('query', '')}",
            "Write": lambda i: f"写入文件: {i.get('file_path', '')}",
            "Edit": lambda i: f"编辑文件: {i.get('file_path', '')}",
            "Grep": lambda i: f"搜索内容: {i.get('pattern', '')}",
            "MultiEdit": lambda i: f"批量编辑: {i.get('file_path', '')}",
            "Skill": lambda i: f"调用技能: {i.get('skill', i.get('name', ''))}",
        }
        fn = hints.get(tool_name)
        if fn:
            try:
                return fn(tool_input)
            except Exception:
                pass
        return f"正在调用工具: {tool_name}..."

    def _sse(self, data: dict) -> str:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


_instance: Optional[AgentService] = None


def get_agent_service() -> Optional[AgentService]:
    return _instance


def init_agent_service(
    skills_dir: Path, workspace_root: Path
) -> AgentService:
    global _instance
    _instance = AgentService(skills_dir, workspace_root)
    logger.info(f"AgentService 初始化完成 - 技能目录: {skills_dir}")
    return _instance
