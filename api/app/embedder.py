"""The embedding provider: the one place that turns text into vectors.

Every vector in the app (passages, property names, agent tags, search queries) comes from
`embed_texts`, and every content hash that keys a stored vector comes from `hash_text`, so a
change of provider, model or dimensions re-embeds everything consistently.
"""

import hashlib
import logging
import math
import re
from collections.abc import Sequence

from app.config import get_settings

log = logging.getLogger(__name__)

MAX_CHARS = 8000    # text longer than this is cut before embedding
BATCH = 256         # texts per provider call


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
    """Test seam."""
    global _embedder
    _embedder = e


def hash_text(text: str) -> str:
    """Content hash of a text under the current embedder (provider, model and dims included, so
    switching any of them invalidates every stored vector)."""
    e = embedder()
    who = f"{type(e).__name__}:{getattr(e, 'model', '')}:{e.dims}"
    return hashlib.sha256(f"{who}:{text}".encode()).hexdigest()


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Vectors for `texts`, in order. Identical texts are sent to the provider once."""
    cut = [t[:MAX_CHARS] for t in texts]
    unique = list(dict.fromkeys(cut))
    vectors: dict[str, list[float]] = {}
    for start in range(0, len(unique), BATCH):
        chunk = unique[start:start + BATCH]
        for text, vec in zip(chunk, await embedder().embed(chunk), strict=True):
            vectors[text] = vec
    return [vectors[t] for t in cut]
