"""The enrich ladder's provider table, its worst-case budget and the free catalogue query."""

from typing import get_args

import pytest

from app.assistants.enrich import budget
from app.assistants.enrich.models import EnrichOptions, ProviderKey
from app.assistants.enrich.passage import Passage
from app.assistants.enrich.providers import PROVIDERS
from app.integrations import profile as profile_mod
from app.integrations.firecrawl import AsyncFirecrawl


def test_provider_table_matches_the_option_and_fits_the_item_cap():
    assert set(PROVIDERS) == set(get_args(ProviderKey)) == {"fullenrich", "people-data-labs"}
    assert PROVIDERS["fullenrich"].source == "fullenrich/people/match"
    assert PROVIDERS["people-data-labs"].source == "people-data-labs/person/enrich"
    assert EnrichOptions().providers == ["fullenrich"]       # PDL bills 60 a match since 2026-11-02
    assert EnrichOptions().provider_policy == "gaps"
    # every provider matched + GitHub + every page + the search: within the per-item cap
    assert budget.worst_case(EnrichOptions()) == 5 + 5 + 4 + 2 <= budget.CAPS["max_credits"]
    assert budget.item_cap(EnrichOptions()) == 16           # what a run reserves per item in flight
    assert budget.worst_case(EnrichOptions(providers=[])) == 11
    assert budget.worst_case(EnrichOptions(providers=["fullenrich", "fullenrich"])) == 16
    every = EnrichOptions(providers=list(PROVIDERS))
    assert budget.worst_case(every) == 60 + 5 + 5 + 4 + 2 <= budget.CAPS["max_credits"]
    assert budget.budget_for(EnrichOptions()).max_scrapes == 4 + 2       # pages + 1 provider + GitHub
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
        "skills": ["Payments", "Python", "Go"], "languages": ["English", "Yoruba"],
        "interests": ["Fintech Policy", "Open Source"]}
    assert pdl.from_record({"summary": "x"}).experience == []
    # fields the org is not entitled to arrive as booleans; skills and interests are title-cased
    redacted = {**rec, "github_url": False, "twitter_url": True, "job_company_website": False,
                "skills": ["machine learning", "ai"], "interests": ["open source"]}
    q = pdl.from_record(redacted)
    assert q.github_url is None and q.other_links == ["https://angel.co/adaokafor"]
    assert q.current_company.website is None
    assert q.skills == ["Machine Learning", "AI"] and q.interests == ["Open Source"]
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


# --- the shared profile and passage helpers (level 0/2 pure functions) ---

def test_passage_merges_lists_and_drops_a_second_text():
    p = Passage()
    assert not p
    p.add("fullenrich/people/match", "LinkedIn profile",
          [("about", "  Founder\nof PayRail "), ("skills", ["Payments", " payments", "Fintech"]),
           ("languages", [])])
    p.add("pdl/person/enrich", "PDL profile", [("skills", ["fintech", "Go-to-market"]),
                                               ("about", "another about")])
    assert p and p.keys == ["about", "skills"]
    assert p.render() == ("# Facts from data providers; each line names the property it belongs to.\n"
                          "# LinkedIn profile (fullenrich/people/match)\n"
                          "about: Founder of PayRail\n"
                          "skills: Payments; Fintech; Go-to-market")
    assert p.source_of("skills") == "fullenrich/people/match, pdl/person/enrich"
    assert p.dropped == [("about", "pdl/person/enrich")]
    assert len(p.digest()) == 16 and p.digest() == Passage.digest(p)


def test_name_fill_from_the_profile():
    prof = profile_mod.LinkedInProfile(full_name="Alina Kharina")
    assert profile_mod.name_fill(prof, "Alina") == "Alina Kharina"
    assert profile_mod.name_fill(prof, "alina ") == "Alina Kharina"
    assert profile_mod.name_fill(prof, None) == "Alina Kharina"
    assert profile_mod.name_fill(prof, "Fleek") == "Alina Kharina"   # not a full name: the profile's
    assert profile_mod.name_fill(prof, "Alina K.") == "Alina Kharina"  # an initial is not a last name
    assert profile_mod.name_fill(prof, "Alina Kharina") is None
    assert profile_mod.name_fill(prof, "Someone Else") is None        # a full name is kept
    # the provider's full_name field is not always the full name
    halves = profile_mod.LinkedInProfile(full_name="Jack", first_name="Jack", last_name="Mathews")
    assert profile_mod.full_name(halves) == "Jack Mathews"
    assert profile_mod.full_name(profile_mod.LinkedInProfile(full_name="jack", last_name=None)) is None
    assert profile_mod.full_name(profile_mod.LinkedInProfile(first_name="Jack", last_name="M.")) is None
    # taglines and handles are not full names, whatever their token count
    for junk in ("Jordan | Cofounder | UXUY", "Lena@Cohere Commerce", "Raj - LootMogul accelerated by a16z",
                 "Anna (she/her)", "DJ_Deep"):
        assert not profile_mod.is_full_name(junk), junk
    assert profile_mod.is_full_name("Rwaida Gharib-Kane") and profile_mod.is_full_name("O'Neil Mcdonald")
    assert profile_mod.name_fill(profile_mod.LinkedInProfile(first_name="Ada"), "Ada") is None


