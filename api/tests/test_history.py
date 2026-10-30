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
    assert p2["v"] == "b" and [e["v"] for e in p2["h"]] == ["a", "b"]
    assert p2["h"][1]["src"] == {"kind": "agent", "by": "enrich/x", "ref": "run:1/item:2"}
    gone = history.append(p2, None, UI, TS)
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
    to_set, changed, removed, unchanged = history.apply(
        doc, {"bio": "new", "name": "N", "tags": None, "legacy": None}, UI, TS, SYSTEM_FIELDS)
    assert changed == ["bio"] and removed == ["legacy"] and unchanged == ["name", "tags"]
    assert to_set["bio"]["v"] == "new" and len(to_set["bio"]["h"]) == 2
    assert to_set["legacy"]["v"] is None and [e["v"] for e in to_set["legacy"]["h"]] == ["flat", None]
    assert to_set["name"]["v"] == "N"            # legacy field upgraded even though unchanged
