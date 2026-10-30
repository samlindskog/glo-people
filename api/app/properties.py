"""Properties: which keys exist on people, their type and enum values, and vectors of their names.

Owns the `fields` collection (the registry: one document per existing property) and the
`property_embeddings` table. A property comes to exist through `ensure` and nowhere else; its type
and canonical enum values come from `app.property_specs` (source code), and a property without a
spec is free text and embeddable. Readers get a 60 s per-process cache; every writer here resets it.
"""

import logging
import math
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel
from pymongo import UpdateOne
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app import db, embedder, fields, mongo
from app.property_specs import EMBEDDABLE, NORMALISABLE, SPECS, PropertyType, Spec

log = logging.getLogger(__name__)

CreatedBy = Literal["import", "agent", "spec"]
CACHE_TTL = 60.0
NAME_VALUES = 8       # enum values quoted in a property's name text


class PropertyError(ValueError):
    pass


class Property(BaseModel):
    key: str
    label: str
    type: PropertyType
    values: list[str] = []            # canonical values (enum, enum_list), seeds first
    embeddable: bool
    normalisable: bool
    source: CreatedBy                 # how the property came to exist
    created_at: datetime | None = None
    count: int = 0                    # people holding a current value (when the caller supplies it)


_catalog: tuple[float, list[Property]] | None = None
_names: tuple[float, dict[str, list[float]]] | None = None


def reset() -> None:
    """Drop the caches (every writer here; tests after touching the stores directly)."""
    global _catalog, _names
    _catalog = _names = None


def spec(key: str) -> Spec | None:
    """The source-code spec of a key, or None for a free-text property."""
    return SPECS.get(key)


def _property(key: str, doc: dict | None) -> Property:
    sp = SPECS.get(key)
    ptype = sp.type if sp else "text"
    seeds = list(sp.values) if sp else []
    extra = [e["v"] for e in ((doc or {}).get("enum") or {}).get("values", [])]
    values = list(dict.fromkeys([*seeds, *extra])) if ptype in ("enum", "enum_list") else []
    return Property(key=key, label=(sp.label if sp else None) or fields.label_for(key),
                    type=ptype, values=values, embeddable=ptype in EMBEDDABLE,
                    normalisable=ptype in NORMALISABLE,
                    source=(doc or {}).get("source") or ("import" if doc else "spec"),
                    created_at=(doc or {}).get("created_at"))


async def _load() -> list[Property]:
    global _catalog
    if _catalog and time.monotonic() - _catalog[0] < CACHE_TTL:
        return _catalog[1]
    docs = {d["_id"]: d async for d in mongo.db().fields.find({})}
    keys = sorted({*docs, *SPECS})      # a spec declares existence even before sync_specs ran
    props = [_property(k, docs.get(k)) for k in keys if fields.valid_key(k)]
    _catalog = (time.monotonic(), props)
    return props


# --- reads ----------------------------------------------------------------------------------------

async def catalog(counts: dict[str, int] | None = None) -> list[Property]:
    """Every property, sorted by key. `counts` (from `people.coverage()`) fills `count`."""
    props = await _load()
    if counts is None:
        return list(props)
    return [p.model_copy(update={"count": counts.get(p.key, 0)}) for p in props]


async def get(key: str) -> Property | None:
    return next((p for p in await _load() if p.key == key), None)


async def known_keys() -> frozenset[str]:
    return frozenset(p.key for p in await _load())


async def is_known(key: str) -> bool:
    return key in await known_keys()


async def embeddable_keys() -> frozenset[str]:
    return frozenset(p.key for p in await _load() if p.embeddable)


async def enum_values(key: str) -> list[str]:
    prop = await get(key)
    return list(prop.values) if prop else []


# --- writes ---------------------------------------------------------------------------------------

async def ensure(keys: Iterable[str], source: CreatedBy) -> list[str]:
    """Make sure these properties exist; returns the keys created now. The only creator.

    Idempotent and safe under concurrency (`$setOnInsert`). A new property is free text and
    embeddable unless `app.property_specs` types it. Its name is embedded right away; if that
    fails the registry entry stands and `sync_names` (CLI `embed`, startup) completes it.
    """
    wanted = list(dict.fromkeys(keys))
    for key in wanted:
        if not fields.valid_key(key):
            raise PropertyError(f"invalid property key {key!r} (lowercase letters, digits, "
                                "underscores; not a system key)")
    if not wanted:
        return []
    now = datetime.now(UTC)
    res = await mongo.db().fields.bulk_write(
        [UpdateOne({"_id": k}, {"$setOnInsert": {"created_at": now, "source": source}},
                   upsert=True) for k in wanted], ordered=False)
    created = [k for k in wanted if k in set(res.upserted_ids.values())] if res.upserted_ids \
        else []
    if created:
        reset()
        try:
            await sync_names(created)
        except Exception:
            log.exception("could not embed the names of new properties %s", created)
    return created