def test_profile_lines_clean_provider_text():
    prof = profile_mod.LinkedInProfile(
        summary="Strategy &amp; ops \u2800\u2800 leader\u200b", languages=["english", "Yoruba"],
        skills=["Payments", " payments "])
    assert dict(profile_mod.profile_lines(prof)) == {
        "about": "Strategy & ops leader", "skills": ["Payments", "payments"],
        "languages": ["English", "Yoruba"]}


def test_profile_lines_use_spec_properties_of_the_right_type():
    from app.property_specs import SPECS
    prof = profile_mod.LinkedInProfile(
        summary="About.", current_title="CTO",
        experience=[profile_mod.Position(title="CTO", company="Leafy", start="2022", is_current=True),
                    profile_mod.Position(title="Engineer", start="2019-01", end="2021-12"),
                    profile_mod.Position(company="Unknown Co")],
        education=[profile_mod.Education(school="TU Berlin", degree="MSc", field="CS", end="2018")],
        skills=["Python"], interests=["Climate"])
    lines = dict(profile_mod.profile_lines(prof))
    assert lines == {"about": "About.", "title_enriched": "CTO",
                     "experience": "CTO at Leafy (2022–present); Engineer (2019–2021); Unknown Co",
                     "education": "MSc CS, TU Berlin (2018)", "skills": ["Python"],
                     "interests": ["Climate"]}
    for key, value in lines.items():
        assert SPECS[key].type == ("list" if isinstance(value, list) else
                                   "keyword" if key == "title_enriched" else "text"), key
    assert profile_mod.profile_lines(profile_mod.LinkedInProfile()) == []


def test_split_urls_handles_lists_and_separators():
    from app.assistants.enrich.assistant import split_urls
    assert split_urls("https://altschoolafrica.com/ , https:/Talentql.com") == [
        "https://altschoolafrica.com/", "https:/Talentql.com"]
    assert split_urls(["a.com; b.com", "c.com"]) == ["a.com", "b.com", "c.com"]
    assert split_urls("no url here") == [] and split_urls(42) == []


async def test_rate_limited_calls_back_off_and_succeed(monkeypatch):
    """A 429 waits (Retry-After, capped) and tries again; the call succeeds once the limit lifts."""
    import httpx

    from app.integrations import firecrawl as fc

    calls: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": "rate limited"})
        return httpx.Response(200, json={"success": True, "data": {"ok": True}})

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(fc.asyncio, "sleep", fake_sleep)
    client = fc.AsyncFirecrawl(api_key="test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://fc.test")
    try:
        data = await client._post("/v2/anything", {})
    finally:
        await client.aclose()
    assert data.get("success") is True and len(calls) == 3 and sleeps == [7.0, 7.0]

    calls.clear(); sleeps.clear()

    def always(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={"error": "rate limited"})

    client = fc.AsyncFirecrawl(api_key="test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(always), base_url="https://fc.test")
    try:
        with pytest.raises(fc.FirecrawlError):
            await client._post("/v2/anything", {})
    finally:
        await client.aclose()
    assert len(calls) == fc.RATE_LIMIT_RETRIES + 1 and sleeps == [2.0, 4.0, 8.0]


def test_a_tool_billing_far_above_its_price_is_flagged():
    from app.integrations import firecrawl as fc
    assert fc.overpriced(5) == 15 and fc.overpriced(1) == 11 and fc.overpriced(60) == 120


def test_options_describe_their_cost_and_warn_about_a_dear_provider(monkeypatch):
    from app.integrations import firecrawl as fc
    monkeypatch.setitem(fc.OBSERVED_PRICES, "people-data-labs/person/enrich", 60)
    prices = {p["key"]: p for p in budget.provider_prices()}
    assert prices["fullenrich"] == {"key": "fullenrich", "label": "FullEnrich",
                                    "credits_per_match": 5, "last_billed": None}
    assert prices["people-data-labs"]["credits_per_match"] == 60
    assert prices["people-data-labs"]["last_billed"] == 60
    d = budget.describe(EnrichOptions())
    assert [p["key"] for p in d["providers"]] == ["fullenrich"] and d["warnings"] == []
    assert d["typical_per_item"] == 5 + 2 and d["worst_case_per_item"] == 16
    assert d["comparable"] == ("providers", ["fullenrich"])
    assert budget.describe(EnrichOptions(providers=[]))["typical_per_item"] == 2
    pdl_first = budget.describe(EnrichOptions(providers=["people-data-labs", "fullenrich"]))
    assert pdl_first["typical_per_item"] == 60 + 2          # gaps: the second is asked rarely
    assert pdl_first["worst_case_per_item"] == 76
    assert len(pdl_first["warnings"]) == 1 and "60 credits per match" in pdl_first["warnings"][0]
    assert "asked for People Data Labs by name" in pdl_first["warnings"][0]
    both = budget.describe(EnrichOptions(providers=["fullenrich", "people-data-labs"],
                                         provider_policy="all"))
    assert both["typical_per_item"] == 67 and both["comparable"][1] == ["fullenrich", "people-data-labs"]
