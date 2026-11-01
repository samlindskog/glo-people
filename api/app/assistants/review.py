"""The closing step every per-person assistant shares: one normalize-tool call with repair on.
The parser confirms the typed values the item wrote, the normaliser agent is asked about what it
cannot read, and the repair tool judges the record as it now stands (kind by common sense, a name
the record's own data states, misfiled values). One shape comes back for the item result, and one
line for a human. Entries are attributed to the agent that made them, under the assistant's name
(`enrich/normalizer`, `normalize/repair`, …)."""

from typing import Any

from app.assistants.context import AssistantDeps
from app.history import Change
from app.tools import normalize as normalize_tool
from app.tools.normalize import NormalizeOptions


async def review(deps: AssistantDeps, *, repair: bool = True, force: bool = False,
                 allocate: bool = True, max_new_values: int = 3) -> dict[str, Any] | None:
    """Normalise and repair the item's record; None when the person no longer exists."""
    opts = NormalizeOptions(allocate=allocate, max_new_values=max_new_values, repair=repair,
                            force=force)
    res = await deps.tool(normalize_tool.normalize, normalize_tool.AGENT_NAME, opts=opts)
    if res is None:
        return None
    assert isinstance(res, normalize_tool.NormalizeResult)
    rep = res.repair
    name = next((c.value for c in res.changes if c.key == "name" and c.action == "set"), None) \
        if rep else None
    return {"kind": rep.kind if rep else None, "name": name,
            "name_source": rep.name_source if rep else None,
            "mechanical": list(rep.mechanical) if rep else [],
            "note": rep.note if rep else "", "rejected": list(rep.rejected) if rep else [],
            "repaired": rep is not None,
            "unmapped": list(res.unmapped), "merged": dict(res.merged),
            "allocated": dict(res.allocated), "rejected_values": dict(res.rejected),
            "agent_called": bool(res.agent_called or (rep and rep.agent_called))}


def describe(rev: dict[str, Any] | None, changes: list[Change]) -> str:
    """One line for a human: what the parser, the normaliser and the repair settled."""
    parts = []
    for c in changes:
        if c.key == "record_kind" and c.action != "enum_value_added":
            continue                                   # summarised as "record kind: …" below
        if c.action == "normalized":
            parts.append(f"{c.key} \u2192 {c.value}")
        elif c.action == "flagged":
            parts.append(f"{c.key}: {'no value' if c.flag == 'non_normalizable' else 'not recognised'}")
        elif c.action == "enum_value_added":
            parts.append(f"{c.key}: new value {c.value!r}")
        elif c.action == "set" and c.key == "name":
            parts.append(f"name \u2192 {c.value}")
        elif c.action == "removed":
            parts.append(f"{c.key} removed")
        elif c.action == "note":
            parts.append(f"{c.key}: {c.value}")
    rev = rev or {}
    for key, values in (rev.get("rejected_values") or {}).items():
        parts.append(f"{key}: {', '.join(repr(v) for v in values)} not added (cap)")
    for key in rev.get("unmapped") or []:
        parts.append(f"{key}: still unmapped")
    if rev.get("kind"):
        parts.append(f"record kind: {rev['kind']}")
    text = "; ".join(parts) if parts else "Everything was already normalised"
    if rev.get("agent_called"):
        text += " (agent consulted)"
    return text + "."
