"""Normalised values: the typed form of a property's text (app.property_specs decides the type).

Pure (level 0): text → number / boolean / date / canonical enum value(s), or a flag saying why
not (`unmapped`: not recognised, an agent may map it; `non_normalizable`: the text says there is
no value). What a document still needs normalised is decided by `people.pending_normalization`;
the tool that brings a person up to date (parser first, then the normaliser agent) is
`app.tools.normalize`. Nothing here writes.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app import fields, urls
from app.property_specs import CANONICAL, NORMALISABLE

UNMAPPED = "unmapped"
NON_NORMALIZABLE = "non_normalizable"


@dataclass(frozen=True)
class Parsed:
    value: Any = None
    flag: str | None = None


# Texts that say "there is no value" (after fields.fold); anything else unrecognised is unmapped.
_MARKERS = frozenset({"", "n a", "na", "none", "null", "nil", "tbd", "tba", "unknown",
                      "not applicable", "not available", "does not apply", "undisclosed",
                      "not disclosed", "private", "confidential"})
_MARKER_PREFIXES = ("prefer not", "prefer to not", "do not disclose", "don t disclose")
# Phrases that mean "zero" for an amount (revenue not started, nothing raised).
_ZERO = frozenset({"0", "pre revenue", "pre rev", "prerevenue", "pre product", "pre funding",
                   "prefunding", "bootstrap", "bootstrapped", "bootstrapping", "self funded",
                   "selffunded", "no revenue", "none yet", "nothing yet", "not yet",
                   "yet to raise", "no funding"})
_TRUE = frozenset({"yes", "y", "true", "t", "1", "yep", "yeah"})
_FALSE = frozenset({"no", "n", "false", "f", "0", "nope", "nah"})
_MULTIPLIER = {"k": 1e3, "m": 1e6, "mm": 1e6, "mn": 1e6, "mil": 1e6, "million": 1e6,
               "b": 1e9, "bn": 1e9, "billion": 1e9}
_UNIT = r"(?:k|mm|mn|mil|million|m|bn|billion|b)"
_AMOUNT = r"(?:usd|us\$|[$€£])?\s*(\d[\d,]*(?:\.\d+)?|\.\d+)\s*(" + _UNIT + r")?\s*\$?"
_NUMBER = re.compile(r"^[<>~≈]?\s*" + _AMOUNT + r"\s*\+?$", re.IGNORECASE)
# "$1k-$5k", "100-500k", "$250k - 1m", "1 to 5m": the unit of the second amount applies to a
# bare first amount; a range normalises to its lower bound ("at least").
_RANGE = re.compile(r"^" + _AMOUNT + r"\s*(?:-|–|—|to)\s*" + _AMOUNT + r"\s*\+?$", re.IGNORECASE)
_TRAILING_UNIT = re.compile(r"\s*(?:in\s+)?(?:mrr|mmr|arr|usd|dollars|per\s+month|/\s*mo(?:nth)?)\s*$",
                            re.IGNORECASE)
_DATE_FORMATS = ("%Y-%m", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y",
                 "%B %d %Y", "%b %Y", "%B %Y", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%Y/%m/%d",
                 "%Y")


def text_hash(value: Any) -> str:
    """Identifies the text a normalised value was derived from (`of` on norm entries)."""
    return hashlib.sha256(fields.text_of(value).encode()).hexdigest()[:16]


def _amount(digits: str, unit: str | None) -> float:
    n = float(digits.replace(",", ""))
    return n * _MULTIPLIER[unit.lower()] if unit else n


def _is_marker(folded: str) -> bool:
    return folded in _MARKERS or folded.startswith(_MARKER_PREFIXES)


def parse_number(text: str) -> int | float | None:
    """An amount: "$12,000", "200k", "1.5M+", "5million", ">200k", "$1k-$5k MRR" (a range is its
    lower bound), "pre-revenue" / "bootstrapped" (zero). None when the text is not an amount."""
    s = _TRAILING_UNIT.sub("", text.strip())
    if fields.fold(s) in _ZERO:
        return 0
    m = _NUMBER.match(s)
    if m:
        n = _amount(m.group(1), m.group(2))
    else:
        r = _RANGE.match(s)
        if not r:
            return None
        n = _amount(r.group(1), r.group(2) or r.group(4))
    return int(n) if n.is_integer() else n


def parse_date(text: str) -> datetime | None:
    s = text.strip()
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        d = None
        for fmt in _DATE_FORMATS:
            try:
                d = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _contact_key(text: str) -> str | None:
    """An email lower-cased, a phone as its digits with a leading +, else None."""
    s = text.strip()
    if s.lower().startswith("mailto:"):
        s = s[7:]
    if "@" in s:
        return s.lower() if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s) else None
    digits = re.sub(r"\D", "", s)
    return f"+{digits}" if len(digits) >= 7 and re.fullmatch(r"[\d\s()+.\-]+", s) else None


def canonical(value: Any, ptype: str) -> Parsed:
    """The lookup key of an identity-like value: keyword → `fields.fold`, url → `urls.url_key`,
    contact → the email lower-cased or the phone digits. A list gives a list of keys. Text that
    is not such a value is `non_normalizable` (never a question for an agent)."""
    items = value if isinstance(value, list) else [value]
    keys: list[str] = []
    for item in items:
        text = fields.text_of(item).strip()
        if ptype == "keyword":
            key = fields.fold(text) or None
        elif ptype == "url":
            normalised = urls.normalize_url(text)
            key = urls.url_key(normalised) if normalised else None
        else:
            key = _contact_key(text)
        if key:
            keys.append(key)
    keys = list(dict.fromkeys(keys))
    if not keys:
        return Parsed(None, NON_NORMALIZABLE)
    return Parsed(keys if isinstance(value, list) else keys[0])


def parse(value: Any, ptype: str, lookup: dict[str, str] | None = None) -> Parsed:
    """The normalised value of one text for a property type. `lookup` (enum types) maps the folded
    canonical values and aliases to canonical values; see properties.enum_lookup."""
    if ptype not in NORMALISABLE:
        raise ValueError(f"type {ptype!r} is not normalisable")
    if value is None or value == [] or value == "":
        return Parsed()
    if ptype in CANONICAL:
        return canonical(value, ptype)
    lookup = lookup or {}
    if ptype == "enum_list":
        items = value if isinstance(value, list) else [value]
        mapped, missing = [], []
        for item in items:
            canon = lookup.get(fields.fold(str(item)))
            if canon:
                mapped.append(canon)
            elif not _is_marker(fields.fold(str(item))):
                missing.append(item)
        mapped = list(dict.fromkeys(mapped))
        if missing:
            return Parsed(mapped or None, UNMAPPED)
        return Parsed(mapped) if mapped else Parsed(None, NON_NORMALIZABLE)
    if isinstance(value, list):
        if len(value) != 1:
            return Parsed(None, UNMAPPED)
        value = value[0]
    text = fields.text_of(value).strip()
    folded = fields.fold(text)
    if ptype == "enum":
        canon = lookup.get(folded)
        if canon:
            return Parsed(canon)
        if folded in _TRUE and "yes" in lookup:      # yes/no enums take the boolean spellings
            return Parsed(lookup["yes"])
        if folded in _FALSE and "no" in lookup:
            return Parsed(lookup["no"])
    elif ptype == "number":
        if isinstance(value, int | float) and not isinstance(value, bool):
            return Parsed(value)
        n = parse_number(text)
        if n is not None:
            return Parsed(n)
    elif ptype == "boolean":
        if isinstance(value, bool):
            return Parsed(value)
        if folded in _TRUE:
            return Parsed(True)
        if folded in _FALSE:
            return Parsed(False)
    elif ptype == "date":
        if isinstance(value, datetime):
            return Parsed(value if value.tzinfo else value.replace(tzinfo=UTC))
        d = parse_date(text)
        if d is not None:
            return Parsed(d)
    return Parsed(None, NON_NORMALIZABLE if _is_marker(folded) else UNMAPPED)
