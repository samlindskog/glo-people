"""The Firecrawl credit balance as the top bar shows it: the team's remaining credits (cached
for a minute, refreshed when an item ends) and what the runs of this process have reserved."""

import logging
import time
from typing import Any

from app.assistants import runner
from app.config import get_settings
from app.integrations.firecrawl import AsyncFirecrawl, FirecrawlError, NotConfigured

log = logging.getLogger(__name__)

_cache: dict[str, Any] = {"at": 0.0, "value": None, "error": None}


def invalidate() -> None:
    _cache["at"] = 0.0


async def summary() -> dict[str, Any] | None:
    """None without a key; else {remaining, plan, period_end, reserved, fetched_at, error}."""
    s = get_settings()
    if not s.firecrawl_api_key:
        return None
    if _cache["value"] is None or time.monotonic() - _cache["at"] > s.firecrawl_credits_cache_s:
        client = AsyncFirecrawl()
        try:
            _cache["value"] = await client.credit_usage()
            _cache["error"] = None
        except (NotConfigured, FirecrawlError) as e:
            _cache["error"] = str(e)
            log.warning("could not read the Firecrawl balance: %s", e)
        finally:
            _cache["at"] = time.monotonic()
            await client.aclose()
    value = _cache["value"] or {}
    return {**value, "reserved": runner.reserved_credits(), "error": _cache["error"]}
