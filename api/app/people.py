"""Person create / update / delete shared by the REST API and the MCP server."""

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId

from app import activity, embeddings, fields
from app.auth import Principal
from app.mongo import db
from app.search import public_person

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


def _clean(changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Split changes into ($set, $unset). None / empty means remove the field."""
    to_set, to_unset = {}, []
    for key, value in changes.items():
        if not fields.valid_key(key):
            raise PersonError(f"invalid field key {key!r} (lowercase letters, digits, underscores)")
        value = _check_value(key, value)
        if value is None:
            to_unset.append(key)
        else:
            to_set[key] = value
    return to_set, to_unset


def _oid(person_id: str) -> ObjectId:
    if not ObjectId.is_valid(person_id):
        raise PersonError("invalid person id")
    return ObjectId(person_id)


async def get(person_id: str) -> dict | None:
    doc = await db().people.find_one({"_id": _oid(person_id)})
    return public_person(doc) if doc else None


async def create(principal: Principal, tier: str, values: dict[str, Any]) -> dict:
    if tier not in fields.TIERS:
        raise PersonError(f"tier must be one of {fields.TIERS}")
    to_set, _ = _clean(values)
    if not to_set.get("name"):
        raise PersonError("name is required")
    now = datetime.now(UTC)
    res = await db().people.insert_one({**to_set, "tier": tier, "created_at": now,
                                        "updated_at": now})
    pid = str(res.inserted_id)
    await _after_write(pid, to_set)
    await activity.record("person.create", principal, pid, {"fields": sorted(to_set)})
    return await get(pid)


async def update(principal: Principal, person_id: str, values: dict[str, Any],
                 tier: str | None = None) -> dict | None:
    oid = _oid(person_id)
    to_set, to_unset = _clean(values)
    if "name" in to_unset:
        raise PersonError("name cannot be removed")
    if tier is not None:
        if tier not in fields.TIERS:
            raise PersonError(f"tier must be one of {fields.TIERS}")
        to_set["tier"] = tier
    update_doc: dict[str, Any] = {"$set": {**to_set, "updated_at": datetime.now(UTC)}}
    if to_unset:
        update_doc["$unset"] = {k: "" for k in to_unset}
    res = await db().people.update_one({"_id": oid}, update_doc)
    if res.matched_count == 0:
        return None
    await _after_write(person_id, {k: v for k, v in to_set.items() if k != "tier"})
    await activity.record("person.update", principal, person_id,
                          {"set": sorted(to_set), "unset": sorted(to_unset)})
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
    await embeddings.sync_people([person_id])
