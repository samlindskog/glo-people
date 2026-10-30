"""The person document service: the only writer of the `people` collection.

Field values are never overwritten: every write appends a time-stamped, source-tagged entry to the
field's history (see app.history) and refreshes the cached current value. Writes accept only
properties that exist (app.properties); the importer creates properties before it calls
`upsert_imported`. Other systems read through the helpers here (`fetch`, `find`, `count`,
`find_ids`, `coverage`, `value_counts`) and react to writes through `on_write` hooks that the
composition root registers (e.g. embeddings).
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo import UpdateOne

from app import activity, fields, history, mongo, properties
from app.auth import Principal
from app.history import Source
from app.mongo import db

log = logging.getLogger(__name__)

Scalar = str | int | float | bool
WRITE_BATCH = 1000

_hooks: list[Callable[[str], Awaitable[None]]] = []


class PersonError(ValueError):
    pass


def on_write(hook: Callable[[str], Awaitable[None]]) -> None:
    """Register a coroutine called with the person id after every successful write."""
    _hooks.append(hook)


# --- reads ----------------------------------------------------------------------------------------

def public(doc: dict, include_history: bool = False) -> dict:
    """Flat view (current values) of a person; with `history`, every field's entries too."""
    out: dict[str, Any] = {"id": str(doc["_id"])}
    for k in ("tier", "source", "created_at", "updated_at", "schema_version"):
        if k in doc:
            out[k] = doc[k].isoformat() if hasattr(doc[k], "isoformat") else doc[k]
    out.update(history.flatten(doc, fields.SYSTEM_FIELDS))
    if include_history:
        out["history"] = {k: history.public_entries(v) for k, v in doc.items()
                          if k not in fields.SYSTEM_FIELDS}
    return out


async def get(person_id: str) -> dict | None:
    doc = await db().people.find_one({"_id": _oid(person_id)})
    return public(doc, include_history=True) if doc else None


async def fetch(ids: list[str], projection: dict | None = None) -> list[dict]:
    """Raw documents for these ids (unknown or invalid ids are skipped)."""
    oids = [ObjectId(i) for i in ids if ObjectId.is_valid(i)]
    if not oids:
        return []
    return await db().people.find({"_id": {"$in": oids}}, projection).to_list(None)


async def find(query: dict, *, sort: list[tuple[str, int]] | None = None, skip: int = 0,
               limit: int = 0, projection: dict | None = None) -> list[dict]:
    cursor = db().people.find(query, projection)
    if sort:
        cursor = cursor.sort(sort)
    if skip:
        cursor = cursor.skip(skip)
    if limit:
        cursor = cursor.limit(limit)
    return await cursor.to_list(None)


async def count(query: dict) -> int:
    return await db().people.count_documents(query)


async def find_ids(query: dict, limit: int = 0) -> list[str]:
    docs = await find(query, sort=[("_id", 1)], limit=limit, projection={"_id": 1})
    return [str(d["_id"]) for d in docs]


async def coverage() -> dict[str, int]:
    """{property key: number of people whose current value is present (not a tombstone)}."""
    pipeline = [
        {"$project": {"kv": {"$objectToArray": "$$ROOT"}}},
        {"$unwind": "$kv"},
        {"$match": {"kv.v.v": {"$exists": True, "$nin": [None, "", []]}}},
        {"$group": {"_id": "$kv.k", "count": {"$sum": 1}}},
    ]
    return {d["_id"]: d["count"] async for d in await db().people.aggregate(pipeline)}


async def value_counts(key: str, limit: int = 200) -> list[tuple[Any, int]]:
    """The most common current values of one property (list items counted individually)."""
    if not fields.valid_key(key):
        raise PersonError(f"invalid field key {key!r}")
    path = f"{key}.v"
    pipeline = [
        {"$match": {path: {"$exists": True, "$nin": [None, ""]}}},
        {"$project": {"v": f"${path}"}},
        {"$unwind": "$v"},
        {"$group": {"_id": "$v", "n": {"$sum": 1}}},
        {"$sort": {"n": -1, "_id": 1}},
        {"$limit": limit},
    ]
    return [(d["_id"], d["n"]) async for d in await db().people.aggregate(pipeline)]


