"""Authored HTML for the public boards (glojobs.app): what a job description, a talent profile
or a board intro may contain. Level 0: a pure function over text.

The allowed vocabulary is what the site's stylesheet styles inside `.prose`: headings from h2
down, paragraphs, lists, emphasis, links, quotes, rules and simple tables. Everything else is
dropped (a `<script>` or `<style>` with its content, any other tag keeping its text), inline
event handlers and styles with it. Links keep only http(s) and mailto targets and get
`rel="noopener noreferrer"`."""

import html as html_mod
import re
from dataclasses import dataclass, field

import nh3

TAGS: frozenset[str] = frozenset({
    "h2", "h3", "h4", "p", "ul", "ol", "li", "strong", "em", "b", "i", "u", "s", "a", "br",
    "blockquote", "hr", "table", "thead", "tbody", "tr", "th", "td", "code", "pre",
})
ATTRIBUTES: dict[str, set[str]] = {"a": {"href", "title"}, "th": {"colspan"}, "td": {"colspan"}}
URL_SCHEMES: set[str] = {"http", "https", "mailto"}
MAX_HTML_CHARS = 200_000

_TAG = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9-]*)")


@dataclass
class Cleaned:
    html: str
    stripped: list[str] = field(default_factory=list)   # tag names the input used and lost


def clean(html: str | None) -> Cleaned:
    """The sanitised HTML and the tags that did not survive, so an author (a person or an
    agent) sees what was dropped. An empty or whitespace input is an empty string."""
    text = (html or "").strip()
    if not text:
        return Cleaned("")
    if len(text) > MAX_HTML_CHARS:
        raise ValueError(f"html longer than {MAX_HTML_CHARS} characters")
    used = {m.group(1).lower() for m in _TAG.finditer(text)}
    out = nh3.clean(text, tags=set(TAGS), attributes=ATTRIBUTES, url_schemes=URL_SCHEMES,
                    link_rel="noopener noreferrer", strip_comments=True)
    return Cleaned(out.strip(), sorted(used - TAGS))


def escape(text: str) -> str:
    """Plain text as HTML text."""
    return html_mod.escape(text, quote=True)


def text_of(html: str | None) -> str:
    """The text content of a fragment, for search indexes and previews."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()
