"""The person document service: the only writer of the `people` collection.

Field values are never overwritten: every write appends a time-stamped, source-tagged entry to the
field's history (see app.history) and refreshes the cached current value; typed properties get
their normalised value (app.normalize) in the same write. Writes accept only properties that exist
(app.properties); the merge assistant creates a CSV's columns before its items write. Every write is
compare-and-set on the document's `rev`, so concurrent writers never lose each other's entries.
Other systems read through the helpers here (`fetch`, `find`, `count`, `find_ids`, `coverage`,
`value_counts`) and react to value writes through `on_write` hooks that the composition root
registers (e.g. embeddings).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app import activity, fields, history, mongo, normalize, properties
from app.auth import Principal
from app.history import Change, Source
from app.mongo import db
from app.normalize import Parsed
from app.property_specs import CANONICAL, IDENTITY_KEYS, NORMALISABLE

log = logging.getLogger(__name__)

Scalar = str | int | float | bool
CAS_RETRIES = 8      # a compare-and-set attempt fails only because another writer succeeded
NORMALIZE_SRC = Source("system", "normalize")

@dataclass(frozen=True)
class WriteInfo:
    """What a commit did, reported to the write observers (`on_write`): the person, who wrote (the
    source of the transaction's first op), the keys whose value was set, appended or removed,
    whether the person was created by this write, and whether it was deleted instead."""

    person_id: str
    src: Source | None
    keys: tuple[str, ...] = ()
    is_new: bool = False
    deleted: bool = False


WriteHook = Callable[[WriteInfo], Awaitable[None]]
_hooks: list[WriteHook] = []


class PersonError(ValueError):
    pass


class ConflictError(PersonError):
    """Another writer kept changing the document; the caller may retry."""


@dataclass
class WriteResult:
    """What a write did: the person afterwards (public view with history) and the changes, in
    the shared `history.Change` vocabulary, including the inline normalisation."""

    person: dict[str, Any]
    changes: list[Change] = field(default_factory=list)


def on_write(hook: WriteHook) -> None:
    """Register an observer called with a `WriteInfo` after every successful value write (and after
    a delete). This module knows no consumer: the composition root (`app.wiring`) registers the
    vector store and whatever else must follow a write, in every process that writes."""
    _hooks.append(hook)


# --- reads ----------------------------------------------------------------------------------------

def public(doc: dict, include_history: bool = False) -> dict:
    """Flat view of a person: current values, `norm` (normalised values of typed properties) and
    `norm_flags` (what could not be normalised); with `history`, every entry too."""
    out: dict[str, Any] = {"id": str(doc["_id"])}
    for k in ("tier", "source", "created_at", "updated_at", "schema_version"):
        if k in doc:
            out[k] = history.jsonable(doc[k])
    out.update(history.flatten(doc, fields.SYSTEM_FIELDS))
    # identity-like properties (name, emails, URLs) carry a lookup key as their normalised value;
    # it serves matching and filters, not readers
    hidden = {k for k in doc if k not in fields.SYSTEM_FIELDS
              and (sp := properties.spec(k)) is not None and sp.type in CANONICAL}
    norm = history.jsonable(history.flatten_norm(doc, fields.SYSTEM_FIELDS))
    out["norm"] = {k: v for k, v in norm.items() if k not in hidden}
    out["norm_flags"] = {k: v for k, v in history.norm_flags(doc, fields.SYSTEM_FIELDS).items()
                         if k not in hidden}
    if include_history:
        props = {k: v for k, v in doc.items() if k not in fields.SYSTEM_FIELDS}
        out["history"] = {k: history.public_entries(v) for k, v in props.items()}
        out["history_norm"] = {k: history.public_norm_entries(v) for k, v in props.items()
                               if history.norm_of(v) and k not in hidden}
    return out


async def get(person_id: str, include_history: bool = True) -> dict | None:
    doc = await db().people.find_one({"_id": _oid(person_id)})
    return public(doc, include_history=include_history) if doc else None


async def fetch(ids: list[str], projection: dict | None = None) -> list[dict]:
    """Raw documents for these ids (unknown or invalid ids are skipped)."""
    oids = [ObjectId(i) for i in ids if ObjectId.is_valid(i)]
    if not oids:
        return []
    return await db().people.find({"_id": {"$in": oids}}, projection).to_list(None)


async def find(query: dict, *, sort: list[tuple[str, int]] | None = None, skip: int = 0,
               limit: int = 0, projection: dict | None = None) -> list[dict]:
    cursor = db().people.find(query, projection)
    if sort:
        cursor = cursor.sort(sort)
    if skip:
        cursor = cursor.skip(skip)
    if limit:
        cursor = cursor.limit(limit)
    return await cursor.to_list(None)


async def count(query: dict) -> int:
    return await db().people.count_documents(query)


async def find_ids(query: dict, limit: int = 0) -> list[str]:
    docs = await find(query, sort=[("_id", 1)], limit=limit, projection={"_id": 1})
    return [str(d["_id"]) for d in docs]


_coverage_cache: tuple[float, dict[str, dict[str, int]]] | None = None
_coverage_lock = asyncio.Lock()


async def coverage(max_age_s: float = 0.0) -> dict[str, dict[str, int]]:
    """Per property: `count` of people whose current value is present (not a tombstone), and how
    many of those are `unmapped` / `non_normalizable` (typed properties). A full scan of the
    collection, so a caller that only shows the counts (an agent's catalog prompt) passes
    `max_age_s` and gets the last result when it is that fresh; concurrent callers share one
    scan. The default is exact."""
    global _coverage_cache
    loop = asyncio.get_running_loop()
    if max_age_s > 0 and _coverage_cache and loop.time() - _coverage_cache[0] < max_age_s:
        return _coverage_cache[1]
    async with _coverage_lock:
        if max_age_s > 0 and _coverage_cache and loop.time() - _coverage_cache[0] < max_age_s:
            return _coverage_cache[1]
        counts = await _coverage()
        _coverage_cache = (loop.time(), counts)
        return counts


async def _coverage() -> dict[str, dict[str, int]]:
    present = {"$and": [{"$ne": ["$kv.v.v", None]}, {"$ne": ["$kv.v.v", ""]},
                        {"$ne": ["$kv.v.v", []]}]}
    flagged = lambda flag: {"$cond": [{"$and": [present, {"$eq": ["$kv.v.norm.flag", flag]}]},
                                      1, 0]}
    pipeline = [
        {"$project": {"kv": {"$objectToArray": "$$ROOT"}}},
        {"$unwind": "$kv"},
        {"$match": {"kv.v.h": {"$exists": True}}},
        {"$group": {"_id": "$kv.k",
                    "count": {"$sum": {"$cond": [present, 1, 0]}},
                    "unmapped": {"$sum": flagged("unmapped")},
                    "non_normalizable": {"$sum": flagged("non_normalizable")}}},
    ]
    return {d.pop("_id"): d async for d in await db().people.aggregate(pipeline)}


async def value_counts(key: str, limit: int = 200) -> list[tuple[Any, int]]:
    """The most common current values of one property (list items counted individually)."""
    if not fields.valid_key(key):
        raise PersonError(f"invalid field key {key!r}")
    path = f"{key}.v"
    pipeline = [
        {"$match": {path: {"$exists": True, "$nin": [None, ""]}}},
        {"$project": {"v": f"${path}"}},
        {"$unwind": "$v"},
        {"$group": {"_id": "$v", "n": {"$sum": 1}}},
        {"$sort": {"n": -1, "_id": 1}},
        {"$limit": limit},
    ]
    return [(d["_id"], d["n"]) async for d in await db().people.aggregate(pipeline)]


# --- writes ---------------------------------------------------------------------------------------

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


async def _clean(changes: dict[str, Any]) -> dict[str, Any]:
    """Validate keys against the property catalog and normalise values. None / empty means
    remove the field. Unknown keys are refused: properties are created only by the importer and
    the storage agent through `properties.ensure`."""
    known = await properties.known_keys()
    out = {}
    for key, value in changes.items():
        if not fields.valid_key(key):
            raise PersonError(f"invalid field key {key!r} (lowercase letters, digits, underscores)")
        if key not in known:
            raise PersonError(f"unknown property {key!r}; the catalog is at GET /api/properties")
        out[key] = _check_value(key, value)
    return out


def _oid(person_id: str) -> ObjectId:
    if not ObjectId.is_valid(person_id):
        raise PersonError("invalid person id")
    return ObjectId(person_id)


def _check_tier(tier: str) -> None:
    if tier not in fields.TIERS:
        raise PersonError(f"tier must be one of {fields.TIERS}")


async def pending_normalization(doc: dict, force: bool = False) -> dict[str, Parsed]:
    """What a document still needs normalised: every typed property whose current text is not
    the one its last norm entry derives from. A removed value normalises to nothing. With
    `force` (a parser upgrade) the parser's own earlier results are redone too; what an agent or
    a person decided for the current text is always kept."""
    out: dict[str, Parsed] = {}
    for key, prop in doc.items():
        if key in fields.SYSTEM_FIELDS or not history.is_prop(prop):
            continue
        sp = properties.spec(key)
        if sp is None or sp.type not in NORMALISABLE:
            continue
        value = history.current(prop)
        last = history.norm_entries(prop)
        if last and last[-1].get("of") == normalize.text_hash(value) and \
                not (force and (last[-1].get("src") or {}).get("kind") == "system"):
            continue
        if value is None:
            if last:
                out[key] = Parsed()
            continue
        lookup = await properties.enum_lookup(key) if sp.type in ("enum", "enum_list") else None
        out[key] = normalize.parse(value, sp.type, lookup)
    return out


def _norm_change(key: str, prop: Any, p: Parsed) -> Change | None:
    """The Change a norm entry represents (None for clearing a removed value's norm, and for the
    lookup key of an identity-like property: internal, not a change anyone reads)."""
    sp = properties.spec(key)
    if sp is not None and sp.type in CANONICAL:
        return None
    previous = history.jsonable(history.norm_value(prop))
    if p.value is not None:
        return Change(key=key, action="normalized", value=history.jsonable(p.value),
                      previous=previous)
    if p.flag:
        return Change(key=key, action="flagged", flag=p.flag, previous=previous)
    return None


def _apply_norm(doc_props: dict, parsed: dict[str, Parsed], to_set: dict[str, Any],
                src: Source, now: datetime) -> list[Change]:
    """Append norm entries for `parsed` to `to_set` (keyed by property) and return the changes."""
    changes = []
    for key, p in parsed.items():
        prop = doc_props[key]
        new = history.append_norm(prop, p.value, src, now,
                                  normalize.text_hash(history.current(prop)), p.flag)
        if new is None:
            continue
        to_set[key] = new
        change = _norm_change(key, prop, p)
        if change:
            changes.append(change)
    return changes


async def _normalize_into(doc: dict, to_set: dict[str, Any], now: datetime,
                          src: Source = NORMALIZE_SRC) -> list[Change]:
    """Add norm entries to `to_set` for every typed property of the prospective document (`doc`
    with `to_set` applied) whose text changed. Returns the normalisation changes."""
    prospective = {**doc, **to_set}
    parsed = await pending_normalization(prospective)
    return _apply_norm(prospective, parsed, to_set, src, now)


def _rev_filter(doc: dict | None) -> dict[str, Any]:
    """The revision condition of a compare-and-set write: the revision we read, or "no revision
    yet" for a document that predates revisions or does not exist (an upsert must not copy a
    `rev: null` into the new document)."""
    if doc is not None and "rev" in doc:
        return {"rev": doc["rev"]}
    return {"rev": {"$exists": False}}


async def _cas_write(oid: ObjectId, doc: dict, to_set: dict[str, Any], *, upsert: bool = False,
                     unset: Iterable[str] = ()) -> bool:
    """Apply `to_set` (and drop the `unset` keys) if the document is still at the revision we
    read; False otherwise. With `upsert` (a new document) the write inserts when nothing is
    there yet."""
    update: dict[str, Any] = {"$set": to_set, "$inc": {"rev": 1}}
    if unset:
        update["$unset"] = {k: "" for k in unset}
    res = await db().people.update_one({"_id": oid, **_rev_filter(doc)}, update, upsert=upsert)
    return res.matched_count == 1 or res.upserted_id is not None


def _merged(prop: Any, value: Any) -> Any:
    """`value` appended to the property's current value: list items unioned (duplicates by
    `fields.fold` dropped, order kept), a text added as a new line unless the text already holds
    that line, anything else replaced."""
    current = history.current(prop)
    if isinstance(value, list) or isinstance(current, list):
        cur_items = current if isinstance(current, list) else (
            [current] if current not in (None, "") else [])
        new_items = value if isinstance(value, list) else [value]
        return fields.distinct([*cur_items, *new_items])
    if current in (None, "") or not isinstance(current, str) or not isinstance(value, str):
        return value
    if fields.fold(value) in {fields.fold(line) for line in current.split("\n")}:
        return current
    return f"{current}\n{value}"


# --- staged writes: one commit per document -----------------------------------------------------------

@dataclass
class _Op:
    """One staged operation. The op log is the only state a transaction keeps between attempts:
    everything else is derived from it against the document of the current attempt."""

    kind: str                                     # values | norm
    data: dict[str, Any]                          # cleaned values, or parsed normalised values
    src: Source
    tier: str | None = None
    append: frozenset[str] = frozenset()          # values merged into the current one (`_merged`)
    derived: frozenset[str] = frozenset()         # values computed from the shown one (supersede it)
    of_expected: dict[str, str] = field(default_factory=dict)   # norm: key → hash of the text judged


class Transaction:
    """Staged changes to ONE person document, committed in a single compare-and-set write.

    Tools stage values and normalised values against `view` (the document as it will be) and
    never write; `commit` writes once, so a tool that is stopped before its commit has changed
    nothing. A commit that loses the compare-and-set race re-reads the document and replays the
    op log on it, so concurrent writers never lose each other's entries; a normalised value whose
    text changed underneath is dropped on replay (with a `note` change) instead of being attached
    to text its decider never saw. Catalog changes (a property created, an enum value added)
    happen in the registry right away and are only *reported* here, with `record_catalog`.
    """

    def __init__(self, doc: dict, *, new: bool = False) -> None:
        self.doc = doc
        self.is_new = new
        self.ops: list[_Op] = []
        self.catalog: list[Change] = []
        self.committed = False
        self._reset()

    def _reset(self) -> None:
        self._to_set: dict[str, Any] = {}
        self._changes: list[Change] = []
        self._set: set[str] = set()
        self._unset: set[str] = set()
        self._unchanged: set[str] = set()
        self._kept: set[str] = set()
        self._value_changed = False

    @classmethod
    async def open(cls, person_id: str) -> "Transaction | None":
        """Read the document; None when the person does not exist."""
        doc = await db().people.find_one({"_id": _oid(person_id)})
        return cls(doc) if doc else None

    @classmethod
    def new(cls, tier: str, source_doc: dict[str, Any] | None = None) -> "Transaction":
        """A person that does not exist yet: staged like any other, inserted by `commit` (which
        refuses a document without a name). Its id is known from the start. `source_doc` is the
        document's `source` field (`{file, source_id, row}`) when a file's row creates the
        person."""
        _check_tier(tier)
        now = datetime.now(UTC)
        doc: dict[str, Any] = {"_id": ObjectId(), "tier": tier, "created_at": now,
                               "updated_at": now, "schema_version": history.SCHEMA_VERSION}
        if source_doc:
            doc["source"] = {k: v for k, v in source_doc.items() if v is not None}
        return cls(doc, new=True)

    @property
    def person_id(self) -> str:
        return str(self.doc["_id"])

    @property
    def view(self) -> dict:
        """The document as it will be after the commit: what later stages read."""
        return {**self.doc, **self._to_set}

    def public(self, include_history: bool = True) -> dict:
        return public(self.view, include_history)

    @property
    def dirty(self) -> bool:
        return self.is_new or bool(self._to_set) \
            or self.doc.get("schema_version") != history.SCHEMA_VERSION

    def changes(self) -> list[Change]:
        """The changes staged so far: catalog changes first, then the document's, in order."""
        return [*self.catalog, *self._changes]

    def record_catalog(self, change: Change) -> None:
        """Report a registry change made on this document's behalf (property_created,
        enum_value_added); a stopped invocation uses the list to clean up."""
        self.catalog.append(change)

    async def set_values(self, values: dict[str, Any], src: Source, *, tier: str | None = None,
                         append: Iterable[str] = (), derived: Iterable[str] = ()) -> list[Change]:
        """Stage new values (None removes); keys in `append` are merged into the current value
        (list items unioned, a text added as a line) instead of replacing it; keys in `derived`
        (and every appended key) were computed from the shown value and supersede it whatever
        its source. A value that does not outrank the shown one is recorded and reported as
        `kept`. Returns the changes this op adds."""
        cleaned = await _clean(values)
        if "name" in cleaned and cleaned["name"] is None:
            raise PersonError("name cannot be removed")
        if tier is not None:
            _check_tier(tier)
        op = _Op("values", cleaned, src, tier, frozenset(append), frozenset(derived))
        return await self._stage(op)

    async def set_norm(self, parsed: dict[str, Parsed], src: Source) -> list[Change]:
        """Stage normalised values for typed properties; returns the changes this op adds."""
        view = self.view
        for key in parsed:
            sp = properties.spec(key)
            if sp is None or sp.type not in NORMALISABLE or not history.is_prop(view.get(key)):
                raise PersonError(f"{key!r} is not a typed property of this person")
        of = {k: normalize.text_hash(history.current(view[k])) for k in parsed}
        op = _Op("norm", dict(parsed), src, of_expected=of)
        return await self._stage(op)

    async def _stage(self, op: _Op) -> list[Change]:
        n = len(self._changes)
        await self._apply(op)
        self.ops.append(op)
        return self._changes[n:]

    async def _apply(self, op: _Op) -> None:
        base = self.view
        now = datetime.now(UTC)
        if op.kind == "values":
            data = {k: _merged(base.get(k), v) if k in op.append and v is not None else v
                    for k, v in op.data.items()}
            before = {k: history.jsonable(history.current(base.get(k))) for k in data}
            to_set, changed, removed, unchanged, kept = history.apply(
                base, data, op.src, now, fields.SYSTEM_FIELDS, derived=op.append | op.derived)
            changes = [Change(key=k, action="append" if k in op.append else "set",
                              value=data[k], previous=before[k]) for k in changed]
            changes += [Change(key=k, action="removed", previous=before[k]) for k in removed]
            changes += [Change(key=k, action="kept", value=data[k], previous=before[k]) for k in kept]
            if op.tier is not None and op.tier != base.get("tier"):
                to_set["tier"] = op.tier
                changed.append("tier")
                changes.append(Change(key="tier", action="set", value=op.tier,
                                      previous=base.get("tier")))
            if changed or removed or kept:
                to_set["updated_at"] = now
                self._value_changed = True          # a kept entry is embedded too
            changes += await _normalize_into(base, to_set, now)
            self._set |= set(changed)
            self._unset |= set(removed)
            self._unchanged |= set(unchanged)
            self._kept |= set(kept)
        else:
            kept: dict[str, Parsed] = {}
            changes = []
            for key, p in op.data.items():
                if normalize.text_hash(history.current(base.get(key))) != op.of_expected.get(key):
                    changes.append(Change(key=key, action="note",
                                          value="text changed concurrently; the normalised value "
                                                "decided for the old text was dropped"))
                    continue
                kept[key] = p
            to_set, more = _write_norm(base, kept, op.src, now)
            changes += more
            self._set |= set(to_set)
        self._to_set.update(to_set)
        self._changes += changes

    async def _replay(self, doc: dict) -> None:
        self.doc = doc
        self._reset()
        for op in self.ops:
            await self._apply(op)

    async def commit(self, principal: Principal | None) -> WriteResult | None:
        """Write once (rebasing on a concurrent write), run the write hooks when a value changed,
        record one activity row. None when the person vanished meanwhile."""
        if self.committed:
            raise PersonError("transaction already committed")
        if self.is_new and not history.current(self.view.get("name")):
            raise PersonError("name is required")
        oid = self.doc["_id"]
        for _ in range(CAS_RETRIES):
            if not self.dirty:
                break
            to_set = {**self._to_set, "schema_version": history.SCHEMA_VERSION}
            if self.is_new:
                to_set.update({k: self.doc[k] for k in ("tier", "created_at", "updated_at",
                                                        "source") if k in self.doc})
            try:
                if await _cas_write(oid, self.doc, to_set, upsert=self.is_new):
                    break
            except DuplicateKeyError as e:               # the unique (file, source_id) of `source`
                src = self.doc.get("source") or {}
                raise ConflictError(f"a person for row {src.get('source_id')!r} of "
                                    f"{src.get('file')!r} was created concurrently; merge the "
                                    "file again") from e
            fresh = await db().people.find_one({"_id": oid})
            if fresh is None:
                return None
            await self._replay(fresh)
        else:
            raise ConflictError(f"person {self.person_id} is being modified concurrently; retry")
        self.committed = True
        pid = self.person_id
        if self._value_changed or self.is_new:
            await _after_write(WriteInfo(pid, self.ops[0].src if self.ops else None,
                                         tuple(sorted(self._set | self._unset | self._kept)),
                                         is_new=self.is_new))
        if self.is_new:
            await activity.record("person.create", principal, pid,
                                  {"fields": sorted(self._set), "src": self.ops[0].src.kind})
        elif self.ops:
            action = "person.update" if any(op.kind == "values" for op in self.ops) \
                else "person.normalize"
            meta: dict[str, Any] = {"set": sorted(self._set), "unset": sorted(self._unset),
                                    "unchanged": sorted(self._unchanged), "src": self.ops[0].src.kind}
            if self._kept:
                meta["kept"] = sorted(self._kept)
            await activity.record(action, principal, pid, meta)
        return WriteResult(await get(pid), self.changes())


