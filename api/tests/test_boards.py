"""glojobs.app boards: the sanitiser, the store, the public pages, the REST surface and the MCP
tools."""

import uuid

import pytest
from sqlalchemy import text

from app import boards, db, safe_html
from tests.conftest import import_people
from tests.test_mcp import call, init, rpc

JD = """<h2>What you'll do</h2>
<ul><li>Ship the product</li><li>Talk to <strong>customers</strong></li></ul>
<p>Apply via <a href="https://example.com/apply" onclick="steal()">the site</a>.</p>
<script>alert(1)</script><style>p{display:none}</style>
<img src="x" onerror="steal()"><p style="color:red">Pay: $1</p>"""


@pytest.fixture(autouse=True)
async def _clean_boards():
    async with db.engine().begin() as conn:
        await conn.execute(text("TRUNCATE boards, board_talent, board_jobs, board_companies CASCADE"))
    yield


@pytest.fixture
async def ada(writer) -> str:
    csv = ("Name,Role,Company,Location,Bio,One-liner\n"
           "Ada Okafor,CTO,PayRail,Lagos,Builds payments rails for merchants.,Payments founder\n")
    res = await import_people(writer, csv, "p.csv", "member")
    run = (await writer.get(f"/api/assistants/runs/{res['run_id']}")).json()
    return run["items"][0]["person_id"]


async def catalogue(client) -> None:
    r = await client.put("/api/boards/companies/fastlane", json={
        "name": "Fastlane", "web": "https://usefastlane.ai/", "line": "AI ads",
        "about_html": "<p>Fastlane makes ads.</p>"})
    assert r.status_code == 200, r.text
    r = await client.put("/api/boards/companies/pulse", json={"name": "Pulse"})
    assert r.status_code == 200, r.text
    for slug, co, title, loc in (("fastlane-founding-engineer", "fastlane", "Founding Engineer",
                                  "Bali (remote ok)"),
                                 ("fastlane-head-of-growth", "fastlane", "Head of Growth", "Bali"),
                                 ("pulse-technical-co-founder", "pulse", "Technical Co-founder",
                                  "New York")):
        r = await client.put(f"/api/boards/jobs/{slug}", json={
            "company": co, "title": title, "location": loc, "type": "Full-time",
            "comp": "$150K + equity", "summary": f"{title} at {co}.", "html": JD})
        assert r.status_code == 200, r.text


# --- the sanitiser ------------------------------------------------------------------------------

def test_clean_keeps_prose_and_drops_the_rest():
    out = safe_html.clean(JD)
    assert "<h2>" in out.html and "<ul>" in out.html and "<strong>" in out.html
    assert "script" not in out.html and "alert" not in out.html
    assert "<style" not in out.html and "display:none" not in out.html
    assert "<img" not in out.html and "onerror" not in out.html and "onclick" not in out.html
    assert 'style="' not in out.html
    assert 'rel="noopener noreferrer"' in out.html and 'href="https://example.com/apply"' in out.html
    assert "Pay: $1" in out.html                      # the paragraph stays, its style goes
    assert out.stripped == ["img", "script", "style"]
    assert safe_html.clean("   ").html == "" and safe_html.clean(None).stripped == []


def test_clean_drops_javascript_links():
    out = safe_html.clean('<a href="javascript:alert(1)">x</a>')
    assert "javascript" not in out.html and ">x</a>" in out.html


def test_slug():
    assert boards.slug("Growth (Technical Growth Operator)") == "growth-technical-growth-operator"
    assert boards.slug("Finance & Investor Relations") == "finance-and-investor-relations"
    assert boards.slug("C++ Engineer") == "c-engineer"


# --- the store through REST ---------------------------------------------------------------------

