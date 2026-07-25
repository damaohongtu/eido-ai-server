"""OpenHarness 执行层封装。

基于 HKUDS/OpenHarness（https://github.com/HKUDS/OpenHarness，PyPI: openharness-ai）。
当 AGENT_HARNESS=open_harness 时，通过 QueryEngine 驱动 Agent 执行。
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import AsyncGenerator, Dict, List, MutableMapping, Optional

from app.services.claude_skill_service import (
    _HEARTBEAT_FRAME,
    HEARTBEAT_INTERVAL_SEC,
    extract_latest_user_text,
)

logger = logging.getLogger(__name__)

OPEN_HARNESS_LOG_CHUNK_CHARS = 4000


def _serialize_log_value(value: object) -> str:
    """Serialize an OpenHarness value without shortening its content."""
    try:
        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        elif hasattr(value, "model_dump"):
            value = value.model_dump()  # type: ignore[union-attr]
        elif hasattr(value, "__dict__"):
            value = vars(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError, RecursionError):
        return repr(value)


def _log_complete_value(
    event_name: str, value: object, level: int = logging.INFO
) -> None:
    """Write the complete value in ordered chunks so log collectors do not cut it."""
    content = _serialize_log_value(value)
    encoded = content.replace("\r", "\\r").replace("\n", "\\n")
    chunks = [
        encoded[offset : offset + OPEN_HARNESS_LOG_CHUNK_CHARS]
        for offset in range(0, len(encoded), OPEN_HARNESS_LOG_CHUNK_CHARS)
    ] or [""]
    for index, chunk in enumerate(chunks, start=1):
        logger.log(
            level,
            "  [OpenHarness/%s chunk=%d/%d chars=%d] %s",
            event_name,
            index,
            len(chunks),
            len(content),
            chunk,
        )


def _log_event(
    event: object, state: Optional[MutableMapping[str, object]] = None
) -> None:
    """Log every semantic OpenHarness stream event at INFO or above."""
    try:
        from openharness.engine.stream_events import (
            AssistantTextDelta,
            AssistantTurnComplete,
            ErrorEvent,
            StatusEvent,
            ToolExecutionCompleted,
            ToolExecutionStarted,
        )
    except ImportError:
        _log_complete_value(type(event).__name__, event)
        return

    event_name = type(event).__name__
    if state is not None:
        counts = state.setdefault("event_counts", {})
        if isinstance(counts, dict):
            counts[event_name] = int(counts.get(event_name, 0)) + 1

    if isinstance(event, AssistantTextDelta):
        text = event.text or ""
        if state is not None:
            state["assistant_chars"] = int(state.get("assistant_chars", 0)) + len(text)
        _log_complete_value("Assistant/TextDelta", text)
    elif isinstance(event, ToolExecutionStarted):
        if state is not None:
            state["tool_calls"] = int(state.get("tool_calls", 0)) + 1
        logger.info("  [OpenHarness/Tool/Call] %s", event.tool_name)
        _log_complete_value(f"Tool/Input:{event.tool_name}", event.tool_input)
    elif isinstance(event, ToolExecutionCompleted):
        status = "ERROR" if event.is_error else "OK"
        logger.info("  [OpenHarness/Tool/Result:%s] %s", status, event.tool_name)
        _log_complete_value(
            f"Tool/Output:{event.tool_name}",
            event.output or "",
            logging.ERROR if event.is_error else logging.INFO,
        )
    elif isinstance(event, ErrorEvent):
        _log_complete_value("Error", event, logging.ERROR)
    elif isinstance(event, StatusEvent):
        _log_complete_value("Status", event.message or "")
    elif isinstance(event, AssistantTurnComplete):
        usage = event.usage
        if state is not None:
            state["input_tokens"] = int(getattr(usage, "input_tokens", 0) or 0)
            state["output_tokens"] = int(getattr(usage, "output_tokens", 0) or 0)
        _log_complete_value("Assistant/TurnComplete", event)
    else:
        _log_complete_value(event_name, event)


SYSTEM_PROMPT = (
    "You are an AI coding assistant. Use the available tools to help the user "
    "with their software engineering tasks. Think carefully before acting, "
    "and always verify your work.\n\n"
    "You have access to a full set of development tools (bash, file read/write/edit, "
    "glob, grep, web fetch, web search, skills, tasks, agents, and more). "
    "Use them freely to accomplish the user's goals.\n\n"
    "IMPORTANT:\n"
    "- You are working in the session workspace. Uploaded files are in `uploads/`.\n"
    "- Write all generated output to `outputs/` (created automatically).\n"
    "- Use the `skill` tool to discover available skills when relevant.\n"
    "- The skills directory is configured, you can read any SKILL.md by its filename.\n"
    "\n"
    "FILE WRITING STRATEGY:\n"
    "- Use `write_file` for small files (markdown, configs, short scripts).\n"
    "- For LARGE files (HTML reports, long documents, files expected to exceed ~3000 tokens),\n"
    "  write in chunks using Bash heredoc to avoid truncation:\n"
    "  1. First chunk:  cat > outputs/file.html << 'ENDOFFILE'\n"
    "     ...content...\n"
    "     ENDOFFILE\n"
    "  2. Append chunks:  cat >> outputs/file.html << 'ENDOFFILE'\n"
    "     ...more content...\n"
    "     ENDOFFILE\n"
    "  - Use an UNIQUE terminator (like ENDOFFILE or REPORTEND) that does NOT\n"
    "    appear in the file content itself.\n"
    "  - Each chunk should fit comfortably in a single response.\n"
    "  - Always use single-quoted terminator ('ENDOFFILE') to prevent shell expansion.\n"
    "  - If you notice the output is getting very long, proactively switch to this method."
)


class OpenHarnessService:

    def __init__(self, skills_dir: Path, workspace_root: Path):
        self.skills_dir = skills_dir
        self.workspace_root = workspace_root
        self._engines: Dict[str, object] = {}

    async def execute_stream(
        self,
        messages: list,
        context: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        run_started = time.perf_counter()
        run_state: MutableMapping[str, object] = {
            "event_counts": {},
            "assistant_chars": 0,
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        logger.info(
            f"▶ OH execute_stream | msgs={len(messages)}"
            + (f" | session={session_id}" if session_id else "")
            + (f" | ctx={len(context)}B" if context else "")
        )

        yield _sse({"type": "thinking", "content": "正在分析请求，自动规划执行..."})
        yield _sse({"type": "workflow_start", "skill_name": "auto"})

        cwd = self.workspace_root
        if session_id:
            from app.services.session_workspace import get_session_workspace_manager

            try:
                cwd = get_session_workspace_manager().session_root(session_id)
            except ValueError as e:
                yield _sse({"type": "error", "message": f"非法 session_id: {e}"})
                yield "data: [DONE]\n\n"
                return

        text = extract_latest_user_text(messages)
        if not text:
            yield _sse({"type": "error", "message": "未找到用户输入"})
            yield "data: [DONE]\n\n"
            return

        if context and context.strip():
            text = f"{text}\n\n---\n## 上一步执行结果（供参考）\n\n{context.strip()[:4000]}"

        engine_reused = (session_id or "_") in self._engines
        try:
            engine = self._get_engine(session_id or "_", cwd)
        except Exception as e:
            logger.error(f"OH 引擎初始化失败: {e}", exc_info=True)
            yield _sse({"type": "error", "message": f"引擎初始化失败: {e}"})
            yield "data: [DONE]\n\n"
            return

        logger.info(
            "  [OpenHarness/Run] engine_reused=%s prompt_chars=%d cwd=%s model=%s",
            engine_reused,
            len(text),
            cwd,
            _resolve_model(),
        )

        async def stream_events():
            try:
                async for event in engine.submit_message(text):  # type: ignore[union-attr]
                    _log_event(event, run_state)
                    for sse in _convert_event(event):
                        yield sse
            except Exception as e:
                logger.error(f"OH 执行失败: {e}", exc_info=True)
                yield _sse({"type": "error", "message": f"执行失败: {e}"})

        queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        async def producer():
            try:
                async for ev in stream_events():
                    await queue.put(ev)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"OH stream error: {e}", exc_info=True)
                await queue.put(_sse({"type": "error", "message": f"执行失败: {e}"}))
            finally:
                await queue.put(_DONE)

        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
                    await queue.put(_HEARTBEAT_FRAME)
            except asyncio.CancelledError:
                pass

        pt = asyncio.create_task(producer())
        ht = asyncio.create_task(heartbeat())
        had_error = False
        try:
            while True:
                ev = await queue.get()
                if ev is _DONE:
                    break
                if isinstance(ev, str) and (
                    '"type":"error"' in ev or '"type": "error"' in ev
                ):
                    had_error = True
                yield ev
            if not had_error:
                yield _sse({"type": "workflow_complete", "data": {"references": []}})
            logger.info(
                "◀ OH execute_stream 完成 | status=%s elapsed_ms=%.1f events=%s "
                "tools=%d assistant_chars=%d input_tokens=%d output_tokens=%d",
                "error" if had_error else "ok",
                (time.perf_counter() - run_started) * 1000,
                _serialize_log_value(run_state["event_counts"]),
                run_state["tool_calls"],
                run_state["assistant_chars"],
                run_state["input_tokens"],
                run_state["output_tokens"],
            )
        finally:
            ht.cancel()
            if not pt.done():
                pt.cancel()
            for t in (ht, pt):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        yield "data: [DONE]\n\n"

    def _get_engine(self, session_id: str, cwd: Path):
        if session_id in self._engines:
            return self._engines[session_id]

        engine = self._build_engine(str(cwd))
        self._engines[session_id] = engine
        return engine

    def _build_engine(self, cwd: str):
        from openharness.config.settings import PermissionSettings
        from openharness.engine import QueryEngine
        from openharness.permissions import PermissionChecker
        from openharness.permissions.modes import PermissionMode
        from openharness.tools import create_default_tool_registry

        api_client = _create_api_client()
        tool_registry = create_default_tool_registry()

        perm = PermissionChecker(
            PermissionSettings(
                mode=PermissionMode.FULL_AUTO,
            )
        )

        extra_skill_dirs = [str(self.skills_dir)]

        sys_prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"当前工作区: {cwd}\n"
            f"产出目录: {cwd}/outputs\n"
            f"技能目录: {self.skills_dir}\n\n"
            f"你可以使用 `skill` 工具发现和读取可用技能。"
        )

        return QueryEngine(
            api_client=api_client,
            tool_registry=tool_registry,
            permission_checker=perm,
            cwd=cwd,
            model=_resolve_model(),
            system_prompt=sys_prompt,
            max_turns=200,
            max_tokens=16384,
            tool_metadata={"extra_skill_dirs": extra_skill_dirs},
        )


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _resolve_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", "").strip() or "claude-sonnet-4-6"


def _create_api_client():
    from openharness.api import AnthropicApiClient

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or None
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip() or None
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None

    if not api_key and not auth_token:
        raise RuntimeError(
            "OpenHarness 需要 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN 环境变量"
        )

    kwargs: dict = {}
    if api_key:
        kwargs["api_key"] = api_key
    if auth_token:
        kwargs["auth_token"] = auth_token
    if base_url:
        kwargs["base_url"] = base_url

    logger.info(f"AnthropicApiClient | base_url={base_url or '默认'}")
    return AnthropicApiClient(**kwargs)


def _convert_event(event: object) -> List[str]:
    try:
        from openharness.engine.stream_events import (
            AssistantTextDelta,
            AssistantTurnComplete,
            ErrorEvent,
            StatusEvent,
            ToolExecutionCompleted,
            ToolExecutionStarted,
        )
    except ImportError:
        return []

    out: List[str] = []

    if isinstance(event, AssistantTextDelta):
        if event.text:
            out.append(_sse({"type": "content", "content": event.text}))

    elif isinstance(event, ToolExecutionStarted):
        hint = f"执行工具: {event.tool_name}"
        try:
            hint += f" | 参数: {str(event.tool_input)[:120]}"
        except Exception:
            pass
        out.append(_sse({"type": "thinking", "content": hint}))

    elif isinstance(event, ToolExecutionCompleted):
        preview = (event.output or "")[:200].strip()
        status = "✗ 工具出错" if event.is_error else "✓ 工具完成"
        out.append(
            _sse(
                {
                    "type": "thinking",
                    "content": f"{status}: {preview}" if preview else status,
                }
            )
        )

    elif isinstance(event, ErrorEvent):
        out.append(_sse({"type": "error", "message": event.message}))

    elif isinstance(event, StatusEvent):
        if event.message:
            out.append(_sse({"type": "thinking", "content": event.message}))

    elif isinstance(event, AssistantTurnComplete):
        u = event.usage
        out.append(
            _sse(
                {
                    "type": "thinking",
                    "content": f"执行完成 | 输入: {u.input_tokens} tokens | 输出: {u.output_tokens} tokens",
                }
            )
        )

    return out


_instance: Optional[OpenHarnessService] = None


def get_open_harness_service() -> Optional[OpenHarnessService]:
    return _instance


def init_open_harness_service(
    skills_dir: Path, workspace_root: Path
) -> OpenHarnessService:
    global _instance
    _instance = OpenHarnessService(skills_dir, workspace_root)
    logger.info(f"OpenHarnessService 初始化完成 - 技能目录: {skills_dir}")
    return _instance
