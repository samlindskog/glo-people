"""Normalised values: the typed form of a property's text (app.property_specs decides the type).

`parse` is pure: text → number / boolean / date / canonical enum value(s), or a flag saying why not
(`unmapped`: not recognised, an agent may map it; `non_normalizable`: the text says there is no
value). `document` reads specs and enum sets from app.properties to find what a document still
needs normalised. Nothing here writes: app.people stores the results beside the text (see
app.history, `norm`).
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app import fields, history, properties
from app.property_specs import NORMALISABLE

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


def parse(value: Any, ptype: str, lookup: dict[str, str] | None = None) -> Parsed:
    """The normalised value of one text for a property type. `lookup` (enum types) maps the folded
    canonical values and aliases to canonical values; see properties.enum_lookup."""
    if ptype not in NORMALISABLE:
        raise ValueError(f"type {ptype!r} is not normalisable")
    if value is None or value == [] or value == "":
        return Parsed()
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


async def document(doc: dict, force: bool = False) -> dict[str, Parsed]:
    """What a document still needs normalised: every normalisable property whose current text
    is not the one its last norm entry derives from. A removed value normalises to nothing.
    With `force` (a parser upgrade) the parser's own earlier results are redone too; what an
    agent or a person decided for the current text is always kept."""
    out: dict[str, Parsed] = {}
    for key, prop in doc.items():
        if key in fields.SYSTEM_FIELDS or not history.is_prop(prop):
            continue
        sp = properties.spec(key)
        if sp is None or sp.type not in NORMALISABLE:
            continue
        value = history.current(prop)
        last = history.norm_entries(prop)
        if last and last[-1].get("of") == text_hash(value) and \
                not (force and (last[-1].get("src") or {}).get("kind") == "system"):
            continue
        if value is None:
            if last:
                out[key] = Parsed()
            continue
        lookup = await properties.enum_lookup(key) if sp.type in ("enum", "enum_list") else None
        out[key] = parse(value, sp.type, lookup)
    return out
