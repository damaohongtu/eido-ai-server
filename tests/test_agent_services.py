import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
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
from pydantic import SecretStr

import app.services.agent_service as agent_service
import app.services.session_workspace as session_workspace
from app.core.config import settings
from app.core.mcp_config import load_mcp_config
from app.services.agent_service import (
    AgentService,
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


class _FakeClient:
    """替代 ClaudeSDKClient：按脚本回放 connect/query/receive_response。"""

    def __init__(self, options: ClaudeAgentOptions, spec: dict):
        self.options = options
        self.prompts: list[str] = []
        self.connect_error = spec.get("connect_error")
        self.query_errors = list(spec.get("query_errors", []))
        self.turns: list = list(spec.get("turns", []))
        self.disconnected = False

    async def connect(self):
        if self.connect_error:
            raise self.connect_error

    async def query(self, prompt):
        if self.query_errors:
            error = self.query_errors.pop(0)
            if error is not None:
                raise error
        self.prompts.append(prompt)

    async def receive_response(self):
        if not self.turns:
            return
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if callable(turn):
            async for message in turn():
                yield message
            return
        for message in turn:
            yield message

    async def disconnect(self):
        self.disconnected = True


def _install_fake_clients(monkeypatch, specs: list[dict]) -> list[_FakeClient]:
    clients: list[_FakeClient] = []

    def factory(options):
        spec = specs[len(clients)] if len(clients) < len(specs) else {}
        client = _FakeClient(options, spec)
        clients.append(client)
        return client

    monkeypatch.setattr(agent_service, "ClaudeSDKClient", factory)
    return clients


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


def test_extract_latest_user_text():
    msgs = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "  第二问  "},
    ]
    assert extract_latest_user_text(msgs) == "第二问"


def test_build_prompt_passthrough_with_context(tmp_path):
    service = AgentService(tmp_path / "skills", tmp_path)

    plain = service._build_prompt(latest_user_text="分析财报", context=None)
    assert plain == "分析财报"

    with_context = service._build_prompt(
        latest_user_text="分析财报", context="上一步输出"
    )
    assert with_context.startswith("分析财报")
    assert "上一步输出" in with_context

    long_context = "x" * 9000
    untruncated = service._build_prompt(
        latest_user_text="q", context=long_context
    )
    assert "x" * 9000 in untruncated


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
    clients = _install_fake_clients(
        monkeypatch,
        [
            {
                "turns": [
                    [
                        SystemMessage(subtype="init", data={"tools": ["Bash"]}),
                        AssistantMessage(
                            content=[TextBlock(text="分析完成")], model="test"
                        ),
                        _result(session_id="cs_run_1"),
                    ]
                ]
            }
        ],
    )

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
    assert service._sessions["sess_ok"].sid == "cs_run_1"
    assert clients[0].options.resume is None
    assert clients[0].prompts == ["分析数据"]
    assert Path(clients[0].options.cwd) == tmp_path / "workspaces" / "sess_ok"
    assert "Skill" in clients[0].options.tools
    assert "Skill" not in clients[0].options.allowed_tools
    assert clients[0].options.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert service._sessions["sess_ok"].client is clients[0]
    assert not clients[0].disconnected
    ws_link = tmp_path / "workspaces" / "sess_ok" / ".claude" / "skills"
    assert ws_link.is_symlink()
    assert service._sessions["sess_ok"].turn is None


