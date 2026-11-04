import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, List, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_TOOLS = ["Bash", "Glob", "Read", "WebFetch"]

HEARTBEAT_INTERVAL_SEC = 12.0
_HEARTBEAT_FRAME = ": ping\n\n"
SKILL_CACHE_TTL_SEC = 5.0
CLIENT_IDLE_TTL_SEC = 15 * 60.0
CLIENT_POOL_MAX = 32
CLAUDE_LOG_CHUNK_CHARS = 4000
_NATIVE_SKILLS_MANIFEST = ".eido-native-skills.json"


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


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content

    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    fm_text = content[3:end].strip()
    body = content[end + 4 :].lstrip("\n")

    try:
        import yaml

        metadata = yaml.safe_load(fm_text) or {}
    except Exception:
        metadata: dict = {}
        for line in fm_text.splitlines():
            if ": " in line and not line.startswith(" ") and not line.startswith("-"):
                k, _, v = line.partition(": ")
                metadata[k.strip()] = v.strip()

    return metadata, body


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


def build_conversation_history(messages: list) -> str:
    lines: list[str] = []
    for msg in messages:
        role = (
            getattr(msg, "role", None)
            or (msg.get("role") if isinstance(msg, dict) else "")
            or ""
        )
        content = (
            getattr(msg, "content", None)
            or (msg.get("content") if isinstance(msg, dict) else "")
            or ""
        )
        if role and content:
            lines.append(f"**{role}**: {content}")
    return "\n\n".join(lines)


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


@dataclass
class SkillMeta:
    id: str
    name: str
    description: str
    allowed_tools: List[str]
    content: str
    skill_dir: Path
    created_at: str = ""
    updated_at: str = ""
    is_system: bool = True
    owner_type: str = "system"
    owner_user_id: Optional[str] = None
    user_id: Optional[str] = None


@dataclass
class _SkillCacheEntry:
    skills: tuple[SkillMeta, ...]
    fingerprint: tuple[tuple[str, int, int], ...]
    expires_at: float


@dataclass
class _ClaudeClientEntry:
    client: Any
    session_id: str
    signature: tuple[Any, ...]
    created_at: float
    last_used: float
    busy: bool = True
    stale: bool = False


class _ResumeUnavailable(RuntimeError):
    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause


class _AgentAuthenticationError(RuntimeError):
    pass


