"""Incoming records (level 1): CSV parsing, the plan for one record (which values are written
verbatim and which go to the storage agent) and the match of a record to the people already in
the database. Nothing here writes: the merge assistant (level 3) composes these with the tools.

A CSV's headers become property keys; cells keep their text as entered (typed properties get
their normalised value at write time) and list columns are split into items. Rows that describe
the same person (same `ID`, or the same name and first email) are grouped so they land in file
order. A record matches a person by a shared identity key (email, phone, LinkedIn, GitHub, the
file's ID), else by the folded name when it is unique or corroborated by company, location or an
email domain; several plausible people is `ambiguous`, nothing is `new`."""

import csv
import io
import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app import fields, history, people, properties
from app.property_specs import IDENTITY_KEYS

_LIST_SEPARATORS = re.compile(r"\s*(?:\n|;|\s\|\s)\s*")
# Free text that often contains commas/newlines but is never a list.
_PROSE = re.compile(r"(^|_)(bio|bios|note|notes|description|message|messages|summary|liner|"
                    r"comment|comments|about|location|address|city|country|region|name|title|"
                    r"role|company|goals?|preferences)($|_)")
LIST_TYPES = ("list", "enum_list")
SIMPLE_TEXT_CHARS = 160       # a text this short is written as entered; longer prose is filed by
                              # the storage agent
MATCH_SAMPLE = 20             # rows a preview matches against the database

csv.field_size_limit(10_000_000)


@dataclass
class Row:
    row_no: int
    values: dict[str, Any]
    source_id: str | None = None


@dataclass
class ParsedCsv:
    """A CSV read into rows ready for the merge assistant."""

    file: str
    columns: list[dict]
    rows: list[Row]
    total: int = 0                 # rows with content
    skipped: int = 0               # rows without a name

    @property
    def used_keys(self) -> list[str]:
        return sorted({k for r in self.rows for k in r.values})


def _looks_like_list(values: list[str], split) -> bool:
    """A column is a list when a good share of values split into several short items."""
    multi = [parts for v in values if len(parts := [p for p in split(v) if p.strip()]) > 1]
    if len(multi) < 0.2 * len(values) or max(len(v) for v in values) > 600:
        return False
    lengths = sorted(len(p.strip()) for parts in multi for p in parts)
    return statistics.median(lengths) <= 30 and lengths[int(0.9 * (len(lengths) - 1))] <= 80


def _split_commas(values: list[str]) -> bool:
    return _looks_like_list(values, lambda v: v.split(","))


def _column_kind(key: str, values: list[str], ptype: str | None) -> str:
    """How a column's cells are stored: list | list_comma | text. Cells keep their text as
    entered (typed properties get a normalised value from app.normalize at write time); list
    types are split into items, and a free-text column (no spec) is inspected for lists."""
    if not values:
        return "text"
    if ptype in ("list", "enum_list"):
        return "list_comma" if _split_commas(values) else "list"
    if ptype in ("url", "contact"):      # several per cell is common: "a@x.io; b@x.io"
        return "list" if _looks_like_list(values, _LIST_SEPARATORS.split) else "text"
    if ptype is not None:
        return "text"
    if _PROSE.search(key):
        return "text"
    if _looks_like_list(values, _LIST_SEPARATORS.split):
        return "list"
    if _split_commas(values):
        return "list_comma"
    return "text"


def _coerce(value: str, kind: str) -> Any:
    if kind == "list":
        return [p for p in _LIST_SEPARATORS.split(value) if p]
    if kind == "list_comma":
        return [p.strip() for p in value.split(",") if p.strip()]
    return value


def header_map(headers: list[str]) -> list[dict]:
    """Map CSV headers to field keys, de-duplicating keys that collide."""
    seen: dict[str, int] = {}
    out = []
    for h in headers:
        key = fields.slugify(h)
        if key == "id":
            out.append({"header": h, "key": "id", "role": "source_id"})
            continue
        if not key or key in fields.SYSTEM_FIELDS:
            key = f"{fields.RESERVED_PREFIX}{key or 'blank'}"
        n = seen.get(key, 0)
        seen[key] = n + 1
        out.append({"header": h, "key": key if n == 0 else f"{key}_{n + 1}", "role": "field"})
    return out


