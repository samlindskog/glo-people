"""Per-field history on people documents.

Every non-system field on a person is a *property*: `{"v": <current value>, "h": [entries]}`, where
`h` is append-only (oldest first) and `v` is a cached copy of `h[-1]["v"]` so Mongo queries, sorts
and indexes work on `field.v`. An entry is `{"v": value | None, "ts": datetime, "src": {...}}`;
`v: None` is a tombstone (the field was removed). `src` records where the value came from:
`{"kind": ui|api|mcp|agent|import|legacy|system, "by": <user, agent or file>, "ref": <key/run/row>}`.
Not to be confused with the document-level `source` sub-document the CSV importer writes.

A typed property (app.property_specs) also carries the normalised form of its text:
`"norm": {"v": <typed value | None>, "flag"?: unmapped | non_normalizable, "h": [entries]}` with
the same append-only history; each norm entry has `of`, a hash of the text it was derived from
(app.normalize), so a re-normalisation is only needed when the text changes. `v` is always the
text as entered; `norm.v` is what filters and sorts use.

Documents written before this module existed hold bare scalars; every reader here tolerates them
(`entries()` synthesises one entry) and `upgrade_doc()` (run by `people.migrate_legacy`) wraps them
for good. This module is pure: no database access.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.auth import Principal

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SourceKind = Literal["ui", "api", "mcp", "agent", "import", "legacy", "system"]
NormFlag = Literal["unmapped", "non_normalizable"]
ChangeAction = Literal["set", "append", "removed", "normalized", "flagged", "property_created",
                       "enum_value_added", "note", "kept"]


class Change(BaseModel):
    """One thing that happened to a person's document: the vocabulary every writer, tool and
    assistant reports in. `set`/`append`/`removed` are value writes that changed what is shown
    (`previous` is the value before), `kept` is a value recorded in the history that does not
    replace the one shown (a weaker source than the shown value's; `previous` is what still
    shows), `normalized`/`flagged` are the typed form of a value (`flag` says why there is none),
    `note` is something that did not happen and why, `property_created` and `enum_value_added`
    are catalog changes (a new property; a new canonical value of an enum property)."""

    key: str
    action: ChangeAction
    value: Any = None
    previous: Any = None
    flag: str | None = None


@dataclass(frozen=True)
class Source:
    kind: SourceKind
    by: str | None = None
    ref: str | None = None

    def doc(self) -> dict[str, str]:
        out: dict[str, str] = {"kind": self.kind}
        if self.by:
            out["by"] = self.by
        if self.ref:
            out["ref"] = self.ref
        return out

    @staticmethod
    def for_rest(p: Principal) -> "Source":
        if p.via_api_key:
            return Source("api", p.username, f"key:{p.api_key_id}")
        return Source("ui", p.username)

    @staticmethod
    def for_mcp(p: Principal, agent: str | None = None) -> "Source":
        """An MCP write is always `kind: mcp` by the key's owner; a caller-supplied agent name is
        kept in `ref` so it can never pass for a workflow write (`kind: agent`)."""
        ref = f"key:{p.api_key_id}"
        if agent and agent.strip():
            ref += f" agent:{agent.strip()[:64]}"
        return Source("mcp", p.username, ref)


def is_prop(x: Any) -> bool:
    return isinstance(x, dict) and "h" in x


def current(prop: Any) -> Any:
    """The current value of a property (or a legacy bare value)."""
    return prop.get("v") if is_prop(prop) else prop


def entries(prop: Any) -> list[dict]:
    """The history entries of a property; a legacy bare value becomes one synthetic entry."""
    if prop is None:
        return []
    if is_prop(prop):
        return list(prop["h"])
    return [{"v": prop}]


def same(a: Any, b: Any) -> bool:
    """Dedupe equality: type-strict (1 != True, 12000 != 12000.0), lists element-wise in order."""
    if isinstance(a, list) or isinstance(b, list):
        return (isinstance(a, list) and isinstance(b, list) and len(a) == len(b)
                and all(same(x, y) for x, y in zip(a, b, strict=True)))
    return type(a) is type(b) and a == b


# --- which entry is shown ---------------------------------------------------------------------------
#
# `h` is append-only; `v` is the SHOWN value, chosen from `h` by `pick`: by the trust of the entry's
# source, newest first within a class. A person (or their directed agent) acting on one record
# outranks an assistant's finding, which outranks a bulk upload. An entry marked `derived` was
# computed from the value shown at the time (a list merged, a broken name fixed, a name completed
# from the profile behind the URL): it supersedes the shown entry whatever its source and inherits
# its trust. A weaker later write is recorded but not shown (`kept`); writing an earlier value
# again from a human surface restores it. Position in `h` is recency (legacy entries lack `ts`).
TRUST: dict[str, int] = {"ui": 2, "api": 2, "mcp": 2, "agent": 1, "import": 0, "legacy": 0,
                         "system": 0}
TRUST_NAMES = {2: "a person's own edit", 1: "an assistant's finding", 0: "an upload"}


def trust(entry: dict) -> int:
    src = entry.get("src") or {}
    return TRUST.get(src.get("kind") or "legacy", 0)


def _derived(entry: dict) -> bool:
    return bool(entry.get("derived")) or (entry.get("src") or {}).get("kind") == "system"


def pick(h: list[dict]) -> tuple[int, int]:
    """The index of the shown entry and the trust it carries: a left fold where a derived entry
    always takes over (keeping the trust reached so far) and any other entry takes over when its
    trust is at least the current winner's."""
    win, wt = 0, trust(h[0])
    for i, e in enumerate(h[1:], 1):
        if _derived(e):
            win, wt = i, max(wt, trust(e))
        elif trust(e) >= wt:
            win, wt = i, trust(e)
    return win, wt


