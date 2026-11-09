import json

import pytest

from app import embedder
from tests.conftest import import_people

HEADERS = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}


def body(r):
    """The JSON-RPC message of a response: plain JSON, or the data line of an SSE stream (the
    2025-era wire streams every response in SSE mode; the 2026 wire streams only long calls)."""
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        lines = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        return json.loads(lines[-1])
    return r.json()


async def rpc(client, key, method, params=None, id_=1):
    h = {**HEADERS, "Authorization": f"Bearer {key}"} if key else HEADERS
    r = await client.post("/mcp", headers=h,
                          json={"jsonrpc": "2.0", "id": id_, "method": method,
                                "params": params or {}})
    return r


async def init(client, key):
    r = await rpc(client, key, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}})
    assert r.status_code == 200, r.text
    return r


async def call(client, key, tool, args):
    """Returns (is_error, payload) where payload is the tool's JSON result."""
    r = await rpc(client, key, "tools/call", {"name": tool, "arguments": args}, 2)
    assert r.status_code == 200, r.text
    result = body(r)["result"]
    if "structuredContent" in result:
        data = result["structuredContent"]
        data = data.get("result", data) if isinstance(data, dict) and len(data) == 1 else data
    else:
        text = result["content"][0]["text"]
        try:
            data = json.loads(text)
        except ValueError:
            data = text
    return bool(result.get("isError")), data


@pytest.fixture
async def keys(writer, reader):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    csv = "Name,Bio\nAda,payments infrastructure\nBo,climate investor\n"
    await import_people(writer, csv, "p.csv", "member")
    w = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    r = (await reader.post("/api/api-keys", json={"name": "r"})).json()["key"]
    return w, r


async def test_requires_key(client_factory):
    c = await client_factory()
    assert (await rpc(c, None, "tools/list")).status_code == 401
    assert (await rpc(c, "glo_nope_nope", "tools/list")).status_code == 401


async def test_tool_visibility_by_role(client_factory, keys):
    w, r = keys
    c = await client_factory()
    for key, expect_write in ((w, True), (r, False)):
        await init(c, key)
        resp = await rpc(c, key, "tools/list", id_=2)
        tools = {t["name"] for t in body(resp)["result"]["tools"]}
        assert {"search_people", "get_person", "list_properties"} <= tools
        assert ("create_person" in tools) is expect_write
        assert ("merge_person" in tools) is expect_write


async def test_search_parity_with_rest(client_factory, keys, reader):
    _, r = keys
    c = await client_factory()
    await init(c, r)
    args = {"semantic": {"query": "payments", "fields": ["bio"], "mode": "max",
                         "threshold": 0.0},
            "filters": [{"field": "name", "op": "contains", "value": "a"}]}
    err, res = await call(c, r, "search_people", args)
    assert not err, res
    mcp_ids = [h["id"] for h in res["results"]]
    rest = (await reader.post("/api/search", json=args)).json()
    assert mcp_ids == [h["id"] for h in rest["results"]] and mcp_ids

    hist = (await reader.get("/api/searches")).json()["items"]
    assert hist[1]["source"] == "mcp"


async def test_reader_key_cannot_write(client_factory, keys):
    w, r = keys
    c = await client_factory()
    await init(c, r)
    err, _ = await call(c, r, "create_person", {"tier": "member", "fields": {"name": "Z"}})
    assert err
    await init(c, w)
    err, res = await call(c, w, "create_person", {"tier": "member", "fields": {"name": "Z"}})
    assert not err, res
    _, got = await call(c, r, "get_person", {"id": res["id"]})
    assert got["name"] == "Z"
    _, fl = await call(c, r, "list_properties", {})
    assert any(f["key"] == "bio" and f["embeddable"] and f["count"] == 2 for f in fl)
    err, msg = await call(c, w, "update_person", {"id": res["id"], "fields": {"gadget": "x"}})
    assert err and "unknown property" in str(msg)


async def test_update_records_source_and_history(client_factory, keys):
    w, _ = keys
    c = await client_factory()
    await init(c, w)
    _, res = await call(c, w, "create_person", {"tier": "member", "fields": {"name": "Q", "bio": "one"}})
    assert res["history"]["name"][0]["src"]["kind"] == "mcp"
    _, res = await call(c, w, "update_person", {"id": res["id"], "fields": {"bio": "two"},
                                                "agent": "scout"})
    assert res["bio"] == "two"
    src = res["history"]["bio"][-1]["src"]
    # The agent name is only a note: an MCP caller cannot forge a workflow ("agent") entry.
    assert src["kind"] == "mcp" and src["by"] == "wendy" and src["ref"].endswith(" agent:scout")
    _, res = await call(c, w, "update_person", {"id": res["id"], "fields": {"bio": "two"}})
    assert len(res["history"]["bio"]) == 2
    _, got = await call(c, w, "get_person", {"id": res["id"]})
    assert [e["v"] for e in got["history"]["bio"]] == ["one", "two"]


async def test_transport_streams_long_calls(client_factory, keys):
    """A long tool call answers as an SSE stream with keepalive pings (Cloudflare drops a proxied
    response after 100 s without bytes), so a client must accept text/event-stream. On the 2026
    wire Claude speaks, a quick call still answers plain JSON; the 2025 wire streams every
    response."""
    w, _ = keys
    c = await client_factory()
    await init(c, w)
    r = await c.post("/mcp", headers={**HEADERS, "Accept": "application/json",
                                      "Authorization": f"Bearer {w}"},
                     json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert r.status_code == 406
    r = await rpc(c, w, "tools/list", id_=3)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "list_properties" in {t["name"] for t in body(r)["result"]["tools"]}
    r = await c.post("/mcp", headers={**HEADERS, "MCP-Protocol-Version": "2026-07-28",
                                      "MCP-Method": "tools/list", "Authorization": f"Bearer {w}"},
                     json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {
                         "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                   "io.modelcontextprotocol/clientCapabilities": {}}}})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert "list_properties" in {t["name"] for t in r.json()["result"]["tools"]}


async def test_no_call_blocks_past_the_wait_cap(client_factory, keys):
    """Claude Code gives a tool call 60 s by default: start_assistant refuses a longer wait."""
    w, _ = keys
    c = await client_factory()
    await init(c, w)
    err, data = await call(c, w, "start_assistant", {
        "key": "normalize", "where": [{"field": "name", "op": "exists", "value": True}],
        "options": {"enrich": False}, "wait_seconds": 120})
    assert err and "50" in str(data)


async def test_get_people_reads_several_at_once(client_factory, keys):
    w, _ = keys
    c = await client_factory()
    await init(c, w)
    err, hits = await call(c, w, "search_people", {"tier": ["member", "plus_one"], "fields": ["name"]})
    assert not err and len(hits["results"]) == 2
    assert all(set(h["person"]) == {"id", "tier", "name"} for h in hits["results"])   # trimmed, not dropped
    ids = [h["id"] for h in hits["results"]]
    names = [h["person"]["name"] for h in hits["results"]]
    err, got = await call(c, w, "get_people", {"ids": [ids[1], ids[0], "000000000000000000000000", ids[0]],
                                                "fields": ["bio"]})
    assert not err, got
    assert [p["name"] for p in got["people"]] == [names[1], names[0]]
    assert set(got["people"][0]) == {"id", "tier", "name", "bio"} and "history" not in got["people"][0]
    assert got["missing"] == ["000000000000000000000000"]
    err, got = await call(c, w, "get_people", {"ids": [ids[0]], "include_history": True})
    assert not err and "history" in got["people"][0]
    err, _ = await call(c, w, "get_people", {"ids": []})
    assert err
