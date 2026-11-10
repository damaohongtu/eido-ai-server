"""Claude Agent SDK 执行服务：原生 resume 多轮续接 + 原生 Skills + SSE 流式输出。"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, List, Optional
from urllib.parse import urlsplit

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ClaudeSDKError
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


class _ResumeUnavailable(RuntimeError):
    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause


class _ClientDied(RuntimeError):
    """常驻客户端本轮尚未产出任何消息即失败：可安全丢弃并重建后重试。"""

    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause


class _AgentAuthenticationError(RuntimeError):
    pass


@dataclass
class _SessionClient:
    client: ClaudeSDKClient
    last_used: float = field(default_factory=time.monotonic)


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
        self._claude_session_ids: dict[str, str] = {}
        self._busy_sessions: set[str] = set()
        self._clients: dict[str, _SessionClient] = {}

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

    async def reset_session(self, session_id: str) -> None:
        self._claude_session_ids.pop(session_id, None)
        self._busy_sessions.discard(session_id)
        await self._drop_client(session_id)

    async def aclose(self) -> None:
        """服务关闭时断开全部常驻 CLI 进程。"""
        for key in list(self._clients):
            await self._drop_client(key)

    async def _drop_client(self, session_key: Optional[str]) -> None:
        if not session_key:
            return
        entry = self._clients.pop(session_key, None)
        if entry is None:
            return
        try:
            await entry.client.disconnect()
        except Exception:
            logger.debug("断开常驻客户端失败: session=%s", session_key, exc_info=True)

    async def _sweep_idle_clients(self) -> None:
        now = time.monotonic()
        stale = [
            key
            for key, entry in self._clients.items()
            if now - entry.last_used > self.CLIENT_IDLE_TTL_SEC
        ]
        for key in stale:
            logger.info(
                "回收空闲常驻客户端: session=%s idle_sec=%.0f",
                key,
                now - self._clients[key].last_used,
            )
            await self._drop_client(key)

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

        await self._sweep_idle_clients()

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

            queue: asyncio.Queue = asyncio.Queue()
            _SENTINEL = object()
            had_error = {"v": False}

            async def producer() -> None:
                try:
                    async for ev in self._turn_with_recovery(
                        session_key=session_id,
                        prompt=self._build_prompt(
                            latest_user_text=latest_user_text,
                            context=context,
                        ),
                        cwd=cwd,
                        agent_env=agent_env,
                        mcp_config=mcp_config,
                    ):
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

    def _build_agent_options(
        self,
        *,
        cwd: Path,
        agent_env: dict[str, str],
        mcp_servers: dict,
        resume_sid: Optional[str],
    ) -> ClaudeAgentOptions:
        available_tools = list(self.AUTO_ALLOWED_TOOLS)
        available_tools.append("Skill")
        allowed_tools = list(available_tools)
        allowed_tools.extend(f"mcp__{name}__*" for name in mcp_servers)
        return ClaudeAgentOptions(
            allowed_tools=allowed_tools,
            tools=available_tools,
            cwd=str(cwd),
            setting_sources=["project"],
            skills="all",
            permission_mode="acceptEdits",
            env=agent_env,
            resume=resume_sid,
            mcp_servers=mcp_servers,
            strict_mcp_config=True,
        )

    async def _connect_entry(
        self,
        session_key: Optional[str],
        *,
        cwd: Path,
        agent_env: dict[str, str],
        mcp_config: Any,
        resume_sid: Optional[str],
    ) -> _SessionClient:
        """新建常驻客户端并启动 CLI 进程；resume 失败抛 _ResumeUnavailable。

        带 session_key 时登记到 self._clients 供后续轮次复用；匿名请求不登记，
        由调用方在本轮结束后断开。注意：客户端的 env/MCP 配置在创建时固化，
        配置变更需等待会话重建或服务重启才生效。
        """
        options = self._build_agent_options(
            cwd=cwd,
            agent_env=agent_env,
            mcp_servers=mcp_config.servers,
            resume_sid=resume_sid,
        )
        client = ClaudeSDKClient(options)
        started = time.perf_counter()
        try:
            await client.connect()
        except ClaudeSDKError as exc:
            if resume_sid:
                raise _ResumeUnavailable(exc) from exc
            raise
        logger.info(
            "  [ClaudeClient] connected mode=%s connect_ms=%.0f tools=%d mcp=%d cwd=%s",
            "resume" if resume_sid else "fresh",
            (time.perf_counter() - started) * 1000,
            len(options.tools or []),
            len(mcp_config.servers),
            cwd,
        )
        entry = _SessionClient(client=client)
        if session_key:
            self._clients[session_key] = entry
        return entry

    async def _run_turn(
        self,
        client: ClaudeSDKClient,
        *,
        prompt: str,
        session_key: Optional[str],
        mode: str,
    ) -> AsyncGenerator[str, None]:
        """在常驻客户端上执行一轮对话，直到 ResultMessage。

        常驻模式下 receive_messages() 不会自行结束，必须以 ResultMessage 为界。
        """
        message_count = 0
        saw_result = False
        run_started = time.perf_counter()
        try:
            await client.query(prompt)
            async for message in client.receive_messages():
                message_count += 1
                self._log_message(message)
                if self._is_not_logged_in_message(message):
                    raise _AgentAuthenticationError(
                        self._agent_auth_failure_message()
                    )
                if isinstance(message, ResultMessage):
                    saw_result = True
                    sid = getattr(message, "session_id", None)
                    if session_key and sid:
                        self._claude_session_ids[session_key] = sid
                for event in self._convert_message(message):
                    yield event
                if saw_result:
                    break
        except ClaudeSDKError as exc:
            if message_count == 0:
                raise _ClientDied(exc) from exc
            raise
        finally:
            logger.info(
                "  [ClaudeRun/Summary] mode=%s status=%s elapsed_ms=%.1f "
                "messages=%d",
                mode,
                "ok" if saw_result else "incomplete",
                (time.perf_counter() - run_started) * 1000,
                message_count,
            )

    async def _turn_with_recovery(
        self,
        *,
        session_key: Optional[str],
        prompt: str,
        cwd: Path,
        agent_env: dict[str, str],
        mcp_config: Any,
    ) -> AsyncGenerator[str, None]:
        """执行一轮：复用常驻客户端，失败时自动降级重试。

        重试链：复用常驻客户端 → 带 sid resume 重建（上下文保留）→
        全新重建（sid 已清）。匿名请求不持久化，本轮结束即断开。
        """
        entry = self._clients.get(session_key) if session_key else None
        if entry is not None:
            entry.last_used = time.monotonic()
        try:
            for attempt in range(1, 4):
                mode = "reuse" if entry is not None else None
                if entry is None:
                    resume_sid = (
                        self._claude_session_ids.get(session_key)
                        if session_key
                        else None
                    )
                    try:
                        entry = await self._connect_entry(
                            session_key,
                            cwd=cwd,
                            agent_env=agent_env,
                            mcp_config=mcp_config,
                            resume_sid=resume_sid,
                        )
                        mode = "resume" if resume_sid else "fresh"
                    except _ResumeUnavailable as exc:
                        logger.warning(
                            "resume(%s) 连接失败，清 sid 并回退到重建模式: %s",
                            resume_sid,
                            exc.cause,
                        )
                        if session_key:
                            self._claude_session_ids.pop(session_key, None)
                        yield self._sse(
                            {"type": "thinking", "content": "原会话已失效，重建中..."}
                        )
                        continue
                try:
                    async for ev in self._run_turn(
                        entry.client,
                        prompt=prompt,
                        session_key=session_key,
                        mode=mode or "fresh",
                    ):
                        yield ev
                    return
                except _ClientDied as exc:
                    await self._drop_client(session_key)
                    logger.warning(
                        "常驻客户端第 %d 次尝试无输出失败，丢弃重建: %s",
                        attempt,
                        exc.cause,
                    )
                    if mode == "resume":
                        # resume 会话已连上但首轮无输出：判定 resume 不可用，清 sid 走全新重建
                        if session_key:
                            self._claude_session_ids.pop(session_key, None)
                        yield self._sse(
                            {"type": "thinking", "content": "原会话已失效，重建中..."}
                        )
                    entry = None
                    continue
            raise ClaudeSDKError("客户端重试多次后仍失败")
        except BaseException:
            # 本轮以失败告终：常驻客户端可能停留在轮次中间态，丢弃以保证下轮干净重建
            if session_key:
                await self._drop_client(session_key)
            raise
        finally:
            if not session_key and entry is not None:
                try:
                    await entry.client.disconnect()
                except Exception:
                    logger.debug("断开匿名客户端失败", exc_info=True)

    def _build_prompt(
        self, *, latest_user_text: str, context: Optional[str]
    ) -> str:
        """原生透传：多轮上下文由 Claude Code 原生 resume 会话承载，不做手工拼接。"""
        if context and context.strip():
            return (
                f"{latest_user_text}\n\n---\n\n"
                f"## 上一步执行结果（供参考）\n\n{context.strip()}"
            )
        return latest_user_text

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
