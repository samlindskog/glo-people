"""Logfire / OpenTelemetry wiring for the agents. Call `setup()` once, before the lazy clients are
created. Without LOGFIRE_TOKEN nothing is exported (spans are still created, cheaply, so code
paths do not differ between dev and prod).

Logfire's scope is the pydantic-ai runs and what they call: the model requests and the HTTP
calls of the tools (data providers, Firecrawl, sites), each assistant item as its own trace. The
application's own record of its running — every request, every warning, nginx's access log —
is not here: that is `app.ops`, kept in Postgres and read over the ops MCP on the tunnel."""

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
    logfire.instrument_httpx()        # the model and tool calls inside an agent run
