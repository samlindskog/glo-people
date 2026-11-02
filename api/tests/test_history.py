from datetime import UTC, datetime

from app import history
from app.auth import Principal
from app.fields import SYSTEM_FIELDS
from app.history import Source

TS = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
UI = Source("ui", "wendy")


def test_append_and_dedupe():
    p = history.append(None, "a", UI, TS)
    assert p == {"v": "a", "h": [{"v": "a", "ts": TS, "src": {"kind": "ui", "by": "wendy"}}]}
    assert history.append(p, "a", UI, TS) is None
    assert history.append(None, None, UI, TS) is None          # removing an absent field
    p2 = history.append(p, "b", Source("agent", "enrich/x", "run:1/item:2"), TS)
    assert p2["v"] == "a" and [e["v"] for e in p2["h"]] == ["a", "b"]   # recorded, not shown
    assert p2["h"][1]["src"] == {"kind": "agent", "by": "enrich/x", "ref": "run:1/item:2"}
    assert history.append(p2, "b", Source("agent", "enrich/x"), TS) is None   # a repeat adds nothing
    p3 = history.append(p, "b", UI, TS)
    assert p3["v"] == "b"                                       # a person's own edit shows
    gone = history.append(p3, None, UI, TS)
    assert gone["v"] is None and len(gone["h"]) == 3
    assert history.append(gone, None, UI, TS) is None          # already removed
    back = history.append(gone, "a", UI, TS)
    assert back["v"] == "a" and len(back["h"]) == 4            # A -> removed -> A keeps all


def test_same_is_type_strict():
    assert history.same(1, 1) and not history.same(1, True) and not history.same(12000, 12000.0)
    assert history.same(["a", "b"], ["a", "b"]) and not history.same(["a", "b"], ["b", "a"])
    assert not history.same(["a"], "a") and history.same(None, None)


def test_legacy_values_are_tolerated():
    assert history.current("x") == "x" and history.current(None) is None
    assert history.current({"v": "y", "h": []}) == "y"
    assert history.entries("x") == [{"v": "x"}] and history.entries(None) == []
    p = history.append("x", "y", UI, TS)
    assert [e["v"] for e in p["h"]] == ["x", "y"] and "ts" not in p["h"][0]
    doc = {"_id": 1, "tier": "member", "name": "N", "bio": {"v": "b", "h": []},
           "gone": {"v": None, "h": []}}
    assert history.flatten(doc, SYSTEM_FIELDS) == {"name": "N", "bio": "b"}


def test_upgrade_doc_sources_and_timestamps():
    created, updated = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    imported = {"_id": 1, "tier": "member", "name": "A", "tags": ["x"], "created_at": created,
                "updated_at": updated, "source": {"file": "m.csv", "source_id": "1", "row": 5},
                "done": {"v": "already", "h": []}}
    up = history.upgrade_doc(imported, TS, SYSTEM_FIELDS)
    assert set(up) == {"name", "tags"}
    assert up["tags"] == {"v": ["x"], "h": [{"v": ["x"], "ts": updated,
                                            "src": {"kind": "import", "by": "m.csv", "ref": "row 5"}}]}
    legacy = {"_id": 2, "name": "B", "created_at": created}
    up = history.upgrade_doc(legacy, TS, SYSTEM_FIELDS)
    assert up["name"]["h"][0]["ts"] == created and up["name"]["h"][0]["src"] == {"kind": "legacy"}
    assert history.upgrade_doc({"_id": 3, "name": "C"}, TS, SYSTEM_FIELDS)["name"]["h"][0]["ts"] == TS


def test_sources_from_principals():
    ui = Principal(user_id=1, username="wendy", role="writer", permissions=frozenset())
    key = Principal(user_id=1, username="wendy", role="writer", permissions=frozenset(),
                    api_key_id=7)
    assert Source.for_rest(ui) == Source("ui", "wendy")
    assert Source.for_rest(key) == Source("api", "wendy", "key:7")
    assert Source.for_mcp(key) == Source("mcp", "wendy", "key:7")
    assert Source.for_mcp(key, "scout") == Source("mcp", "wendy", "key:7 agent:scout")
    assert Source("legacy").doc() == {"kind": "legacy"}


