"""The data providers the enrich ladder can ask about a LinkedIn URL, as a table: one entry per
provider with its Alexandria capability, listed price and `match` function (a client and the URL
to the shared profile shape, or None for a miss). Adding a provider is one integration module
(`match` + `from_record` into `integrations.profile.LinkedInProfile`) and one row here; the
passage format, the storage step and the budget check are shared. `python -m app.cli providers
--alexandria` prints the catalogue's live prices and inputs (free) for the next candidate."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import get_args

from app.assistants.enrich.models import ProviderKey
from app.integrations import fullenrich
from app.integrations.firecrawl import AsyncFirecrawl
from app.integrations.profile import LinkedInProfile

Match = Callable[[AsyncFirecrawl, str], Awaitable[LinkedInProfile | None]]


@dataclass(frozen=True)
class ProviderSpec:
    key: ProviderKey
    label: str
    capability: str       # the Alexandria tool, for the passage's source line and the activity
    credits: int          # listed price per record found; a miss is free
    match: Match

    @property
    def source(self) -> str:
        return f"{self.key}/{self.capability}"


PROVIDERS: dict[ProviderKey, ProviderSpec] = {
    "fullenrich": ProviderSpec(key="fullenrich", label="FullEnrich", capability="people/match",
                               credits=fullenrich.MATCH_CREDITS, match=fullenrich.match),
}

assert set(PROVIDERS) == set(get_args(ProviderKey)), "models.ProviderKey and PROVIDERS disagree"