def test_execute_stream_reuses_client_on_second_turn(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    clients = _install_fake_clients(
        monkeypatch,
        [
            {
                "turns": [
                    [
                        AssistantMessage(content=[TextBlock(text="第一答")], model="test"),
                        _result(session_id="cs_persist"),
                    ],
                    [
                        AssistantMessage(content=[TextBlock(text="第二答")], model="test"),
                        _result(session_id="cs_persist"),
                    ],
                ]
            },
        ],
    )

    first = asyncio.run(
        _collect(
            service.execute_stream(
                [{"role": "user", "content": "第一问"}], session_id="sess_reuse"
            )
        )
    )
    second = asyncio.run(
        _collect(
            service.execute_stream(
                [{"role": "user", "content": "第二问"}], session_id="sess_reuse"
            )
        )
    )
    first_payloads = [_payload(e) for e in first if e.startswith("data: {")]
    second_payloads = [_payload(e) for e in second if e.startswith("data: {")]
    assert {"type": "content", "content": "第一答"} in first_payloads
    assert {"type": "content", "content": "第二答"} in second_payloads
    assert second_payloads[-1]["type"] == "workflow_complete"
    assert len(clients) == 1, "第二轮应复用常驻客户端，不再新建进程"
    assert clients[0].prompts == ["第一问", "第二问"]


def test_execute_stream_resume_falls_back_to_fresh(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    service._sessions["sess_resume"] = agent_service._Session(sid="cs_stale")
    error = "No conversation found with session ID: cs_stale"
    clients = _install_fake_clients(
        monkeypatch,
        [
            {"connect_error": ResultError(error, data={"errors": [error]})},
            {
                "turns": [
                    [
                        AssistantMessage(
                            content=[TextBlock(text="重建成功")], model="test"
                        ),
                        _result(session_id="cs_new"),
                    ]
                ]
            },
        ],
    )

    events = asyncio.run(
        _collect(
            service.execute_stream(
                [{"role": "user", "content": "继续"}],
                session_id="sess_resume",
            )
        )
    )
    payloads = [_payload(e) for e in events if e.startswith("data: {")]
    assert [c.options.resume for c in clients] == ["cs_stale", None]
    assert any("原会话已失效" in p["content"] for p in payloads if p["type"] == "thinking")
    assert {"type": "content", "content": "重建成功"} in payloads
    assert payloads[-1]["type"] == "workflow_complete"
    assert service._sessions["sess_resume"].sid == "cs_new"
    assert service._sessions["sess_resume"].client is clients[1]


def test_execute_stream_recovers_dead_client_with_resume(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    clients = _install_fake_clients(
        monkeypatch,
        [
            {
                "turns": [
                    [
                        AssistantMessage(content=[TextBlock(text="第一答")], model="test"),
                        _result(session_id="cs_alive"),
                    ],
                ],
                "query_errors": [None, CLIConnectionError("Cannot write to terminated process")],
            },
            {
                "turns": [
                    [
                        AssistantMessage(
                            content=[TextBlock(text="重建后作答")], model="test"
                        ),
                        _result(session_id="cs_alive"),
                    ]
                ]
            },
        ],
    )

    first = asyncio.run(
        _collect(
            service.execute_stream(
                [{"role": "user", "content": "第一问"}], session_id="sess_dead"
            )
        )
    )
    second = asyncio.run(
        _collect(
            service.execute_stream(
                [{"role": "user", "content": "第二问"}], session_id="sess_dead"
            )
        )
    )
    first_payloads = [_payload(e) for e in first if e.startswith("data: {")]
    second_payloads = [_payload(e) for e in second if e.startswith("data: {")]
    assert {"type": "content", "content": "第一答"} in first_payloads
    assert {"type": "content", "content": "重建后作答"} in second_payloads
    assert second_payloads[-1]["type"] == "workflow_complete"
    assert clients[0].disconnected, "死亡客户端应被断开回收"
    assert clients[1].options.resume == "cs_alive", "重建应带 sid 保留上下文"
    assert service._sessions["sess_dead"].client is clients[1]


def test_execute_stream_error_suppresses_workflow_complete(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    clients = _install_fake_clients(
        monkeypatch,
        [{"turns": [RuntimeError("agent crashed")]}],
    )

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
    assert service._sessions["sess_err"].turn is None
    assert service._sessions["sess_err"].client is None
    assert clients[0].disconnected


def test_busy_session_survives_idle_sweep_and_can_be_deleted(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "CLIENT_IDLE_TTL_SEC", 0)
    monkeypatch.setattr(agent_service, "HEARTBEAT_INTERVAL_SEC", 0.01)

    async def run():
        started = asyncio.Event()
        heartbeat = asyncio.Event()

        async def blocked_response():
            started.set()
            await asyncio.Future()
            yield _result()

        clients = _install_fake_clients(
            monkeypatch,
            [{"turns": [blocked_response]}, {"turns": [[_result("cs_other")]]}],
        )

        async def ask(session_id):
            frames = []
            async for frame in service.execute_stream(
                [{"role": "user", "content": "hi"}], session_id=session_id
            ):
                frames.append(frame)
                if frame == ": ping\n\n":
                    heartbeat.set()
            return frames

        active = asyncio.create_task(ask("sess_busy"))
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(heartbeat.wait(), 2)
        rejected = await ask("sess_busy")
        assert "正在执行中" in _payload(rejected[-2])["message"]

        await ask("sess_other")
        assert not clients[0].disconnected

        assert await service.delete_session("sess_busy")
        frames = await asyncio.wait_for(active, 2)
        assert _payload(frames[-2]) == {"type": "error", "message": "会话执行已取消"}
        assert frames[-1] == "data: [DONE]\n\n"
        assert clients[0].disconnected
        assert "sess_busy" not in service._sessions
        assert not (tmp_path / "workspaces" / "sess_busy").exists()
        await service.aclose()

    asyncio.run(run())


def test_execute_stream_requires_successful_sdk_result(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    clients = _install_fake_clients(
        monkeypatch,
        [
            {"turns": [ClaudeSDKError("CLI exited after dispatch")]},
            {"turns": [[]]},
            {"turns": [[replace(_result(), is_error=True, errors=["provider error"])]]},
        ],
    )

    async def run():
        for index, expected_error in enumerate(
            ("CLI exited after dispatch", "未收到执行结果", "provider error")
        ):
            frames = await _collect(
                service.execute_stream(
                    [{"role": "user", "content": "hi"}], session_id=f"sess_err_{index}"
                )
            )
            assert _payload(frames[-2])["type"] == "error"
            assert expected_error in _payload(frames[-2])["message"]
            assert frames[-1] == "data: [DONE]\n\n"
            assert all(
                _payload(frame)["type"] != "workflow_complete"
                for frame in frames[:-1]
            )
            assert len(clients) == index + 1, "请求提交后的失败不能自动重放"
            assert clients[index].disconnected

    asyncio.run(run())


def test_execute_stream_reuse_skips_config_load(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    real_load = agent_service.load_mcp_config
    calls = {"load": 0}

    def counting_load(path, environment=None):
        calls["load"] += 1
        return real_load(path, environment=environment)

    monkeypatch.setattr(agent_service, "load_mcp_config", counting_load)
    clients = _install_fake_clients(
        monkeypatch,
        [
            {
                "turns": [
                    [
                        AssistantMessage(content=[TextBlock(text="第一答")], model="test"),
                        _result(session_id="cs_cfg"),
                    ],
                    [
                        AssistantMessage(content=[TextBlock(text="第二答")], model="test"),
                        _result(session_id="cs_cfg"),
                    ],
                ]
            },
        ],
    )

    for question in ("第一问", "第二问"):
        events = asyncio.run(
            _collect(
                service.execute_stream(
                    [{"role": "user", "content": question}],
                    session_id="sess_cfg",
                )
            )
        )
        payloads = [_payload(e) for e in events if e.startswith("data: {")]
        assert payloads[-1]["type"] == "workflow_complete"

    assert calls["load"] == 1, "复用常驻客户端的轮次不应重新加载 MCP 配置"
    assert len(clients) == 1


def test_execute_stream_mcp_config_error_reports_frame(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        '{"mcpServers":{"remote":{"type":"http","url":"https://example.test/mcp",'
        '"headers":{"Authorization":"{env:MISSING_MCP_TOKEN}"}}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "MCP_CONFIG_PATH", str(config_path))
    clients = _install_fake_clients(monkeypatch, [{}])

    events = asyncio.run(
        _collect(
            service.execute_stream(
                [{"role": "user", "content": "hi"}],
                session_id="sess_mcp_err",
            )
        )
    )
    payloads = [_payload(e) for e in events if e.startswith("data: {")]
    assert payloads[-1]["type"] == "error"
    assert "MCP 配置错误" in payloads[-1]["message"]
    assert "MISSING_MCP_TOKEN" in payloads[-1]["message"]
    assert not any(p["type"] == "workflow_complete" for p in payloads)
    assert clients == [], "配置加载失败不应创建客户端"


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