def parse_csv(text: str, filename: str) -> ParsedCsv:
    """Rows and the property keys they use; nothing is written."""
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    headers = next(reader, None)
    if not headers:
        raise ValueError("CSV has no header row")
    cols = header_map(headers)
    raw_rows = [r for r in reader if any(c.strip() for c in r)]

    kinds = {}
    for i, c in enumerate(cols):
        if c["role"] != "field":
            continue
        values = [r[i].strip() for r in raw_rows if i < len(r) and r[i].strip()]
        sp = properties.spec(c["key"])
        kinds[c["key"]] = _column_kind(c["key"], values, sp.type if sp else None)

    parsed = ParsedCsv(file=filename.rsplit("/", 1)[-1], columns=cols, rows=[], total=len(raw_rows))
    for row_no, row in enumerate(raw_rows, start=2):
        doc: dict[str, Any] = {}
        raw_id = None
        for i, c in enumerate(cols):
            value = row[i].strip() if i < len(row) else ""
            if not value:
                continue
            if c["role"] == "source_id":
                raw_id = value
            else:
                doc[c["key"]] = _coerce(value, kinds[c["key"]])
        if "name" not in doc:
            first, last = doc.get("first_name", ""), doc.get("last_name", "")
            if first or last:
                doc["name"] = f"{first} {last}".strip()
        if not doc.get("name"):
            parsed.skipped += 1
            continue
        parsed.rows.append(Row(row_no, doc, raw_id))
    return parsed


def _first_email(values: dict[str, Any]) -> str:
    email = values.get("email") or values.get("emails")
    if isinstance(email, list):
        email = email[0] if email else None
    return str(email or "").strip().lower()


def group_rows(rows: list[Row]) -> list[list[Row]]:
    """The rows of one file that describe the same person, in file order: the same `ID`, else the
    same folded name and first email. One merge item each, so a person's rows land in order and
    never race each other."""
    groups: dict[str, list[Row]] = {}
    for r in rows:
        key = f"id:{r.source_id}" if r.source_id else \
            f"nm:{fields.fold(str(r.values.get('name') or ''))}|{_first_email(r.values)}"
        groups.setdefault(key, []).append(r)
    return list(groups.values())


# --- the plan for one record ----------------------------------------------------------------------

def is_simple(value: Any, ptype: str) -> bool:
    """A value written as entered: anything but prose, and a text up to `SIMPLE_TEXT_CHARS`.
    Longer prose is filed by the storage agent, which can place its facts."""
    if ptype != "text":
        return True
    return len(fields.text_of(value)) <= SIMPLE_TEXT_CHARS


@dataclass
class RowPlan:
    direct: dict[str, Any]              # property → value, written verbatim
    appends: list[str]                  # the list properties among them (items are appended)
    lines: list[tuple[str, str]]        # (key, text) for the storage agent
    unknown: list[str]                  # keys with no property (their lines name the slug)

    def public(self) -> dict[str, Any]:
        return {"direct": sorted(self.direct), "agent": [k for k, _ in self.lines],
                "unknown": list(self.unknown)}


def property_type(key: str, catalog: dict[str, properties.Property]) -> str | None:
    """The type of a known property: registered, or typed in code but not written yet."""
    if key in catalog:
        return catalog[key].type
    sp = properties.spec(key)
    return sp.type if sp else None


def plan_row(values: dict[str, Any], catalog: dict[str, properties.Property], *,
             agent: bool = True) -> RowPlan:
    """Which of a record's values are written verbatim (a known property, not long prose) and
    which go to the storage agent as labelled lines (long prose, keys with no property). Without
    the agent every known value is verbatim and unknown keys are left out."""
    direct: dict[str, Any] = {}
    lines: list[tuple[str, str]] = []
    unknown: list[str] = []
    for key, value in values.items():
        if value in (None, "", []):
            continue
        ptype = property_type(key, catalog)
        if ptype is None:
            unknown.append(key)
            if agent:
                lines.append((key, fields.clean(fields.text_of(value))))
        elif not agent or is_simple(value, ptype):
            direct[key] = value
        else:
            lines.append((key, fields.clean(fields.text_of(value))))
    appends = [k for k in direct if property_type(k, catalog) in LIST_TYPES]
    return RowPlan(direct, appends, lines, unknown)


def render_lines(lines: list[tuple[str, str]]) -> str:
    """The labelled passage the storage agent reads: one fact per line, `key: text`."""
    return "# Values of an incoming record; each line names the property it belongs to.\n" + \
        "\n".join(f"{k}: {v}" for k, v in lines)


# --- matching a record to a person ----------------------------------------------------------------

