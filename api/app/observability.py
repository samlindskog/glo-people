"""Logfire / OpenTelemetry wiring. Call `setup()` once, before the FastAPI app and the lazy
database clients are created. Without LOGFIRE_TOKEN nothing is exported (spans are still
created, cheaply, so code paths do not differ between dev and prod)."""

import logging

import logfire
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings

from app.config import get_settings

log = logging.getLogger(__name__)

_done = False


def setup() -> None:
    global _done
    if _done:
        return
    _done = True
    s = get_settings()
    logfire.configure(service_name="glo-people-api", environment=s.logfire_environment,
                      send_to_logfire="if-token-present", console=False)
    logfire.instrument_pydantic_ai()
    # Prompts, completions and tool arguments carry page text about real people; only record
    # them where LOGFIRE_INCLUDE_CONTENT says so (dev: yes, prod: no).
    Agent.instrument_all(InstrumentationSettings(include_content=s.logfire_include_content))
    logfire.instrument_httpx()
    logfire.instrument_pymongo()
    logfire.instrument_asyncpg()


def instrument_app(app) -> None:
    logfire.instrument_fastapi(app, excluded_urls="/api/health")
