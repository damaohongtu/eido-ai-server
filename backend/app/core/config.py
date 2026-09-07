import json
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_WORKSPACE_ROOT = Path(__file__).parents[3]


class ModelConfig(BaseModel):
    """A single configurable model, resolved from MODELS_CONFIG or legacy env."""

    id: str
    name: str = ""
    harness: str = "claude_code"
    base_url: str = ""
    api_key: SecretStr = SecretStr("")
    auth_token: SecretStr = SecretStr("")
    model: str = ""
    small_fast_model: str = ""

    def display_name(self) -> str:
        return self.name or self.id

    def claude_env(self) -> dict[str, str]:
        """Build the Claude Code subprocess env vars for this model."""
        env: dict[str, str] = {}
        for key, value in {
            "ANTHROPIC_BASE_URL": self.base_url,
            "ANTHROPIC_MODEL": self.model,
            "ANTHROPIC_SMALL_FAST_MODEL": self.small_fast_model,
        }.items():
            if value.strip():
                env[key] = value.strip()
        for key, value in {
            "ANTHROPIC_API_KEY": self.api_key,
            "ANTHROPIC_AUTH_TOKEN": self.auth_token,
        }.items():
            raw = value.get_secret_value().strip()
            if raw:
                env[key] = raw
        return env


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="ignore"
    )

    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "Eido AI Server"
    VERSION: str = "1.0.0"

    BACKEND_CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ]

    DEBUG: bool = False

    SKILLS_DIR: str = str(_WORKSPACE_ROOT / ".claude" / "skills")
    WORKSPACE_ROOT: str = str(_WORKSPACE_ROOT)

    EIDO_DATA_ROOT: str = ""

    AGENT_HARNESS: str = "claude_code"

    ANTHROPIC_BASE_URL: str = ""
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")
    ANTHROPIC_AUTH_TOKEN: SecretStr = SecretStr("")
    ANTHROPIC_MODEL: str = ""
    ANTHROPIC_SMALL_FAST_MODEL: str = ""
    API_TIMEOUT_MS: int = Field(default=300000, gt=0)
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: bool = False
    CLAUDE_CODE_USE_BEDROCK: bool = False
    CLAUDE_CODE_USE_ANTHROPIC_AWS: bool = False
    CLAUDE_CODE_USE_VERTEX: bool = False
    CLAUDE_CODE_USE_FOUNDRY: bool = False

    OPENCODE_MODEL: str = ""
    OPENCODE_CONFIG: str = ""
    OPENCODE_CONFIG_CONTENT: str = ""
    MCP_CONFIG_PATH: str = str(_WORKSPACE_ROOT / "mcp.json")

    # Multi-model support: a JSON array of ModelConfig objects. When set,
    # individual model entries override the flat ANTHROPIC_* / OPENCODE_MODEL
    # vars for matching requests. When empty, the legacy flat vars are used.
    MODELS_CONFIG: str = ""
    DEFAULT_MODEL_ID: str = ""

    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"

    @property
    def data_root(self) -> Path:
        if self.EIDO_DATA_ROOT.strip():
            return Path(self.EIDO_DATA_ROOT)
        return Path(self.WORKSPACE_ROOT) / ".eido"

    @property
    def workspaces_root(self) -> Path:
        return self.data_root / "workspaces"

    @property
    def claude_agent_env(self) -> dict[str, str]:
        """Build the explicit environment passed to the Claude Code subprocess."""
        env: dict[str, str] = {}
        for key, value in {
            "ANTHROPIC_BASE_URL": self.ANTHROPIC_BASE_URL,
            "ANTHROPIC_MODEL": self.ANTHROPIC_MODEL,
            "ANTHROPIC_SMALL_FAST_MODEL": self.ANTHROPIC_SMALL_FAST_MODEL,
        }.items():
            if value.strip():
                env[key] = value.strip()
        for key, value in {
            "ANTHROPIC_API_KEY": self.ANTHROPIC_API_KEY,
            "ANTHROPIC_AUTH_TOKEN": self.ANTHROPIC_AUTH_TOKEN,
        }.items():
            raw = value.get_secret_value().strip()
            if raw:
                env[key] = raw
        env["API_TIMEOUT_MS"] = str(self.API_TIMEOUT_MS)
        for key, enabled in {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": self.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC,
            "CLAUDE_CODE_USE_BEDROCK": self.CLAUDE_CODE_USE_BEDROCK,
            "CLAUDE_CODE_USE_ANTHROPIC_AWS": self.CLAUDE_CODE_USE_ANTHROPIC_AWS,
            "CLAUDE_CODE_USE_VERTEX": self.CLAUDE_CODE_USE_VERTEX,
            "CLAUDE_CODE_USE_FOUNDRY": self.CLAUDE_CODE_USE_FOUNDRY,
        }.items():
            if enabled:
                env[key] = "1"
        return env

    @property
    def models(self) -> list[ModelConfig]:
        """Return the configured model list.

        When MODELS_CONFIG is a non-empty JSON array, parse it into ModelConfig
        objects. Otherwise fall back to a single-model list built from the
        legacy flat environment variables so the server keeps working without
        any config changes.
        """
        raw = self.MODELS_CONFIG.strip()
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"MODELS_CONFIG 不是合法 JSON: {exc}") from exc
            if not isinstance(data, list):
                raise ValueError("MODELS_CONFIG 必须是 JSON 数组")
            models: list[ModelConfig] = []
            for index, entry in enumerate(data):
                if not isinstance(entry, dict):
                    raise ValueError(
                        f"MODELS_CONFIG[{index}] 必须是 JSON 对象"
                    )
                models.append(ModelConfig(**entry))
            return models

        # Legacy fallback: synthesize a single model from flat env vars.
        legacy = ModelConfig(
            id=self.DEFAULT_MODEL_ID or "default",
            name="Default",
            harness=self.AGENT_HARNESS,
            base_url=self.ANTHROPIC_BASE_URL,
            api_key=self.ANTHROPIC_API_KEY,
            auth_token=self.ANTHROPIC_AUTH_TOKEN,
            model=self.ANTHROPIC_MODEL or self.OPENCODE_MODEL,
            small_fast_model=self.ANTHROPIC_SMALL_FAST_MODEL,
        )
        return [legacy]

    @property
    def default_model_id(self) -> str:
        """The model id used when a request does not pass model_id."""
        if self.DEFAULT_MODEL_ID.strip():
            return self.DEFAULT_MODEL_ID.strip()
        models = self.models
        return models[0].id if models else ""

    def get_model(self, model_id: str | None) -> ModelConfig | None:
        """Resolve a ModelConfig by id.

        Returns the default model when *model_id* is empty or None. Returns
        None when *model_id* is provided but does not match any configured
        model.
        """
        models = self.models
        if not model_id or not model_id.strip():
            target_id = self.default_model_id
        else:
            target_id = model_id.strip()
        for m in models:
            if m.id == target_id:
                return m
        return None


settings = Settings()
