"""Generic CSV → people importer. Any columns are accepted; each header becomes a field key."""

import csv
import hashlib
import io
import re
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pymongo import UpdateOne

from app import fields
from app.mongo import db

_NUMBER = re.compile(r"^-?\$?\d[\d,]*(\.\d+)?$")
_NEVER_NUMERIC = re.compile(r"(^|_)(id|phone|phones|zip|postal|whatsapp)($|_)")
_LIST_SEPARATORS = re.compile(r"\s*(?:\n|;|\s\|\s)\s*")
# Free text that often contains commas/newlines but is never a list.
_PROSE = re.compile(r"(^|_)(bio|bios|note|notes|description|message|messages|summary|liner|"
                    r"comment|comments|about|location|address|city|country|region|name|title|"
                    r"role|company|goals?|preferences)($|_)")
WRITE_BATCH = 1000

csv.field_size_limit(10_000_000)


@dataclass
class ImportResult:
    rows: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    columns: list[dict] = field(default_factory=list)
    new_fields: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)


def _to_number(s: str) -> int | float | None:
    if not _NUMBER.match(s):
        return None
    s = s.replace("$", "").replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() and "." not in s else f


def _looks_like_list(values: list[str], split) -> bool:
    """A column is a list when a good share of values split into several short items."""
    multi = [parts for v in values if len(parts := [p for p in split(v) if p.strip()]) > 1]
    if len(multi) < 0.2 * len(values) or max(len(v) for v in values) > 600:
        return False
    lengths = sorted(len(p.strip()) for parts in multi for p in parts)
    return statistics.median(lengths) <= 30 and lengths[int(0.9 * (len(lengths) - 1))] <= 80


def _split_commas(values: list[str]) -> bool:
    return _looks_like_list(values, lambda v: v.split(","))


def _column_kind(key: str, values: list[str], registered: str | None) -> str:
    """Decide how to coerce a column: number | list | list_comma | text."""
    if not values:
        return "text"
    if registered in ("number", "list", "text", "url", "date"):
        if registered == "list":
            return "list_comma" if _split_commas(values) else "list"
        return "number" if registered == "number" else "text"
    if not _NEVER_NUMERIC.search(key) and \
            sum(_to_number(v) is not None for v in values) >= 0.9 * len(values):
        return "number"
    if _PROSE.search(key):
        return "text"
    if _looks_like_list(values, _LIST_SEPARATORS.split):
        return "list"
    if _split_commas(values):
        return "list_comma"
    return "text"


def _coerce(value: str, kind: str) -> Any:
    if kind == "number":
        n = _to_number(value)
        return value if n is None else n
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
            key = f"col_{key or 'blank'}"
        n = seen.get(key, 0)
        seen[key] = n + 1
        out.append({"header": h, "key": key if n == 0 else f"{key}_{n + 1}", "role": "field"})
    return out


async def preview(text: str) -> dict:
    reader = csv.reader(io.StringIO(text))
    headers = next(reader, [])
    rows = sum(1 for r in reader if any(c.strip() for c in r))
    cols = header_map(headers)
    known = {f["key"] for f in await fields.list_fields()}
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


async def import_csv(text: str, filename: str, tier: str) -> ImportResult:
    if tier not in fields.TIERS:
        raise ValueError(f"tier must be one of {fields.TIERS}")
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    headers = next(reader, None)
    if not headers:
        raise ValueError("CSV has no header row")
    cols = header_map(headers)
    raw_rows = [r for r in reader if any(c.strip() for c in r)]

    registry = {f["key"]: f["type"] for f in await fields.list_fields()}
    kinds = {}
    for i, c in enumerate(cols):
        if c["role"] != "field":
            continue
        values = [r[i].strip() for r in raw_rows if i < len(r) and r[i].strip()]
        kinds[c["key"]] = _column_kind(c["key"], values, registry.get(c["key"]))

    result = ImportResult(columns=cols)
    now = datetime.now(UTC)
    docs: list[tuple[str, dict]] = []
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
            result.skipped += 1
            continue
        docs.append((_source_id(doc, raw_id, row_no), {**doc, "_row": row_no}))
    result.rows = len(raw_rows)

    samples: dict[str, list[Any]] = {}
    for _, d in docs:
        for k, v in d.items():
            if k != "_row":
                samples.setdefault(k, []).append(v)
    result.new_fields = await fields.register(samples)

    source_file = filename.rsplit("/", 1)[-1]
    ops = []
    for source_id, d in docs:
        row_no = d.pop("_row")
        ops.append(UpdateOne(
            {"source.file": source_file, "source.source_id": source_id},
            {"$set": {**d, "tier": tier, "source": {"file": source_file, "source_id": source_id,
                                                     "row": row_no}, "updated_at": now},
             "$setOnInsert": {"created_at": now}},
            upsert=True))
    for start in range(0, len(ops), WRITE_BATCH):
        res = await db().people.bulk_write(ops[start:start + WRITE_BATCH], ordered=False)
        result.inserted += res.upserted_count
        result.updated += res.matched_count

    ids = await db().people.find({"source.file": source_file}, {"_id": 1}).to_list(None)
    result.ids = [str(d["_id"]) for d in ids]
    await fields.refresh_counts()
    return result