async def test_catalogue_roundtrip(writer, reader):
    await catalogue(writer)
    jobs = (await reader.get("/api/boards/jobs")).json()
    assert [j["slug"] for j in jobs] == ["fastlane-founding-engineer", "fastlane-head-of-growth",
                                         "pulse-technical-co-founder"]
    assert jobs[0]["company_name"] == "Fastlane" and "<script>" not in jobs[0]["html"]
    cos = (await reader.get("/api/boards/companies")).json()
    assert [c["slug"] for c in cos] == ["fastlane", "pulse"]
    assert cos[0]["web"] == "usefastlane.ai"                # scheme and slash dropped

    # a put reports what the sanitiser dropped and replaces the job
    r = await writer.put("/api/boards/jobs/fastlane-head-of-growth", json={
        "company": "fastlane", "title": "Head of Growth", "html": "<p>x</p><iframe></iframe>",
        "status": "closed"})
    assert r.status_code == 200 and r.json()["stripped"] == ["iframe"]
    assert (await reader.get("/api/boards/jobs?status=open")).json().__len__() == 2
    assert (await reader.get("/api/boards/jobs?q=pulse")).json()[0]["slug"] == "pulse-technical-co-founder"

    # an unknown company, a bad status, a reader writing
    r = await writer.put("/api/boards/jobs/x", json={"company": "nope", "title": "X"})
    assert r.status_code == 422 and "unknown company" in r.text
    r = await writer.put("/api/boards/jobs/x", json={"company": "pulse", "title": "X", "status": "gone"})
    assert r.status_code == 422
    r = await writer.put("/api/boards/jobs/x", json={"company": "pulse", "title": "X",
                                                     "source_url": "javascript:alert(1)"})
    assert r.status_code == 422 and "source_url" in r.text      # it becomes a public href
    assert (await reader.put("/api/boards/jobs/x", json={"company": "pulse", "title": "X"})).status_code == 403

    # a company with jobs cannot go; one without can
    assert (await writer.delete("/api/boards/companies/fastlane")).status_code == 422
    assert (await writer.delete("/api/boards/jobs/fastlane-head-of-growth")).status_code == 204
    assert (await writer.delete("/api/boards/jobs/fastlane-founding-engineer")).status_code == 204
    assert (await writer.delete("/api/boards/companies/fastlane")).status_code == 204
    assert (await writer.delete("/api/boards/companies/fastlane")).status_code == 404


async def test_talent_draft_and_upsert(writer, ada):
    draft = (await writer.get(f"/api/boards/talent/draft?person_id={ada}")).json()
    assert draft["name"] == "Ada Okafor" and draft["slug"] == "ada-okafor"
    assert draft["headline"] == "CTO at PayRail" and draft["location"] == "Lagos"
    assert draft["summary"] == "Payments founder"
    assert draft["html"] == "<p>Builds payments rails for merchants.</p>"

    # the person alone drafts every field; a passed field wins
    r = await writer.put("/api/boards/talent/ada-okafor", json={"person_id": ada, "summary": "Hi"})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["name"] == "Ada Okafor" and row["headline"] == "CTO at PayRail"
    assert row["summary"] == "Hi" and row["person_id"] == ada and row["status"] == "active"
    assert (await writer.get("/api/boards/talent?person_id=" + ada)).json()[0]["slug"] == "ada-okafor"
    r = await writer.put("/api/boards/talent/no-one", json={"html": "<p>x</p>"})
    assert r.status_code == 422 and "needs a name" in r.text
    assert (await writer.get("/api/boards/talent/draft?person_id=000000000000000000000000")).status_code == 404


async def test_boards_lists_and_deletes(writer, reader, ada):
    await catalogue(writer)
    await writer.put("/api/boards/talent/ada-okafor", json={"person_id": ada})
    r = await writer.post("/api/boards", json={
        "person_id": ada, "kind": "both", "intro_html": "<p>Hello <b>Ada</b></p><script>x</script>",
        "job_slugs": ["pulse-technical-co-founder", "fastlane-founding-engineer",
                      "pulse-technical-co-founder"],
        "talent_slugs": ["ada-okafor"]})
    assert r.status_code == 201, r.text
    board = r.json()
    assert board["stripped"] == ["script"] and board["person_name"] == "Ada Okafor"
    assert board["job_slugs"] == ["pulse-technical-co-founder", "fastlane-founding-engineer"]
    assert board["url"].endswith("/" + board["id"]) and uuid.UUID(board["id"]).version == 4
    assert board["title"] is None

    # unknown slugs and people are refused
    r = await writer.post("/api/boards", json={"person_id": ada, "kind": "jobs", "job_slugs": ["nope"]})
    assert r.status_code == 422 and "unknown job(s): nope" in r.text
    r = await writer.post("/api/boards", json={"person_id": "000000000000000000000000", "kind": "jobs"})
    assert r.status_code == 404

    # resolved: ordered, grouped by company, the default title from the person's first name
    got = (await reader.get(f"/api/boards/{board['id']}")).json()
    assert got["title"] == "Roles and people for Ada"
    assert [j["slug"] for j in got["jobs"]] == ["pulse-technical-co-founder", "fastlane-founding-engineer"]
    assert [c["slug"] for c in got["companies"]] == ["pulse", "fastlane"]
    assert got["talent"][0]["name"] == "Ada Okafor"
    assert (await reader.get("/api/boards?person_id=" + ada)).json()[0]["job_count"] == 2
    assert (await reader.get("/api/boards/jobs/pulse-technical-co-founder")).json()["boards"][0]["id"] == board["id"]

    # a patch: title, a replaced list; a closed job drops out of the page but not the list
    r = await writer.patch(f"/api/boards/{board['id']}", json={
        "title": "Ada's picks", "job_slugs": ["fastlane-founding-engineer", "fastlane-head-of-growth"]})
    assert r.status_code == 200 and r.json()["title"] == "Ada's picks"
    await writer.put("/api/boards/jobs/fastlane-head-of-growth", json={
        "company": "fastlane", "title": "Head of Growth", "status": "closed"})
    got = (await reader.get(f"/api/boards/{board['id']}")).json()
    assert [j["slug"] for j in got["jobs"]] == ["fastlane-founding-engineer"]
    assert got["job_slugs"] == ["fastlane-founding-engineer", "fastlane-head-of-growth"]

    # deleting a job or talent entry removes it from the board's list
    await writer.delete("/api/boards/jobs/fastlane-head-of-growth")
    await writer.delete("/api/boards/talent/ada-okafor")
    got = (await reader.get(f"/api/boards/{board['id']}")).json()
    assert got["job_slugs"] == ["fastlane-founding-engineer"] and got["talent_slugs"] == []

    assert (await reader.get("/api/boards/not-a-uuid")).status_code == 404
    assert (await writer.delete(f"/api/boards/{board['id']}")).status_code == 204
    assert (await reader.get(f"/api/boards/{board['id']}")).status_code == 404
    assert (await reader.delete(f"/api/boards/{board['id']}")).status_code == 403


