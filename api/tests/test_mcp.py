import json

import pytest

from app import embedder
from tests.conftest import import_people

HEADERS = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}


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
    result = r.json()["result"]
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
        tools = {t["name"] for t in resp.json()["result"]["tools"]}
        assert {"search_people", "get_person", "list_properties"} <= tools
        assert ("create_person" in tools) is expect_write


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