async def create(principal: Principal, tier: str, values: dict[str, Any],
                 source: Source | None = None) -> WriteResult:
    """A new person: a transaction on a document that does not exist yet, committed once."""
    tx = Transaction.new(tier)
    await tx.set_values(values, source or Source.for_rest(principal))
    res = await tx.commit(principal)
    assert res is not None
    return res


async def update(principal: Principal, person_id: str, values: dict[str, Any],
                 tier: str | None = None, source: Source | None = None) -> WriteResult | None:
    """Append new values to the person's fields: a one-op transaction. Returns the write result
    (person and changes), or None if the person does not exist."""
    tx = await Transaction.open(person_id)
    if tx is None:
        return None
    await tx.set_values(values, source or Source.for_rest(principal), tier=tier)
    return await tx.commit(principal)


def _write_norm(doc: dict, parsed: dict[str, Parsed], src: Source,
                now: datetime) -> tuple[dict[str, Any], list[Change]]:
    """The `$set` that records `parsed` on `doc`'s typed properties (empty when nothing changes)
    and the changes it represents."""
    for key in parsed:
        sp = properties.spec(key)
        if sp is None or sp.type not in NORMALISABLE or not history.is_prop(doc.get(key)):
            raise PersonError(f"{key!r} is not a typed property of this person")
    to_set: dict[str, Any] = {}
    changes = _apply_norm(doc, parsed, to_set, src, now)
    return to_set, changes


