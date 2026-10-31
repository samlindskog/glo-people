"""The person document service: the only writer of the `people` collection.

Field values are never overwritten: every write appends a time-stamped, source-tagged entry to the
field's history (see app.history) and refreshes the cached current value; typed properties get
their normalised value (app.normalize) in the same write. Writes accept only properties that exist
(app.properties); the importer creates properties before it calls `upsert_imported`. Every write is
compare-and-set on the document's `rev`, so concurrent writers never lose each other's entries.
Other systems read through the helpers here (`fetch`, `find`, `count`, `find_ids`, `coverage`,
`value_counts`) and react to value writes through `on_write` hooks that the composition root
registers (e.g. embeddings).
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError, DuplicateKeyError

from app import activity, fields, history, mongo, normalize, properties
from app.auth import Principal
from app.history import Change, Source
from app.mongo import db
from app.normalize import Parsed
from app.property_specs import NORMALISABLE

log = logging.getLogger(__name__)

Scalar = str | int | float | bool
WRITE_BATCH = 1000
CAS_RETRIES = 8      # a compare-and-set attempt fails only because another writer succeeded
NORMALIZE_SRC = Source("system", "normalize")

_hooks: list[Callable[[str], Awaitable[None]]] = []


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


def on_write(hook: Callable[[str], Awaitable[None]]) -> None:
    """Register a coroutine called with the person id after every successful value write."""
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
    out["norm"] = history.jsonable(history.flatten_norm(doc, fields.SYSTEM_FIELDS))
    out["norm_flags"] = history.norm_flags(doc, fields.SYSTEM_FIELDS)
    if include_history:
        props = {k: v for k, v in doc.items() if k not in fields.SYSTEM_FIELDS}
        out["history"] = {k: history.public_entries(v) for k, v in props.items()}
        out["history_norm"] = {k: history.public_norm_entries(v) for k, v in props.items()
                               if history.norm_of(v)}
    return out


async def get(person_id: str) -> dict | None:
    doc = await db().people.find_one({"_id": _oid(person_id)})
    return public(doc, include_history=True) if doc else None


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


async def coverage() -> dict[str, dict[str, int]]:
    """Per property: `count` of people whose current value is present (not a tombstone), and how
    many of those are `unmapped` / `non_normalizable` (typed properties)."""
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
    """The Change a norm entry represents (None for clearing a removed value's norm)."""
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


async def _cas_write(oid: ObjectId, doc: dict, to_set: dict[str, Any]) -> bool:
    """Apply `to_set` if the document is still at the revision we read; False otherwise."""
    res = await db().people.update_one({"_id": oid, **_rev_filter(doc)},
                                       {"$set": to_set, "$inc": {"rev": 1}})
    return res.matched_count == 1


# --- staged writes: one commit per document -----------------------------------------------------------

@dataclass
class _Op:
    """One staged operation. The op log is the only state a transaction keeps between attempts:
    everything else is derived from it against the document of the current attempt."""

    kind: str                                     # values | norm
    data: dict[str, Any]                          # cleaned values, or parsed normalised values
    src: Source
    tag: str = ""
    tier: str | None = None
    appended: frozenset[str] = frozenset()        # values whose `set` is reported as `append`
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

    def __init__(self, doc: dict) -> None:
        self.doc = doc
        self.ops: list[_Op] = []
        self.catalog: list[tuple[str, Change]] = []
        self.committed = False
        self._reset()

    def _reset(self) -> None:
        self._to_set: dict[str, Any] = {}
        self._changes: list[tuple[str, Change]] = []
        self._set: set[str] = set()
        self._unset: set[str] = set()
        self._unchanged: set[str] = set()
        self._value_changed = False

    @classmethod
    async def open(cls, person_id: str) -> "Transaction | None":
        """Read the document; None when the person does not exist."""
        doc = await db().people.find_one({"_id": _oid(person_id)})
        return cls(doc) if doc else None

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
        return bool(self._to_set) or self.doc.get("schema_version") != history.SCHEMA_VERSION

    def changes(self, *tags: str) -> list[Change]:
        """The changes staged so far: catalog changes first, then the document's, in order;
        with `tags`, only the ones staged under those tags."""
        wanted = set(tags)
        out = [c for t, c in self.catalog if not wanted or t in wanted]
        return out + [c for t, c in self._changes if not wanted or t in wanted]

    def record_catalog(self, change: Change, tag: str = "") -> None:
        """Report a registry change made on this document's behalf (property_created,
        enum_value_added); a stopped invocation uses the list to clean up."""
        self.catalog.append((tag, change))

    async def set_values(self, values: dict[str, Any], src: Source, *, tier: str | None = None,
                         appended: frozenset[str] | set[str] = frozenset(),
                         tag: str = "") -> list[Change]:
        """Stage new values (None removes); returns the changes this op adds."""
        cleaned = await _clean(values)
        if "name" in cleaned and cleaned["name"] is None:
            raise PersonError("name cannot be removed")
        if tier is not None:
            _check_tier(tier)
        op = _Op("values", cleaned, src, tag, tier, frozenset(appended))
        return await self._stage(op)

    async def set_norm(self, parsed: dict[str, Parsed], src: Source, *, tag: str = "") -> list[Change]:
        """Stage normalised values for typed properties; returns the changes this op adds."""
        view = self.view
        for key in parsed:
            sp = properties.spec(key)
            if sp is None or sp.type not in NORMALISABLE or not history.is_prop(view.get(key)):
                raise PersonError(f"{key!r} is not a typed property of this person")
        of = {k: normalize.text_hash(history.current(view[k])) for k in parsed}
        op = _Op("norm", dict(parsed), src, tag, of_expected=of)
        return await self._stage(op)

    async def _stage(self, op: _Op) -> list[Change]:
        n = len(self._changes)
        await self._apply(op)
        self.ops.append(op)
        return [c for _, c in self._changes[n:]]

    async def _apply(self, op: _Op) -> None:
        base = self.view
        now = datetime.now(UTC)
        if op.kind == "values":
            before = {k: history.jsonable(history.current(base.get(k))) for k in op.data}
            to_set, changed, removed, unchanged = history.apply(base, op.data, op.src, now,
                                                                fields.SYSTEM_FIELDS)
            changes = [Change(key=k, action="append" if k in op.appended else "set",
                              value=op.data[k], previous=before[k]) for k in changed]
            changes += [Change(key=k, action="removed", previous=before[k]) for k in removed]
            if op.tier is not None and op.tier != base.get("tier"):
                to_set["tier"] = op.tier
                changed.append("tier")
                changes.append(Change(key="tier", action="set", value=op.tier,
                                      previous=base.get("tier")))
            if changed or removed:
                to_set["updated_at"] = now
                self._value_changed = True
            changes += await _normalize_into(base, to_set, now)
            self._set |= set(changed)
            self._unset |= set(removed)
            self._unchanged |= set(unchanged)
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
        self._changes += [(op.tag, c) for c in changes]

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
        oid = self.doc["_id"]
        for _ in range(CAS_RETRIES):
            if not self.dirty:
                break
            if await _cas_write(oid, self.doc, {**self._to_set,
                                                "schema_version": history.SCHEMA_VERSION}):
                break
            fresh = await db().people.find_one({"_id": oid})
            if fresh is None:
                return None
            await self._replay(fresh)
        else:
            raise ConflictError(f"person {self.person_id} is being modified concurrently; retry")
        self.committed = True
        pid = self.person_id
        if self._value_changed:
            await _after_write(pid)
        if self.ops:
            action = "person.update" if any(op.kind == "values" for op in self.ops) \
                else "person.normalize"
            await activity.record(action, principal, pid, {
                "set": sorted(self._set), "unset": sorted(self._unset),
                "unchanged": sorted(self._unchanged), "src": self.ops[0].src.kind})
        return WriteResult(await get(pid), self.changes())


async def create(principal: Principal, tier: str, values: dict[str, Any],
                 source: Source | None = None) -> WriteResult:
    _check_tier(tier)
    cleaned = await _clean(values)
    if not cleaned.get("name"):
        raise PersonError("name is required")
    src = source or Source.for_rest(principal)
    now = datetime.now(UTC)
    present = {k: v for k, v in cleaned.items() if v is not None}
    doc: dict[str, Any] = {k: history.new_prop(v, src, now) for k, v in present.items()}
    doc.update({"tier": tier, "created_at": now, "updated_at": now,
                "schema_version": history.SCHEMA_VERSION, "rev": 1})
    norm: dict[str, Any] = {}
    changes = [Change(key=k, action="set", value=v) for k, v in present.items()]
    changes += await _normalize_into(doc, norm, now)
    doc.update(norm)
    res = await db().people.insert_one(doc)
    pid = str(res.inserted_id)
    await _after_write(pid)
    await activity.record("person.create", principal, pid,
                          {"fields": sorted(present), "src": src.kind})
    return WriteResult(await get(pid), changes)


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


async def delete(principal: Principal, person_id: str) -> bool:
    res = await db().people.delete_one({"_id": _oid(person_id)})
    if res.deleted_count == 0:
        return False
    for hook in _hooks:
        try:
            await hook(person_id)
        except Exception:
            log.exception("write hook failed after deleting person %s", person_id)
    await activity.record("person.delete", principal, person_id)
    return True


# --- import -----------------------------------------------------------------------------------------

async def _import_set(cur: dict | None, sid: str, rows: list[tuple[int, dict[str, Any]]],
                      source_file: str, tier: str, now: datetime) -> dict[str, Any]:
    """The `$set` that applies a source_id's rows (in file order) to its current document."""
    set_doc: dict[str, Any] = {}
    for row_no, values in rows:
        src = Source("import", source_file, f"row {row_no}")
        base = {**(cur or {}), **set_doc}
        to_set, changed, _, _ = history.apply(base, values, src, now, fields.SYSTEM_FIELDS)
        set_doc.update(to_set)
        set_doc["source"] = {"file": source_file, "source_id": sid, "row": row_no}
        if changed or cur is None:
            set_doc["updated_at"] = now
    set_doc.update({"tier": tier, "schema_version": history.SCHEMA_VERSION})
    await _normalize_into(cur or {}, set_doc, now)
    return set_doc


async def upsert_imported(rows: list[tuple[str, int, dict[str, Any]]], source_file: str,
                          tier: str, now: datetime) -> tuple[int, int, list[str]]:
    """Bulk upsert of imported rows `(source_id, row_no, values)` keyed by (file, source_id).
    The caller has created the properties the rows use (`properties.ensure`).

    Rows sharing a source_id fold into one update applied in file order, so no row's entry is lost
    to a later $set of the same property. Each write is compare-and-set on the document's `rev`
    (a concurrent edit makes the upsert collide on the unique source index, and that row is
    redone on its own). Returns (inserted, updated, ids of the documents touched). Values are
    appended to the field histories like any other write; unchanged values append nothing and
    leave `updated_at`. Write hooks do not run: the caller embeds the ids itself.
    """
    inserted = updated = 0
    ids: list[str] = []
    for start in range(0, len(rows), WRITE_BATCH):
        chunk = rows[start:start + WRITE_BATCH]
        groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for source_id, row_no, values in chunk:
            groups.setdefault(source_id, []).append((row_no, values))
        existing = {d["source"]["source_id"]: d for d in await find(
            {"source.file": source_file, "source.source_id": {"$in": list(groups)}})}

        ops, sids = [], []
        for sid, group in groups.items():
            cur = existing.get(sid)
            set_doc = await _import_set(cur, sid, group, source_file, tier, now)
            ops.append(UpdateOne({"source.file": source_file, "source.source_id": sid,
                                  **_rev_filter(cur)},
                                 {"$set": set_doc, "$setOnInsert": {"created_at": now},
                                  "$inc": {"rev": 1}}, upsert=True))
            sids.append(sid)
        try:
            res = await db().people.bulk_write(ops, ordered=False)
            inserted += res.upserted_count
            updated += res.matched_count
            failed: list[str] = []
        except BulkWriteError as e:
            errors = e.details.get("writeErrors", [])
            if any(we.get("code") != 11000 for we in errors):
                raise
            inserted += e.details.get("nUpserted", 0)
            updated += e.details.get("nMatched", 0)
            failed = [sids[we["index"]] for we in errors]
        for sid in failed:   # lost the race with another writer: redo this source_id alone
            inserted_now, updated_now = await _reimport_one(sid, groups[sid], source_file, tier,
                                                            now)
            inserted += inserted_now
            updated += updated_now
        ids += await find_ids({"source.file": source_file, "source.source_id": {"$in": sids}})
    return inserted, updated, ids


async def _reimport_one(sid: str, group: list[tuple[int, dict[str, Any]]], source_file: str,
                        tier: str, now: datetime) -> tuple[int, int]:
    key = {"source.file": source_file, "source.source_id": sid}
    for _ in range(CAS_RETRIES):
        cur = await db().people.find_one(key)
        set_doc = await _import_set(cur, sid, group, source_file, tier, now)
        try:
            res = await db().people.update_one(
                {**key, **_rev_filter(cur)},
                {"$set": set_doc, "$setOnInsert": {"created_at": now}, "$inc": {"rev": 1}},
                upsert=True)
        except DuplicateKeyError:   # the document changed or appeared meanwhile: read again
            continue
        if res.upserted_id is not None:
            return 1, 0
        if res.matched_count:
            return 0, 1
    raise ConflictError(f"row {sid!r} of {source_file} is being modified concurrently; retry")


async def _after_write(person_id: str) -> None:
    for hook in _hooks:
        try:
            await hook(person_id)
        except Exception:
            # The write is already persisted; a hook failure (e.g. the vector store) must not fail
            # the request. `python -m app.cli embed` heals any embedding gap.
            log.exception("write hook %s failed for person %s", getattr(hook, "__name__", hook),
                          person_id)


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
