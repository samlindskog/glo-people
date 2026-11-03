"""Field keys and values: the pure rules every system shares.

A field is a key on a person document. Which fields exist, what type they have and whether they
are embedded is the business of `app.properties`; this module only knows what a valid key looks
like, which keys are reserved, and how a value is rendered as text.
"""

import html
import re
from collections.abc import Callable, Iterable
from typing import Any, Literal

SYSTEM_FIELDS = frozenset({"_id", "id", "tier", "source", "created_at", "updated_at",
                           "schema_version", "rev", "history", "norm", "norm_flags",
                           "history_norm"})
TIERS = ("member", "plus_one", "plus_two")
Tier = Literal["member", "plus_one", "plus_two"]      # the same tuple as a type, for every surface
TIER_LABELS = {"member": "Member", "plus_one": "Plus one", "plus_two": "Plus two"}
# Cell texts that mean "no value" in exported files; an importer treats them as empty cells.
NULL_TOKENS = frozenset({"null", "none", "n/a", "na", "nan", "-", "\u2014", "\u2013"})
# Common export headers (slugified) → the property they mean. The LinkedIn connections export
# says URL / Email Address / Position / Connected On; CRM exports say Title / Phone; a foreign
# database id is the file's ID (the match key, not a property).
HEADER_ALIASES = {
    "url": "linkedin", "linkedin_url": "linkedin", "profile": "linkedin", "profile_url": "linkedin",
    "linkedin_profile": "linkedin",
    "email_address": "email", "e_mail": "email", "mail": "email", "emails": "email",
    "email_addresses": "email",
    "position": "role", "title": "role", "job_title": "role",
    "connected_on": "linkedin_connected",
    "phone": "phone_number", "mobile": "phone_number", "telephone": "phone_number",
    "phones": "phone_number", "phone_numbers": "phone_number",
    "mongo_id": "id", "uuid": "id", "external_id": "id", "record_id": "id",
}
RESERVED_PREFIX = "col_"      # a CSV header that slugifies to a system key becomes col_<key>
MAX_TEXT_CHARS = 40_000       # about 5000 words: text values are not capped below this,
                              # neither when stored nor when an agent reads them

_KEY_OK = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def slugify(header: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")
    if not key:
        return ""
    if key[0].isdigit():
        key = f"f_{key}"
    return key[:64]


def valid_key(key: str) -> bool:
    return bool(_KEY_OK.match(key)) and key not in SYSTEM_FIELDS


def label_for(key: str) -> str:
    if key.startswith(RESERVED_PREFIX) and len(key) > len(RESERVED_PREFIX):
        key = key[len(RESERVED_PREFIX):]
    return key.replace("_", " ").capitalize()


def fold(text: str) -> str:
    """Case-, whitespace- and punctuation-insensitive form of a short text, for matching enum
    values and their aliases: fold("Yes-future events") == fold("yes, future events")."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


_INVISIBLE = str.maketrans({"\u2800": " ", "\u200b": "", "\u00a0": " ", "\ufeff": ""})


def clean(text: Any, *, keep_lines: bool = False) -> str:
    """One line of plain text: HTML entities decoded ("&amp;"), braille blanks and zero-width
    characters (used as spacers on profiles) removed, whitespace collapsed. The storage form of
    any text we did not write ourselves. With `keep_lines` the line breaks stay (a value entered
    as several lines, such as a note) and each line is cleaned on its own."""
    raw = html.unescape(str(text if text is not None else "")).translate(_INVISIBLE)
    if keep_lines:
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line).strip()
    return " ".join(raw.split())


_LOST_BREAK = re.compile(r"(?<=[A-Za-z][.!?])(?=[A-Z][a-z])")


def respace(text: str) -> str:
    """A space restored where a data provider dropped a line break between two sentences: a
    letter, sentence punctuation and a capitalised word run together ("users.Currently at",
    "GSB.What"). Only the unambiguous case: a lower-case word after the period could
    be a domain, so it is left alone, and so is any token that holds an address or a path (an
    email, a URL)."""
    out = []
    for tok in text.split(" "):
        if "@" in tok or "/" in tok or tok.lower().startswith(("http", "www.")):
            out.append(tok)
        else:
            out.append(_LOST_BREAK.sub(" ", tok))
    return " ".join(out)


def repair_text(text: str) -> str:
    """A stored text brought to the form `clean` gives new ones, line breaks kept, plus the
    spacing a provider dropped (`respace`): the mechanical repair of texts stored before the
    cleaning existed or filed from a provider that strips line breaks."""
    return respace(clean(text, keep_lines=True))


def distinct(items: Iterable[Any], key: Callable[[Any], Any] = fold) -> list:
    """The items with duplicates by `key` removed, first occurrence kept, empties dropped."""
    out: list = []
    seen: set = set()
    for item in items:
        if item in (None, ""):
            continue
        k = key(item)
        if k not in seen:
            out.append(item)
            seen.add(k)
    return out


def sentence(text: str) -> str:
    """First letter upper-cased, the rest untouched (str.capitalize lower-cases the rest)."""
    return text[:1].upper() + text[1:] if text else text


def text_of(value: Any) -> str:
    """The string we embed / show for one field value (a history entry's `v`)."""
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v not in (None, ""))
    return "" if value is None else str(value)
