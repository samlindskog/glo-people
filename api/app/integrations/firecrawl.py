"""Minimal async Firecrawl v2 client (scrape + search) with credit accounting and a budget.

Uses httpx directly: the official SDK pulls in requests, aiohttp, websockets and nest_asyncio for
features this app never calls, and a two-method client is trivial to fake in tests.

Credits: Firecrawl bills 1 credit per scraped page, 2 per search call, and each Alexandria data
tool at its listed price per record found (an empty result costs nothing).
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

RATE_LIMIT_RETRIES = 3          # 429s: wait Retry-After (capped) or 2, 4, 8 s, then give up
RATE_LIMIT_MAX_WAIT_S = 30.0

SCRAPE_CREDITS = 1
SEARCH_CREDITS = 2


class FirecrawlError(Exception):
    """Base class; `credits` is what the failed call still cost (usually 0)."""

    credits = 0


class NotFound(FirecrawlError):
    """No data provider has a record for this URL (LinkedIn), or the page does not exist."""


class CreditsExhausted(FirecrawlError):
    """Firecrawl answered 402: the account is out of credits."""


class BudgetExceeded(FirecrawlError):
    """This run item's own cap (scrapes, searches or credits) would be exceeded."""


class NotConfigured(FirecrawlError):
    """FIRECRAWL_API_KEY is not set."""


