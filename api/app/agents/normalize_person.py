"""Operation: bring one person's normalised values up to date. The parser runs first (free);
whatever it left `unmapped` goes to the normaliser agent in one call, and its decisions are
written with the caller's source so they are never redone by the parser."""

from typing import Any

from pydantic import BaseModel, Field

from app import fields, history, normalize, people, properties
from app.agents import normalizer
from app.auth import Principal
from app.history import Source
from app.normalize import NON_NORMALIZABLE, Parsed


class NormalizeReport(BaseModel):
    parsed: list[str] = Field(default_factory=list, description="Properties the parser settled")
    mapped: dict[str, Any] = Field(default_factory=dict, description="Agent: property → value")
    non_normalizable: list[str] = Field(default_factory=list, description="Agent: no value")
    unmapped: list[str] = Field(default_factory=list, description="Still unmapped afterwards")
    agent_called: bool = False


async def run(person_id: str, principal: Principal | None, source: Source) -> NormalizeReport | None:
    docs = await people.fetch([person_id])
    if not docs:
        return None
    doc = docs[0]
    report = NormalizeReport()
    parsed = await normalize.document(doc)
    if parsed:
        report.parsed = sorted(parsed)
        await people.update_norm(principal, person_id, parsed, people.NORMALIZE_SRC)
        doc = (await people.fetch([person_id]))[0]

    items: list[normalizer.Item] = []
    lookups: dict[str, dict[str, str]] = {}
    for key, prop in doc.items():
        if key in fields.SYSTEM_FIELDS or not history.is_prop(prop):
            continue
        value = history.current(prop)
        if value is None or history.norm_flag(prop) != "unmapped":
            continue
        sp = properties.spec(key)
        if sp is None:
            continue
        values = await properties.enum_values(key)
        if values:
            lookups[key] = await properties.enum_lookup(key)
        items.append(normalizer.Item(field=key, text=fields.text_of(value), type=sp.type,
                                     values=values))
    if not items:
        return report

    report.agent_called = True
    result = await normalizer.run(items, lookups)
    decided: dict[str, Parsed] = {}
    for m in result.items:
        item = next((i for i in items if i.field == m.field), None)
        if item is None:
            continue
        if m.non_normalizable or m.value is None:
            decided[m.field] = Parsed(None, NON_NORMALIZABLE)
            report.non_normalizable.append(m.field)
        else:
            parsed_value = normalize.parse(m.value, item.type, lookups.get(m.field))
            decided[m.field] = parsed_value
            report.mapped[m.field] = history.jsonable(parsed_value.value)
    report.unmapped = sorted(i.field for i in items if i.field not in decided)
    if decided:
        await people.update_norm(principal, person_id, decided, source)
    return report