Decision = Literal["matched", "ambiguous", "new"]
_IDENTIFIERS = frozenset({*IDENTITY_KEYS, "source_id"})


@dataclass
class Match:
    decision: Decision
    person_id: str | None = None
    tier: str | None = None             # identifier | name | name+context
    keys: list[str] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)   # {person_id, name, keys}

    def public(self) -> dict[str, Any]:
        return asdict(self)


def _summary(doc: dict, hits: list[str]) -> dict:
    return {"person_id": str(doc["_id"]), "name": history.current(doc.get("name")), "keys": hits}


def _domain(email: Any) -> str | None:
    first = email[0] if isinstance(email, list) and email else email
    return str(first).rsplit("@", 1)[-1].lower() if isinstance(first, str) and "@" in first else None


def _corroborates(values: dict[str, Any], doc: dict) -> bool:
    """A same-named person is the one when the record agrees on the company, the location or an
    email domain."""
    for key in ("company", "location"):
        mine, theirs = values.get(key), history.current(doc.get(key))
        if isinstance(mine, str) and isinstance(theirs, str) and fields.fold(mine) \
                and fields.fold(mine) == fields.fold(theirs):
            return True
    mine_domain = _domain(values.get("email") or values.get("emails"))
    theirs_domain = _domain(history.current(doc.get("email")) or history.current(doc.get("emails")))
    return bool(mine_domain and mine_domain == theirs_domain)


async def match_record(values: dict[str, Any], source: dict | None = None) -> Match:
    """Who an incoming record is, deterministically: a shared identifier (email, phone, LinkedIn,
    GitHub, the file's ID) decides; else a folded name decides when exactly one person has it or
    exactly one of them is corroborated by company, location or email domain; several plausible
    people are `ambiguous` (listed, nothing decided); nothing is `new`."""
    cands = await people.find_candidates(values, source)
    if not cands:
        return Match("new")
    by_identifier = [(d, h) for d, h in cands if set(h) & _IDENTIFIERS]
    if by_identifier:
        best = max(len(h) for _, h in by_identifier)
        tops = [(d, h) for d, h in by_identifier if len(h) == best]
        if len({str(d["_id"]) for d, _ in tops}) == 1:
            doc, hits = tops[0]
            return Match("matched", str(doc["_id"]), "identifier", hits,
                         [_summary(d, h) for d, h in cands])
        return Match("ambiguous", candidates=[_summary(d, h) for d, h in by_identifier])
    by_name = [(d, h) for d, h in cands if "name" in h]
    if len(by_name) == 1:
        doc, hits = by_name[0]
        return Match("matched", str(doc["_id"]), "name", hits, [_summary(doc, hits)])
    if by_name:
        agreeing = [(d, h) for d, h in by_name if _corroborates(values, d)]
        if len(agreeing) == 1:
            doc, hits = agreeing[0]
            return Match("matched", str(doc["_id"]), "name+context", hits,
                         [_summary(d, h) for d, h in by_name])
        return Match("ambiguous", candidates=[_summary(d, h) for d, h in by_name])
    return Match("new")


# --- the preview a surface shows before a run ---------------------------------------------------------

async def preview(text: str, filename: str = "upload.csv") -> dict:
    """Columns with their plan (identifier | verbatim | agent | new), the row count, and the match
    decisions of the first rows against the database."""
    parsed = parse_csv(text, filename)
    known = await properties.known_keys()
    catalog = {p.key: p for p in await properties.catalog()}
    for col in parsed.columns:
        key = col["key"]
        if col["role"] == "source_id":
            col["plan"], col["new"] = "identifier", False
            continue
        col["new"] = key not in known
        if col["new"]:
            col["plan"] = "new"
            continue
        ptype = property_type(key, catalog) or "text"
        samples = [r.values[key] for r in parsed.rows if key in r.values][:50]
        col["plan"] = "verbatim" if all(is_simple(v, ptype) for v in samples) else "agent"
    matches = []
    for r in parsed.rows[:MATCH_SAMPLE]:
        source = {"file": parsed.file, "source_id": r.source_id} if r.source_id else None
        m = await match_record(r.values, source)
        matches.append({"row": r.row_no, "name": r.values.get("name"), "decision": m.decision,
                        "tier": m.tier, "person_id": m.person_id,
                        "candidates": [c["name"] for c in m.candidates][:3]})
    return {"rows": parsed.total, "skipped": parsed.skipped, "columns": parsed.columns,
            "matches": matches, "groups": len(group_rows(parsed.rows))}