def explain(h: list[dict], i: int) -> str:
    """Why entry `i` is the one shown, for a reader."""
    if i == len(h) - 1:
        return "newest entry"
    later = [e for e in h[i + 1:] if not _derived(e)]
    if _derived(h[i]):
        return "derived from the value shown before it"
    kinds = sorted({(e.get("src") or {}).get("kind") or "legacy" for e in later})
    return f"{TRUST_NAMES[trust(h[i])]} outranks the later {', '.join(kinds)} entries"


def new_entry(value: Any, src: Source, ts: datetime, derived: bool = False) -> dict:
    entry: dict[str, Any] = {"v": value, "ts": ts, "src": src.doc()}
    if derived:
        entry["derived"] = True
    return entry


def new_prop(value: Any, src: Source, ts: datetime) -> dict:
    return {"v": value, "h": [new_entry(value, src, ts)]}


def append(prop: Any, value: Any, src: Source, ts: datetime, derived: bool = False) -> dict | None:
    """The property with `value` appended and `v` recomputed as the shown entry, or None when the
    write would change nothing.

    `prop` may be None (field absent), a legacy bare value, or a property. Writing the shown value
    is a no-op; so is removing (value None) an absent or already removed field; so is repeating the
    last entry's value from a source that still would not be shown (a repeated enrichment adds
    nothing). A human re-writing a kept value lands and shows.
    """
    if prop is None:
        return None if value is None else new_prop(value, src, ts)
    if not is_prop(prop):
        prop = {"v": prop, "h": [{"v": prop}]}
    if same(prop.get("v"), value):
        return None
    new_h = [*prop["h"], new_entry(value, src, ts, derived)]
    shown, _ = pick(new_h)
    if shown != len(new_h) - 1 and prop["h"] and same(prop["h"][-1].get("v"), value):
        return None
    return {**prop, "v": new_h[shown]["v"], "h": new_h}   # keeps `norm`


def apply(doc: dict, changes: dict[str, Any], src: Source, now: datetime, system: frozenset[str],
          derived: frozenset[str] | set[str] = frozenset()
          ) -> tuple[dict[str, Any], list[str], list[str], list[str], list[str]]:
    """Diff `changes` ({key: value | None}) against `doc` and return what to `$set`.

    Returns (to_set, changed, removed, unchanged, kept). `to_set` holds the upgraded form of every
    legacy bare field (so a first write migrates the whole document) plus the appended properties;
    `changed`/`removed` list the keys whose shown value changed, `kept` the keys whose entry was
    recorded while a stronger value stays shown, `unchanged` the keys nothing was appended for.
    Keys in `derived` were computed from the shown value and supersede it.
    """
    upgraded = upgrade_doc(doc, now, system)
    props = {**{k: v for k, v in doc.items() if k not in system}, **upgraded}
    to_set: dict[str, Any] = dict(upgraded)
    changed, removed, unchanged, kept = [], [], [], []
    for key, value in changes.items():
        before = current(props.get(key))
        new = append(props.get(key), value, src, now, derived=key in derived)
        if new is None:
            unchanged.append(key)
            continue
        to_set[key] = props[key] = new
        if same(current(new), before):
            kept.append(key)
        else:
            (removed if current(new) is None else changed).append(key)
    return to_set, changed, removed, unchanged, kept


