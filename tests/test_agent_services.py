import asyncio
import json
import logging
import os
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk import ClaudeSDKError
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
from pydantic import SecretStr

import app.services.session_workspace as session_workspace
from app.core.config import settings
from app.core.mcp_config import load_mcp_config
from app.services.agent_service import (
    AgentService,
    build_conversation_history_before_latest_user,
    extract_latest_user_text,
)
from app.services.session_workspace import SessionWorkspaceManager


def _payload(frame: str) -> dict:
    return json.loads(frame.removeprefix("data: ").strip())


def _result(session_id: str = "cs_1") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1200,
        duration_api_ms=900,
        is_error=False,
        num_turns=2,
        session_id=session_id,
    )


def _make_service(tmp_path: Path, monkeypatch) -> AgentService:
    monkeypatch.setattr(settings, "MCP_CONFIG_PATH", "")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", SecretStr("test-key"))
    monkeypatch.setattr(
        session_workspace,
        "_instance",
        SessionWorkspaceManager(root=tmp_path / "workspaces"),
    )
    return AgentService(tmp_path / "skills", tmp_path)


async def _collect(gen) -> list[str]:
    return [event async for event in gen]


def test_native_skills_link_is_idempotent_and_repairing(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "demo").mkdir()
    (skills_root / "demo" / "SKILL.md").write_text("demo", encoding="utf-8")
    service = AgentService(skills_root, tmp_path)
    cwd = tmp_path / "session"
    cwd.mkdir()

    service._ensure_native_skills_link(cwd)
    link = cwd / ".claude" / "skills"
    assert link.is_symlink()
    assert link.resolve() == skills_root.resolve()

    service._ensure_native_skills_link(cwd)
    assert link.resolve() == skills_root.resolve()

    link.unlink()
    (tmp_path / "other").mkdir()
    link.symlink_to(tmp_path / "other", target_is_directory=True)
    service._ensure_native_skills_link(cwd)
    assert link.resolve() == skills_root.resolve()

    unmanaged = tmp_path / "session2" / ".claude" / "skills"
    unmanaged.mkdir(parents=True)
    service._ensure_native_skills_link(unmanaged.parent.parent)
    assert not unmanaged.is_symlink()


def test_extract_latest_user_text_and_history():
    msgs = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "  第二问  "},
    ]
    assert extract_latest_user_text(msgs) == "第二问"
    history = build_conversation_history_before_latest_user(msgs)
    assert "**user**: 第一问" in history
    assert "第二问" not in history


def test_build_prompt_fresh_and_resume(tmp_path):
    service = AgentService(tmp_path / "skills", tmp_path)
    cwd = tmp_path / "ws"

    fresh = service._build_prompt(
        cwd=cwd,
        latest_user_text="分析财报",
        conversation_history="**user**: 上一轮",
        context="上一步输出",
    )
    assert str(cwd / "outputs") in fresh
    assert "技能" in fresh
    assert "上一轮" in fresh
    assert "分析财报" in fresh
    assert "上一步输出" in fresh

    resumed = service._build_prompt(
        cwd=cwd,
        latest_user_text="继续",
        conversation_history="",
        context=None,
        resume=True,
    )
    assert resumed.strip().startswith("## 用户最新请求")
    assert "继续" in resumed

    long_context = "x" * 5000
    truncated = service._build_prompt(
        cwd=cwd,
        latest_user_text="q",
        conversation_history="",
        context=long_context,
        resume=True,
    )
    assert "x" * 4000 in truncated
    assert "x" * 4001 not in truncated


def test_claude_sdk_logging_keeps_complete_content(tmp_path, caplog):
    service = AgentService(tmp_path / "skills", tmp_path)
    tail = "完整日志结尾"
    message = AssistantMessage(
        content=[TextBlock(text="x" * 9000 + tail)], model="test"
    )
    with caplog.at_level(logging.INFO, logger="app.services.agent_service"):
        service._log_message(message)
    messages = [
        record.getMessage()
        for record in caplog.records
        if "[Claude/Assistant/Text chunk=" in record.getMessage()
    ]
    assert len(messages) == 3
    assert tail in messages[-1]


def test_convert_message_emits_protocol_events(tmp_path):
    service = AgentService(tmp_path / "skills", tmp_path)

    assistant = AssistantMessage(
        content=[
            TextBlock(text="回答正文"),
            ThinkingBlock(thinking="推理" * 200, signature="sig"),
            ToolUseBlock(id="t1", name="Read", input={"file_path": "/tmp/a.md"}),
        ],
        model="test",
    )
    events = [_payload(e) for e in service._convert_message(assistant)]
    assert {"type": "content", "content": "回答正文"} in events
    assert any(
        e["type"] == "thinking" and e["content"].startswith("[深度思考]") for e in events
    )
    assert any("读取文件" in e["content"] for e in events)

    tool_msg = UserMessage(
        content=[
            ToolResultBlock(tool_use_id="t1", content="结果预览", is_error=False),
            ToolResultBlock(tool_use_id="t2", content="boom", is_error=True),
        ]
    )
    events = [_payload(e) for e in service._convert_message(tool_msg)]
    assert any("✓ 工具完成" in e["content"] for e in events)
    assert any("✗ 工具出错" in e["content"] for e in events)

    init = SystemMessage(subtype="init", data={"tools": ["Bash", "Read"]})
    events = [_payload(e) for e in service._convert_message(init)]
    assert any("已加载工具" in e["content"] for e in events)

    result_events = [_payload(e) for e in service._convert_message(_result())]
    assert any("执行完成" in e["content"] for e in result_events)


