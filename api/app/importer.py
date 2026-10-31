"""Generic CSV parsing for the import assistant (level 1). Any columns are accepted; each header
becomes a property key. Parsing and coercion live here; the import assistant creates the new
properties (`properties.ensure`) and writes the rows (`people.upsert_imported`) in batches."""

import csv
import hashlib
import io
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from app import fields, properties

_LIST_SEPARATORS = re.compile(r"\s*(?:\n|;|\s\|\s)\s*")
# Free text that often contains commas/newlines but is never a list.
_PROSE = re.compile(r"(^|_)(bio|bios|note|notes|description|message|messages|summary|liner|"
                    r"comment|comments|about|location|address|city|country|region|name|title|"
                    r"role|company|goals?|preferences)($|_)")

csv.field_size_limit(10_000_000)


@dataclass
class ParsedCsv:
    """A CSV read into rows ready for `people.upsert_imported`: (source_id, row_no, values)."""

    file: str
    columns: list[dict]
    rows: list[tuple[str, int, dict[str, Any]]]
    total: int = 0                 # rows with content
    skipped: int = 0               # rows without a name
    used_keys: list[str] = field(default_factory=list)


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


async def preview(text: str) -> dict:
    reader = csv.reader(io.StringIO(text))
    headers = next(reader, [])
    rows = sum(1 for r in reader if any(c.strip() for c in r))
    cols = header_map(headers)
    known = await properties.known_keys()
    for c in cols:
        c["new"] = c["role"] == "field" and c["key"] not in known
    return {"rows": rows, "columns": cols}


def _source_id(doc: dict, raw_id: str | None, row_no: int) -> str:
    if raw_id:
        return raw_id
    email = doc.get("email") or doc.get("emails")
    if isinstance(email, list):
        email = email[0] if email else None
    basis = f"{str(doc.get('name', '')).strip().lower()}|{str(email or '').strip().lower()}"
    if basis == "|":
        basis = f"row:{row_no}"
    return hashlib.sha1(basis.encode()).hexdigest()


def parse_csv(text: str, filename: str) -> ParsedCsv:
    """Rows and the property keys they use; nothing is written."""
    text = text.lstrip("\ufeff")
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
        parsed.rows.append((_source_id(doc, raw_id, row_no), row_no, doc))
    parsed.used_keys = sorted({k for _, _, d in parsed.rows for k in d})
    return parsed
