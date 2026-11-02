"""Composition root shared by the API and the CLI: what reacts to a person write.

Level 1 (`app.people`) reports every committed write to its observers and knows no consumer; this
module decides who listens: the vector store (every embeddable value of a person is re-synced as
soon as a write lands), and nothing else yet. Every process that writes — the API, and the CLI
executing a run — calls `register_write_hooks()` once at start, so a write embeds whatever surface
or assistant made it and whatever process performed it."""

from app import embeddings, people

_registered = False


def register_write_hooks() -> None:
    """Register the write observers once per process (idempotent)."""
    global _registered
    if _registered:
        return
    people.on_write(embeddings.sync_person)
    _registered = True
