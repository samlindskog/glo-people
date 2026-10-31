"""Assistant (level 3): bring the passage vectors in line with the records, for everyone (or the
chosen people), in batches; the property-name vectors first. The level-1 API it drives is
`embeddings.sync_people`."""

from typing import Any

from pydantic import BaseModel, Field

from app import embeddings, people, properties
from app.assistants.base import AssistantSpec, ItemSpec, register
from app.assistants.context import AssistantDeps
from app.auth import Principal
from app.integrations.firecrawl import Budget

ASSISTANT = "embed"


class EmbedOptions(BaseModel):
    fields: list[str] | None = Field(None, description="Only these properties (default: every "
                                                       "embeddable one)")
    batch: int = Field(200, ge=1, le=2000, description="People per item")


def batches(ids: list[str], size: int, what: str = "people") -> list[ItemSpec]:
    return [{"person_id": None, "payload": {"ids": ids[i:i + size]},
             "label": f"{what} {i + 1}–{min(i + size, len(ids))}"}
            for i in range(0, len(ids), size)]


async def all_or_chosen(body: dict[str, Any]) -> list[str]:
    ids = list(dict.fromkeys(body.get("person_ids") or [])) or await people.find_ids({})
    if not ids:
        raise ValueError("no people to run over")
    return ids


async def prepare(body: dict[str, Any], options: EmbedOptions) -> list[ItemSpec]:
    ids = await all_or_chosen(body)
    await properties.sync_specs()
    await properties.sync_names()
    return batches(ids, options.batch)


async def make_deps(run_id: int, item: dict, options: EmbedOptions,
                    principal: Principal | None) -> AssistantDeps:
    return AssistantDeps(assistant=ASSISTANT, run_id=run_id, item_id=item["id"],
                         payload=item["payload"], label=item.get("label"), principal=principal,
                         budget=Budget(max_scrapes=0, max_searches=0, max_credits=0),
                         options=options)


async def run_item(deps: AssistantDeps) -> dict[str, Any]:
    ids = deps.payload["ids"]
    only = set(deps.options.fields) if deps.options.fields else None
    n = await embeddings.sync_people(ids, only)
    return {"text": f"{n} vectors for {len(ids)} people.", "changes": [], "vectors": n}


SPEC = register(AssistantSpec(
    key=ASSISTANT,
    label="Embed passages",
    description=("Computes the missing passage vectors (and removes stale ones) for every person, "
                 "in batches, after syncing the property-name vectors. Free; the OpenAI embedder "
                 "is the only external call."),
    options_model=EmbedOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=lambda options: 0,
    scope="batches",
    prepare=prepare,
))