def test_apply_diffs_a_document():
    doc = {"_id": 1, "tier": "member", "name": "N", "bio": {"v": "old", "h": [{"v": "old"}]},
           "legacy": "flat"}
    to_set, changed, removed, unchanged, kept = history.apply(
        doc, {"bio": "new", "name": "N", "tags": None, "legacy": None}, UI, TS, SYSTEM_FIELDS)
    assert changed == ["bio"] and removed == ["legacy"] and unchanged == ["name", "tags"]
    assert kept == []
    assert to_set["bio"]["v"] == "new" and len(to_set["bio"]["h"]) == 2
    assert to_set["legacy"]["v"] is None and [e["v"] for e in to_set["legacy"]["h"]] == ["flat", None]
    assert to_set["name"]["v"] == "N"            # legacy field upgraded even though unchanged


AGENT = Source("agent", "enrich/linkedin_reader", "run:1/item:1")
IMPORT = Source("import", "m.csv", "row 2")


def _h(*specs):
    """Entries from (value, source[, derived]) tuples."""
    return [history.new_entry(v, src, TS, *(rest or ())) for v, src, *rest in specs]


def test_pick_by_trust_then_position():
    h = _h(("Google", IMPORT), ("Anthropic", AGENT))
    assert history.pick(h) == (1, 1)                              # a finding beats an upload
    h = _h(("Anthropic", AGENT), ("Google", IMPORT))
    assert history.pick(h) == (0, 1)                              # a later upload does not displace it
    assert "finding" in history.explain(h, 0) and "import" in history.explain(h, 0)
    h = _h(("Anthropic", AGENT), ("Google", UI))
    assert history.pick(h) == (1, 2)                              # a person's own edit wins
    h = _h(("Google", UI), ("Anthropic", AGENT), ("Meta", Source("mcp", "wendy", "key:1")))
    assert history.pick(h) == (2, 2)                              # newest within the human class
    assert history.explain(h, 2) == "newest entry"
    h = _h(("a", Source("legacy")), ("b", IMPORT))
    assert history.pick(h) == (1, 0)                              # equal trust: newest


def test_derived_and_system_entries_supersede_and_inherit_trust():
    h = _h(("alina", UI), ("Alina", Source("system", "repair")))
    assert history.pick(h) == (1, 2)                              # a mechanical fix of a human value
    h = _h(("alina", UI), ("Alina Kharina", AGENT, True), ("Alina", IMPORT))
    assert history.pick(h) == (1, 2)                              # the completion keeps human trust
    assert history.explain(h, 1) == "derived from the value shown before it"
    h = _h((["a"], UI), (["a", "b"], IMPORT, True))
    assert history.pick(h) == (1, 2)                              # a merge from an upload shows


def test_tombstones_follow_trust_and_restore_is_a_rewrite():
    p = history.append(None, "Google", IMPORT, TS)
    p = history.append(p, None, UI, TS)                           # a person removed it
    assert p["v"] is None
    p = history.append(p, "Google", AGENT, TS)                    # a finding cannot bring it back
    assert p["v"] is None and len(p["h"]) == 3
    p = history.append(p, "Google", UI, TS)                       # the person can
    assert p["v"] == "Google" and len(p["h"]) == 4
    pub = history.public_entries(p)
    assert [e.get("shown") for e in pub] == [None, None, None, True]
    assert pub[3]["why"] == "newest entry"


def test_apply_reports_kept_keys():
    doc = {"_id": 1, "company": history.append(None, "Anthropic", AGENT, TS)}
    to_set, changed, removed, unchanged, kept = history.apply(
        doc, {"company": "Google", "role": "CEO"}, IMPORT, TS, SYSTEM_FIELDS)
    assert changed == ["role"] and kept == ["company"] and unchanged == [] and removed == []
    assert to_set["company"]["v"] == "Anthropic" and len(to_set["company"]["h"]) == 2
