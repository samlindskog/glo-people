"""Names: the pure rules for a person's name shared by the tools, the providers and the assistants.

What a full name is, how a lowercase name or title is cased, how mojibake is undone, and the
marks of something that is not a name at all (a handle, an email, a URL, a company suffix, a
tagline). One home, so the repair tool, the data-provider mappings and the fakes agree."""

import re

from app import fields

COMPANY_WORDS = re.compile(r"\b(inc|llc|ltd|ventures?|capital|labs?|studio|partners|holdings|"
                           r"group|technologies|fund|gmbh|corp|company|co)\b", re.IGNORECASE)
HANDLE = re.compile(r"@\w")
URLISH = re.compile(r"https?:|www\.|\.(com|io|ai|co|org|net)\b", re.IGNORECASE)
EMAILISH = re.compile(r"^[^@\s]+@[^@\s]+\.\w+$")
# UTF-8 bytes read as Latin-1 show up as A-tilde / A-circumflex followed by a C1/Latin-1 char.
MOJIBAKE = re.compile(r"[ÃÂ][-¿]")
SYMBOLS = re.compile(r"[^\w\s.'’\-(),&/]", re.UNICODE)
NOT_A_NAME = re.compile(r"[|@/#&_(){}\[\]<>\d]|\bhttp")     # handles, titles, taglines, addresses

SMALL_WORDS = {"of", "and", "the", "at", "in", "for", "on", "de", "da", "du", "von", "van", "der", "y"}
SHORT_WORDS = {"go", "do", "my", "so", "to", "we", "no", "is", "if", "or", "up"}   # not acronyms


def tokens(name: str) -> list[str]:
    return fields.fold(name or "").split()


def is_full_name(name: str) -> bool:
    """A first and a last name: two or more tokens, the last one a word of two or more letters,
    and none of the marks of a handle or tagline ("Polly I.", "Ada O", "Jordan | Cofounder | UXUY"
    and "Lena@Cohere" do not count)."""
    if NOT_A_NAME.search(name or ""):
        return False
    toks = tokens(name)
    return len(toks) >= 2 and len(toks[-1]) >= 2 and not toks[-1].isdigit()


def demojibake(text: str) -> str:
    """UTF-8 that was read as Latin-1 back to what it was (Nin~o -> Niño style damage)."""
    if not MOJIBAKE.search(text):
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return fixed if len(MOJIBAKE.findall(fixed)) < len(MOJIBAKE.findall(text)) else text


def _cased(word: str, first: bool, acronyms: bool) -> str:
    if not first and word in SMALL_WORDS:
        return word
    if acronyms and len(word) <= 2 and word.isalpha() and word not in SHORT_WORDS:
        return word.upper()                          # td, it, hr, ai, vp
    return re.sub(r"(^|[\-'’])(\w)", lambda m: m.group(1) + m.group(2).upper(), word)


def title_case(text: str, *, acronyms: bool = False) -> str:
    """An all-lowercase name or title in Title Case: "elizabeth hsu" -> "Elizabeth Hsu",
    "o'neil mcdonald-smith" -> "O'Neil Mcdonald-Smith", and with `acronyms` (data providers that
    lower-case everything) "it service desk manager at td" -> "IT Service Desk Manager at TD".
    Mixed-case text is left alone: it was cased by someone."""
    if not text or text != text.lower():
        return text
    return " ".join(_cased(w, i == 0, acronyms) for i, w in enumerate(text.split(" ")) if w)


def looks_broken(name: str) -> bool:
    """A name that is a handle, an email, a URL or carries digits: not something to re-case."""
    return bool(HANDLE.search(name) or URLISH.search(name) or re.search(r"\d", name))
