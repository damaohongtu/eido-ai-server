import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_TOOLS = ["Bash", "Glob", "Read", "WebFetch"]

HEARTBEAT_INTERVAL_SEC = 12.0
_HEARTBEAT_FRAME = ": ping\n\n"


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content

    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    fm_text = content[3:end].strip()
    body = content[end + 4:].lstrip("\n")

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
        c = (
            getattr(m, "content", None)
            if not isinstance(m, dict)
            else m.get("content")
        )
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


class ClaudeSkillService:

    AUTO_ALLOWED_TOOLS = ["Bash", "Glob", "Read", "Write", "Edit", "WebFetch"]

    def __init__(self, skills_dir: Path, workspace_root: Path):
        self.skills_dir = skills_dir
        self.workspace_root = workspace_root

    def scan_skills(self) -> List[SkillMeta]:
        skills: List[SkillMeta] = []
        if not self.skills_dir.exists():
            return skills
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
        logger.info("扫描到 %d 个技能", len(skills))
        return skills

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

        raw_tools = meta.get("allowed_tools")
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

    async def execute_stream(
        self, messages: list, context: Optional[str] = None,
        *, session_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        logger.info(
            f"▶ execute_stream 开始 | 消息数: {len(messages)}"
            + (f" | session={session_id}" if session_id else "")
            + (f" | 含上下文 {len(context)} 字符" if context else "")
        )

        yield self._sse({"type": "thinking", "content": "正在分析请求，自动规划执行..."})
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

        try:
            from claude_agent_sdk import query, ClaudeAgentOptions
        except ImportError:
            logger.error("claude_agent_sdk 未安装")
            yield self._sse({
                "type": "error",
                "message": "claude_agent_sdk 未安装"
            })
            yield "data: [DONE]\n\n"
            return

        latest_user_text = extract_latest_user_text(messages)
        if not latest_user_text:
            yield self._sse({"type": "error", "message": "未找到用户输入"})
            yield "data: [DONE]\n\n"
            return

        conversation_history = build_conversation_history(messages)
        prompt = self._build_prompt(
            cwd=cwd,
            latest_user_text=latest_user_text,
            conversation_history=conversation_history,
            context=context,
        )
        options = ClaudeAgentOptions(
            allowed_tools=self.AUTO_ALLOWED_TOOLS,
            cwd=str(cwd),
            setting_sources=["project"],
            permission_mode="acceptEdits",
            include_partial_messages=True,
            max_buffer_size=10 * 1024 * 1024,
        )
        logger.info(
            f"  工具集={self.AUTO_ALLOWED_TOOLS} "
            f"| prompt={len(prompt)}B | cwd={cwd}"
        )

        async def _run_once() -> AsyncGenerator[str, None]:
            async for message in query(prompt=prompt, options=options):
                self._log_message(message)
                for event in self._convert_message(message):
                    yield event

        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()
        had_error = {"v": False}

        async def producer() -> None:
            try:
                async for ev in _run_once():
                    await queue.put(ev)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"技能自动执行失败: {e}", exc_info=True)
                had_error["v"] = True
                await queue.put(self._sse({"type": "error", "message": f"执行失败: {e}"}))
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
                yield self._sse({"type": "workflow_complete", "data": {"references": []}})
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
    ) -> str:
        context_section = ""
        if context and context.strip():
            truncated = context.strip()[:4000]
            context_section = (
                f"\n\n---\n\n## 上一步执行结果（供参考）\n\n{truncated}\n"
            )

        skills_index = self._build_skills_index()
        skills_root_abs = Path(self.skills_dir).resolve()
        workspace_section = (
            f"**当前会话工作区（你的 cwd）**: `{cwd}`\n"
            f"  - 用户上传文件位于: `{cwd / 'uploads'}`\n"
            f"  - 你生成的所有产物请写入: `{cwd / 'outputs'}`\n"
            f"**技能库根目录（绝对路径，仅可读取）**: `{skills_root_abs}`\n"
        )
        history_section = ""
        if conversation_history:
            history_section = (
                f"\n\n## 对话历史\n\n{conversation_history}\n\n---\n\n"
            )
        return (
            f"{workspace_section}\n"
            f"## 可用技能列表\n\n{skills_index}\n\n"
            f"---\n\n"
            f"## 执行说明\n\n"
            f"请根据用户的最新请求，判断需要使用哪个技能（必要时可组合多个技能），"
            f"使用 Read 工具读取对应 SKILL.md 的**绝对路径**，"
            f"然后严格按照技能说明完成任务。\n"
            f"- 所有写文件操作请落在 `{cwd / 'outputs'}` 目录下；不要写到工作区之外。\n"
            f"- 用户上传文件已在消息中提供绝对路径，可直接 Read。\n\n"
            f"---\n"
            f"{history_section}"
            f"## 用户最新请求\n\n{latest_user_text}"
            f"{context_section}"
        )

    def _log_message(self, message: object) -> None:
        try:
            from claude_agent_sdk.types import (
                AssistantMessage, UserMessage, SystemMessage, ResultMessage,
                TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
            )
        except ImportError:
            return

        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    preview = block.text[:120].replace("\n", " ")
                    logger.info(f"  [Assistant/Text] {preview}{'…' if len(block.text) > 120 else ''}")
                elif isinstance(block, ThinkingBlock):
                    preview = block.thinking[:120].replace("\n", " ")
                    logger.debug(f"  [Assistant/Thinking] {preview}…")
                elif isinstance(block, ToolUseBlock):
                    logger.info(f"  [Tool/Call] {block.name} | 参数: {str(block.input)[:120]}")

        elif isinstance(message, UserMessage):
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        raw = block.content
                        content_str = raw if isinstance(raw, str) else str(raw or "")
                        preview = content_str[:120].replace("\n", " ")
                        status = "ERROR" if block.is_error else "OK"
                        logger.info(f"  [Tool/Result:{status}] {preview}{'…' if len(content_str) > 120 else ''}")

        elif isinstance(message, SystemMessage):
            logger.info(f"  [System/{message.subtype}] {str(message.data)[:120]}")

        elif isinstance(message, ResultMessage):
            cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "N/A"
            duration = f"{message.duration_ms / 1000:.1f}s"
            status = "ERROR" if message.is_error else "OK"
            logger.info(
                f"  [Result/{status}] 用时={duration} | 费用={cost} | "
                f"轮次={message.num_turns} | session={message.session_id}"
            )

    def _convert_message(self, message: object) -> List[str]:
        try:
            from claude_agent_sdk.types import (
                AssistantMessage, UserMessage, SystemMessage, ResultMessage,
                TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
            )
        except ImportError:
            return []

        events: List[str] = []

        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    if block.text:
                        events.append(self._sse({"type": "content", "content": block.text}))
                elif isinstance(block, ThinkingBlock):
                    preview = block.thinking[:300].strip()
                    if preview:
                        events.append(self._sse({
                            "type": "thinking",
                            "content": f"[深度思考] {preview}{'…' if len(block.thinking) > 300 else ''}"
                        }))
                elif isinstance(block, ToolUseBlock):
                    hint = self._tool_hint(block.name, block.input)
                    events.append(self._sse({"type": "thinking", "content": hint}))

        elif isinstance(message, UserMessage):
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        raw = block.content
                        content_str = raw if isinstance(raw, str) else (
                            str(raw) if raw is not None else ""
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
                    events.append(self._sse({
                        "type": "thinking",
                        "content": f"已加载工具: {tool_list}"
                    }))

        elif isinstance(message, ResultMessage):
            cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "N/A"
            duration = f"{message.duration_ms / 1000:.1f}s"
            events.append(self._sse({
                "type": "thinking",
                "content": (
                    f"执行完成 | 用时: {duration} | "
                    f"费用: {cost} | 轮次: {message.num_turns}"
                    + (" | ⚠️ 出错" if message.is_error else "")
                )
            }))

        return events

    def _tool_hint(self, tool_name: str, tool_input: dict) -> str:
        hints = {
            "Read":      lambda i: f"读取文件: {i.get('file_path', '')}",
            "Bash":      lambda i: f"执行命令: {str(i.get('command', ''))[:120]}",
            "Glob":      lambda i: f"查找文件: {i.get('pattern', '')}",
            "WebFetch":  lambda i: f"获取网页: {i.get('url', '')}",
            "WebSearch": lambda i: f"搜索: {i.get('query', '')}",
            "Write":     lambda i: f"写入文件: {i.get('file_path', '')}",
            "Edit":      lambda i: f"编辑文件: {i.get('file_path', '')}",
            "Grep":      lambda i: f"搜索内容: {i.get('pattern', '')}",
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