class TermsRequired(FirecrawlError):
    """An org admin must accept a data provider's terms first (`url` says where)."""

    def __init__(self, message: str, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


@dataclass
class ScrapeResult:
    url: str
    markdown: str
    links: list[str]
    status_code: int | None
    title: str
    credits: int
    provider: str | None = None     # third-party provider id for LinkedIn URLs


_SHAPE_VALUE_KEYS = ("date", "year", "month", "start", "end", "current", "primary", "likelihood",
                     "credits", "count", "total")


def shape(value: Any, depth: int = 0) -> Any:
    """A record's key shape for the logs: keys and value types, with the values of date-like,
    numeric-count and boolean fields kept (they decide how a mapping reads the record) and every
    other string reduced to `<str>`; lists show their first element and their length."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                out[k] = shape(v, depth + 1) if depth < 4 else type(v).__name__
            elif isinstance(v, bool) or v is None or any(t in k.lower() for t in _SHAPE_VALUE_KEYS):
                out[k] = v
            else:
                out[k] = f"<{type(v).__name__}>"
        return out
    if isinstance(value, list):
        return [shape(value[0], depth + 1), f"… {len(value)} items"] if value else []
    return value if isinstance(value, (bool, int, float)) or value is None else f"<{type(value).__name__}>"


@dataclass
class AlexandriaResult:
    """One Alexandria tool call: the provider's payload (None when it had no record)."""

    provider: str
    capability: str
    data: Any
    credits: int


@dataclass
class SearchHit:
    url: str
    title: str
    description: str


@dataclass
class Budget:
    """Per-item caps. Counts every paid call made through the client it is attached to."""

    max_scrapes: int
    max_searches: int
    max_credits: int
    scrapes: int = 0
    searches: int = 0
    credits: int = 0
    calls: list[dict] = field(default_factory=list)
    # Called with each paid call as it is charged ({kind, target, credits}); an invocation hooks
    # it to report provider activity live.
    on_charge: Callable[[dict], None] | None = field(default=None, repr=False, compare=False)

    def check_scrape(self, expected: int) -> None:
        if self.scrapes + 1 > self.max_scrapes:
            raise BudgetExceeded(f"scrape cap ({self.max_scrapes}) reached")
        if self.credits + expected > self.max_credits:
            raise BudgetExceeded(f"credit cap ({self.max_credits}) would be exceeded")

    def check_search(self) -> None:
        if self.searches + 1 > self.max_searches:
            raise BudgetExceeded(f"search cap ({self.max_searches}) reached")
        if self.credits + SEARCH_CREDITS > self.max_credits:
            raise BudgetExceeded(f"credit cap ({self.max_credits}) would be exceeded")

    def charge(self, kind: str, target: str, credits: int) -> None:
        if kind == "scrape":
            self.scrapes += 1
        else:
            self.searches += 1
        self.credits += credits
        self.calls.append({"kind": kind, "target": target, "credits": credits})
        if self.on_charge is not None:
            self.on_charge(self.calls[-1])


class AsyncFirecrawl:
    def __init__(self, api_key: str | None = None, api_url: str | None = None,
                 budget: Budget | None = None, client: httpx.AsyncClient | None = None) -> None:
        s = get_settings()
        self.api_key = api_key if api_key is not None else s.firecrawl_api_key
        self.api_url = (api_url or s.firecrawl_api_url).rstrip("/")
        self.budget = budget
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.api_url, timeout=httpx.Timeout(90.0, connect=15.0),
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"})
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, body: dict) -> dict:
        if not self.api_key:
            raise NotConfigured("FIRECRAWL_API_KEY is not set")
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            resp = await self._http().post(path, json=body)
            if resp.status_code in (502, 503, 504) and attempt < 1:   # a gateway blip: once more
                log.warning("Firecrawl %s: HTTP %s, retrying once", path, resp.status_code)
                await asyncio.sleep(2.0)
                continue
            if resp.status_code == 429 and attempt < RATE_LIMIT_RETRIES:
                # rate limited: wait what the server asks (capped), else 2, 4, 8 s
                try:
                    wait = min(float(resp.headers.get("retry-after") or 0), RATE_LIMIT_MAX_WAIT_S)
                except ValueError:
                    wait = 0.0
                wait = wait or 2.0 * 2 ** attempt
                log.warning("Firecrawl %s: rate limited, waiting %.0fs (attempt %d)", path, wait,
                            attempt + 1)
                await asyncio.sleep(wait)
                continue
            break
        if resp.status_code == 402:
            raise CreditsExhausted("Firecrawl: insufficient credits")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if isinstance(data, dict) and data.get("code") == "THIRD_PARTY_DATA_TERMS_REQUIRED":
            raise TermsRequired(str(data.get("error") or data["code"]),
                                (data.get("requiresAction") or {}).get("url"))
        if resp.status_code == 404 or (isinstance(data, dict) and
                                       data.get("code") == "THIRD_PARTY_DATA_NOT_FOUND"):
            raise NotFound(str(data.get("error") or data.get("code") or "not found"))
        if resp.status_code >= 400 or not data.get("success", True):
            raise FirecrawlError(f"Firecrawl HTTP {resp.status_code}: "
                                 f"{str(data.get('error') or data)[:200]}")
        return data

    async def credit_usage(self) -> dict[str, Any]:
        """The team's balance: {remaining, plan, period_start, period_end} (GET /v2/team/credit-usage;
        free). NotConfigured without a key."""
        if not self.api_key:
            raise NotConfigured("FIRECRAWL_API_KEY is not set")
        resp = await self._http().get("/v2/team/credit-usage")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or not data.get("success", True):
            raise FirecrawlError(f"Firecrawl HTTP {resp.status_code}: "
                                 f"{str(data.get('error') or data)[:200]}")
        d = data.get("data") or {}
        return {"remaining": d.get("remainingCredits"), "plan": d.get("planCredits"),
                "period_start": d.get("billingPeriodStart"), "period_end": d.get("billingPeriodEnd")}

    async def scrape(self, url: str, formats: tuple[str, ...] = ("markdown",),
                     expected_credits: int = SCRAPE_CREDITS,
                     timeout_ms: int = 60_000) -> ScrapeResult:
        if self.budget:
            self.budget.check_scrape(expected_credits)
        body: dict[str, Any] = {"url": url, "formats": list(formats),
                                "onlyMainContent": True, "timeout": timeout_ms}
        max_age = get_settings().firecrawl_max_age_s
        if max_age:
            body["maxAge"] = max_age * 1000
        data = await self._post("/v2/scrape", body)
        d = data.get("data") or {}
        meta = d.get("metadata") or {}
        provider = meta.get("provider") or {}
        credits = int(provider.get("creditsCost") or SCRAPE_CREDITS)
        if self.budget:
            self.budget.charge("scrape", url, credits)
        return ScrapeResult(url=url, markdown=d.get("markdown") or "",
                            links=[x for x in (d.get("links") or []) if isinstance(x, str)],
                            status_code=meta.get("statusCode"), title=meta.get("title") or "",
                            credits=credits, provider=provider.get("id"))

    async def alexandria(self, provider: str, capability: str, options: dict[str, Any],
                         expected_credits: int) -> AlexandriaResult:
        """Run one catalogued data-provider tool (Firecrawl Alexandria) instead of scraping.

        Billed at the tool's listed price per record found; an empty result costs nothing. A
        tool whose terms the org has not accepted raises TermsRequired; a provider-side
        not_found raises NotFound; other tool errors raise FirecrawlError.
        """
        if self.budget:
            self.budget.check_scrape(expected_credits)
        data = await self._post("/v2/scrape", {"alexandria": {
            "provider": provider, "capability": capability, "options": options}})
        items = (data.get("data") or {}).get("alexandria") or []
        item = items[0] if items else {}
        credits = int(item.get("creditsCost") or 0)
        if self.budget:
            self.budget.charge("scrape", f"{provider}/{capability}", credits)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("%s/%s record shape (%s credits): %s", provider, capability, credits,
                      json.dumps(shape(item.get("data")), ensure_ascii=False)[:6000])
        err = item.get("error")
        if err:
            code = str(err.get("code") or "")
            if code == "THIRD_PARTY_DATA_TERMS_REQUIRED":
                raise TermsRequired(str(err.get("message") or code),
                                    (err.get("requiresAction") or {}).get("url"))
            if code in ("not_found", "THIRD_PARTY_DATA_NOT_FOUND"):
                raise NotFound(str(err.get("message") or code))
            raise FirecrawlError(f"{provider}/{capability}: {code} {err.get('message') or ''}")
        return AlexandriaResult(provider=provider, capability=capability,
                                data=item.get("data"), credits=credits)

    async def find_tools(self, *, providers: list[str] | None = None,
                         categories: list[str] | None = None,
                         capabilities: list[str] | None = None, level: str = "tools",
                         expand: tuple[str, ...] = ("options",), limit: int = 50,
                         offset: int = 0) -> list[dict[str, Any]]:
        """The Alexandria catalogue (free). At the `tools` level: the tools matching the filters,
        each with `provider`, `capability`, `description`, `creditsCost` (`perRecord`),
        `requiresOneOf` and, expanded, its `options` (inputs). At the `providers` level: the
        providers of a category (`expand` is not accepted there). Read a provider's live price
        here before adding it to the enrich ladder."""
        options = {k: v for k, v in {"providers": providers, "categories": categories,
                                     "capabilities": capabilities, "level": level,
                                     "expand": list(expand) if level == "tools" else None,
                                     "limit": limit, "offset": offset}.items() if v}
        res = await self.alexandria("firecrawl", "find-tools", options, expected_credits=0)
        items = res.data.get("items") if isinstance(res.data, dict) else res.data
        return [t for t in items or [] if isinstance(t, dict)]

    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        if self.budget:
            self.budget.check_search()
        data = await self._post("/v2/search", {"query": query, "limit": limit})
        if self.budget:
            self.budget.charge("search", query, SEARCH_CREDITS)
        raw = data.get("data") or {}
        items = raw.get("web", raw) if isinstance(raw, dict) else raw
        out = []
        for it in items or []:
            if isinstance(it, dict) and it.get("url"):
                out.append(SearchHit(url=it["url"], title=it.get("title") or "",
                                     description=it.get("description") or ""))
        return out
