"""Claude Agent SDK 执行服务：每会话常驻 ClaudeSDKClient + 原生多轮上下文 + SSE 流式输出。"""

import asyncio
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
    for msg in reversed(messages or []):
        if isinstance(msg, dict):
            role, content = msg.get("role"), msg.get("content")
        else:
            role = getattr(msg, "role", None)
            content = getattr(msg, "content", None)
        if role == "user":
            return (content or "").strip()
    return ""


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
        "Edit",
        "WebFetch",
        "WebSearch",
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
                logger.info("回收空闲常驻客户端: session=%s", key)
                await self._drop_client(session)

    async def execute_stream(
        self,
        messages: list,
        context: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        logger.info(
            "▶ execute_stream 开始 | 消息数: %d | session=%s | 上下文字符数: %d",
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

            prompt = self._build_prompt(latest_user_text=latest_user_text, context=context)
            if session is None:
                session = _Session()
                if session_id:
                    self._sessions[session_id] = session
            events = self._stream_turn(session, session_id, cwd, prompt)
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
        finally:
            logger.info("◀ execute_stream 结束")

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
        self, cwd: Path, session_id: Optional[str], sid: Optional[str]
    ) -> ClaudeAgentOptions:
        agent_env = self._build_agent_env(session_id)
        auth_key = next((key for key in _AUTH_ENV_KEYS if agent_env.get(key)), None)
        if not auth_key:
            raise _AgentRequestError(_AUTH_FAILURE)

        base_url = agent_env.get("ANTHROPIC_BASE_URL")
        provider = urlsplit(base_url).hostname if base_url else "api.anthropic.com"
        logger.info("  [ClaudeAuth] mode=%s provider=%s", auth_key, provider or "custom")
        try:
            mcp_config = load_mcp_config(settings.MCP_CONFIG_PATH, environment=agent_env)
        except (ValueError, OSError) as exc:
            raise _AgentRequestError(f"MCP 配置错误: {exc}") from exc
        logger.info(
            "  [ClaudeMCP] path=%s servers=%s",
            mcp_config.path,
            ",".join(mcp_config.server_names) or "(none)",
        )
        return ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code"},
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
        self, session: _Session, session_id: Optional[str], cwd: Path, prompt: str
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
                options = self._build_agent_options(cwd, session_id, session.sid)
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
    def _build_prompt(*, latest_user_text: str, context: Optional[str]) -> str:
        """原生会话承载多轮历史，prompt 只附加调用方传入的流水线上下文。"""
        if context and context.strip():
            return f"{latest_user_text}\n\n---\n\n## 上一步执行结果（供参考）\n\n{context.strip()}"
        return latest_user_text

    @staticmethod
    def _build_agent_env(session_id: Optional[str]) -> dict[str, str]:
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