def _legacy_source(doc: dict) -> Source:
    src = doc.get("source")
    if isinstance(src, dict) and src.get("file"):
        row = src.get("row")
        return Source("import", str(src["file"]), f"row {row}" if row is not None else None)
    return Source("legacy")


def upgrade_doc(doc: dict, now: datetime, system: frozenset[str]) -> dict[str, dict]:
    """{key: property} for every non-system key of `doc` that is still a bare value."""
    out: dict[str, dict] = {}
    ts = doc.get("updated_at") or doc.get("created_at") or now
    src = _legacy_source(doc)
    for key, value in doc.items():
        if key in system or is_prop(value):
            continue
        if isinstance(value, dict):
            log.warning("person %s field %s holds a dict; left as is", doc.get("_id"), key)
            continue
        out[key] = new_prop(value, src, ts)
    return out


def flatten(doc: dict, system: frozenset[str]) -> dict[str, Any]:
    """{key: current value} for the non-system keys whose current value is not a tombstone."""
    out = {}
    for key, value in doc.items():
        if key in system:
            continue
        cur = current(value)
        if cur is not None:
            out[key] = cur
    return out


def jsonable(value: Any) -> Any:
    """Values as the API returns them: datetimes as ISO strings, containers element-wise."""
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return value.isoformat() if hasattr(value, "isoformat") else value


def _public_entry(e: dict) -> dict:
    pub = dict(e)
    pub["v"] = jsonable(pub.get("v"))
    if "ts" in pub:
        pub["ts"] = jsonable(pub["ts"])
    return pub


def public_entries(prop: Any) -> list[dict]:
    """The entries as the API returns them; the shown one carries `shown: true` and `why`."""
    out = [_public_entry(e) for e in entries(prop)]
    if is_prop(prop) and prop.get("h"):
        i, _ = pick(prop["h"])
        out[i] = {**out[i], "shown": True, "why": explain(prop["h"], i)}
    return out


# --- normalised values ----------------------------------------------------------------------------

def norm_of(prop: Any) -> dict | None:
    return prop.get("norm") if is_prop(prop) else None


def norm_value(prop: Any) -> Any:
    n = norm_of(prop)
    return n.get("v") if n else None


def norm_flag(prop: Any) -> str | None:
    n = norm_of(prop)
    return n.get("flag") if n else None


def norm_entries(prop: Any) -> list[dict]:
    n = norm_of(prop)
    return list(n.get("h", [])) if n else []


def append_norm(prop: dict, value: Any, src: Source, ts: datetime, of: str,
                flag: str | None = None) -> dict | None:
    """The property with a normalised value appended, or None when the last norm entry already
    derives from the same text (`of`) with the same value and flag."""
    norm = prop.get("norm") or {}
    h = list(norm.get("h", []))
    if h and h[-1].get("of") == of and same(norm.get("v"), value) and norm.get("flag") == flag:
        return None
    entry: dict[str, Any] = {"v": value, "ts": ts, "src": src.doc(), "of": of}
    new: dict[str, Any] = {"v": value, "h": [*h, entry]}
    if flag:
        entry["flag"] = new["flag"] = flag
    return {**prop, "norm": new}


def flatten_norm(doc: dict, system: frozenset[str]) -> dict[str, Any]:
    """{key: normalised value} for present (non-tombstone) properties that have one."""
    out = {}
    for key, prop in doc.items():
        if key in system or current(prop) is None:
            continue
        nv = norm_value(prop)
        if nv is not None:
            out[key] = nv
    return out


def norm_flags(doc: dict, system: frozenset[str]) -> dict[str, str]:
    """{key: flag} for present properties whose text could not be (fully) normalised."""
    return {key: flag for key, prop in doc.items()
            if key not in system and current(prop) is not None and (flag := norm_flag(prop))}


def public_norm_entries(prop: Any) -> list[dict]:
    return [_public_entry(e) for e in norm_entries(prop)]