class ClaudeSkillService:

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
        self._skill_cache: Optional[_SkillCacheEntry] = None
        self._native_skill_views: dict[str, tuple[tuple[Any, ...], int]] = {}
        self._clients: dict[str, _ClaudeClientEntry] = {}
        self._client_cleanup_tasks: set[asyncio.Task] = set()
        self._claude_session_ids: dict[str, str] = {}

    def scan_skills(self) -> List[SkillMeta]:
        now = time.monotonic()
        cached = self._skill_cache
        if cached and now < cached.expires_at:
            return list(cached.skills)
        fingerprint = self._catalog_fingerprint()
        if cached and cached.fingerprint == fingerprint:
            cached.expires_at = now + SKILL_CACHE_TTL_SEC
            return list(cached.skills)

        skills: List[SkillMeta] = []
        if self.skills_dir.exists():
            for skill_dir in sorted(self.skills_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                if not (skill_dir / "SKILL.md").exists():
                    continue
                try:
                    meta = self._load_skill(skill_dir)
                    skills.append(meta)
                except Exception as e:
                    logger.warning(f"加载技能失败 [{skill_dir.name}]: {e}")
        skills.sort(key=lambda s: s.id)
        self._skill_cache = _SkillCacheEntry(
            skills=tuple(skills),
            fingerprint=fingerprint,
            expires_at=now + SKILL_CACHE_TTL_SEC,
        )
        if cached is not None:
            for entry in self._clients.values():
                entry.stale = True
        logger.info("刷新技能缓存: %d 个", len(skills))
        return skills

    def _catalog_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        if not self.skills_dir.exists():
            return ()
        entries = []
        for skill_dir in sorted(self.skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_dir.is_dir() or not skill_md.is_file():
                continue
            try:
                stat = skill_md.stat()
            except OSError:
                continue
            entries.append((str(skill_dir.resolve()), stat.st_mtime_ns, stat.st_size))
        return tuple(entries)

    def invalidate_skill_cache(self) -> None:
        self._skill_cache = None
        for entry in self._clients.values():
            entry.stale = True

    def get_skill(self, skill_id: str) -> SkillMeta:
        sys_dir = self.skills_dir / skill_id
        if (sys_dir / "SKILL.md").exists():
            return self._load_skill(sys_dir)
        raise FileNotFoundError(f"技能不存在: {skill_id}")

    def _load_skill(self, skill_dir: Path) -> SkillMeta:
        skill_md = skill_dir / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        meta, _body = _parse_frontmatter(content)

        skill_id = skill_dir.name
        name = meta.get("name", skill_id)
        description = meta.get("description") or _body[:200].strip()

        raw_tools = meta.get("allowed_tools") or meta.get("allowed-tools")
        if isinstance(raw_tools, list):
            allowed_tools = [str(t) for t in raw_tools]
        elif isinstance(raw_tools, str) and raw_tools:
            allowed_tools = [t.strip() for t in raw_tools.split(",") if t.strip()]
        else:
            allowed_tools = list(DEFAULT_ALLOWED_TOOLS)

        stat = skill_md.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()

        return SkillMeta(
            id=skill_id,
            name=name,
            description=description,
            allowed_tools=allowed_tools,
            content=content,
            skill_dir=skill_dir,
            created_at=mtime,
            updated_at=mtime,
        )

    def _build_skills_index(self) -> str:
        skills = self.scan_skills()
        if not skills:
            return "（当前没有可用技能）"
        lines = []
        for s in skills:
            abs_path = (s.skill_dir / "SKILL.md").resolve()
            lines.append(
                f"- **{s.id}**: {s.description}\n  SKILL.md 绝对路径: `{abs_path}`"
            )
        return "\n".join(lines)

    def _materialize_native_skills(self, cwd: Path) -> tuple[tuple[Any, ...], int]:
        """Expose the flat server skill catalog through Claude Code native Skills."""
        skills = self.scan_skills()
        claude_dir = cwd / ".claude"
        native_root = claude_dir / "skills"
        native_root.mkdir(parents=True, exist_ok=True)
        manifest_path = claude_dir / _NATIVE_SKILLS_MANIFEST
        managed: dict[str, str] = {}
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                managed = {
                    str(name): str(target)
                    for name, target in raw.items()
                    if isinstance(name, str) and isinstance(target, str)
                }
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

        expected: dict[str, str] = {}
        revision = []
        for skill in skills:
            if Path(skill.id).name != skill.id or skill.id in {"", ".", ".."}:
                logger.warning("跳过无法映射到原生目录的技能 ID: %r", skill.id)
                continue
            target = skill.skill_dir.resolve()
            expected[skill.id] = str(target)
            revision.append(
                (skill.id, str(target), skill.updated_at, len(skill.content))
            )
        revision_tuple = tuple(revision)
        view_key = str(cwd.resolve())
        previous = self._native_skill_views.get(view_key)
        if previous and previous[0] == revision_tuple and manifest_path.is_file():
            return previous

        for name, old_target in managed.items():
            if expected.get(name) == old_target:
                continue
            link = native_root / name
            if link.is_symlink():
                try:
                    current = (link.parent / os.readlink(link)).resolve()
                    if str(current) == old_target:
                        link.unlink()
                except OSError:
                    logger.warning("清理旧技能链接失败: %s", link, exc_info=True)

        installed: dict[str, str] = {}
        for name, target_text in expected.items():
            link = native_root / name
            target = Path(target_text)
            if link.is_symlink():
                try:
                    current = (link.parent / os.readlink(link)).resolve()
                    if current != target:
                        link.unlink()
                        link.symlink_to(target, target_is_directory=True)
                except OSError:
                    logger.warning("更新技能链接失败: %s", link, exc_info=True)
                    continue
            elif link.exists():
                logger.warning("原生技能路径已存在且非托管链接，保留并跳过: %s", link)
                continue
            else:
                try:
                    link.symlink_to(target, target_is_directory=True)
                except OSError:
                    logger.warning(
                        "创建技能链接失败: %s -> %s", link, target, exc_info=True
                    )
                    continue
            installed[name] = target_text

        if managed != installed or not manifest_path.exists():
            manifest_path.write_text(
                json.dumps(installed, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        result = (revision_tuple, len(installed))
        self._native_skill_views[view_key] = result
        return result

    async def _close_client_entry(self, entry: _ClaudeClientEntry) -> None:
        try:
            await entry.client.disconnect()
        except Exception:
            logger.debug("关闭 ClaudeSDKClient 失败", exc_info=True)

    def _schedule_client_close(self, entry: _ClaudeClientEntry) -> None:
        try:
            task = asyncio.create_task(self._close_client_entry(entry))
        except RuntimeError:
            return
        self._client_cleanup_tasks.add(task)
        task.add_done_callback(self._client_cleanup_tasks.discard)

    async def _prune_clients(self) -> None:
        now = time.monotonic()
        stale = []
        for session_id, entry in list(self._clients.items()):
            if entry.busy:
                continue
            if entry.stale or now - entry.last_used > CLIENT_IDLE_TTL_SEC:
                if self._clients.pop(session_id, None) is entry:
                    stale.append(entry)
        for entry in stale:
            await self._close_client_entry(entry)

    async def _acquire_client(
        self,
        *,
        options: Any,
        session_id: str,
        signature: tuple[Any, ...],
    ) -> tuple[_ClaudeClientEntry, bool, float]:
        from claude_agent_sdk import ClaudeSDKClient

        await self._prune_clients()
        current = self._clients.get(session_id)
        if (
            current
            and not current.busy
            and not current.stale
            and current.signature == signature
        ):
            current.busy = True
            return current, True, 0.0
        if current is not None:
            if current.busy:
                raise RuntimeError(f"Claude session 正在执行: {session_id}")
            self._clients.pop(session_id, None)
            await self._close_client_entry(current)

        started = time.perf_counter()
        client = ClaudeSDKClient(options=options)
        await client.connect()
        connect_ms = (time.perf_counter() - started) * 1000
        now = time.monotonic()
        entry = _ClaudeClientEntry(
            client=client,
            session_id=session_id,
            signature=signature,
            created_at=now,
            last_used=now,
        )
        self._clients[session_id] = entry
        if len(self._clients) > CLIENT_POOL_MAX:
            candidates = sorted(
                (
                    item
                    for item in self._clients.values()
                    if not item.busy and item is not entry
                ),
                key=lambda item: item.last_used,
            )
            while len(self._clients) > CLIENT_POOL_MAX and candidates:
                victim = candidates.pop(0)
                if self._clients.pop(victim.session_id, None) is victim:
                    self._schedule_client_close(victim)
        return entry, False, connect_ms

    async def _release_client(
        self, entry: _ClaudeClientEntry, *, healthy: bool
    ) -> None:
        entry.busy = False
        entry.last_used = time.monotonic()
        if not healthy or entry.stale:
            if self._clients.pop(entry.session_id, None) is entry:
                await self._close_client_entry(entry)

    def reset_session(self, session_id: str) -> None:
        self._claude_session_ids.pop(session_id, None)
        for path in list(self._native_skill_views):
            if Path(path).name == session_id:
                self._native_skill_views.pop(path, None)
        entry = self._clients.get(session_id)
        if entry is not None:
            entry.stale = True
            if not entry.busy and self._clients.pop(session_id, None) is entry:
                self._schedule_client_close(entry)

    async def shutdown(self) -> None:
        entries = list(self._clients.values())
        self._clients.clear()
        if entries:
            await asyncio.gather(
                *(self._close_client_entry(entry) for entry in entries),
                return_exceptions=True,
            )
        if self._client_cleanup_tasks:
            await asyncio.gather(
                *tuple(self._client_cleanup_tasks), return_exceptions=True
            )

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
            from app.services.session_workspace import get_session_workspace_manager

            try:
                cwd = get_session_workspace_manager().session_root(session_id)
            except ValueError as e:
                yield self._sse({"type": "error", "message": f"非法 session_id: {e}"})
                yield "data: [DONE]\n\n"
                return
        else:
            cwd = self.workspace_root

        native_skills = bool(session_id)
        skill_revision: tuple[Any, ...] = ()
        skill_count = 0
        if native_skills:
            try:
                skill_revision, skill_count = self._materialize_native_skills(cwd)
            except Exception:
                logger.exception("原生技能映射失败，回退到兼容技能索引")
                native_skills = False

        try:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, query
        except ImportError:
            logger.error("claude_agent_sdk 未安装")
            yield self._sse({"type": "error", "message": "claude_agent_sdk 未安装"})
            yield "data: [DONE]\n\n"
            return

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

        # MCP values may reference variables loaded from backend/.env. Resolve
        # them before logging any provider summary; resolved secrets are never logged.
        mcp_environment = {**os.environ, **agent_env}
        try:
            mcp_config = load_mcp_config(
                settings.MCP_CONFIG_PATH, environment=mcp_environment
            )
        except ValueError as exc:
            logger.error("Claude MCP 配置加载失败: %s", exc)
            yield self._sse({"type": "error", "message": f"MCP 配置错误: {exc}"})
            yield "data: [DONE]\n\n"
            return
        auth_mode, provider = self._agent_auth_summary(agent_env)
        logger.info("  [ClaudeAuth] mode=%s provider=%s", auth_mode, provider)
        logger.info(
            "  [ClaudeMCP] path=%s servers=%s",
            mcp_config.path,
            ",".join(mcp_config.server_names) or "(none)",
        )
        client_signature = (
            str(cwd.resolve()),
            skill_revision,
            mcp_config.revision,
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
                native_skills=native_skills,
            )
            available_tools = list(self.AUTO_ALLOWED_TOOLS)
            if native_skills:
                available_tools.append("Skill")
            allowed_tools = list(available_tools)
            allowed_tools.extend(
                f"mcp__{name}__*" for name in mcp_config.claude_servers
            )
            options = ClaudeAgentOptions(
                allowed_tools=allowed_tools,
                tools=available_tools,
                cwd=str(cwd),
                setting_sources=["project"] if native_skills else [],
                skills="all" if native_skills else None,
                permission_mode="acceptEdits",
                env=agent_env,
                include_partial_messages=False,
                max_buffer_size=10 * 1024 * 1024,
                resume=resume_sid,
                mcp_servers=mcp_config.claude_servers,
                strict_mcp_config=True,
            )
            entry: Optional[_ClaudeClientEntry] = None
            warm_hit = False
            connect_ms = 0.0
            message_seen = False
            message_count = 0
            saw_result = False
            run_started = time.perf_counter()
            try:
                if session_id:
                    try:
                        entry, warm_hit, connect_ms = await self._acquire_client(
                            options=options,
                            session_id=session_id,
                            signature=client_signature,
                        )
                        await entry.client.query(prompt)
                    except ClaudeSDKError as exc:
                        if resume_sid:
                            raise _ResumeUnavailable(exc) from exc
                        raise
                    messages_iter = entry.client.receive_response()
                else:
                    messages_iter = query(prompt=prompt, options=options)

                logger.info(
                    "  [ClaudeRun] mode=%s warm=%s connect_ms=%.1f prompt_chars=%d "
                    "skills=%d tools=%d mcp=%d cwd=%s",
                    "resume" if resume_sid else "fresh",
                    warm_hit,
                    connect_ms,
                    len(prompt),
                    skill_count,
                    len(available_tools),
                    len(mcp_config.claude_servers),
                    cwd,
                )
                async for message in messages_iter:
                    if not message_seen:
                        logger.info(
                            "  [ClaudeRun] first_message_ms=%.1f warm=%s session=%s",
                            (time.perf_counter() - run_started) * 1000,
                            warm_hit,
                            session_id or "(none)",
                        )
                    message_seen = True
                    message_count += 1
                    self._log_message(message)
                    if self._is_not_logged_in_message(message):
                        raise _AgentAuthenticationError(
                            self._agent_auth_failure_message()
                        )
                    try:
                        from claude_agent_sdk.types import ResultMessage

                        if isinstance(message, ResultMessage):
                            saw_result = True
                            sid = getattr(message, "session_id", None)
                            if session_id and sid:
                                self._claude_session_ids[session_id] = sid
                    except ImportError:
                        pass
                    for event in self._convert_message(message):
                        yield event
            except ClaudeSDKError as exc:
                if resume_sid and not message_seen:
                    raise _ResumeUnavailable(exc) from exc
                raise
            finally:
                if entry is not None:
                    await self._release_client(entry, healthy=saw_result)
                logger.info(
                    "  [ClaudeRun/Summary] mode=%s status=%s elapsed_ms=%.1f "
                    "messages=%d warm=%s",
                    "resume" if resume_sid else "fresh",
                    "ok" if saw_result else "incomplete",
                    (time.perf_counter() - run_started) * 1000,
                    message_count,
                    warm_hit,
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
                            self._sse({"type": "error", "message": f"执行失败: {exc}"})
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

        yield "data: [DONE]\n\n"

    def _build_prompt(
        self,
        *,
        cwd: Path,
        latest_user_text: str,
        conversation_history: str,
        context: Optional[str],
        resume: bool = False,
        native_skills: bool = False,
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
        if native_skills:
            skills_section = (
                "## 技能使用\n\n"
                "可用技能已由 Claude Code 原生 Skills 机制注册。根据请求按需调用 Skill；"
                "技能正文会在命中后加载，无需先读取或枚举 SKILL.md。\n\n---\n\n"
            )
        else:
            skills_section = (
                f"## 可用技能列表\n\n{self._build_skills_index()}\n\n---\n\n"
                "## 技能使用\n\n根据最新请求选择技能，并用 Read 读取对应 SKILL.md 的绝对路径。\n\n"
                "---\n\n"
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
        return ClaudeSkillService._agent_auth_failure_message()

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
        try:
            from claude_agent_sdk.types import AssistantMessage, TextBlock
        except ImportError:
            return False
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
        try:
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
        except ImportError:
            return

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
        try:
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
        except ImportError:
            return []

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


_instance: Optional[ClaudeSkillService] = None


def get_claude_skill_service() -> Optional[ClaudeSkillService]:
    return _instance


def init_claude_skill_service(
    skills_dir: Path, workspace_root: Path
) -> ClaudeSkillService:
    global _instance
    _instance = ClaudeSkillService(skills_dir, workspace_root)
    logger.info(f"ClaudeSkillService 初始化完成 - 技能目录: {skills_dir}")
    return _instance
