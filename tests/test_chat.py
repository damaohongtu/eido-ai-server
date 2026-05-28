import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)


def _unique_id() -> str:
    return uuid.uuid4().hex[:12]


class TestHealthAndRoot:

    def test_health(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "version" in data

    def test_root(self):
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data
        assert "version" in data


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
        resp = client.get("/api/v1/workspace/files", params={"session_id": _unique_id()})
        assert resp.status_code == 200
        assert "files" in resp.json()

    def test_get_file_missing_session(self):
        resp = client.get("/api/v1/workspace/file", params={
            "path": "test.txt",
            "session_id": _unique_id(),
        })
        assert resp.status_code == 404

    def test_get_file_without_session(self):
        resp = client.get("/api/v1/workspace/file", params={
            "path": "test.txt",
        })
        assert resp.status_code == 422

    def test_delete_file_missing_session(self):
        resp = client.delete("/api/v1/workspace/file", params={
            "path": "test.txt",
            "session_id": _unique_id(),
        })
        assert resp.status_code == 404

    def test_invalid_session_for_file(self):
        resp = client.get("/api/v1/workspace/file", params={
            "path": "test.txt",
            "session_id": "invalid spaces",
        })
        assert resp.status_code == 400


class TestChatValidation:

    def test_chat_empty_messages(self):
        resp = client.post("/api/v1/chat/chat", json={
            "messages": [],
            "session_id": _unique_id(),
        })
        assert resp.status_code == 400

    def test_chat_missing_session_id(self):
        resp = client.post("/api/v1/chat/chat", json={
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert resp.status_code == 422

    def test_chat_invalid_session_id(self):
        resp = client.post("/api/v1/chat/chat", json={
            "messages": [{"role": "user", "content": "hello"}],
            "session_id": "invalid spaces",
        })
        assert resp.status_code == 400

    def test_chat_health(self):
        resp = client.get("/api/v1/chat/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_upload_missing_session(self):
        resp = client.post("/api/v1/chat/upload")
        assert resp.status_code == 422