# --- writes ---------------------------------------------------------------------------------------

def _check_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, list) and all(isinstance(v, Scalar) for v in value):
        cleaned = [v.strip() if isinstance(v, str) else v for v in value]
        return [v for v in cleaned if v != ""] or None
    if value is None:
        return None
    raise PersonError(f"field {key!r}: values must be strings, numbers, booleans or lists of them")


async def _clean(changes: dict[str, Any]) -> dict[str, Any]:
    """Validate keys against the property catalog and normalise values. None / empty means
    remove the field. Unknown keys are refused: properties are created only by the importer and
    the storage agent through `properties.ensure`."""
    known = await properties.known_keys()
    out = {}
    for key, value in changes.items():
        if not fields.valid_key(key):
            raise PersonError(f"invalid field key {key!r} (lowercase letters, digits, underscores)")
        if key not in known:
            raise PersonError(f"unknown property {key!r}; the catalog is at GET /api/properties")
        out[key] = _check_value(key, value)
    return out


def _oid(person_id: str) -> ObjectId:
    if not ObjectId.is_valid(person_id):
        raise PersonError("invalid person id")
    return ObjectId(person_id)


def _check_tier(tier: str) -> None:
    if tier not in fields.TIERS:
        raise PersonError(f"tier must be one of {fields.TIERS}")


async def create(principal: Principal, tier: str, values: dict[str, Any],
                 source: Source | None = None) -> dict:
    _check_tier(tier)
    cleaned = await _clean(values)
    if not cleaned.get("name"):
        raise PersonError("name is required")
    src = source or Source.for_rest(principal)
    now = datetime.now(UTC)
    props = {k: history.new_prop(v, src, now) for k, v in cleaned.items() if v is not None}
    res = await db().people.insert_one({**props, "tier": tier, "created_at": now,
                                        "updated_at": now,
                                        "schema_version": history.SCHEMA_VERSION})
    pid = str(res.inserted_id)
    await _after_write(pid)
    await activity.record("person.create", principal, pid,
                          {"fields": sorted(props), "src": src.kind})
    return await get(pid)


async def update(principal: Principal, person_id: str, values: dict[str, Any],
                 tier: str | None = None, source: Source | None = None) -> dict | None:
    """Append new values to the person's fields. Returns the person, or None if not found."""
    oid = _oid(person_id)
    cleaned = await _clean(values)
    if "name" in cleaned and cleaned["name"] is None:
        raise PersonError("name cannot be removed")
    if tier is not None:
        _check_tier(tier)
    doc = await db().people.find_one({"_id": oid})
    if doc is None:
        return None
    src = source or Source.for_rest(principal)
    now = datetime.now(UTC)

    to_set, changed, removed, unchanged = history.apply(doc, cleaned, src, now,
                                                        fields.SYSTEM_FIELDS)
    if tier is not None and tier != doc.get("tier"):
        to_set["tier"] = tier
        changed.append("tier")

    if changed or removed:
        to_set["updated_at"] = now
    if to_set or doc.get("schema_version") != history.SCHEMA_VERSION:
        to_set["schema_version"] = history.SCHEMA_VERSION
        await db().people.update_one({"_id": oid}, {"$set": to_set})
    if changed or removed:
        await _after_write(person_id)
    await activity.record("person.update", principal, person_id,
                          {"set": sorted(changed), "unset": sorted(removed),
                           "unchanged": sorted(unchanged), "src": src.kind})
    return await get(person_id)