async def test_preview(reader):
    r = await reader.post("/api/boards/preview", json={"html": "<p>a</p><script>b</script>"})
    assert r.json() == {"html": "<p>a</p>", "stripped": ["script"]}


# --- the public pages ---------------------------------------------------------------------------

async def test_public_pages(writer, client_factory, ada):
    await catalogue(writer)
    await writer.put("/api/boards/talent/ada-okafor", json={"person_id": ada})
    board = (await writer.post("/api/boards", json={
        "person_id": ada, "kind": "both", "intro_html": "<p>Picked for <em>you</em></p>",
        "job_slugs": ["fastlane-founding-engineer", "fastlane-head-of-growth",
                      "pulse-technical-co-founder"],
        "talent_slugs": ["ada-okafor"]})).json()
    anon = await client_factory()
    bid = board["id"]

    r = await anon.get(f"/glojobs/{bid}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert '<meta name="robots" content="noindex">' in html
    assert "Roles and people for Ada" in html
    assert "Picked for <em>you</em>" in html                    # the intro is HTML
    assert "3 open roles at 2 companies and 1 person" in html
    assert f'href="/glojobs/{bid}/roles/fastlane-founding-engineer"' in html
    assert f'href="/glojobs/{bid}/people/ada-okafor"' in html
    assert 'data-filter="fastlane"' in html and 'data-filter="pulse"' in html
    assert "Bali</span>" in html                                 # the parenthetical is gone
    assert 'src="/glojobs/static/board.js"' in html
    assert "<script>alert" not in html

    r = await anon.get(f"/glojobs/{bid}/roles/fastlane-founding-engineer")
    assert r.status_code == 200
    assert "<title>Founding Engineer at Fastlane · GLO Hiring</title>" in r.text
    assert "<h2>What you&#39;ll do</h2>" in r.text or "<h2>What you'll do</h2>" in r.text
    assert "Fastlane makes ads." in r.text and "Talk to <strong>customers</strong>" in r.text
    assert "More at Fastlane" in r.text and "Head of Growth" in r.text
    assert "Technical Co-founder" not in r.text                 # another company's role

    r = await anon.get(f"/glojobs/{bid}/people/ada-okafor")
    assert r.status_code == 200 and "<h1 class=\"job-title\">Ada Okafor</h1>" in r.text
    assert "CTO at PayRail" in r.text and "Builds payments rails" in r.text

    # the counter moved for the board page only
    views = (await writer.get(f"/api/boards/{bid}")).json()
    assert views["views"] == 1 and views["last_viewed_at"]

    # a role not on this board, a talent slug on a jobs-only board, unknown and disabled boards
    await writer.patch(f"/api/boards/{bid}", json={"job_slugs": ["pulse-technical-co-founder"]})
    assert (await anon.get(f"/glojobs/{bid}/roles/fastlane-founding-engineer")).status_code == 404
    await writer.patch(f"/api/boards/{bid}", json={"kind": "jobs"})
    assert (await anon.get(f"/glojobs/{bid}/people/ada-okafor")).status_code == 404
    r = await anon.get(f"/glojobs/{uuid.uuid4()}")
    assert r.status_code == 404 and "This page isn't here" in r.text
    assert (await anon.get("/glojobs/not-a-uuid")).status_code == 404
    await writer.patch(f"/api/boards/{bid}", json={"enabled": False})
    assert (await anon.get(f"/glojobs/{bid}")).status_code == 404

    # the bare host, robots, the static files
    r = await anon.get("/glojobs/")
    assert r.status_code == 200 and "GLO" in r.text and bid not in r.text
    assert (await anon.get("/glojobs/robots.txt")).text.startswith("User-agent: *")
    r = await anon.get("/glojobs/static/styles.css")
    assert r.status_code == 200 and 'url("fonts/' in r.text
    assert (await anon.get("/glojobs/static/board.js")).status_code == 200


async def test_board_with_no_open_roles(writer, client_factory, ada):
    board = (await writer.post("/api/boards", json={"person_id": ada, "kind": "jobs"})).json()
    anon = await client_factory()
    r = await anon.get(f"/glojobs/{board['id']}")
    assert r.status_code == 200 and "No open roles on this page right now" in r.text
    assert "board.js" not in r.text


# --- MCP ----------------------------------------------------------------------------------------

@pytest.fixture
async def mcp_keys(writer, reader):
    w = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    r = (await reader.post("/api/api-keys", json={"name": "r"})).json()["key"]
    return w, r


async def test_mcp_board_tools(client_factory, mcp_keys, ada):
    w, r = mcp_keys
    c = await client_factory()
    await init(c, r)
    tools = {t["name"] for t in (await rpc(c, r, "tools/list", id_=2)).json()["result"]["tools"]}
    assert {"list_board_jobs", "get_board", "list_boards"} <= tools
    assert not {"upsert_board_job", "create_board", "delete_board"} & tools

    await init(c, w)
    err, co = await call(c, w, "upsert_board_company", {"slug": "fastlane", "name": "Fastlane",
                                                          "about_html": "<p>Ads</p>"})
    assert not err and co["slug"] == "fastlane"
    err, job = await call(c, w, "upsert_board_job", {
        "company": "fastlane", "title": "Founding Engineer", "html": JD, "comp": "$150K"})
    assert not err, job
    assert job["slug"] == "fastlane-founding-engineer" and job["stripped"] == ["img", "script", "style"]
    err, talent = await call(c, w, "upsert_board_talent", {"person_id": ada})
    assert not err and talent["slug"] == "ada-okafor" and talent["headline"] == "CTO at PayRail"

    # a board for a person who is not in the database yet: matched or created like merge_person
    err, board = await call(c, w, "create_board", {
        "kind": "both", "person": {"name": "Bo Chen", "email": "bo@x.io", "company": "Leafy"},
        "job_slugs": ["fastlane-founding-engineer"], "talent_slugs": ["ada-okafor"],
        "intro_html": "<p>Hi Bo</p>"})
    assert not err, board
    assert board["person_name"] == "Bo Chen" and board["url"].endswith(board["id"])
    bo = (await c.get("/api/people/find?email=bo@x.io",
                      headers={"Authorization": f"Bearer {w}"})).json()
    assert bo["decision"] == "matched" and bo["person_id"] == board["person_id"]

    # the same record again matches the same person
    err, again = await call(c, w, "create_board", {"kind": "jobs", "person": {"name": "Bo Chen", "email": "bo@x.io"}})
    assert not err and again["person_id"] == board["person_id"]

    err, got = await call(c, w, "get_board", {"id": board["id"]})
    assert not err and [j["slug"] for j in got["jobs"]] == ["fastlane-founding-engineer"]
    err, upd = await call(c, w, "update_board", {"id": board["id"], "remove_jobs": ["fastlane-founding-engineer"],
                                                  "add_talent": ["ada-okafor"], "title": "Bo's page"})
    assert not err and upd["job_slugs"] == [] and upd["talent_slugs"] == ["ada-okafor"]
    assert upd["title"] == "Bo's page"
    err, res = await call(c, w, "update_board", {"id": board["id"], "add_jobs": ["nope"]})
    assert err and "unknown job" in str(res)
    err, res = await call(c, w, "create_board", {"kind": "jobs"})
    assert err and "person_id or person" in str(res)
    err, lst = await call(c, w, "list_boards", {"person_id": board["person_id"]})
    assert not err and len(lst) == 2
    err, res = await call(c, w, "delete_board", {"id": board["id"]})
    assert not err and res == {"deleted": board["id"]}
    err, res = await call(c, w, "delete_board_job", {"slug": "fastlane-founding-engineer"})
    assert not err
    err, res = await call(c, w, "get_board_job", {"slug": "fastlane-founding-engineer"})
    assert err
