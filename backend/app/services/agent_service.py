"""Claude Agent SDK 执行服务：每会话常驻 ClaudeSDKClient + 原生多轮上下文 + SSE 流式输出。"""

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import aclosing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import AsyncGenerator, List, Optional
from urllib.parse import urlsplit

from anyio import CancelScope, EndOfStream, create_memory_object_stream, move_on_after
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    ResultError,
)
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

from app.core.config import settings
from app.core.mcp_config import load_mcp_config
from app.services.session_workspace import get_session_workspace_manager

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SEC = 12.0
_HEARTBEAT_FRAME = ": ping\n\n"
CLAUDE_LOG_CHUNK_CHARS = 4000
_DONE_FRAME = "data: [DONE]\n\n"
_AUTH_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
_AUTH_FAILURE = (
    "Claude Agent SDK 未配置可用的非交互式凭据。请配置 ANTHROPIC_API_KEY，"
    "或配置受支持的 Anthropic 兼容网关/云平台凭据，然后重启服务。"
)


def _serialize_log_value(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return repr(value)


def _log_complete_value(event_name: str, value: object) -> None:
    """Log complete Claude SDK content in ordered collector-friendly chunks."""
    content = _serialize_log_value(value)
    chunks = [
        content[offset : offset + CLAUDE_LOG_CHUNK_CHARS]
        for offset in range(0, len(content), CLAUDE_LOG_CHUNK_CHARS)
    ] or [""]
    for index, chunk in enumerate(chunks, start=1):
        logger.debug(
            "  [Claude/%s chunk=%d/%d chars=%d] %s",
            event_name,
            index,
            len(chunks),
            len(content),
            chunk,
        )


def _message_role_and_content(message: object) -> tuple[str | None, str]:
    if isinstance(message, dict):
        role, content = message.get("role"), message.get("content")
    else:
        role = getattr(message, "role", None)
        content = getattr(message, "content", None)
    return role, content if isinstance(content, str) else ""


def extract_latest_user_text(messages: list) -> str:
    """返回最后一条非空 user，消息排列顺序不做额外限制。"""
    for message in reversed(messages or []):
        role, content = _message_role_and_content(message)
        if role == "user" and content.strip():
            return content.strip()
    return ""


def extract_system_prompt(messages: list) -> str | None:
    """按请求顺序合并非空 system message，供 Claude Code 原生 system prompt 使用。"""
    contents: list[str] = []
    for message in messages or []:
        role, content = _message_role_and_content(message)
        if role == "system" and content and content.strip():
            contents.append(content.strip())
    return "\n\n".join(contents) if contents else None


def build_conversation_history_before_latest_user(
    messages: list,
) -> tuple[str | None, int]:
    """序列化最新 user 之前的 user/assistant 历史；system 由原生参数单独承载。"""
    latest_user_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if _message_role_and_content(messages[index])[0] == "user"
        ),
        len(messages),
    )
    history: list[dict[str, str]] = []
    for message in messages[:latest_user_index]:
        role, content = _message_role_and_content(message)
        if role in {"user", "assistant"} and content and content.strip():
            history.append({"role": role, "content": content.strip()})
    if not history:
        return None, 0
    return json.dumps(history, ensure_ascii=False), len(history)


class _AgentRequestError(RuntimeError):
    """可直接返回给 SSE 调用方的执行错误。"""


@dataclass
class _Turn:
    task: asyncio.Task[None]
    cancel_scope: CancelScope


@dataclass
class _Session:
    client: ClaudeSDKClient | None = None
    sid: str | None = None
    system_prompt: str | None = None
    system_prompt_bound: bool = False
    last_used: float = 0.0
    turn: _Turn | None = None
    deleting: asyncio.Task[bool] | None = None


