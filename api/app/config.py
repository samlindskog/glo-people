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

    # Workflows (app.workflows): Firecrawl API, the LLM behind the pydantic-ai agents, concurrency.
    firecrawl_api_key: str = ""
    firecrawl_api_url: str = "https://api.firecrawl.dev"
    firecrawl_max_age_s: int = 172_800   # reuse Firecrawl's cached copy of a page up to 2 days old
    agent_model: str = "openai:gpt-6-luna"
    fake_agents: bool = False            # deterministic stand-ins for every agent (e2e, demos)
    enrich_ttl_days: int = 90

    # Assistant runs (app.assistants.runner): items in flight across all runs of this process,
    # heartbeat/staleness for adopting runs left by a dead process, poison protection, retention.
    assistant_concurrency: int = 3
    run_heartbeat_s: int = 10
    run_stale_s: int = 60
    item_max_attempts: int = 3
    run_events_retention_days: int = 30
    firecrawl_credits_cache_s: int = 60

    # Logfire: LOGFIRE_TOKEN is read by the SDK itself; nothing is exported without it.
    logfire_environment: str = "dev"
    logfire_include_content: bool = True   # record prompts/page text in spans (PII): off in prod
    logfire_project_url: str = ""          # e.g. https://logfire-us.pydantic.dev/<org>/<project>

    # Dev only: every request without a key or cookie acts as the local `dev` writer account.
    # Refused at startup when COOKIE_SECURE is on (prod).
    dev_auto_login: bool = False

    session_days: int = 14
    cookie_secure: bool = False
    login_max_failures: int = 10
    login_window_seconds: int = 900


@lru_cache
def get_settings() -> Settings:
    return Settings()
