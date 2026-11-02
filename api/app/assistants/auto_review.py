"""Assistant plumbing (level 3): review what people and their agents write, wherever they write it.

A UI edit, a REST PATCH, an MCP update or create: each lands through `people.Transaction` and the
parser normalises the typed values inline, but nobody re-judges the record (the normaliser agent
for an enum text the parser could not map, the repair of a kind or a broken name). Assistants end
their own items with the shared review; human surfaces did not. This observer, registered by
`app.wiring` next to the vector store, collects the people written by human-class sources and,
after a short debounce, starts one `normalize` run over them (parser, normaliser, repair; no
enrichment, no credits). Writes made inside an assistant's item (whatever
their source: the merge's CSV rows, the update assistant writing as its starter, an agent's
verdict) are not collected: those items review themselves, and the review's own writes must
never trigger another review."""

import asyncio
import logging
from typing import Any

from app import people
from app.assistants import normalize as normalize_assistant
from app.assistants import runner
from app.config import get_settings

log = logging.getLogger(__name__)

DELAY_S = 15.0                       # how long to collect writes before one run starts
HUMAN = frozenset({"ui", "api", "mcp"})
LABEL = "auto review"

_pending: dict[str, str | None] = {}            # person id → label
_task: asyncio.Task | None = None


async def after_write(info: people.WriteInfo) -> None:
    """The write observer: collect the person when a human-class source changed a property."""
    if not get_settings().auto_review or info.deleted or info.src is None:
        return
    if runner.CURRENT_ITEM.get() is not None:          # an assistant's item: it reviews itself
        return
    if info.src.kind not in HUMAN or not (set(info.keys) - {"tier"}):
        return
    _pending.setdefault(info.person_id, None)
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_later(), name="auto-review")


async def _later() -> None:
    try:
        await asyncio.sleep(DELAY_S)
    except asyncio.CancelledError:
        return
    await flush()


async def flush() -> dict[str, Any] | None:
    """Start the review run for everyone collected so far (tests call it directly)."""
    global _task
    _task = None
    ids = list(_pending)
    _pending.clear()
    if not ids:
        return None
    try:
        names = {str(d["_id"]): (d.get("name") or {}).get("v")
                 for d in await people.fetch(ids, projection={"name": 1})}
        items = [{"person_id": pid, "payload": {}, "label": names.get(pid) or pid} for pid in ids
                 if pid in names]
        if not items:
            return None
        options = normalize_assistant.NormalizeRunOptions(enrich=False)
        run = await runner.create_run(normalize_assistant.SPEC, None, items,
                                      {"options": options.model_dump(mode="json"), "auto": True},
                                      label=LABEL)
        await runner.start(run["id"], None)
        log.info("auto review run %s over %d people", run["id"], len(items))
        return run
    except Exception:
        log.exception("auto review could not start for %d people", len(ids))
        return None