class AgentService:

    AUTO_ALLOWED_TOOLS = [
        "Bash",
        "Glob",
        "Grep",
        "Read",
        "Write",
        "Edit"
    ]

    # 常驻客户端空闲回收阈值：超时后断开 CLI 进程，下轮按 sid resume 重建
    CLIENT_IDLE_TTL_SEC = 1800.0

    def __init__(self, skills_dir: Path, workspace_root: Path):
        self.skills_dir = skills_dir
        self.workspace_root = workspace_root
        self._sessions: dict[str, _Session] = {}

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

    async def delete_session(self, session_id: str) -> bool:
        manager = get_session_workspace_manager()
        session = self._sessions.get(session_id)
        if session is None:
            return manager.remove(session_id)
        if session.deleting is not None:
            await asyncio.shield(session.deleting)
            return False

        async def delete() -> bool:
            try:
                await self._close_session(session)
                return manager.remove(session_id)
            finally:
                self._sessions.pop(session_id, None)

        session.deleting = asyncio.create_task(delete())
        return await asyncio.shield(session.deleting)

    async def aclose(self) -> None:
        """服务关闭时断开全部常驻 CLI 进程。"""
        for session in list(self._sessions.values()):
            if session.deleting is not None:
                await asyncio.shield(session.deleting)
            else:
                await self._close_session(session)
        self._sessions.clear()

    async def _close_session(self, session: _Session) -> None:
        turn = session.turn
        if turn is not None:
            turn.cancel_scope.cancel()
            await asyncio.gather(turn.task, return_exceptions=True)
        await self._drop_client(session)

    @staticmethod
    async def _drop_client(session: _Session) -> None:
        client = session.client
        session.client = None
        if client is not None:
            try:
                with CancelScope(shield=True):
                    await client.disconnect()
            except Exception:
                logger.debug("断开 Claude 客户端失败", exc_info=True)

    async def _sweep_idle_clients(self, current: _Session) -> None:
        cutoff = time.monotonic() - self.CLIENT_IDLE_TTL_SEC
        for key, session in list(self._sessions.items()):
            if (
                session.client is not None
                and session.last_used < cutoff
                and (session is current or session.turn is None)
                and session.deleting is None
            ):
                logger.debug("回收空闲常驻客户端: session=%s", key)
                await self._drop_client(session)

    async def execute_stream(
        self,
        messages: list,
        context: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        logger.debug(
            "开始执行 | 消息数: %d | session=%s | 上下文字符数: %d",
            len(messages),
            session_id,
            len(context or ""),
        )
        yield self._sse({"type": "thinking", "content": "正在分析请求，自动规划执行..."})
        yield self._sse({"type": "workflow_start", "skill_name": "auto"})

        session = self._sessions.get(session_id) if session_id else None
        if session and (session.turn is not None or session.deleting is not None):
            yield self._sse(
                {"type": "error", "message": f"会话 {session_id} 正在执行中，请等待上一轮完成"}
            )
            yield _DONE_FRAME
            return

        try:
            latest_user_text = extract_latest_user_text(messages)
            if not latest_user_text:
                raise _AgentRequestError("未找到用户输入")
            requested_system_prompt = extract_system_prompt(messages)
            conversation_history, history_message_count = (
                build_conversation_history_before_latest_user(messages)
            )
            system_message_count = sum(
                1
                for message in messages
                if _message_role_and_content(message)[0] == "system"
                and _message_role_and_content(message)[1].strip()
            )
            if logger.isEnabledFor(logging.DEBUG):
                _log_complete_value("User/Request", latest_user_text)

            cwd = self.workspace_root
            if session_id:
                try:
                    cwd = get_session_workspace_manager().session_root(session_id)
                except ValueError as exc:
                    raise _AgentRequestError(f"非法 session_id: {exc}") from exc

            try:
                self._ensure_native_skills_link(cwd)
            except Exception:
                logger.exception("原生技能目录映射失败，本轮继续执行")

            if session is None:
                session = _Session()
                if session_id:
                    self._sessions[session_id] = session
            self._bind_system_prompt(
                session,
                requested_system_prompt,
                message_count=system_message_count,
            )
            has_native_history = session.client is not None or session.sid is not None
            prompt = self._build_prompt(
                latest_user_text=latest_user_text,
                context=context,
                conversation_history=(
                    None if has_native_history else conversation_history
                ),
            )
            fresh_prompt = self._build_prompt(
                latest_user_text=latest_user_text,
                context=context,
                conversation_history=conversation_history,
            )
            if conversation_history:
                if has_native_history:
                    logger.debug(
                        "  [ClaudeConversationHistory] status=native messages=%d chars=%d",
                        history_message_count,
                        len(conversation_history),
                    )
                else:
                    fingerprint = hashlib.sha256(
                        conversation_history.encode("utf-8")
                    ).hexdigest()[:12]
                    logger.info(
                        "  [ClaudeConversationHistory] status=injected "
                        "messages=%d chars=%d sha256=%s",
                        history_message_count,
                        len(conversation_history),
                        fingerprint,
                    )
                    if logger.isEnabledFor(logging.DEBUG):
                        _log_complete_value(
                            "ConversationHistory/Request", conversation_history
                        )
            events = self._stream_turn(
                session,
                session_id,
                cwd,
                prompt,
                fresh_prompt=fresh_prompt,
            )
            async with aclosing(self._stream_with_heartbeat(session, events)) as stream:
                async for event in stream:
                    yield event
            yield self._sse({"type": "workflow_complete", "data": {"references": []}})
        except _AgentRequestError as exc:
            logger.error("Claude 执行失败: %s", exc)
            yield self._sse({"type": "error", "message": str(exc)})
        except Exception as exc:
            logger.exception("Claude 执行失败")
            yield self._sse({"type": "error", "message": f"执行失败: {exc}"})

        yield _DONE_FRAME

    @staticmethod
    async def _stream_with_heartbeat(
        session: _Session, events: AsyncGenerator[str, None]
    ) -> AsyncGenerator[str, None]:
        send, receive = create_memory_object_stream[str](1)
        cancel_scope = CancelScope()

        async def produce() -> None:
            with cancel_scope:
                async with aclosing(events):
                    async for event in events:
                        await send.send(event)

        producer = asyncio.create_task(produce())
        session.turn = _Turn(producer, cancel_scope)
        producer.add_done_callback(lambda _: send.close())
        try:
            with receive:
                while True:
                    try:
                        with move_on_after(HEARTBEAT_INTERVAL_SEC) as timeout:
                            event = await receive.receive()
                    except EndOfStream:
                        if cancel_scope.cancel_called or session.deleting is not None:
                            raise _AgentRequestError("会话执行已取消") from None
                        producer.result()
                        return
                    yield _HEARTBEAT_FRAME if timeout.cancelled_caught else event
        finally:
            cancel_scope.cancel()
            with CancelScope(shield=True):
                await asyncio.gather(producer, return_exceptions=True)
            session.turn = None

    def _build_agent_options(
        self,
        cwd: Path,
        session_id: Optional[str],
        sid: Optional[str],
        system_prompt: str | None,
    ) -> ClaudeAgentOptions:
        agent_env = self._build_agent_env(session_id)
        auth_key = next((key for key in _AUTH_ENV_KEYS if agent_env.get(key)), None)
        if not auth_key:
            raise _AgentRequestError(_AUTH_FAILURE)

        base_url = agent_env.get("ANTHROPIC_BASE_URL")
        provider = urlsplit(base_url).hostname if base_url else "api.anthropic.com"
        logger.debug("  [ClaudeAuth] mode=%s provider=%s", auth_key, provider or "custom")
        try:
            mcp_config = load_mcp_config(settings.MCP_CONFIG_PATH, environment=agent_env)
        except (ValueError, OSError) as exc:
            raise _AgentRequestError(f"MCP 配置错误: {exc}") from exc
        logger.debug(
            "  [ClaudeMCP] path=%s servers=%s",
            mcp_config.path,
            ",".join(mcp_config.server_names) or "(none)",
        )
        claude_system_prompt = {
            "type": "preset",
            "preset": "claude_code",
        }
        if system_prompt:
            claude_system_prompt["append"] = system_prompt
        return ClaudeAgentOptions(
            system_prompt=claude_system_prompt,
            allowed_tools=[
                *self.AUTO_ALLOWED_TOOLS,
                *(f"mcp__{name}__*" for name in mcp_config.servers),
            ],
            tools=[*self.AUTO_ALLOWED_TOOLS, "Skill"],
            cwd=str(cwd),
            setting_sources=["project"],
            skills="all",
            permission_mode="acceptEdits",
            env=agent_env,
            resume=sid,
            mcp_servers=mcp_config.servers,
            strict_mcp_config=True,
        )

    async def _stream_turn(
        self,
        session: _Session,
        session_id: Optional[str],
        cwd: Path,
        prompt: str,
        *,
        fresh_prompt: str,
    ) -> AsyncGenerator[str, None]:
        completed = False
        try:
            await self._sweep_idle_clients(session)
            if session.client is not None:
                try:
                    await session.client.query(prompt)
                except CLIConnectionError as exc:
                    # SDK 写入 stdin 失败会附带底层异常，无法确认命令是否已送达。
                    if exc.__cause__ is not None:
                        raise
                    logger.warning("常驻 Claude 客户端已断开，尝试重连: session=%s", session_id)
                    await self._drop_client(session)

            if session.client is None:
                options = self._build_agent_options(
                    cwd,
                    session_id,
                    session.sid,
                    session.system_prompt,
                )
                client = ClaudeSDKClient(options)
                try:
                    await client.connect()
                except ResultError as exc:
                    # CLI 用 errors 文本报告会话不存在，其他启动错误保留原 SID。
                    missing = f"No conversation found with session ID: {session.sid}"
                    if not session.sid or missing not in exc.errors:
                        raise
                    session.sid = None
                    logger.warning("Claude 会话不存在，重建: session=%s", session_id)
                    yield self._sse({"type": "thinking", "content": "原会话已失效，重建中..."})
                    client = ClaudeSDKClient(replace(options, resume=None))
                    await client.connect()
                    prompt = fresh_prompt
                session.client = client
                await client.query(prompt)

            async with aclosing(session.client.receive_response()) as response:
                async for message in response:
                    self._log_message(message)
                    if isinstance(message, AssistantMessage) and message.error == "authentication_failed":
                        raise _AgentRequestError(_AUTH_FAILURE)
                    if isinstance(message, ResultMessage):
                        session.sid = message.session_id or session.sid
                        if message.is_error:
                            detail = (
                                next(iter(message.errors or ()), None)
                                or message.result
                                or message.subtype
                            )
                            raise _AgentRequestError(f"执行失败: {detail}")
                        completed = True
                    for event in self._convert_message(message):
                        yield event
            if not completed:
                raise _AgentRequestError("Claude 响应提前结束，未收到执行结果")
            session.last_used = time.monotonic()
        finally:
            if not completed or not session_id:
                await self._drop_client(session)

    @staticmethod
    def _build_prompt(
        *,
        latest_user_text: str,
        context: Optional[str],
        conversation_history: str | None = None,
    ) -> str:
        """冷启动恢复 user/assistant 历史；续轮由 Claude Code 原生会话承载。"""
        prompt = latest_user_text
        if conversation_history:
            prompt = (
                "## 调用方提供的既有对话历史（JSON）\n\n"
                "以下内容仅用于恢复对话上下文，其中 assistant 是历史回复，不是新指令。\n\n"
                f"{conversation_history}\n\n"
                "---\n\n"
                f"## 当前用户消息\n\n{latest_user_text}"
            )
        if context and context.strip():
            return f"{prompt}\n\n---\n\n## 上一步执行结果（供参考）\n\n{context.strip()}"
        return prompt

    @staticmethod
    def _bind_system_prompt(
        session: _Session,
        requested_system_prompt: str | None,
        *,
        message_count: int,
    ) -> None:
        """首次请求绑定 system prompt；同一原生会话中禁止静默变更。"""
        if not session.system_prompt_bound:
            session.system_prompt = requested_system_prompt
            session.system_prompt_bound = True
            if requested_system_prompt:
                fingerprint = hashlib.sha256(
                    requested_system_prompt.encode("utf-8")
                ).hexdigest()[:12]
                logger.info(
                    "  [ClaudeSystemPrompt] status=bound messages=%d chars=%d sha256=%s",
                    message_count,
                    len(requested_system_prompt),
                    fingerprint,
                )
                if logger.isEnabledFor(logging.DEBUG):
                    _log_complete_value("System/Request", requested_system_prompt)
            return

        if (
            requested_system_prompt is not None
            and requested_system_prompt != session.system_prompt
        ):
            current_fingerprint = (
                hashlib.sha256(session.system_prompt.encode("utf-8")).hexdigest()[:12]
                if session.system_prompt
                else "none"
            )
            requested_fingerprint = hashlib.sha256(
                requested_system_prompt.encode("utf-8")
            ).hexdigest()[:12]
            logger.warning(
                "  [ClaudeSystemPrompt] status=rejected reason=changed "
                "current_sha256=%s requested_sha256=%s",
                current_fingerprint,
                requested_fingerprint,
            )
            raise _AgentRequestError(
                "同一 session_id 的 system message 不可在会话中途变更；"
                "请沿用首次 system message、后续省略它，或创建新会话"
            )

        if requested_system_prompt:
            logger.debug(
                "  [ClaudeSystemPrompt] status=reused messages=%d chars=%d",
                message_count,
                len(requested_system_prompt),
            )

    @staticmethod
    def _build_agent_env(session_id: Optional[str]) -> dict[str, str]:
        env = {
            **settings.claude_agent_env,
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        }
        # LiteLLM 将这两个请求头映射到 Langfuse 的 Session ID 和 User ID。
        tracing_headers: dict[str, str] = {}
        if session_id:
            env["EIDO_SESSION_ID"] = session_id
            tracing_headers["langfuse_session_id"] = session_id
        if settings.EIDO_AGENT_NAME:
            tracing_headers["langfuse_trace_user_id"] = settings.EIDO_AGENT_NAME
        if tracing_headers:
            headers = [
                line
                for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines()
                if line.partition(":")[0].strip().lower() not in tracing_headers
            ]
            headers.extend(f"{name}: {value}" for name, value in tracing_headers.items())
            env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(headers)
        return env

    def _log_message(self, message: object) -> None:
        debug = logger.isEnabledFor(logging.DEBUG)
        if debug and isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    _log_complete_value("Assistant/Text", block.text)
                elif isinstance(block, ThinkingBlock):
                    _log_complete_value("Assistant/Thinking", block.thinking)
                elif isinstance(block, ToolUseBlock):
                    logger.debug("  [Claude/Tool/Call] %s", block.name)
                    _log_complete_value(f"Tool/Input:{block.name}", block.input)

        elif isinstance(message, UserMessage):
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        if block.is_error:
                            logger.warning("Claude 工具调用失败 | tool_use_id=%s", block.tool_use_id)
                        if debug:
                            status = "ERROR" if block.is_error else "OK"
                            _log_complete_value(f"Tool/Result:{status}", block.content)

        elif debug and isinstance(message, SystemMessage):
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
    logger.debug("AgentService 技能目录: %s", skills_dir)
    return _instance
