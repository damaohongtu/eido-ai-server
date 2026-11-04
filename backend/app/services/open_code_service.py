"""OpenCode CLI adapter for Eido's streaming chat interface."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SEC = 12.0
_HEARTBEAT_FRAME = ": ping\n\n"
OPENCODE_STDOUT_CHUNK_SIZE = 64 * 1024
OPENCODE_MAX_EVENT_BYTES = 64 * 1024 * 1024
OPENCODE_LOG_CHUNK_CHARS = 4000
PROCESS_DETAIL_MAX_CHARS = 600


def _log_complete_output(
    stream_name: str, content: str, level: int = logging.INFO
) -> None:
    """Log complete output in ordered, single-line chunks without truncation."""
    if not content:
        return
    encoded = content.replace("\r", "\\r").replace("\n", "\\n")
    chunks = [
        encoded[offset : offset + OPENCODE_LOG_CHUNK_CHARS]
        for offset in range(0, len(encoded), OPENCODE_LOG_CHUNK_CHARS)
    ]
    for index, chunk in enumerate(chunks, start=1):
        logger.log(
            level,
            "[OpenCode/%s chunk=%d/%d chars=%d] %s",
            stream_name,
            index,
            len(chunks),
            len(content),
            chunk,
        )


def _serialize_log_value(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError, RecursionError):
        return repr(value)


def _log_semantic_event(event: dict) -> None:
    """Add readable, complete process logs alongside OpenCode's raw NDJSON."""
    event_type = str(event.get("type") or "unknown")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    if event_type == "text":
        _log_complete_output("Assistant/Text", str(part.get("text") or ""))
    elif event_type == "reasoning":
        _log_complete_output("Assistant/Reasoning", str(part.get("text") or ""))
    elif event_type == "tool_use":
        tool = str(part.get("tool") or "tool")
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = str(state.get("status") or "unknown")
        logger.info("  [OpenCode/Tool] name=%s status=%s", tool, status)
        if "input" in state:
            _log_complete_output(
                f"Tool/Input:{tool}", _serialize_log_value(state.get("input"))
            )
        if "output" in state:
            _log_complete_output(
                f"Tool/Output:{tool}", _serialize_log_value(state.get("output"))
            )
        if "error" in state:
            _log_complete_output(
                f"Tool/Error:{tool}",
                _serialize_log_value(state.get("error")),
                logging.ERROR,
            )
    elif event_type == "step_start":
        _log_complete_output("Step/Start", _serialize_log_value(part))
    elif event_type == "step_finish":
        _log_complete_output("Step/Finish", _serialize_log_value(part))
    elif event_type == "error":
        _log_complete_output("Error", _serialize_log_value(event), logging.ERROR)
    else:
        logger.info("  [OpenCode/Event] type=%s", event_type)


async def _iter_stdout_lines(
    stream: asyncio.StreamReader,
) -> AsyncGenerator[bytes, None]:
    """Read NDJSON without StreamReader.readline()'s default 64 KiB ceiling."""
    buffer = bytearray()
    while True:
        chunk = await stream.read(OPENCODE_STDOUT_CHUNK_SIZE)
        if not chunk:
            if buffer:
                yield bytes(buffer)
            return
        buffer.extend(chunk)
        while True:
            newline_at = buffer.find(b"\n")
            if newline_at < 0:
                if len(buffer) > OPENCODE_MAX_EVENT_BYTES:
                    raise ValueError("OpenCode 单条 JSON 事件超过 64 MiB 安全上限")
                break
            if newline_at > OPENCODE_MAX_EVENT_BYTES:
                raise ValueError("OpenCode 单条 JSON 事件超过 64 MiB 安全上限")
            yield bytes(buffer[:newline_at])
            del buffer[: newline_at + 1]


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _field(message: object, name: str) -> str:
    value = (
        message.get(name) if isinstance(message, dict) else getattr(message, name, "")
    )
    return str(value or "").strip()


def _latest_user_text(messages: list) -> str:
    for message in reversed(messages or []):
        if _field(message, "role") == "user":
            return _field(message, "content")
    return ""


