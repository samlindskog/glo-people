"""The enrich ladder's provider table, its worst-case budget and the free catalogue query."""

from typing import get_args

from app.assistants.enrich import budget
from app.assistants.enrich.models import EnrichOptions, ProviderKey
from app.assistants.enrich.providers import PROVIDERS
from app.integrations.firecrawl import AsyncFirecrawl


def test_provider_table_matches_the_option_and_fits_the_item_cap():
    assert set(PROVIDERS) == set(get_args(ProviderKey)) == {"fullenrich"}
    assert PROVIDERS["fullenrich"].source == "fullenrich/people/match"
    assert EnrichOptions().providers == ["fullenrich"]
    # every provider matched + GitHub + every page + the search: within the per-item cap
    assert budget.worst_case(EnrichOptions()) == 5 + 5 + 4 + 2 <= budget.CAPS["max_credits"]
    assert budget.worst_case(EnrichOptions(providers=[])) == 11
    assert budget.worst_case(EnrichOptions(providers=["fullenrich", "fullenrich"])) == 16


async def test_find_tools_is_a_free_catalogue_query(monkeypatch):
    sent: list[tuple[str, dict]] = []

    async def fake_post(path, body):
        sent.append((path, body))
        return {"data": {"alexandria": [{"creditsCost": 0, "data": {"level": "tools", "items": [
            {"provider": "people-data-labs", "capability": "person/enrich", "creditsCost": 1,
             "perRecord": True, "options": [{"name": "profile", "type": "string"}]}]}}]}}

    client = AsyncFirecrawl(api_key="k")
    monkeypatch.setattr(client, "_post", fake_post)
    tools = await client.find_tools(categories=["people"], limit=10)
    assert [t["provider"] for t in tools] == ["people-data-labs"]
    assert sent == [("/v2/scrape", {"alexandria": {
        "provider": "firecrawl", "capability": "find-tools",
        "options": {"categories": ["people"], "level": "tools", "expand": ["options"],
                    "limit": 10}}})]
    await client.find_tools(categories=["people"], level="providers")
    assert "expand" not in sent[1][1]["alexandria"]["options"]       # not accepted at that level