async def update_norm(principal: Principal | None, person_id: str, parsed: dict[str, Parsed],
                      source: Source) -> WriteResult | None:
    """Record normalised values (from the normaliser agent or a re-run of the parser) on a
    person's typed properties: a one-op transaction. Returns the write result, or None if not
    found. Value hooks do not run: the text, and so the embeddings, are unchanged."""
    tx = await Transaction.open(person_id)
    if tx is None:
        return None
    await tx.set_norm(parsed, source)
    return await tx.commit(principal)


async def drop_field(key: str) -> int:
    """Remove a property from every document holding it, for a property that is being deleted
    from the catalog: a hard unset with a revision bump, no tombstone. Returns the documents
    changed. The caller deletes the field's vectors and registry entry afterwards."""
    if not fields.valid_key(key):
        raise PersonError(f"invalid field key {key!r}")
    res = await db().people.update_many({key: {"$exists": True}},
                                        {"$unset": {key: ""}, "$inc": {"rev": 1}})
    if res.modified_count:
        await activity.record("property.drop", None, key, {"documents": res.modified_count})
    return res.modified_count


Candidate = tuple[dict, list[str]]


async def find_candidates(values: dict[str, Any], source: dict | None = None) -> list[Candidate]:
    """The people that share an identity key with `values` (an email or phone, the LinkedIn or
    GitHub URL, the folded name) or the `(file, source_id)` of `source`, each with the keys that
    matched, most matches first. The lookup behind matching an incoming record to a person; the
    comparison runs on the canonical `norm.v` of each key (indexed)."""
    wanted: dict[str, Any] = {}
    for key in ("name", *IDENTITY_KEYS):
        value = values.get(key)
        sp = properties.spec(key)
        if value in (None, "", []) or sp is None or sp.type not in CANONICAL:
            continue
        parsed = normalize.parse(value, sp.type)
        if parsed.value is not None:
            wanted[key] = parsed.value
    ors: list[dict[str, Any]] = [{f"{k}.norm.v": {"$in": v} if isinstance(v, list) else v}
                                 for k, v in wanted.items()]
    src_file, src_id = (source or {}).get("file"), (source or {}).get("source_id")
    if src_file and src_id:
        ors.append({"source.file": src_file, "source.source_id": src_id})
    if not ors:
        return []
    out: list[Candidate] = []
    for doc in await find({"$or": ors}):
        hits = []
        for k, v in wanted.items():
            have = history.norm_value(doc.get(k))
            mine = set(v) if isinstance(v, list) else {v}
            theirs = set(have) if isinstance(have, list) else ({have} if have is not None else set())
            if mine & theirs:
                hits.append(k)
        doc_src = doc.get("source") or {}
        if src_file and src_id and doc_src.get("file") == src_file \
                and doc_src.get("source_id") == src_id:
            hits.append("source_id")
        out.append((doc, hits))
    out.sort(key=lambda c: -len(c[1]))
    return out


