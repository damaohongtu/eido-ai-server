import asyncio
import json
import logging
import sys
import types

from app.services.claude_skill_service import ClaudeSkillService
from app.services.open_code_service import (
    OpenCodeService,
    _convert_event,
    _log_complete_output,
    _log_semantic_event,
)
from app.core.mcp_config import load_mcp_config, merge_opencode_mcp_config


def _payload(frame: str) -> dict:
    return json.loads(frame.removeprefix("data: ").strip())


def _write_skill(root, skill_id="demo", description="demo"):
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: {description}\n---\n",
        encoding="utf-8",
    )
    return skill_dir


def test_claude_skill_cache_and_native_mapping(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    source = _write_skill(skills_root)
    service = ClaudeSkillService(skills_root, tmp_path)
    original = service._load_skill
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_load_skill", counted)
    assert service.scan_skills()[0].description == "demo"
    assert service.scan_skills()[0].description == "demo"
    assert calls == 1

    session = tmp_path / "session"
    revision, count = service._materialize_native_skills(session)
    assert revision
    assert count == 1
    assert (session / ".claude" / "skills" / "demo").resolve() == source.resolve()


def test_claude_client_pool_reuses_and_resets(tmp_path, monkeypatch):
    clients = []

    class FakeClient:
        def __init__(self, options):
            self.options = options
            self.connected = False
            self.disconnected = False
            clients.append(self)

        async def connect(self):
            self.connected = True

        async def disconnect(self):
            self.disconnected = True

    fake_sdk = types.ModuleType("claude_agent_sdk")
    fake_sdk.ClaudeSDKClient = FakeClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    service = ClaudeSkillService(tmp_path / "skills", tmp_path)

    async def exercise():
        first, warm, _ = await service._acquire_client(
            options=object(), session_id="s1", signature=("revision",)
        )
        assert not warm
        await service._release_client(first, healthy=True)
        second, warm, _ = await service._acquire_client(
            options=object(), session_id="s1", signature=("revision",)
        )
        assert warm
        assert second is first
        await service._release_client(second, healthy=True)
        service.reset_session("s1")
        if service._client_cleanup_tasks:
            await next(iter(service._client_cleanup_tasks))

    asyncio.run(exercise())
    assert clients[0].disconnected


def test_claude_sdk_logging_keeps_complete_content(tmp_path, monkeypatch, caplog):
    classes = {
        name: type(name, (), {})
        for name in (
            "AssistantMessage",
            "UserMessage",
            "SystemMessage",
            "ResultMessage",
            "TextBlock",
            "ThinkingBlock",
            "ToolUseBlock",
            "ToolResultBlock",
        )
    }
    sdk_module = types.ModuleType("claude_agent_sdk")
    sdk_module.__path__ = []
    sdk_types = types.ModuleType("claude_agent_sdk.types")
    for name, value in classes.items():
        setattr(sdk_types, name, value)
    sdk_module.types = sdk_types
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk_module)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", sdk_types)

    tail = "完整日志结尾"
    block = classes["TextBlock"]()
    block.text = "x" * 9000 + tail
    message = classes["AssistantMessage"]()
    message.content = [block]
    service = ClaudeSkillService(tmp_path / "skills", tmp_path)
    with caplog.at_level(logging.INFO, logger="app.services.claude_skill_service"):
        service._log_message(message)
    messages = [
        record.getMessage()
        for record in caplog.records
        if "[Claude/Assistant/Text chunk=" in record.getMessage()
    ]
    assert len(messages) == 3
    assert tail in messages[-1]


def test_opencode_complete_logs_and_event_conversion(caplog):
    tail = "complete-tail"
    content = "x" * 9000 + "\n" + tail
    with caplog.at_level(logging.INFO, logger="app.services.open_code_service"):
        _log_complete_output("stdout", content)
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("[OpenCode/stdout chunk=")
    ]
    assert len(messages) == 3
    assert tail in messages[-1]

    event = _convert_event({"type": "text", "part": {"text": "完成"}})
    assert _payload(event[0]) == {"type": "content", "content": "完成"}


def test_opencode_semantic_tool_log_keeps_complete_output(caplog):
    tail = "semantic-tool-tail"
    event = {
        "type": "tool_use",
        "part": {
            "tool": "read",
            "state": {
                "status": "completed",
                "input": {"path": "/tmp/report.txt"},
                "output": "x" * 9000 + tail,
            },
        },
    }
    with caplog.at_level(logging.INFO, logger="app.services.open_code_service"):
        _log_semantic_event(event)
    assert any(tail in record.getMessage() for record in caplog.records)


def test_mcp_config_converts_for_claude_and_opencode(tmp_path):
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
    assert loaded.claude_servers["local"] == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "server"],
        "env": {"TOKEN": "secret"},
    }
    assert loaded.claude_servers["remote"]["type"] == "sse"
    assert loaded.opencode_servers["local"]["command"] == [
        "npx",
        "-y",
        "server",
    ]
    merged = json.loads(
        merge_opencode_mcp_config('{"share":"disabled"}', loaded.opencode_servers)
    )
    assert merged["share"] == "disabled"
    assert set(merged["mcp"]) == {"local", "remote"}


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


def test_opencode_executes_large_json_event(tmp_path):
    fake = tmp_path / "fake-opencode"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'text', 'sessionID': 'ses_1', "
        "'part': {'text': 'x' * 100_000}}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    service = OpenCodeService(tmp_path / "skills", tmp_path, binary=str(fake))

    async def collect():
        return [
            event
            async for event in service.execute_stream(
                [{"role": "user", "content": "analyze"}]
            )
        ]

    events = asyncio.run(collect())
    payloads = [_payload(event) for event in events if event.startswith("data: {")]
    content = next(payload for payload in payloads if payload.get("type") == "content")
    assert len(content["content"]) == 100_000
    assert payloads[-1]["type"] == "workflow_complete"


def test_opencode_local_mode_preserves_existing_auth_data_home(tmp_path, monkeypatch):
    from app.core.config import settings

    fake = tmp_path / "fake-opencode-env"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "print(json.dumps({'type': 'text', 'part': "
        "{'text': os.environ.get('XDG_DATA_HOME', 'missing')}}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setattr(settings, "EIDO_DATA_ROOT", "")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    service = OpenCodeService(tmp_path / "skills", tmp_path, binary=str(fake))

    async def collect():
        return [
            event
            async for event in service.execute_stream(
                [{"role": "user", "content": "analyze"}]
            )
        ]

    payloads = [
        _payload(event)
        for event in asyncio.run(collect())
        if event.startswith("data: {")
    ]
    content = next(payload for payload in payloads if payload.get("type") == "content")
    assert content["content"] == "missing"


def test_opencode_zero_token_empty_run_returns_error(tmp_path):
    fake = tmp_path / "fake-opencode-empty"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'step_finish', 'part': "
        "{'reason': 'unknown', 'tokens': {'input': 0, 'output': 0}}}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    service = OpenCodeService(tmp_path / "skills", tmp_path, binary=str(fake))

    async def collect():
        return [
            event
            async for event in service.execute_stream(
                [{"role": "user", "content": "analyze"}]
            )
        ]

    payloads = [
        _payload(event)
        for event in asyncio.run(collect())
        if event.startswith("data: {")
    ]
    assert payloads[-1]["type"] == "error"
    assert "OPENCODE_MODEL" in payloads[-1]["message"]
    assert not any(payload.get("type") == "workflow_complete" for payload in payloads)
