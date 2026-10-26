from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_PERMISSIONS = Path(__file__).resolve().parents[2] / "shared" / "permissions.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://glo:glo@localhost:5432/glo"
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "glo"

    # "openai" uses the OpenAI API; "fake" is a deterministic local hashing embedder
    # for tests and for dev without a key.
    embedding_provider: str = "openai"
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dims: int = 512

    permissions_file: Path = (
        _REPO_PERMISSIONS if _REPO_PERMISSIONS.exists() else Path("/shared/permissions.json")
    )

    session_days: int = 14
    cookie_secure: bool = False
    login_max_failures: int = 10
    login_window_seconds: int = 900


@lru_cache
def get_settings() -> Settings:
    return Settings()
