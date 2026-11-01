"""The passage the enrich assistant hands to the storage agent: labelled lines, one per property
key, in a section per source, so the real model and the deterministic fake file it the same way
and the storage validator's rules hold by construction (every evidence fragment is a line of the
passage, no key appears twice). The rendered passage is also what the record keeps as
`enrichment_passage`: the audit trail of what was filed, next to the facts it produced."""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from app import fields

HEADER = "# Facts from data providers; each line names the property it belongs to."
Value = str | list[str]


def _clean(value: Value) -> Value:
    if isinstance(value, list):
        out: list[str] = []
        seen: set[str] = set()
        for item in (" ".join(str(v).split()) for v in value):
            if item and fields.fold(item) not in seen:
                out.append(item)
                seen.add(fields.fold(item))
        return out
    return " ".join(str(value).split())


def _text(value: Value) -> str:
    return "; ".join(value) if isinstance(value, list) else value


@dataclass
class Passage:
    sections: list[tuple[str, dict[str, Value]]] = field(default_factory=list)  # (title, lines)
    sources: dict[str, list[str]] = field(default_factory=dict)   # key → the sources stating it
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (key, source): a second text

    def __bool__(self) -> bool:
        return bool(self.sources)

    @property
    def keys(self) -> list[str]:
        return list(self.sources)

    def add(self, source: str, title: str, lines: Iterable[tuple[str, Value]]) -> None:
        """One section of facts from `source`. A key seen before keeps its first line: list
        values are unioned into it (fold-deduped), a second text value is dropped and reported."""
        section: dict[str, Value] = {}
        for key, raw in lines:
            value = _clean(raw)
            if not value:
                continue
            if key in self.sources:
                lines_of = next(ls for _, ls in self.sections if key in ls)
                if isinstance(lines_of[key], list) and isinstance(value, list):
                    lines_of[key] = _clean([*lines_of[key], *value])
                    self.sources[key].append(source)
                else:
                    self.dropped.append((key, source))
                continue
            section[key] = value
            self.sources[key] = [source]
        if section:
            self.sections.append((f"{title} ({source})", section))

    def source_of(self, key: str) -> str:
        return ", ".join(self.sources.get(key, []))

    def render(self) -> str:
        """The passage text, at most `fields.MAX_TEXT_CHARS` (clipped at a line end)."""
        out = [HEADER]
        for title, lines in self.sections:
            out.append(f"# {title}")
            out += [f"{key}: {_text(value)}" for key, value in lines.items()]
        text = "\n".join(out)
        if len(text) > fields.MAX_TEXT_CHARS:
            text = text[:fields.MAX_TEXT_CHARS].rsplit("\n", 1)[0]
        return text

    def digest(self) -> str:
        return hashlib.sha256(self.render().encode()).hexdigest()[:16]
