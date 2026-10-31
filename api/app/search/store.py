"""Agentic storage: file what a passage says about a person into their properties.

`propose` asks the storage agent for writes against the catalog (no side effects); `apply` resolves
new properties, performs the one `people.update` and then has the normaliser clean up whatever the
write left flagged; `store` chains the two. All three are the programmatic API: a program may
inspect or edit a proposal between `propose` and `apply`, and `apply` holds a hand-written
proposal to the same rules as the agent's (`agents.storage.check`)."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app import embedder, fields, people, properties
from app.agents import storage
from app.agents.storage import LIST_TYPES, Proposal
from app.auth import Principal
from app.history import Source
from app.normalize import person as normalize_person
from app.normalize.person import NormalizeReport

Action = Literal["set", "append", "confirmed", "created", "redirected", "to_notes", "ignored"]
NOTES = "notes"


class StoreOptions(BaseModel):
    """Caps on new properties, and whether to write at all."""

    max_new: int = Field(2, ge=0, le=5, description="New properties one passage may create; "
                                                    "the rest of the new facts go to notes")
    near_duplicate: float = Field(0.8, ge=0, le=1,
                                  description="A proposed new property whose name is at least "
                                              "this similar to an existing one is redirected to it")
    dry_run: bool = Field(False, description="Return the proposal and what would happen, write "
                                             "nothing")


class ProposalError(ValueError):
    """A proposal that does not fit the catalog (apply of a hand-written one)."""


class StoredItem(BaseModel):
    key: str | None = None
    action: Action
    value: Any = None
    conflict: bool = False
    evidence: str | None = None
    reason: str | None = None


class StoreReport(BaseModel):
    proposal: Proposal
    applied: bool
    items: list[StoredItem]
    written: dict[str, Any] = Field(default_factory=dict,
                                    description="Property → value this call added")
    created: list[str] = Field(default_factory=list, description="New properties")
    normalized: NormalizeReport | None = Field(None, description="The clean-up pass")
    person: dict[str, Any] | None = None


async def propose(person_id: str, passage: str) -> Proposal | None:
    """The agent's proposal for this passage, or None if the person does not exist."""
    person = await people.get(person_id)
    if person is None:
        return None
    return await storage.propose(passage, person, await properties.catalog(await people.coverage()))


def _merge_list(current: Any, items: list[str]) -> list[str]:
    base = list(current) if isinstance(current, list) else ([current] if current else [])
    seen = {fields.fold(str(v)) for v in base}
    for item in items:
        if fields.fold(item) not in seen:
            base.append(item)
            seen.add(fields.fold(item))
    return base


def _as_text(value: storage.Value) -> str:
    return "; ".join(value) if isinstance(value, list) else value


async def apply(person_id: str, proposal: Proposal, opts: StoreOptions, principal: Principal,
                source: Source, commit: bool = True) -> StoreReport | None:
    """Carry out a proposal: resolve new properties (redirect near-duplicates, create up to
    `max_new`, send the rest to notes), write everything in one `people.update`, then run the
    normaliser clean-up. With `commit=False` nothing is written or created and the report says
    what would happen."""
    person = await people.get(person_id)
    if person is None:
        return None
    cat = {p.key: p for p in await properties.catalog()}
    problems = storage.check(proposal, cat)
    if problems:
        raise ProposalError("; ".join(problems))

    values: dict[str, Any] = {}
    items: list[StoredItem] = []

    def put(key: str, value: storage.Value, action: Action, append: bool = False, **extra) -> None:
        prop = cat.get(key)
        if prop is not None and prop.type in LIST_TYPES:
            incoming = value if isinstance(value, list) else [value]
            base = values.get(key, person.get(key) if append or key in values else None)
            values[key] = _merge_list(base, incoming) if append or key in values else incoming
        else:
            text = _as_text(value)
            values[key] = f"{values[key]}\n{text}" if key in values else text
        items.append(StoredItem(key=key, action=action, value=values[key], **extra))

    for w in proposal.writes:
        put(w.key, w.value, w.action, append=w.action == "append", conflict=w.conflict,
            evidence=w.evidence or None)

    created: list[str] = []
    if proposal.new_properties:
        vectors = await embedder.embed_texts([f"{n.label}: {n.description}"
                                              for n in proposal.new_properties])
        nearest = await properties.nearest(vectors, k=1)
    for n, near in zip(proposal.new_properties, nearest if proposal.new_properties else [],
                       strict=True):
        best = near[0] if near else None
        if best is not None and best[1] >= opts.near_duplicate:
            put(best[0], n.value, "redirected", append=True, evidence=n.evidence or None,
                reason=f"{n.key!r} is too close to the existing property {best[0]!r} "
                       f"({best[1]:.2f})")
        elif len(created) < opts.max_new:
            if commit:
                await properties.ensure([n.key], "agent", descriptions={n.key: n.description})
                cat[n.key] = await properties.get(n.key)  # type: ignore[assignment]
            created.append(n.key)
            put(n.key, n.value, "created", evidence=n.evidence or None,
                reason=f"new property: {n.description}")
        else:
            put(NOTES, f"{n.label}: {_as_text(n.value)}", "to_notes", evidence=n.evidence or None,
                reason=f"at most {opts.max_new} new properties per passage")
    for key in proposal.confirmed:
        items.append(StoredItem(key=key, action="confirmed", value=person.get(key),
                                reason="the passage restates the current value"))
    for text in proposal.ignored:
        items.append(StoredItem(action="ignored", value=text))

    report = StoreReport(proposal=proposal, applied=commit, items=items, created=created,
                         person=person)
    if not commit:
        return report
    if values:
        updated = await people.update(principal, person_id, values, source=source)
        if updated is not None:
            report.person = updated
            for key, value in values.items():
                entries = (updated.get("history") or {}).get(key) or []
                if entries and (entries[-1].get("src") or {}) == source.doc():
                    report.written[key] = value
    report.normalized = await normalize_person.run(
        person_id, principal, Source("agent", "normalizer", f"{source.ref or 'store'}"))
    if report.normalized is not None:
        report.person = await people.get(person_id)
    return report


async def store(person_id: str, passage: str, opts: StoreOptions, principal: Principal,
                source: Source) -> StoreReport | None:
    proposal = await propose(person_id, passage)
    if proposal is None:
        return None
    return await apply(person_id, proposal, opts, principal, source, commit=not opts.dry_run)
