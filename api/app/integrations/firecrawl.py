"""Minimal async Firecrawl v2 client (scrape + search) with credit accounting and a budget.

Uses httpx directly: the official SDK pulls in requests, aiohttp, websockets and nest_asyncio for
features this app never calls, and a two-method client is trivial to fake in tests.

Credits: Firecrawl bills 1 credit per scraped page, 2 per search call, and each Alexandria data
tool at its listed price per record found (an empty result costs nothing).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

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
        for attempt in range(3):
            resp = await self._http().post(path, json=body)
            if resp.status_code == 429 and attempt < 2:
                await asyncio.sleep(2.0 * (attempt + 1))
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
