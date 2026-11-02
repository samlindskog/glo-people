"""Composition root shared by the API and the CLI: what reacts to a person write.

Level 1 (`app.people`) reports every committed write to its observers and knows no consumer; this
module decides who listens: the vector store (every embeddable value of a person is re-synced as
soon as a write lands) and the auto-review (people written by a human or an API key are reviewed
by the normalise assistant shortly after; off with AUTO_REVIEW=0). Every process that writes — the
API, and the CLI executing a run — calls `register_write_hooks()` once at start, so a write embeds
and is reviewed whatever surface or assistant made it and whatever process performed it."""

from app import embeddings, people
from app.assistants import auto_review

_registered = False


def register_write_hooks() -> None:
    """Register the write observers once per process (idempotent)."""
    global _registered
    if _registered:
        return
    people.on_write(embeddings.sync_person)
    people.on_write(auto_review.after_write)       # decides per write whether the setting allows it
    _registered = True
