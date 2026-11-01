"""The enrich ladder's provider table, its worst-case budget and the free catalogue query."""

from typing import get_args

from app.assistants.enrich import budget
from app.assistants.enrich.models import EnrichOptions, ProviderKey
from app.assistants.enrich.providers import PROVIDERS
from app.integrations.firecrawl import AsyncFirecrawl


def test_provider_table_matches_the_option_and_fits_the_item_cap():
    assert set(PROVIDERS) == set(get_args(ProviderKey)) == {"fullenrich", "people-data-labs"}
    assert PROVIDERS["fullenrich"].source == "fullenrich/people/match"
    assert PROVIDERS["people-data-labs"].source == "people-data-labs/person/enrich"
    assert EnrichOptions().providers == ["fullenrich"]             # PDL is opt-in
    # every provider matched + GitHub + every page + the search: within the per-item cap
    assert budget.worst_case(EnrichOptions()) == 5 + 5 + 4 + 2 <= budget.CAPS["max_credits"]
    assert budget.worst_case(EnrichOptions(providers=[])) == 11
    assert budget.worst_case(EnrichOptions(providers=["fullenrich", "fullenrich"])) == 16
    every = EnrichOptions(providers=list(PROVIDERS))
    assert budget.worst_case(every) == 17 <= budget.CAPS["max_credits"]
    assert budget.budget_for(EnrichOptions()).max_scrapes == 4 + 2       # pages + FullEnrich + GitHub
    assert budget.budget_for(every).max_scrapes == 4 + 3


def test_pdl_record_maps_to_the_shared_profile():
    import json
    from pathlib import Path

    from app.integrations import pdl, profile
    rec = json.loads((Path(__file__).parent / "fixtures" / "firecrawl" / "pdl_ada.json")
                     .read_text())["data"]
    p = pdl.from_record(rec)
    assert p.full_name == "Ada Okafor" and p.current_title == "Chief Executive Officer"
    assert p.current_company.name == "Payrail" and p.current_company.website == "https://payrail.io"
    assert p.github_url == "https://github.com/adaok" and p.location == "Lagos, Lagos, Nigeria"
    assert p.other_links == ["https://twitter.com/adaokafor", "https://angel.co/adaokafor"]
    assert [x.company for x in p.experience] == ["Payrail", "Paystack", "Stripe"]  # current first
    assert dict(profile.profile_lines(p)) == {
        "about": "Payments builder. Ex-Paystack, ex-Stripe.",
        "title_enriched": "Chief Executive Officer",
        "experience": "Chief Executive Officer at Payrail (2021–present); Product Lead at Paystack "
                      "(2018–2021); Engineer at Stripe (2015–2017)",
        "education": "Bachelors Computer Science, University of Lagos (2010–2014)",
        "skills": ["payments", "python", "go"], "languages": ["English", "Yoruba"],
        "interests": ["fintech policy", "open source"]}
    assert pdl.from_record({"summary": "x"}).experience == []
    assert pdl._title("it service desk & incident manager at td") == "IT Service Desk & Incident Manager at TD"
    assert pdl._title("head of it") == "Head of IT" and pdl._title("supra its") == "Supra Its"
    assert pdl._title("McKinsey & Company") == "McKinsey & Company"       # mixed case: untouched
    assert pdl._title({"name": "university of lagos"}) == "University of Lagos"
    assert pdl._title("") is None and pdl._title(None) is None


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