def _conversation_before_latest_user(messages: list, *, max_chars: int = 12_000) -> str:
    latest_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if _field(messages[index], "role") == "user"
        ),
        len(messages),
    )
    labels = {"user": "用户", "assistant": "助手", "system": "系统"}
    blocks = []
    used = 0
    for message in reversed(messages[:latest_index]):
        role = _field(message, "role")
        content = _field(message, "content")
        if content:
            block = f"### {labels.get(role, role or '消息')}\n\n{content[:4000]}"
            if blocks and used + len(block) > max_chars:
                break
            if len(block) > max_chars:
                block = block[:max_chars]
            blocks.append(block)
            used += len(block)
    if not blocks:
        return ""
    blocks.reverse()
    return "## 当前会话历史\n\n" + "\n\n".join(blocks)


def _event_error_message(event: dict) -> str:
    error = event.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        return str(error.get("message") or error.get("name") or error)
    return "OpenCode 执行失败"


def _compact_process_value(value: object, limit: int = PROCESS_DETAIL_MAX_CHARS) -> str:
    """Compact only client-facing progress text; raw process output is logged fully."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), default=str
            )
        except (TypeError, ValueError, RecursionError):
            text = repr(value)
    preview = " ".join(text[:limit].split())
    return preview if len(text) <= limit else f"{preview}…（共 {len(text)} 字符）"


def _tool_input_summary(tool: str, tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return _compact_process_value(tool_input)

    def first(*keys: str) -> object:
        for key in keys:
            value = tool_input.get(key)
            if value not in (None, ""):
                return value
        return ""

    normalized = tool.lower()
    if normalized in {"bash", "shell"}:
        return _compact_process_value(first("command", "cmd"))
    if normalized in {"read", "glob", "grep", "list", "ls"}:
        return _compact_process_value(
            first("filePath", "file_path", "path", "pattern", "query") or tool_input
        )
    if normalized in {"write", "edit", "apply_patch", "patch"}:
        target = first("filePath", "file_path", "path")
        content = first("content", "text", "patch", "patchText")
        details = []
        if target:
            details.append(f"文件: {_compact_process_value(target, 300)}")
        if content:
            details.append(f"内容: {len(str(content))} 字符")
        return " · ".join(details) or _compact_process_value(tool_input)
    if normalized in {"webfetch", "web_fetch", "fetch"}:
        return _compact_process_value(first("url", "uri") or tool_input)
    return _compact_process_value(tool_input)


def _tool_event_content(part: dict) -> str:
    tool = str(part.get("tool") or "tool")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    status = str(state.get("status") or "completed")
    failed = status == "error"
    segments = [f"{'✗' if failed else '✓'} 工具{'失败' if failed else '完成'}: {tool}"]
    input_summary = _tool_input_summary(tool, state.get("input"))
    if input_summary:
        segments.append(f"调用: {input_summary}")
    result = (
        state.get("error") if failed else (state.get("title") or state.get("output"))
    )
    if result:
        segments.append(f"结果: {_compact_process_value(result)}")
    timing = state.get("time") if isinstance(state.get("time"), dict) else {}
    start, end = timing.get("start"), timing.get("end")
    if (
        isinstance(start, (int, float))
        and isinstance(end, (int, float))
        and end >= start
    ):
        segments.append(f"耗时: {(end - start) / 1000:.2f}s")
    return " · ".join(segments)


def _convert_event(event: dict, state: dict | None = None) -> list[str]:
    event_type = event.get("type")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    if event_type == "text" and part.get("text"):
        return [_sse({"type": "content", "content": part["text"]})]
    if event_type == "reasoning" and part.get("text"):
        return [_sse({"type": "thinking", "content": part["text"]})]
    if event_type == "step_start":
        step_number = None
        if state is not None:
            state["step_number"] = int(state.get("step_number") or 0) + 1
            step_number = state["step_number"]
        content = (
            f"OpenCode 开始第 {step_number} 个推理步骤..."
            if step_number
            else "OpenCode 开始执行推理步骤..."
        )
        return [_sse({"type": "thinking", "content": content})]
    if event_type == "step_finish":
        number = state.get("step_number") if state else None
        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        total = tokens.get("total")
        if total is None:
            total = sum(
                value
                for value in (
                    tokens.get("input"),
                    tokens.get("output"),
                    tokens.get("reasoning"),
                )
                if isinstance(value, (int, float))
            )
        details = [
            (
                f"OpenCode 第 {number} 个推理步骤完成"
                if number
                else "OpenCode 推理步骤完成"
            )
        ]
        if part.get("reason"):
            details.append(f"原因: {part['reason']}")
        if total:
            details.append(f"Token: {int(total)}")
        if isinstance(part.get("cost"), (int, float)):
            details.append(f"费用: ${part['cost']:.6f}")
        return [_sse({"type": "thinking", "content": " · ".join(details)})]
    if event_type == "tool_use":
        return [_sse({"type": "thinking", "content": _tool_event_content(part)})]
    if event_type == "error":
        return [_sse({"type": "error", "message": _event_error_message(event)})]
    return []


class OpenCodeService:
    """Run one OpenCode process per turn and resume by Eido session ID."""

    def __init__(
        self, skills_dir: Path, workspace_root: Path, binary: str = "opencode"
    ):
        self.skills_dir = skills_dir
        self.workspace_root = workspace_root
        self.binary = binary
        self._session_ids: dict[str, str] = {}
        if not shutil.which(binary):
            raise RuntimeError(
                "OpenCode CLI 未安装，请运行: npm install -g opencode-ai"
            )

    def reset_session(self, session_id: str) -> None:
        self._session_ids.pop(session_id, None)

    def _skills_index(self) -> str:
        if not self.skills_dir.exists():
            return "（当前没有可用技能）"
        skills = {
            skill_file.parent.name: skill_file.resolve()
            for skill_file in sorted(self.skills_dir.glob("*/SKILL.md"))
        }
        if not skills:
            return "（当前没有可用技能）"
        return "\n".join(f"- {name}: `{path}`" for name, path in sorted(skills.items()))

    def _build_prompt(
        self,
        text: str,
        cwd: Path,
        context: Optional[str],
        *,
        resume: bool,
        conversation_history: str = "",
    ) -> str:
        context_section = ""
        if context and context.strip():
            context_section = (
                "\n\n---\n## 上一步执行结果（供参考）\n" f"{context.strip()[:4000]}"
            )
        if resume:
            return f"## 用户最新请求\n{text}{context_section}"
        history = f"{conversation_history}\n\n---\n\n" if conversation_history else ""
        return (
            f"当前会话工作区是 `{cwd}`。上传文件位于 `{cwd / 'uploads'}`，"
            f"所有生成产物必须写入 `{cwd / 'outputs'}`。\n\n"
            f"## 可用技能\n{self._skills_index()}\n\n"
            "需要技能时读取对应的 SKILL.md，并严格遵循技能说明。"
            "不要把产物写到会话工作区之外。\n\n"
            f"{history}## 用户最新请求\n{text}{context_section}"
        )

    async def execute_stream(
        self,
        messages: list,
        context: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        run_started = time.perf_counter()
        yield _sse({"type": "thinking", "content": "正在通过 OpenCode 分析请求..."})
        yield _sse({"type": "workflow_start", "skill_name": "auto"})
        text = _latest_user_text(messages)
        if not text:
            yield _sse({"type": "error", "message": "未找到用户输入"})
            yield "data: [DONE]\n\n"
            return

        if session_id:
            from app.services.session_workspace import get_session_workspace_manager

            try:
                cwd = get_session_workspace_manager().session_root(session_id)
            except ValueError as exc:
                yield _sse({"type": "error", "message": f"非法 session_id: {exc}"})
                yield "data: [DONE]\n\n"
                return
        else:
            cwd = self.workspace_root

        native_session_id = self._session_ids.get(session_id or "")
        prompt = self._build_prompt(
            text,
            cwd,
            context,
            resume=bool(native_session_id),
            conversation_history=_conversation_before_latest_user(messages),
        )
        args = [
            self.binary,
            "run",
            "--format",
            "json",
            "--thinking",
            "--auto",
            "--dir",
            str(cwd),
        ]
        from app.core.config import settings
        from app.core.mcp_config import load_mcp_config, merge_opencode_mcp_config

        model = (
            settings.OPENCODE_MODEL.strip()
            or os.environ.get("OPENCODE_MODEL", "").strip()
        )
        if model:
            args.extend(["--model", model])
        if native_session_id:
            args.extend(["--session", native_session_id])
        args.append(prompt)

        env = os.environ.copy()
        # Settings reads backend/.env without exporting it. Make values referenced
        # by OpenCode config available to the child process.
        env.update(settings.claude_agent_env)
        try:
            mcp_config = load_mcp_config(settings.MCP_CONFIG_PATH, environment=env)
            merged_inline_config = merge_opencode_mcp_config(
                settings.OPENCODE_CONFIG_CONTENT,
                mcp_config.opencode_servers,
            )
        except ValueError as exc:
            logger.error("OpenCode MCP 配置加载失败: %s", exc)
            yield _sse({"type": "error", "message": f"MCP 配置错误: {exc}"})
            yield "data: [DONE]\n\n"
            return
        logger.info(
            "  [OpenCode/MCP] path=%s servers=%s",
            mcp_config.path,
            ",".join(mcp_config.server_names) or "(none)",
        )
        if settings.OPENCODE_CONFIG.strip():
            env["OPENCODE_CONFIG"] = settings.OPENCODE_CONFIG.strip()
        if merged_inline_config:
            env["OPENCODE_CONFIG_CONTENT"] = merged_inline_config
        # In local mode retain ~/.local/share/opencode so credentials created by
        # `opencode auth` remain visible. Docker can opt into persistent isolation.
        if settings.EIDO_DATA_ROOT.strip():
            env.setdefault("XDG_DATA_HOME", str(settings.data_root / "opencode-data"))
        if session_id:
            env["EIDO_SESSION_ID"] = session_id

        queue: asyncio.Queue[object] = asyncio.Queue()
        done = object()
        state = {
            "had_error": False,
            "step_number": 0,
            "saw_content": False,
            "saw_tool": False,
            "token_total": 0,
            "assistant_chars": 0,
            "tool_events": 0,
            "event_counts": {},
        }
        process: Optional[asyncio.subprocess.Process] = None

        async def producer() -> None:
            nonlocal process
            stderr_text = ""
            try:
                logger.info(
                    "▶ OpenCode 开始执行 | binary=%s cwd=%s resume=%s prompt_chars=%d",
                    self.binary,
                    cwd,
                    bool(native_session_id),
                    len(prompt),
                )
                process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=str(cwd),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                async def read_stderr() -> str:
                    assert process and process.stderr
                    return (await process.stderr.read()).decode(
                        "utf-8", errors="replace"
                    )

                stderr_task = asyncio.create_task(read_stderr())
                assert process.stdout
                async for line in _iter_stdout_lines(process.stdout):
                    raw = line.decode("utf-8", errors="replace")
                    if not raw.strip():
                        continue
                    _log_complete_output("stdout", raw)
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("忽略上方无法解析的 OpenCode stdout")
                        continue
                    _log_semantic_event(event)
                    sid = event.get("sessionID")
                    if session_id and isinstance(sid, str) and sid:
                        self._session_ids[session_id] = sid
                    event_type = event.get("type")
                    event_counts = state["event_counts"]
                    if isinstance(event_counts, dict):
                        event_counts[event_type or "unknown"] = (
                            int(event_counts.get(event_type or "unknown", 0)) + 1
                        )
                    if event_type == "text" and isinstance(event.get("part"), dict):
                        if event["part"].get("text"):
                            state["saw_content"] = True
                            state["assistant_chars"] += len(str(event["part"]["text"]))
                    elif event_type == "tool_use":
                        state["saw_tool"] = True
                        state["tool_events"] += 1
                    elif event_type == "step_finish" and isinstance(
                        event.get("part"), dict
                    ):
                        tokens = event["part"].get("tokens")
                        if isinstance(tokens, dict):
                            total = tokens.get("total")
                            if not isinstance(total, (int, float)):
                                total = sum(
                                    value
                                    for value in (
                                        tokens.get("input"),
                                        tokens.get("output"),
                                        tokens.get("reasoning"),
                                    )
                                    if isinstance(value, (int, float))
                                )
                            state["token_total"] += int(total or 0)
                    if event_type == "error":
                        state["had_error"] = True
                    for converted in _convert_event(event, state):
                        await queue.put(converted)
                return_code = await process.wait()
                stderr_text = await stderr_task
                if stderr_text:
                    _log_complete_output(
                        "stderr",
                        stderr_text,
                        logging.ERROR if return_code != 0 else logging.WARNING,
                    )
                logger.info("  [OpenCode/ProcessExit] return_code=%d", return_code)
                if return_code != 0 and not state["had_error"]:
                    state["had_error"] = True
                    detail = stderr_text.strip()[-1000:] or f"退出码 {return_code}"
                    await queue.put(
                        _sse(
                            {"type": "error", "message": f"OpenCode 执行失败: {detail}"}
                        )
                    )
                elif (
                    return_code == 0
                    and not state["had_error"]
                    and not state["saw_content"]
                    and not state["saw_tool"]
                ):
                    state["had_error"] = True
                    logger.error(
                        "OpenCode 未产生正文或工具事件 | token_total=%d | model=%s",
                        state["token_total"],
                        model or "(OpenCode default)",
                    )
                    await queue.put(
                        _sse(
                            {
                                "type": "error",
                                "message": (
                                    "OpenCode 未产生有效输出。请确认本机已执行 "
                                    "`opencode auth list`，并配置可用的 "
                                    "OPENCODE_MODEL=provider/model；Docker 部署还需注入 "
                                    "OPENCODE_CONFIG_CONTENT 或 provider 凭据。"
                                ),
                            }
                        )
                    )
            except asyncio.CancelledError:
                if process and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                raise
            except Exception as exc:
                state["had_error"] = True
                logger.error("OpenCode 执行异常: %s", exc, exc_info=True)
                await queue.put(
                    _sse({"type": "error", "message": f"OpenCode 执行失败: {exc}"})
                )
            finally:
                logger.info(
                    "◀ OpenCode 执行结束 | status=%s return_code=%s elapsed_ms=%.1f "
                    "events=%s tool_events=%d assistant_chars=%d token_total=%d",
                    "error" if state["had_error"] else "ok",
                    process.returncode if process else "not-started",
                    (time.perf_counter() - run_started) * 1000,
                    _serialize_log_value(state["event_counts"]),
                    state["tool_events"],
                    state["assistant_chars"],
                    state["token_total"],
                )
                await queue.put(done)

        async def heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
                    await queue.put(_HEARTBEAT_FRAME)
            except asyncio.CancelledError:
                pass

        producer_task = asyncio.create_task(producer())
        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            while True:
                event = await queue.get()
                if event is done:
                    break
                yield str(event)
            if not state["had_error"]:
                yield _sse({"type": "workflow_complete", "data": {"references": []}})
        finally:
            heartbeat_task.cancel()
            if not producer_task.done():
                producer_task.cancel()
            for task in (heartbeat_task, producer_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        yield "data: [DONE]\n\n"


_instance: Optional[OpenCodeService] = None


def get_open_code_service() -> Optional[OpenCodeService]:
    return _instance


def init_open_code_service(skills_dir: Path, workspace_root: Path) -> OpenCodeService:
    global _instance
    _instance = OpenCodeService(skills_dir, workspace_root)
    logger.info("OpenCodeService 初始化完成 - 技能目录: %s", skills_dir)
    return _instance