async def reparse(ids: list[str], force: bool = True) -> int:
    """Run the parser over these documents (free). With `force` (the default; a parser upgrade)
    the parser's earlier results are redone and only writes that change something land; agent
    decisions stay. Returns the number of documents written. The level-1 batch API the reparse
    assistant drives."""
    written = 0
    for doc in await fetch(ids):
        parsed = await pending_normalization(doc, force=force)
        if not parsed:
            continue
        to_set, _ = _write_norm(doc, parsed, NORMALIZE_SRC, datetime.now(UTC))
        if to_set and await _cas_write(doc["_id"], doc, to_set):
            written += 1
    return written


async def normalize_all(page: int = 500, progress: Callable[[int], None] | None = None,
                        force: bool = True) -> int:
    """`reparse` over every document, a page at a time."""
    written = 0
    last = None
    while True:
        ids = await find_ids({"_id": {"$gt": last}} if last else {}, limit=page)
        if not ids:
            return written
        last = ObjectId(ids[-1])
        written += await reparse(ids, force=force)
        if progress:
            progress(written)


async def recompute_shown(*, dry_run: bool = True, page: int = 500,
                          progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """Recompute the shown value of every property from its history under `history.pick` (the
    backfill for documents written when the last entry always showed). Reports the flips: by key,
    by `from_kind → to_kind`, and samples. With `dry_run` nothing is written."""
    report: dict[str, Any] = {"documents": 0, "flipped_documents": 0, "flips": 0,
                              "by_key": {}, "by_kinds": {}, "samples": []}
    last = None
    while True:
        ids = await find_ids({"_id": {"$gt": last}} if last else {}, limit=page)
        if not ids:
            return report
        last = ObjectId(ids[-1])
        for doc in await fetch(ids):
            report["documents"] += 1
            to_set: dict[str, Any] = {}
            for key, prop in doc.items():
                if key in fields.SYSTEM_FIELDS or not history.is_prop(prop) or not prop.get("h"):
                    continue
                i, _ = history.pick(prop["h"])
                shown = prop["h"][i].get("v")
                if history.same(shown, prop.get("v")):
                    continue
                to_set[key] = {**prop, "v": shown}
                old_kind = (prop["h"][-1].get("src") or {}).get("kind") or "legacy"
                new_kind = (prop["h"][i].get("src") or {}).get("kind") or "legacy"
                report["flips"] += 1
                report["by_key"][key] = report["by_key"].get(key, 0) + 1
                pair = f"{old_kind} -> {new_kind}"
                report["by_kinds"][pair] = report["by_kinds"].get(pair, 0) + 1
                if len(report["samples"]) < 200:
                    report["samples"].append({"id": str(doc["_id"]),
                                              "name": history.current(doc.get("name")), "key": key,
                                              "from": history.jsonable(prop.get("v")),
                                              "to": history.jsonable(shown), "kinds": pair})
            if not to_set:
                continue
            report["flipped_documents"] += 1
            if not dry_run:
                now = datetime.now(UTC)
                await _normalize_into(doc, to_set, now)
                to_set["updated_at"] = now
                if await _cas_write(doc["_id"], doc, to_set):
                    await _after_write(WriteInfo(str(doc["_id"]), Source("system", "recompute-shown"),
                                                 tuple(sorted(k for k in to_set if k != "updated_at"))))
        if progress:
            progress(report["documents"], report["flips"])


async def merge_documents(principal: Principal | None, keep_id: str, drop_id: str) -> WriteResult:
    """Fold one person's document into another's: every entry of every property of `drop` is
    appended to `keep`'s histories in its original order with its own time and source (nothing is
    rewritten), the normalised values follow, `drop` is deleted and both are reported to the write
    observers. The remedy for two documents that are one person (the same LinkedIn URL)."""
    if keep_id == drop_id:
        raise PersonError("keep and drop are the same person")
    keep = await db().people.find_one({"_id": _oid(keep_id)})
    drop = await db().people.find_one({"_id": _oid(drop_id)})
    if keep is None or drop is None:
        raise PersonError("person not found")
    now = datetime.now(UTC)
    to_set: dict[str, Any] = {}
    changes: list[Change] = []
    for key, prop in drop.items():
        if key in fields.SYSTEM_FIELDS:
            continue
        merged = {**keep, **to_set}.get(key)
        before = history.current(merged)
        for e in history.entries(prop):
            src = Source(**e["src"]) if isinstance(e.get("src"), dict) else Source("legacy")
            new = history.append(merged, e.get("v"), src, e.get("ts") or now)
            if new is not None:
                merged = new
        if merged is not None and merged is not keep.get(key):
            to_set[key] = merged
            after = history.current(merged)
            if not history.same(before, after):
                changes.append(Change(key=key, action="set" if before is None else "append",
                                      value=after, previous=before))
    if to_set:
        to_set["updated_at"] = now
        changes += await _normalize_into(keep, to_set, now)
        if not await _cas_write(keep["_id"], keep, to_set):
            raise ConflictError(f"person {keep_id} is being modified concurrently; retry")
    await db().people.delete_one({"_id": drop["_id"]})
    keys = tuple(sorted(k for k in to_set if k not in ("updated_at",)))
    await _after_write(WriteInfo(keep_id, Source("system", "merge"), keys))
    await _after_write(WriteInfo(drop_id, None, deleted=True))
    await activity.record("person.merge", principal, keep_id,
                          {"dropped": drop_id, "keys": list(keys)})
    doc = await get(keep_id)
    assert doc is not None
    return WriteResult(doc, changes)


def _ts_key(e: dict) -> datetime:
    """An entry's time for ordering a replay; one without a time (legacy) counts as oldest."""
    ts = e.get("ts")
    if not isinstance(ts, datetime):
        return datetime.min.replace(tzinfo=UTC)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _replay_order(dst: Any, src: Any) -> list[dict]:
    """Both histories merged by time. On the same time the destination's own entry counts as
    the newer (it stays shown within a class): one upload wrote both keys at once, and the
    property the record already showed is the one the person saw."""
    tagged = [(e, 0) for e in history.entries(src)] + [(e, 1) for e in history.entries(dst)]
    tagged.sort(key=lambda t: (_ts_key(t[0]), t[1]))
    return [e for e, _ in tagged]


async def fold_property(src_key: str, dst_key: str, *, dry_run: bool = True, page: int = 500,
                        sample: int = 20) -> dict[str, Any]:
    """Fold one property into another on every document holding it: the entries of both
    histories are replayed in time order through `history.append`, so the shown value follows
    the rules every write follows (an agent's title outranks an upload's, newest wins within a
    class with the destination's own entry newer on a tie, a repeated value lands once), the
    source key is dropped, the normalised values
    follow and the write observers hear of it. The data half of retiring a property whose
    meaning another one carries (`title_enriched` → `role`, `emails` → `email`); the catalog
    half is `properties.remove`. With `dry_run` nothing is written. Reports the documents
    folded, how many had no destination value (`filled`) or one already (`merged`), how many
    show another value now, and samples."""
    if src_key == dst_key or not fields.valid_key(src_key) or not fields.valid_key(dst_key) \
            or src_key in fields.SYSTEM_FIELDS or dst_key in fields.SYSTEM_FIELDS:
        raise PersonError(f"cannot fold {src_key!r} into {dst_key!r}")
    report: dict[str, Any] = {"documents": 0, "filled": 0, "merged": 0, "shown_changed": 0,
                              "samples": []}
    last = None
    while True:
        query: dict[str, Any] = {src_key: {"$exists": True}}
        if last is not None:
            query["_id"] = {"$gt": last}
        ids = await find_ids(query, limit=page)
        if not ids:
            return report
        last = ObjectId(ids[-1])
        for doc in await fetch(ids):
            now = datetime.now(UTC)
            src_prop, dst_prop = doc.get(src_key), doc.get(dst_key)
            before = history.current(dst_prop)
            merged = None
            for e in _replay_order(dst_prop, src_prop):
                src = Source(**e["src"]) if isinstance(e.get("src"), dict) else Source("legacy")
                new = history.append(merged, e.get("v"), src, e.get("ts") or now,
                                     bool(e.get("derived")))
                if new is not None:
                    merged = new
            after = history.current(merged)
            report["documents"] += 1
            report["filled" if dst_prop is None else "merged"] += 1
            if not history.same(before, after):
                report["shown_changed"] += 1
                if len(report["samples"]) < sample:
                    report["samples"].append({"id": str(doc["_id"]),
                                              "name": history.current(doc.get("name")),
                                              "from": history.jsonable(before),
                                              "to": history.jsonable(after)})
            elif merged is not None and history.is_prop(dst_prop) and "norm" in dst_prop:
                merged = {**merged, "norm": dst_prop["norm"]}      # the text stands: so does its key
            if dry_run:
                continue
            to_set: dict[str, Any] = {"updated_at": now}
            if merged is not None and merged != dst_prop:
                to_set[dst_key] = merged
            base = {k: v for k, v in doc.items() if k != src_key}
            await _normalize_into(base, to_set, now)
            if not await _cas_write(doc["_id"], doc, to_set, unset=(src_key,)):
                raise ConflictError(f"person {doc['_id']} is being modified concurrently; retry")
            await _after_write(WriteInfo(str(doc["_id"]), Source("system", "fold"), (dst_key,)))


async def rewrite_values(key: str, fn: Callable[[str], str], *, dry_run: bool = True,
                         source: Source | None = None, page: int = 500, sample: int = 20
                         ) -> dict[str, Any]:
    """Apply `fn` to the shown text of `key` on every document holding it and record each
    changed result as a derived entry of `source`: a mechanical repair of what is shown, which
    the history keeps the original of and a later human value still outranks. With `dry_run`
    nothing is written."""
    if not fields.valid_key(key) or key in fields.SYSTEM_FIELDS:
        raise PersonError(f"invalid field key {key!r}")
    source = source or Source("system", "rewrite")
    report: dict[str, Any] = {"documents": 0, "changed": 0, "samples": []}
    last = None
    while True:
        query: dict[str, Any] = {key: {"$exists": True}}
        if last is not None:
            query["_id"] = {"$gt": last}
        ids = await find_ids(query, limit=page)
        if not ids:
            return report
        last = ObjectId(ids[-1])
        for doc in await fetch(ids, projection={key: 1, "name": 1}):
            report["documents"] += 1
            current = history.current(doc.get(key))
            if not isinstance(current, str):
                continue
            new = fn(current)
            if new == current:
                continue
            report["changed"] += 1
            if len(report["samples"]) < sample:
                report["samples"].append({"id": str(doc["_id"]), "name": history.current(doc.get("name")),
                                          "from": current, "to": new})
            if dry_run:
                continue
            tx = await Transaction.open(str(doc["_id"]))
            if tx is not None:
                await tx.set_values({key: new}, source, derived=[key])
                await tx.commit(None)


async def duplicate_groups() -> list[dict[str, Any]]:
    """Documents that share a canonical identity value (a LinkedIn URL, an email): one group per
    value, `ids` oldest first with each one's name and folded name, for a dedup pass to judge."""
    groups: dict[str, dict[str, Any]] = {}
    for key in ("linkedin", "email"):
        pipeline = [
            {"$match": {f"{key}.norm.v": {"$exists": True, "$ne": None}}},
            {"$unwind": f"${key}.norm.v"},
            {"$group": {"_id": f"${key}.norm.v", "ids": {"$push": "$_id"}, "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": 1}}},
        ]
        async for g in await db().people.aggregate(pipeline):
            ids = sorted({str(i) for i in g["ids"]})
            if len(ids) < 2:
                continue
            groups.setdefault("|".join(ids), {"key": key, "value": g["_id"], "ids": ids})
    out = []
    for g in groups.values():
        docs = await fetch(g["ids"], projection={"name": 1, "created_at": 1, "tier": 1})
        docs.sort(key=lambda d: (d.get("created_at") or datetime.min.replace(tzinfo=UTC), str(d["_id"])))
        people_ = [{"id": str(d["_id"]), "name": history.current(d.get("name")), "tier": d.get("tier"),
                    "created_at": d.get("created_at")} for d in docs]
        names = {fields.fold(str(p["name"] or "")) for p in people_}
        out.append({**g, "ids": [p["id"] for p in people_], "people": people_,
                    "same_name": len(names) == 1})
    return out


