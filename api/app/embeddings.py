"""Per-field embeddings stored in Postgres (pgvector halfvec), one row per (person, field)."""

import hashlib
import logging
import math
import re
from collections.abc import Iterable, Sequence

from bson import ObjectId
from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from app import db, fields
from app.config import get_settings
from app.mongo import db as mongo

log = logging.getLogger(__name__)

MAX_CHARS = 8000
BATCH = 256


class FakeEmbedder:
    """Deterministic hashed bag-of-words. Shared words → positive cosine similarity, which is
    enough for tests and offline dev; not a semantic model."""

    def __init__(self, dims: int) -> None:
        self.dims = dims

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dims
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                vec[h % self.dims] += 1.0 if (h >> 64) & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str, dims: int) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.dims = dims

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        resp = await self.client.embeddings.create(model=self.model, input=list(texts),
                                                   dimensions=self.dims)
        return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]


_embedder = None


def embedder():
    global _embedder
    if _embedder is None:
        s = get_settings()
        if s.embedding_provider == "openai" and s.openai_api_key:
            _embedder = OpenAIEmbedder(s.openai_api_key, s.embedding_model, s.embedding_dims)
        else:
            if s.embedding_provider == "openai":
                log.warning("OPENAI_API_KEY not set; using the fake hashing embedder")
            _embedder = FakeEmbedder(s.embedding_dims)
    return _embedder


def set_embedder(e) -> None:
    global _embedder
    _embedder = e


def _hash(text: str) -> str:
    # Includes the embedder identity so switching provider/model/dims re-embeds everything.
    e = embedder()
    who = f"{type(e).__name__}:{getattr(e, 'model', '')}:{e.dims}"
    return hashlib.sha256(f"{who}:{text}".encode()).hexdigest()


async def embed_query(text: str) -> list[float]:
    return (await embedder().embed([text[:MAX_CHARS]]))[0]


async def sync_people(ids: Iterable[str], only_fields: set[str] | None = None) -> int:
    """Bring field_embeddings in line with the current Mongo docs for these people.

    Embeds fields whose text changed, and deletes rows for fields that were removed, emptied or
    are no longer marked `embed`. Returns the number of vectors computed.
    """
    ids = [i for i in ids if ObjectId.is_valid(i)]
    if not ids:
        return 0
    embed_keys = await fields.embedded_field_keys()
    if only_fields is not None:
        embed_keys &= only_fields

    docs = await mongo().people.find({"_id": {"$in": [ObjectId(i) for i in ids]}}).to_list(None)
    found = {str(d["_id"]) for d in docs}

    wanted: dict[tuple[str, str], tuple[str, str, str]] = {}  # (id, field) -> (tier, text, hash)
    for doc in docs:
        for key in embed_keys:
            text = fields.text_of(doc.get(key)).strip()[:MAX_CHARS]
            if text:
                wanted[(str(doc["_id"]), key)] = (doc.get("tier", ""), text, _hash(text))

    fe = db.field_embeddings
    async with db.engine().connect() as conn:
        q = select(fe.c.mongo_id, fe.c.field, fe.c.content_hash, fe.c.tier).where(
            fe.c.mongo_id.in_(ids))
        if only_fields is not None:
            q = q.where(fe.c.field.in_(only_fields))
        existing = {(r.mongo_id, r.field): (r.content_hash, r.tier)
                    for r in await conn.execute(q)}

    stale = [k for k in existing
             if k[0] not in found or k not in wanted]
    todo = [(k, v) for k, v in wanted.items()
            if existing.get(k) != (v[2], v[0])]

    async with db.engine().begin() as conn:
        if stale:
            await conn.execute(delete(fe).where(tuple_(fe.c.mongo_id, fe.c.field).in_(stale)))

    computed = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        vectors = await embedder().embed([v[1] for _, v in chunk])
        rows = [{"mongo_id": k[0], "field": k[1], "tier": v[0], "content_hash": v[2],
                 "embedding": vec} for (k, v), vec in zip(chunk, vectors, strict=True)]
        stmt = insert(fe).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[fe.c.mongo_id, fe.c.field],
            set_={"tier": stmt.excluded.tier, "content_hash": stmt.excluded.content_hash,
                  "embedding": stmt.excluded.embedding, "updated_at": stmt.excluded.updated_at})
        async with db.engine().begin() as conn:
            await conn.execute(stmt)
        computed += len(rows)
    return computed


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
        q = {"_id": {"$gt": last}} if last else {}
        batch = await mongo().people.find(q, {"_id": 1}).sort("_id", 1).limit(page).to_list(None)
        if not batch:
            return total
        last = batch[-1]["_id"]
        total += await sync_people([str(d["_id"]) for d in batch], only_fields)
        if progress:
            progress(total)
