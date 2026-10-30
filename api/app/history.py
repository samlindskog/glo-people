"""Per-field history on people documents.

Every non-system field on a person is a *property*: `{"v": <current value>, "h": [entries]}`, where
`h` is append-only (oldest first) and `v` is a cached copy of `h[-1]["v"]` so Mongo queries, sorts
and indexes work on `field.v`. An entry is `{"v": value | None, "ts": datetime, "src": {...}}`;
`v: None` is a tombstone (the field was removed). `src` records where the value came from:
`{"kind": ui|api|mcp|agent|import|legacy, "by": <user, agent or file>, "ref": <key/run/row>}`.
Not to be confused with the document-level `source` sub-document the CSV importer writes.

Documents written before this module existed hold bare scalars; every reader here tolerates them
(`entries()` synthesises one entry) and `upgrade_doc()` / `migrate_legacy()` wrap them for good.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from app.auth import Principal

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SourceKind = Literal["ui", "api", "mcp", "agent", "import", "legacy"]


@dataclass(frozen=True)
class Source:
    kind: SourceKind
    by: str | None = None
    ref: str | None = None

    def doc(self) -> dict[str, str]:
        out: dict[str, str] = {"kind": self.kind}
        if self.by:
            out["by"] = self.by
        if self.ref:
            out["ref"] = self.ref
        return out

    @staticmethod
    def for_rest(p: Principal) -> "Source":
        if p.via_api_key:
            return Source("api", p.username, f"key:{p.api_key_id}")
        return Source("ui", p.username)

    @staticmethod
    def for_mcp(p: Principal, agent: str | None = None) -> "Source":
        """An MCP write is always `kind: mcp` by the key's owner; a caller-supplied agent name is
        kept in `ref` so it can never pass for a workflow write (`kind: agent`)."""
        ref = f"key:{p.api_key_id}"
        if agent and agent.strip():
            ref += f" agent:{agent.strip()[:64]}"
        return Source("mcp", p.username, ref)


def is_prop(x: Any) -> bool:
    return isinstance(x, dict) and "h" in x


def current(prop: Any) -> Any:
    """The current value of a property (or a legacy bare value)."""
    return prop.get("v") if is_prop(prop) else prop


def entries(prop: Any) -> list[dict]:
    """The history entries of a property; a legacy bare value becomes one synthetic entry."""
    if prop is None:
        return []
    if is_prop(prop):
        return list(prop["h"])
    return [{"v": prop}]


def same(a: Any, b: Any) -> bool:
    """Dedupe equality: type-strict (1 != True, 12000 != 12000.0), lists element-wise in order."""
    if isinstance(a, list) or isinstance(b, list):
        return (isinstance(a, list) and isinstance(b, list) and len(a) == len(b)
                and all(same(x, y) for x, y in zip(a, b, strict=True)))
    return type(a) is type(b) and a == b


def new_entry(value: Any, src: Source, ts: datetime) -> dict:
    return {"v": value, "ts": ts, "src": src.doc()}


def new_prop(value: Any, src: Source, ts: datetime) -> dict:
    return {"v": value, "h": [new_entry(value, src, ts)]}


def append(prop: Any, value: Any, src: Source, ts: datetime) -> dict | None:
    """The property with `value` appended, or None when that would change nothing.

    `prop` may be None (field absent), a legacy bare value, or a property. Appending the current
    value is a no-op; so is removing (value None) an absent or already removed field.
    """
    if prop is None:
        return None if value is None else new_prop(value, src, ts)
    if not is_prop(prop):
        prop = {"v": prop, "h": [{"v": prop}]}
    if same(prop.get("v"), value):
        return None
    return {"v": value, "h": [*prop["h"], new_entry(value, src, ts)]}


def apply(doc: dict, changes: dict[str, Any], src: Source, now: datetime,
          system: frozenset[str]) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    """Diff `changes` ({key: value | None}) against `doc` and return what to `$set`.

    Returns (to_set, changed, removed, unchanged). `to_set` holds the upgraded form of every
    legacy bare field (so a first write migrates the whole document) plus the appended
    properties; `changed`/`removed`/`unchanged` list the keys by outcome.
    """
    upgraded = upgrade_doc(doc, now, system)
    props = {**{k: v for k, v in doc.items() if k not in system}, **upgraded}
    to_set: dict[str, Any] = dict(upgraded)
    changed, removed, unchanged = [], [], []
    for key, value in changes.items():
        new = append(props.get(key), value, src, now)
        if new is None:
            unchanged.append(key)
            continue
        to_set[key] = props[key] = new
        (removed if value is None else changed).append(key)
    return to_set, changed, removed, unchanged


def _legacy_source(doc: dict) -> Source:
    src = doc.get("source")
    if isinstance(src, dict) and src.get("file"):
        row = src.get("row")
        return Source("import", str(src["file"]), f"row {row}" if row is not None else None)
    return Source("legacy")


def upgrade_doc(doc: dict, now: datetime, system: frozenset[str]) -> dict[str, dict]:
    """{key: property} for every non-system key of `doc` that is still a bare value."""
    out: dict[str, dict] = {}
    ts = doc.get("updated_at") or doc.get("created_at") or now
    src = _legacy_source(doc)
    for key, value in doc.items():
        if key in system or is_prop(value):
            continue
        if isinstance(value, dict):
            log.warning("person %s field %s holds a dict; left as is", doc.get("_id"), key)
            continue
        out[key] = new_prop(value, src, ts)
    return out


def flatten(doc: dict, system: frozenset[str]) -> dict[str, Any]:
    """{key: current value} for the non-system keys whose current value is not a tombstone."""
    out = {}
    for key, value in doc.items():
        if key in system:
            continue
        cur = current(value)
        if cur is not None:
            out[key] = cur
    return out


def public_entries(prop: Any) -> list[dict]:
    out = []
    for e in entries(prop):
        pub = dict(e)
        ts = pub.get("ts")
        if hasattr(ts, "isoformat"):
            pub["ts"] = ts.isoformat()
        out.append(pub)
    return out


async def migrate_legacy(dry_run: bool = False, page: int = 500) -> int:
    """Wrap every bare field value on every person into a property. Idempotent; never bumps
    updated_at. Returns the number of documents migrated."""
    from app import fields, mongo

    people = mongo.db().people
    now = datetime.now(UTC)
    migrated = 0
    last = None
    while True:
        q: dict[str, Any] = {"schema_version": {"$ne": SCHEMA_VERSION}}
        if last is not None:
            q["_id"] = {"$gt": last}
        batch = await people.find(q).sort("_id", 1).limit(page).to_list(None)
        if not batch:
            break
        last = batch[-1]["_id"]
        for doc in batch:
            upgrades = upgrade_doc(doc, now, fields.SYSTEM_FIELDS)
            if dry_run:
                migrated += 1
                continue
            res = await people.update_one(
                {"_id": doc["_id"], "schema_version": {"$ne": SCHEMA_VERSION}},
                {"$set": {**upgrades, "schema_version": SCHEMA_VERSION}})
            migrated += res.modified_count
    if migrated and not dry_run:
        log.info("migrated %d people documents to field history", migrated)
    if not dry_run:
        indexes = await people.index_information()
        if "name_1" in indexes:
            await people.drop_index("name_1")
        await mongo.ensure_indexes()
        if migrated:
            await fields.refresh_counts()
    return migrated