async def delete(principal: Principal, person_id: str) -> bool:
    res = await db().people.delete_one({"_id": _oid(person_id)})
    if res.deleted_count == 0:
        return False
    await _after_write(WriteInfo(person_id, None, deleted=True))
    await activity.record("person.delete", principal, person_id)
    return True


async def _after_write(info: WriteInfo) -> None:
    for hook in _hooks:
        try:
            await hook(info)
        except Exception:
            # The write is already persisted; a hook failure (e.g. the vector store) must not fail
            # the request. `python -m app.cli embed` heals any embedding gap.
            log.exception("write hook %s failed for person %s", getattr(hook, "__name__", hook),
                          info.person_id)


# --- migration --------------------------------------------------------------------------------------

async def migrate_legacy(dry_run: bool = False, page: int = 500) -> int:
    """Wrap every bare field value on every person into a property (see app.history). Idempotent;
    never bumps updated_at. Returns the number of documents migrated."""
    people = db().people
    now = datetime.now(UTC)
    migrated = 0
    last = None
    while True:
        q: dict[str, Any] = {"schema_version": {"$ne": history.SCHEMA_VERSION}}
        if last is not None:
            q["_id"] = {"$gt": last}
        batch = await people.find(q).sort("_id", 1).limit(page).to_list(None)
        if not batch:
            break
        last = batch[-1]["_id"]
        for doc in batch:
            upgrades = history.upgrade_doc(doc, now, fields.SYSTEM_FIELDS)
            if dry_run:
                migrated += 1
                continue
            res = await people.update_one(
                {"_id": doc["_id"], "schema_version": {"$ne": history.SCHEMA_VERSION}},
                {"$set": {**upgrades, "schema_version": history.SCHEMA_VERSION},
                 "$inc": {"rev": 1}})
            migrated += res.modified_count
    if migrated and not dry_run:
        log.info("migrated %d people documents to field history", migrated)
    if not dry_run:
        indexes = await people.index_information()
        if "name_1" in indexes:
            await people.drop_index("name_1")
        await mongo.ensure_indexes()
    return migrated
