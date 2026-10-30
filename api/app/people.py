"""Person create / update / delete shared by the REST API, the MCP server and the workflows.

Field values are never overwritten: every write appends a time-stamped, source-tagged entry to the
field's history (see app.history) and refreshes the cached current value.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId

from app import activity, embeddings, fields, history
from app.auth import Principal
from app.history import Source
from app.mongo import db
from app.search import public_person

log = logging.getLogger(__name__)

Scalar = str | int | float | bool


class PersonError(ValueError):
    pass


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


def _clean(changes: dict[str, Any]) -> dict[str, Any]:
    """Validate keys and normalise values. None / empty means remove the field."""
    out = {}
    for key, value in changes.items():
        if not fields.valid_key(key):
            raise PersonError(f"invalid field key {key!r} (lowercase letters, digits, underscores)")
        out[key] = _check_value(key, value)
    return out


def _oid(person_id: str) -> ObjectId:
    if not ObjectId.is_valid(person_id):
        raise PersonError("invalid person id")
    return ObjectId(person_id)


def _check_tier(tier: str) -> None:
    if tier not in fields.TIERS:
        raise PersonError(f"tier must be one of {fields.TIERS}")


async def get(person_id: str) -> dict | None:
    doc = await db().people.find_one({"_id": _oid(person_id)})
    return public_person(doc, include_history=True) if doc else None


async def create(principal: Principal, tier: str, values: dict[str, Any],
                 source: Source | None = None) -> dict:
    _check_tier(tier)
    cleaned = _clean(values)
    if not cleaned.get("name"):
        raise PersonError("name is required")
    src = source or Source.for_rest(principal)
    now = datetime.now(UTC)
    props = {k: history.new_prop(v, src, now) for k, v in cleaned.items() if v is not None}
    res = await db().people.insert_one({**props, "tier": tier, "created_at": now,
                                        "updated_at": now,
                                        "schema_version": history.SCHEMA_VERSION})
    pid = str(res.inserted_id)
    await _after_write(pid, {k: v for k, v in cleaned.items() if v is not None})
    await activity.record("person.create", principal, pid,
                          {"fields": sorted(props), "src": src.kind})
    return await get(pid)


async def update(principal: Principal, person_id: str, values: dict[str, Any],
                 tier: str | None = None, source: Source | None = None) -> dict | None:
    """Append new values to the person's fields. Returns the person, or None if not found."""
    oid = _oid(person_id)
    cleaned = _clean(values)
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
    written = {k: cleaned[k] for k in changed if k != "tier"}
    if changed or removed:
        await _after_write(person_id, written)
    await activity.record("person.update", principal, person_id,
                          {"set": sorted(changed), "unset": sorted(removed),
                           "unchanged": sorted(unchanged), "src": src.kind})
    return await get(person_id)


async def delete(principal: Principal, person_id: str) -> bool:
    res = await db().people.delete_one({"_id": _oid(person_id)})
    if res.deleted_count == 0:
        return False
    await embeddings.delete_people([person_id])
    await activity.record("person.delete", principal, person_id)
    return True


async def _after_write(person_id: str, written: dict[str, Any]) -> None:
    if written:
        await fields.register({k: [v] for k, v in written.items()})
    await fields.refresh_counts()
    try:
        await embeddings.sync_people([person_id])
    except Exception:
        # The Mongo write is already persisted; a vector failure must not fail the request.
        # `python -m app.cli embed` heals any gap.
        log.exception("embedding sync failed for person %s", person_id)