async def add_enum_value(key: str, value: str, aliases: Sequence[str] = (),
                         src: str = "spec") -> bool:
    """Append a canonical value to an enum property's live set; False if already present."""
    value = value.strip()
    sp = SPECS.get(key)
    if not sp or sp.type not in ("enum", "enum_list"):
        raise PropertyError(f"{key!r} is not an enum property")
    if not value:
        raise PropertyError("enum value must not be empty")
    res = await mongo.db().fields.update_one(
        {"_id": key, "enum.values.v": {"$ne": value}},
        {"$push": {"enum.values": {"v": value, "aliases": list(aliases), "src": src,
                                   "ts": datetime.now(UTC)}}})
    if res.matched_count == 0 and await mongo.db().fields.count_documents({"_id": key}) == 0:
        raise PropertyError(f"property {key!r} does not exist; call ensure first")
    if res.modified_count:
        reset()
    return bool(res.modified_count)


async def sync_specs() -> None:
    """Bring the registry in line with the source-code specs (startup, CLI). Idempotent: creates
    the typed properties, seeds their enum values, never removes anything, and re-embeds the
    names whose text changed."""
    await ensure(SPECS, "spec")
    for key, sp in SPECS.items():
        for value in sp.values:
            await add_enum_value(key, value, src="spec")
    # Fields the registry no longer carries (label, type and embed come from specs; coverage
    # from people.coverage()).
    await mongo.db().fields.update_many(
        {"$or": [{"label": {"$exists": True}}, {"type": {"$exists": True}},
                 {"embed": {"$exists": True}}, {"count": {"$exists": True}}]},
        {"$unset": {"label": "", "type": "", "embed": "", "count": ""}})
    reset()
    await sync_names()


# --- name embeddings ------------------------------------------------------------------------------

def name_text(prop: Property) -> str:
    """The text that stands for a property when names are compared with tags or queries."""
    text = prop.label.lower()
    if prop.values:
        text += ". Values: " + "; ".join(prop.values[:NAME_VALUES])
    return text


async def sync_names(keys: Iterable[str] | None = None) -> int:
    """Embed the names of all (or these) properties whose text changed; with `keys=None` also
    delete vectors of properties that no longer exist. Returns the number embedded."""
    global _names
    props = {p.key: p for p in await _load()}
    todo_keys = list(props) if keys is None else [k for k in keys if k in props]
    texts = {k: name_text(props[k]) for k in todo_keys}
    hashes = {k: embedder.hash_text(t) for k, t in texts.items()}
    pe = db.property_embeddings
    async with db.engine().connect() as conn:
        existing = {r.key: r.content_hash for r in
                    await conn.execute(select(pe.c.key, pe.c.content_hash))}
    stale = [k for k in existing if k not in props] if keys is None else []
    todo = [k for k in todo_keys if existing.get(k) != hashes[k]]
    if stale:
        async with db.engine().begin() as conn:
            await conn.execute(delete(pe).where(pe.c.key.in_(stale)))
    if todo:
        vectors = await embedder.embed_texts([texts[k] for k in todo])
        rows = [{"key": k, "text": texts[k], "content_hash": hashes[k], "embedding": v,
                 "updated_at": datetime.now(UTC)}
                for k, v in zip(todo, vectors, strict=True)]
        stmt = insert(pe).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[pe.c.key],
            set_={"text": stmt.excluded.text, "content_hash": stmt.excluded.content_hash,
                  "embedding": stmt.excluded.embedding, "updated_at": stmt.excluded.updated_at})
        async with db.engine().begin() as conn:
            await conn.execute(stmt)
    if stale or todo:
        _names = None
    return len(todo)


def _unit(vec: Sequence[float]) -> list[float]:
    vals = [float(x) for x in (vec.to_list() if hasattr(vec, "to_list") else vec)]
    norm = math.sqrt(sum(x * x for x in vals)) or 1.0
    return [x / norm for x in vals]


async def name_vectors() -> dict[str, list[float]]:
    """{key: unit vector of the property's name}, cached."""
    global _names
    if _names and time.monotonic() - _names[0] < CACHE_TTL:
        return _names[1]
    pe = db.property_embeddings
    async with db.engine().connect() as conn:
        rows = await conn.execute(select(pe.c.key, pe.c.embedding))
        vectors = {r.key: _unit(r.embedding) for r in rows}
    _names = (time.monotonic(), vectors)
    return vectors


async def nearest(vectors: Sequence[Sequence[float]], k: int = 5) -> list[list[tuple[str, float]]]:
    """For each query vector, the k most similar property names as (key, cosine), best first."""
    names = await name_vectors()
    out = []
    for q in vectors:
        qu = _unit(q)
        sims = [(key, sum(a * b for a, b in zip(qu, v, strict=True))) for key, v in names.items()]
        sims.sort(key=lambda kv: -kv[1])
        out.append(sims[:k])
    return out
