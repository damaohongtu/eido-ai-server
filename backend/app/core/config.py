from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_WORKSPACE_ROOT = Path(__file__).parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

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


settings = Settings()
