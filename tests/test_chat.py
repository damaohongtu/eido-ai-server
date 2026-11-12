import asyncio
import logging
import uuid
from types import SimpleNamespace

from fastapi import Request
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)


def _unique_id() -> str:
    return uuid.uuid4().hex[:12]


class TestHealthAndRoot:

    def test_health(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.main"):
            resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert not any(record.name == "app.main" for record in caplog.records)

    def test_root(self):
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data
        assert "version" in data

    def test_trace_id_is_propagated(self):
        resp = client.get("/health", headers={"X-Trace-Id": "test-trace-123"})
        assert resp.headers["X-Trace-Id"] == "test-trace-123"


class TestSessions:

    def test_create_session(self):
        resp = client.post("/api/v1/sessions/", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"]
        assert len(data["id"]) == 12

    def test_list_sessions(self):
        resp = client.get("/api/v1/sessions/")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_session(self):
        sid = _unique_id()
        workspace = settings.workspaces_root / sid
        workspace.mkdir(parents=True, exist_ok=True)
        resp = client.get(f"/api/v1/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["id"] == sid

    def test_get_nonexistent_session(self):
        resp = client.get("/api/v1/sessions/nonexistent")
        assert resp.status_code == 404

    def test_delete_session(self):
        sid = _unique_id()
        workspace = settings.workspaces_root / sid
        workspace.mkdir(parents=True, exist_ok=True)
        resp = client.delete(f"/api/v1/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_delete_nonexistent_session(self):
        resp = client.delete("/api/v1/sessions/nonexistent")
        assert resp.status_code == 404

    def test_invalid_session_id(self):
        resp = client.get("/api/v1/sessions/invalid with spaces")
        assert resp.status_code == 400


class TestWorkspace:

    def test_list_files_missing_session(self):
        resp = client.get(
            "/api/v1/workspace/files", params={"session_id": _unique_id()}
        )
        assert resp.status_code == 200
        assert "files" in resp.json()

    def test_get_file_missing_session(self):
        resp = client.get(
            "/api/v1/workspace/file",
            params={
                "path": "test.txt",
                "session_id": _unique_id(),
            },
        )
        assert resp.status_code == 404

    def test_get_file_without_session(self):
        resp = client.get(
            "/api/v1/workspace/file",
            params={
                "path": "test.txt",
            },
        )
        assert resp.status_code == 422

    def test_delete_file_missing_session(self):
        resp = client.delete(
            "/api/v1/workspace/file",
            params={
                "path": "test.txt",
                "session_id": _unique_id(),
            },
        )
        assert resp.status_code == 404

    def test_invalid_session_for_file(self):
        resp = client.get(
            "/api/v1/workspace/file",
            params={
                "path": "test.txt",
                "session_id": "invalid spaces",
            },
        )
        assert resp.status_code == 400


class TestChatValidation:
    def test_chat_schema_accepts_all_message_roles(self):
        from app.schemas.chat import ChatRequest

        request = ChatRequest(
            session_id=_unique_id(),
            messages=[
                {"role": "system", "content": "系统规则"},
                {"role": "user", "content": "问题"},
                {"role": "assistant", "content": "历史回答"},
                {"role": "user", "content": "继续"},
            ],
        )
        assert [message.role for message in request.messages] == [
            "system",
            "user",
            "assistant",
            "user",
        ]

    def test_disconnect_during_send_closes_agent_stream(self, monkeypatch, tmp_path):
        from app.api.v1.endpoints import chat
        from app.schemas.chat import ChatRequest
        from app.services import agent_service
        from app.services.session_workspace import SessionWorkspaceManager

        closed = False

        async def execute_stream(*args, **kwargs):
            nonlocal closed
            try:
                yield 'data: {"type":"content","content":"hello"}\n\n'
            finally:
                closed = True

        monkeypatch.setattr(
            agent_service, "get_agent_service", lambda: SimpleNamespace(execute_stream=execute_stream)
        )
        workspaces = SessionWorkspaceManager(tmp_path / "workspaces")
        monkeypatch.setattr(chat, "get_session_workspace_manager", lambda: workspaces)

        async def run():
            sending_body = asyncio.Event()

            async def send(message):
                if message["type"] == "http.response.body":
                    sending_body.set()
                    await asyncio.Future()

            async def receive():
                await sending_body.wait()
                return {"type": "http.disconnect"}

            scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
            request = ChatRequest(
                messages=[{"role": "user", "content": "hello"}], session_id=_unique_id()
            )
            response = await chat.chat_completion(request, Request(scope))
            await response(scope, receive, send)
            assert closed

        asyncio.run(asyncio.wait_for(run(), timeout=5))

    def test_chat_logs_trace_and_session_ids(self, caplog, monkeypatch):
        import app.services.agent_service as agent_service

        monkeypatch.setattr(agent_service, "get_agent_service", lambda: None)
        session_id = _unique_id()
        with caplog.at_level(logging.INFO):
            resp = client.post(
                "/api/v1/chat/chat",
                headers={"X-Trace-Id": "chat-trace-123"},
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "session_id": session_id,
                },
            )
        assert resp.status_code == 503
        response_logs = [
            record
            for record in caplog.records
            if record.name == "app.main" and record.getMessage().startswith("← POST")
        ]
        assert response_logs[-1].trace_id == "chat-trace-123"
        assert response_logs[-1].session_id == session_id

    def test_chat_empty_messages(self):
        resp = client.post(
            "/api/v1/chat/chat",
            json={
                "messages": [],
                "session_id": _unique_id(),
            },
        )
        assert resp.status_code == 400

    def test_chat_missing_session_id(self):
        resp = client.post(
            "/api/v1/chat/chat",
            json={
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert resp.status_code == 422

    def test_chat_invalid_session_id(self):
        resp = client.post(
            "/api/v1/chat/chat",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "session_id": "invalid spaces",
            },
        )
        assert resp.status_code == 400

    def test_chat_health(self):
        resp = client.get("/api/v1/chat/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_upload_missing_session(self):
        resp = client.post("/api/v1/chat/upload")
        assert resp.status_code == 422
