from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_WORKSPACE_ROOT = Path(__file__).parents[3]


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


settings = Settings()