async def delete(principal: Principal, person_id: str) -> bool:
    res = await db().people.delete_one({"_id": _oid(person_id)})
    if res.deleted_count == 0:
        return False
    for hook in _hooks:
        try:
            await hook(person_id)
        except Exception:
            log.exception("write hook failed after deleting person %s", person_id)
    await activity.record("person.delete", principal, person_id)
    return True


async def upsert_imported(rows: list[tuple[str, int, dict[str, Any]]], source_file: str,
                          tier: str, now: datetime) -> tuple[int, int]:
    """Bulk upsert of imported rows `(source_id, row_no, values)` keyed by (file, source_id).
    The caller has created the properties the rows use (`properties.ensure`).

    Rows sharing a source_id fold into one update applied in file order, so no row's entry is lost
    to a later $set of the same property. Returns (inserted, updated). Values are appended to the
    field histories like any other write; unchanged values append nothing and leave `updated_at`.
    """
    inserted = updated = 0
    system = fields.SYSTEM_FIELDS
    for start in range(0, len(rows), WRITE_BATCH):
        chunk = rows[start:start + WRITE_BATCH]
        existing = {d["source"]["source_id"]: d for d in await find(
            {"source.file": source_file, "source.source_id": {"$in": [sid for sid, _, _ in chunk]}})}
        pending: dict[str, dict[str, Any]] = {}
        for source_id, row_no, values in chunk:
            src = Source("import", source_file, f"row {row_no}")
            cur = pending.get(source_id) or existing.get(source_id)
            to_set, changed, _, _ = history.apply(cur or {}, values, src, now, system)
            set_doc: dict[str, Any] = {
                **(pending.get(source_id) or {}), **to_set, "tier": tier,
                "source": {"file": source_file, "source_id": source_id, "row": row_no},
                "schema_version": history.SCHEMA_VERSION}
            if changed or cur is None:
                set_doc["updated_at"] = now
            pending[source_id] = set_doc
        ops = [UpdateOne({"source.file": source_file, "source.source_id": sid},
                         {"$set": set_doc, "$setOnInsert": {"created_at": now}}, upsert=True)
               for sid, set_doc in pending.items()]
        res = await db().people.bulk_write(ops, ordered=False)
        inserted += res.upserted_count
        updated += res.matched_count
    return inserted, updated


async def _after_write(person_id: str) -> None:
    for hook in _hooks:
        try:
            await hook(person_id)
        except Exception:
            # The write is already persisted; a hook failure (e.g. the vector store) must not fail
            # the request. `python -m app.cli embed` heals any embedding gap.
            log.exception("write hook %s failed for person %s", getattr(hook, "__name__", hook),
                          person_id)


# --- migration --------------------------------------------------------------------------------------

async def migrate_legacy(dry_run: bool = False, page: int = 500) -> int:
    """Wrap every bare field value on every person into a property (see app.history). Idempotent;
    never bumps updated_at. Returns the number of documents migrated."""
    people = db().people
    now = datetime.now(UTC)
    migrated = 0
    last = None
    while True:
        q: dict[str, Any] = {"schema_version": {"$ne": history.SCHEMA_VERSION}}
        if last is not None:
            q["_id"] = {"$gt": last}
        batch = await people.find(q).sort("_id", 1).limit(page).to_list(None)
        if not batch:
            break
        last = batch[-1]["_id"]
        for doc in batch:
            upgrades = history.upgrade_doc(doc, now, fields.SYSTEM_FIELDS)
            if dry_run:
                migrated += 1
                continue
            res = await people.update_one(
                {"_id": doc["_id"], "schema_version": {"$ne": history.SCHEMA_VERSION}},
                {"$set": {**upgrades, "schema_version": history.SCHEMA_VERSION}})
            migrated += res.modified_count
    if migrated and not dry_run:
        log.info("migrated %d people documents to field history", migrated)
    if not dry_run:
        indexes = await people.index_information()
        if "name_1" in indexes:
            await people.drop_index("name_1")
        await mongo.ensure_indexes()
    return migrated
