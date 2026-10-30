"""Operation: store a passage about a person as values of properties. The extractor splits the
passage into tagged statements; each lands on the existing property the agent picked (when its
name is close enough to the tag), else on the property whose name is nearest the tag, else on a
new free-text property, within caps that keep the catalog from sprawling."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app import embedder, fields, people, properties
from app.agents import extract
from app.auth import Principal
from app.history import Source

Action = Literal["existing", "created", "dropped"]


class StoreOptions(BaseModel):
    """How readily statements map onto existing properties, and how many new ones a passage may
    create."""

    threshold: float = Field(0.5, ge=0, le=1,
                             description="Minimum name similarity between a tag and a property "
                                         "for the statement to land on that property")
    near_duplicate: float = Field(0.8, ge=0, le=1,
                                  description="A new property is not created when an existing "
                                              "name is at least this similar to the tag")
    max_new: int = Field(1, ge=0, le=5, description="New properties one passage may create")


class StoredItem(BaseModel):
    tag: str
    text: str
    action: Action
    key: str | None = None
    similarity: float | None = None          # name similarity that decided the mapping
    reason: str | None = None


class StoreReport(BaseModel):
    items: list[StoredItem]
    written: dict[str, Any]                  # property -> value this call added
    created: list[str]                       # new properties
    person: dict[str, Any]


def _about(person: dict[str, Any]) -> str:
    bits = [str(person.get("name") or "")]
    for key in ("role", "company", "location"):
        if person.get(key):
            bits.append(f"{key}: {person[key]}")
    return "; ".join(bits)


async def store(person_id: str, passage: str, opts: StoreOptions, principal: Principal,
                source: Source) -> StoreReport | None:
    person = await people.get(person_id)
    if person is None:
        return None
    catalog = await properties.catalog()
    cat = {p.key: p for p in catalog}
    extraction = await extract.extract(passage, _about(person), catalog)
    if not extraction.items:
        return StoreReport(items=[], written={}, created=[], person=person)

    vectors = await embedder.embed_texts([it.tag.lower() for it in extraction.items])
    nearest = await properties.nearest(vectors, k=1)
    values: dict[str, str] = {}
    items: list[StoredItem] = []
    created: list[str] = []
    for it, vec, near in zip(extraction.items, vectors, nearest, strict=True):
        best = near[0] if near else None
        picked = await properties.similarity(vec, it.existing_key) if it.existing_key in cat \
            else None
        if picked is not None and picked >= opts.threshold:
            item = StoredItem(tag=it.tag, text=it.text, action="existing", key=it.existing_key,
                              similarity=round(picked, 3), reason="agent's pick")
        elif best is not None and best[1] >= opts.threshold:
            item = StoredItem(tag=it.tag, text=it.text, action="existing", key=best[0],
                              similarity=round(best[1], 3), reason="nearest property name")
        else:
            new_key = fields.slugify(it.tag)
            if new_key in cat:
                item = StoredItem(tag=it.tag, text=it.text, action="existing", key=new_key,
                                  reason="tag is the property's key")
            elif not fields.valid_key(new_key):
                item = StoredItem(tag=it.tag, text=it.text, action="dropped",
                                  reason=f"{new_key!r} is not a valid property key")
            elif best is not None and best[1] >= opts.near_duplicate:
                item = StoredItem(tag=it.tag, text=it.text, action="dropped", key=best[0],
                                  similarity=round(best[1], 3),
                                  reason=f"too close to the existing property {best[0]!r}")
            elif len(created) >= opts.max_new:
                item = StoredItem(tag=it.tag, text=it.text, action="dropped",
                                  reason=f"at most {opts.max_new} new propert"
                                         f"{'y' if opts.max_new == 1 else 'ies'} per passage")
            else:
                await properties.ensure([new_key], "agent")
                cat[new_key] = (await properties.get(new_key))  # type: ignore[assignment]
                created.append(new_key)
                item = StoredItem(tag=it.tag, text=it.text, action="created", key=new_key,
                                  similarity=round(best[1], 3) if best else None,
                                  reason="no property close enough")
        if item.key and item.action != "dropped":
            values[item.key] = f"{values[item.key]} {it.text}" if item.key in values else it.text
        items.append(item)

    written: dict[str, Any] = {}
    if values:
        updated = await people.update(principal, person_id, values, source=source)
        if updated is not None:
            person = updated
            for key, value in values.items():
                entries = (updated.get("history") or {}).get(key) or []
                if entries and (entries[-1].get("src") or {}) == source.doc():
                    written[key] = value
    return StoreReport(items=items, written=written, created=created, person=person)
