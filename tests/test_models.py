"""Tests for multi-model configuration and model selection in chat."""

import json

from fastapi.testclient import TestClient

from app.core.config import ModelConfig, settings
from app.main import app

client = TestClient(app)


class TestModelsEndpoint:
    def test_list_models_legacy_fallback(self):
        """Without MODELS_CONFIG, the legacy single model should be returned."""
        # Ensure no MODELS_CONFIG is set for this test
        original = settings.MODELS_CONFIG
        settings.MODELS_CONFIG = ""
        try:
            resp = client.get("/api/v1/models")
            assert resp.status_code == 200
            data = resp.json()
            assert "models" in data
            assert "default_model_id" in data
            assert len(data["models"]) >= 1
            m = data["models"][0]
            assert "id" in m
            assert "name" in m
            assert "harness" in m
            assert "is_default" in m
            assert data["default_model_id"] == m["id"]
        finally:
            settings.MODELS_CONFIG = original

    def test_list_models_with_config(self):
        """MODELS_CONFIG should produce the configured models."""
        original = settings.MODELS_CONFIG
        original_default = settings.DEFAULT_MODEL_ID
        settings.MODELS_CONFIG = json.dumps(
            [
                {
                    "id": "claude-sonnet",
                    "name": "Claude Sonnet 4.6",
                    "harness": "claude_code",
                    "model": "claude-sonnet-4-6",
                },
                {
                    "id": "glm-5",
                    "name": "GLM-5.1",
                    "harness": "opencode",
                    "model": "zai/glm-5.1",
                },
            ]
        )
        settings.DEFAULT_MODEL_ID = "claude-sonnet"
        try:
            resp = client.get("/api/v1/models")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["models"]) == 2
            ids = [m["id"] for m in data["models"]]
            assert "claude-sonnet" in ids
            assert "glm-5" in ids
            assert data["default_model_id"] == "claude-sonnet"
            # Verify secrets are not leaked
            for m in data["models"]:
                assert "api_key" not in m
                assert "auth_token" not in m
        finally:
            settings.MODELS_CONFIG = original
            settings.DEFAULT_MODEL_ID = original_default

    def test_get_model_info(self):
        original = settings.MODELS_CONFIG
        settings.MODELS_CONFIG = json.dumps(
            [
                {
                    "id": "test-model",
                    "name": "Test Model",
                    "harness": "claude_code",
                    "model": "test-model-1",
                }
            ]
        )
        try:
            resp = client.get("/api/v1/models/test-model")
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == "test-model"
            assert data["model"] == "test-model-1"
            assert "api_key" not in data
        finally:
            settings.MODELS_CONFIG = original

    def test_get_nonexistent_model(self):
        resp = client.get("/api/v1/models/nonexistent-model-xyz")
        assert resp.status_code == 404


class TestChatModelSelection:
    def test_chat_rejects_unknown_model_id(self):
        """Passing a model_id that does not exist should return 400."""
        original = settings.MODELS_CONFIG
        settings.MODELS_CONFIG = json.dumps(
            [
                {
                    "id": "real-model",
                    "name": "Real",
                    "harness": "claude_code",
                    "model": "real-model-1",
                }
            ]
        )
        try:
            resp = client.post(
                "/api/v1/chat/chat",
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "session_id": "test12345678",
                    "model_id": "nonexistent-model-xyz",
                },
            )
            assert resp.status_code == 400
            assert "未找到模型" in resp.json()["detail"]
        finally:
            settings.MODELS_CONFIG = original

    def test_chat_accepts_valid_model_id(self):
        """A valid model_id should not produce a 400/404 model error."""
        original = settings.MODELS_CONFIG
        original_default = settings.DEFAULT_MODEL_ID
        settings.MODELS_CONFIG = json.dumps(
            [
                {
                    "id": "claude-test",
                    "name": "Claude Test",
                    "harness": "claude_code",
                    "model": "claude-sonnet-4-6",
                }
            ]
        )
        settings.DEFAULT_MODEL_ID = "claude-test"
        try:
            # The request should pass validation. It may fail later due to
            # missing credentials, but it should not fail with a model error.
            resp = client.post(
                "/api/v1/chat/chat",
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "session_id": "test12345678",
                    "model_id": "claude-test",
                },
            )
            # Should not be a 400 model-not-found error
            assert resp.status_code != 400 or "未找到模型" not in resp.json().get(
                "detail", ""
            )
        finally:
            settings.MODELS_CONFIG = original
            settings.DEFAULT_MODEL_ID = original_default

    def test_chat_model_id_overrides_harness(self):
        """When model_id is set, its harness should be used, not the request harness."""
        original = settings.MODELS_CONFIG
        original_default = settings.DEFAULT_MODEL_ID
        settings.MODELS_CONFIG = json.dumps(
            [
                {
                    "id": "claude-model",
                    "name": "Claude",
                    "harness": "claude_code",
                    "model": "claude-sonnet-4-6",
                }
            ]
        )
        settings.DEFAULT_MODEL_ID = "claude-model"
        try:
            resp = client.post(
                "/api/v1/chat/chat",
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "session_id": "test12345678",
                    "model_id": "claude-model",
                    "harness": "unknown_harness",
                },
            )
            # The model's harness (claude_code) should be used, so the
            # "不支持的 AI 后端" error should NOT appear.
            assert resp.status_code != 400 or "不支持的 AI 后端" not in resp.json().get(
                "detail", ""
            )
        finally:
            settings.MODELS_CONFIG = original
            settings.DEFAULT_MODEL_ID = original_default


class TestModelConfig:
    def test_claude_env_includes_model_fields(self):
        m = ModelConfig(
            id="test",
            base_url="https://example.com",
            api_key="sk-test",
            model="test-model",
            small_fast_model="fast-model",
        )
        env = m.claude_env()
        assert env["ANTHROPIC_BASE_URL"] == "https://example.com"
        assert env["ANTHROPIC_MODEL"] == "test-model"
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "fast-model"
        assert env["ANTHROPIC_API_KEY"] == "sk-test"

    def test_claude_env_omits_empty_fields(self):
        m = ModelConfig(id="test", model="test-model")
        env = m.claude_env()
        assert "ANTHROPIC_BASE_URL" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert env["ANTHROPIC_MODEL"] == "test-model"

    def test_get_model_returns_default(self):
        original = settings.MODELS_CONFIG
        original_default = settings.DEFAULT_MODEL_ID
        settings.MODELS_CONFIG = json.dumps(
            [
                {"id": "first", "harness": "claude_code", "model": "m1"},
                {"id": "second", "harness": "opencode", "model": "m2"},
            ]
        )
        settings.DEFAULT_MODEL_ID = "second"
        try:
            m = settings.get_model(None)
            assert m is not None
            assert m.id == "second"

            m = settings.get_model("")
            assert m is not None
            assert m.id == "second"

            m = settings.get_model("first")
            assert m is not None
            assert m.id == "first"

            m = settings.get_model("nonexistent")
            assert m is None
        finally:
            settings.MODELS_CONFIG = original
            settings.DEFAULT_MODEL_ID = original_default
