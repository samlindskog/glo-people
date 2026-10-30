"""Passage embeddings: one pgvector row per (person, field, content hash), i.e. one vector for
every distinct history entry of an embeddable field. Owns the `field_embeddings` table."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Iterable

from bson import ObjectId
from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from app import db, embedder, fields, history, people

log = logging.getLogger(__name__)

MAX_CHARS = embedder.MAX_CHARS
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)   # one sync per person at a time


def passage_text(value) -> str:
    """The text that represents one field value, for embedding and for matching a stored vector
    back to its history entry. The single normalisation point for passages."""
    return fields.text_of(value).strip()[:MAX_CHARS]


async def embed_query(text: str) -> list[float]:
    return (await embedder.embed_texts([text]))[0]


async def sync_people(ids: Iterable[str], only_fields: set[str] | None = None) -> int:
    """Bring field_embeddings in line with the current documents of these people.

    Embeds every history entry (passage) of each embeddable field that has no vector yet, and
    deletes rows for passages that no longer apply (person deleted, field removed or no longer
    embeddable). A removed field (current value is a tombstone) keeps its history but gets no
    vectors. Returns the number of vectors computed.
    """
    ids = [i for i in ids if ObjectId.is_valid(i)]
    if not ids:
        return 0
    embed_keys = await fields.embedded_field_keys()
    if only_fields is not None:
        embed_keys &= only_fields

    docs = await people.fetch(ids)

    # (id, field, hash) -> (tier, text)
    wanted: dict[tuple[str, str, str], tuple[str, str]] = {}
    for doc in docs:
        for key in embed_keys:
            prop = doc.get(key)
            if history.current(prop) is None:      # absent or removed: nothing to match
                continue
            for entry in history.entries(prop):
                text = passage_text(entry.get("v"))
                if text:
                    wanted[(str(doc["_id"]), key, embedder.hash_text(text))] = (
                        doc.get("tier", ""), text)

    fe = db.field_embeddings
    async with db.engine().connect() as conn:
        q = select(fe.c.mongo_id, fe.c.field, fe.c.content_hash, fe.c.tier).where(
            fe.c.mongo_id.in_(ids))
        if only_fields is not None:
            q = q.where(fe.c.field.in_(only_fields))
        existing = {(r.mongo_id, r.field, r.content_hash): r.tier
                    for r in await conn.execute(q)}

    stale = [k for k in existing if k not in wanted]
    todo = [(k, v) for k, v in wanted.items() if existing.get(k) != v[0]]

    async with db.engine().begin() as conn:
        if stale:
            await conn.execute(delete(fe).where(
                tuple_(fe.c.mongo_id, fe.c.field, fe.c.content_hash).in_(stale)))

    computed = 0
    for start in range(0, len(todo), embedder.BATCH):
        chunk = todo[start:start + embedder.BATCH]
        vectors = await embedder.embed_texts([v[1] for _, v in chunk])
        rows = [{"mongo_id": k[0], "field": k[1], "content_hash": k[2], "tier": v[0],
                 "embedding": vec} for (k, v), vec in zip(chunk, vectors, strict=True)]
        stmt = insert(fe).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[fe.c.mongo_id, fe.c.field, fe.c.content_hash],
            set_={"tier": stmt.excluded.tier, "embedding": stmt.excluded.embedding,
                  "updated_at": stmt.excluded.updated_at})
        async with db.engine().begin() as conn:
            await conn.execute(stmt)
        computed += len(rows)
    return computed


async def sync_person(person_id: str) -> None:
    """Write hook: re-sync one person, serialised per person within this process."""
    async with _locks[person_id]:
        await sync_people([person_id])


async def delete_people(ids: Iterable[str]) -> None:
    ids = list(ids)
    async with db.engine().begin() as conn:
        await conn.execute(delete(db.field_embeddings).where(db.field_embeddings.c.mongo_id.in_(ids)))


async def delete_field(key: str) -> None:
    async with db.engine().begin() as conn:
        await conn.execute(delete(db.field_embeddings).where(db.field_embeddings.c.field == key))


async def backfill(only_fields: set[str] | None = None, page: int = 1000, progress=None) -> int:
    """Sync embeddings for every person (optionally only some fields)."""
    total = 0
    last = None
    while True:
        batch = await people.find_ids({"_id": {"$gt": last}} if last else {}, limit=page)
        if not batch:
            return total
        last = ObjectId(batch[-1])
        total += await sync_people(batch, only_fields)
        if progress:
            progress(total)
