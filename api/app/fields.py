"""Field keys and values: the pure rules every system shares.

A field is a key on a person document. Which fields exist, what type they have and whether they
are embedded is the business of `app.properties`; this module only knows what a valid key looks
like, which keys are reserved, and how a value is rendered as text.
"""

import re
from typing import Any

SYSTEM_FIELDS = frozenset({"_id", "id", "tier", "source", "created_at", "updated_at",
                           "schema_version", "rev", "history", "norm", "norm_flags",
                           "history_norm"})
TIERS = ("member", "plus_one")
RESERVED_PREFIX = "col_"      # a CSV header that slugifies to a system key becomes col_<key>

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


def text_of(value: Any) -> str:
    """The string we embed / show for one field value (a history entry's `v`)."""
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v not in (None, ""))
    return "" if value is None else str(value)