def test_mcp_config_loads_and_resolves(tmp_path):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local": {
                        "command": "npx",
                        "args": ["-y", "server"],
                        "env": {"TOKEN": "{env:MCP_TOKEN}"},
                    },
                    "remote": {
                        "type": "sse",
                        "url": "https://example.test/sse",
                        "headers": {"Authorization": "${MCP_AUTH}"},
                    },
                    "off": {"command": "ignored", "disabled": True},
                }
            }
        ),
        encoding="utf-8",
    )
    loaded = load_mcp_config(
        config_path,
        environment={"MCP_TOKEN": "secret", "MCP_AUTH": "Bearer token"},
    )
    assert loaded.server_names == ("local", "remote")
    assert loaded.servers["local"] == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "server"],
        "env": {"TOKEN": "secret"},
    }
    assert loaded.servers["remote"]["type"] == "sse"

    empty = load_mcp_config("")
    assert empty.servers == {}
    assert empty.server_names == ()


def test_mcp_config_rejects_missing_environment(tmp_path):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        '{"mcpServers":{"remote":{"type":"http","url":"https://example.test/mcp","headers":{"Authorization":"{env:MISSING_MCP_TOKEN}"}}}}',
        encoding="utf-8",
    )
    try:
        load_mcp_config(config_path, environment={})
    except ValueError as exc:
        assert "MISSING_MCP_TOKEN" in str(exc)
    else:
        raise AssertionError("missing MCP environment variable was accepted")


def test_execute_stream_success_flow(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    calls = []

    def fake_query(prompt, options):
        async def gen():
            calls.append((prompt, options))
            yield SystemMessage(subtype="init", data={"tools": ["Bash"]})
            yield AssistantMessage(
                content=[TextBlock(text="分析完成")], model="test"
            )
            yield _result(session_id="cs_run_1")

        return gen()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    events = asyncio.run(
        _collect(
            service.execute_stream(
                [{"role": "user", "content": "分析数据"}],
                session_id="sess_ok",
            )
        )
    )
    payloads = [_payload(e) for e in events if e.startswith("data: {")]
    assert events[0].startswith("data: ")
    assert payloads[0]["type"] == "thinking"
    assert {"type": "workflow_start", "skill_name": "auto"} in payloads
    assert {"type": "content", "content": "分析完成"} in payloads
    assert payloads[-1] == {"type": "workflow_complete", "data": {"references": []}}
    assert events[-1] == "data: [DONE]\n\n"
    assert service._claude_session_ids["sess_ok"] == "cs_run_1"
    assert calls[0][1].resume is None
    assert "Skill" in calls[0][1].tools
    ws_link = tmp_path / "workspaces" / "sess_ok" / ".claude" / "skills"
    assert ws_link.is_symlink()
    assert service._busy_sessions == set()


def test_execute_stream_resume_falls_back_to_fresh(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    service._claude_session_ids["sess_resume"] = "cs_stale"
    attempts = []

    def fake_query(prompt, options):
        async def gen():
            attempts.append(options.resume)
            if options.resume == "cs_stale":
                raise ClaudeSDKError("resume failed")
            yield AssistantMessage(content=[TextBlock(text="重建成功")], model="test")
            yield _result(session_id="cs_new")

        return gen()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    events = asyncio.run(
        _collect(
            service.execute_stream(
                [{"role": "user", "content": "继续"}],
                session_id="sess_resume",
            )
        )
    )
    payloads = [_payload(e) for e in events if e.startswith("data: {")]
    assert attempts == ["cs_stale", None]
    assert any("原会话已失效" in p["content"] for p in payloads if p["type"] == "thinking")
    assert {"type": "content", "content": "重建成功"} in payloads
    assert payloads[-1]["type"] == "workflow_complete"
    assert service._claude_session_ids["sess_resume"] == "cs_new"


def test_execute_stream_error_suppresses_workflow_complete(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)

    def fake_query(prompt, options):
        async def gen():
            raise RuntimeError("agent crashed")
            yield  # pragma: no cover

        return gen()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    events = asyncio.run(
        _collect(
            service.execute_stream(
                [{"role": "user", "content": "hi"}],
                session_id="sess_err",
            )
        )
    )
    payloads = [_payload(e) for e in events if e.startswith("data: {")]
    assert payloads[-1]["type"] == "error"
    assert "执行失败" in payloads[-1]["message"]
    assert not any(p["type"] == "workflow_complete" for p in payloads)
    assert events[-1] == "data: [DONE]\n\n"
    assert service._busy_sessions == set()


def test_execute_stream_busy_session_rejected(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    service._busy_sessions.add("sess_busy")

    events = asyncio.run(
        _collect(
            service.execute_stream(
                [{"role": "user", "content": "hi"}],
                session_id="sess_busy",
            )
        )
    )
    payloads = [_payload(e) for e in events if e.startswith("data: {")]
    assert payloads[-1]["type"] == "error"
    assert "正在执行中" in payloads[-1]["message"]
    assert events[-1] == "data: [DONE]\n\n"
    assert "sess_busy" in service._busy_sessions


def test_execute_stream_missing_auth_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MCP_CONFIG_PATH", "")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", SecretStr(""))
    monkeypatch.setattr(settings, "ANTHROPIC_AUTH_TOKEN", SecretStr(""))
    monkeypatch.setattr(
        session_workspace,
        "_instance",
        SessionWorkspaceManager(root=tmp_path / "workspaces"),
    )
    service = AgentService(tmp_path / "skills", tmp_path)

    events = asyncio.run(
        _collect(
            service.execute_stream(
                [{"role": "user", "content": "hi"}],
                session_id="sess_noauth",
            )
        )
    )
    payloads = [_payload(e) for e in events if e.startswith("data: {")]
    assert payloads[-1]["type"] == "error"
    assert "凭据" in payloads[-1]["message"]
