"""Field registry: the set of keys that appear on people documents, with type and embed flag.

People documents are schema-less. Every key that is not a system field is a "field" and gets a
registry entry the first time it is seen, so the UI and MCP can list what is queryable.
"""

import re
from datetime import UTC, datetime
from typing import Any, Literal

from app.mongo import db

FieldType = Literal["text", "number", "date", "list", "url"]

SYSTEM_FIELDS = frozenset({"_id", "id", "tier", "source", "created_at", "updated_at",
                           "schema_version", "history"})
TIERS = ("member", "plus_one")

_NOT_EMBEDDED = re.compile(
    r"(^|_)(id|email|emails|phone|phones|whatsapp|linkedin|instagram|twitter|website|url|"
    r"github|related_links|do_not_contact|in_whatsapp|linkedin_connected)($|_)")
# `github_summary` is prose and stays embeddable: the pattern above matches `github` only as a
# whole key segment, and `github_summary`'s segments are `github` and `summary`... so exempt it.
_EMBED_ANYWAY = frozenset({"github_summary"})
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?")
_URLISH = re.compile(r"^(https?://|www\.)", re.IGNORECASE)
_KEY_OK = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def slugify(header: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")
    if not key:
        return ""
    if key[0].isdigit():
        key = f"f_{key}"
    return key[:64]


def valid_key(key: str) -> bool:
    return bool(_KEY_OK.match(key)) and key not in SYSTEM_FIELDS


def label_for(key: str) -> str:
    return key.replace("_", " ").capitalize()


MIN_EMBED_CHARS = 15


def default_embed(key: str, ftype: FieldType, values: list[Any] = ()) -> bool:
    """Embed descriptive text by default; skip contact details, numbers, dates, and short
    categorical values (status, tier, stage…) where similarity search is meaningless."""
    if ftype not in ("text", "list") or (_NOT_EMBEDDED.search(key) and key not in _EMBED_ANYWAY):
        return False
    texts = [text_of(v) for v in values if v not in (None, "", [])]
    return not texts or sum(map(len, texts)) / len(texts) >= MIN_EMBED_CHARS


def infer_type(values: list[Any]) -> FieldType:
    vals = [v for v in values if v not in (None, "", [])]
    if not vals:
        return "text"
    if any(isinstance(v, list) for v in vals):
        return "list"
    if all(isinstance(v, int | float) and not isinstance(v, bool) for v in vals):
        return "number"
    strs = [str(v) for v in vals]
    if sum(bool(_URLISH.match(s)) for s in strs) >= 0.8 * len(strs):
        return "url"
    if sum(bool(_ISO_DATE.match(s)) for s in strs) >= 0.8 * len(strs):
        return "date"
    return "text"


def text_of(value: Any) -> str:
    """The string we embed / show for one field value (a history entry's `v`)."""
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v not in (None, ""))
    return "" if value is None else str(value)


async def list_fields() -> list[dict]:
    docs = await db().fields.find({}).sort("_id", 1).to_list(None)
    return [_public(d) for d in docs]


async def get_field(key: str) -> dict | None:
    doc = await db().fields.find_one({"_id": key})
    return _public(doc) if doc else None


async def embedded_field_keys() -> set[str]:
    return {d["_id"] async for d in db().fields.find({"embed": True}, {"_id": 1})}


async def register(samples: dict[str, list[Any]]) -> list[str]:
    """Add registry entries for keys not seen before. Returns the newly added keys."""
    existing = {d["_id"] async for d in db().fields.find({"_id": {"$in": list(samples)}}, {"_id": 1})}
    new = []
    now = datetime.now(UTC)
    for key, values in samples.items():
        if key in existing or key in SYSTEM_FIELDS:
            continue
        ftype = infer_type(values)
        await db().fields.update_one(
            {"_id": key},
            {"$setOnInsert": {"label": label_for(key), "type": ftype,
                              "embed": default_embed(key, ftype, values), "count": 0, "created_at": now}},
            upsert=True)
        new.append(key)
    return new


async def refresh_counts() -> None:
    """Recompute how many people have each field (coverage): a field counts when its current
    value (`field.v`) is present and not a tombstone."""
    pipeline = [
        {"$project": {"kv": {"$objectToArray": "$$ROOT"}}},
        {"$unwind": "$kv"},
        {"$match": {"kv.v.v": {"$exists": True, "$nin": [None, "", []]}}},
        {"$group": {"_id": "$kv.k", "count": {"$sum": 1}}},
    ]
    counts = {d["_id"]: d["count"] async for d in await db().people.aggregate(pipeline)}
    async for f in db().fields.find({}, {"_id": 1}):
        await db().fields.update_one({"_id": f["_id"]}, {"$set": {"count": counts.get(f["_id"], 0)}})


async def update_field(key: str, changes: dict) -> dict | None:
    allowed = {k: v for k, v in changes.items() if k in ("label", "embed") and v is not None}
    if allowed:
        await db().fields.update_one({"_id": key}, {"$set": allowed})
    return await get_field(key)


def _public(doc: dict) -> dict:
    return {"key": doc["_id"], "label": doc.get("label") or label_for(doc["_id"]),
            "type": doc.get("type", "text"), "embed": bool(doc.get("embed")),
            "count": doc.get("count", 0)}
