"""What an assistant is: its spec, the registry, and the contract with the runner.

An assistant composes tools for one item and is run over many items by `app.assistants.runner`.
An item is a person (`scope` "people"), a batch of ids or rows (`scope` "batches", carried in the
item's `payload`) or the single item of a one-shot assistant such as ask (`scope` "single"). The
runner knows nothing about external services: an assistant builds its own dependencies
(`make_deps`), reports spend through `deps.budget.credits`, returns `{"text", "changes", ...}` per
item, and raises `PauseRun` when the run must wait for a human (e.g. the account is out of
credits).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

from pydantic import BaseModel

from app.auth import Principal

if TYPE_CHECKING:
    from app.assistants.context import AssistantDeps


class Budget(Protocol):
    credits: int        # spent so far by this item
    max_credits: int    # the most this item may spend


class PauseRun(Exception):
    """Raised by an assistant for one item when the run as a whole must pause (e.g. the external
    account is out of credits). The item goes back to the queue and the run waits for a resume."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StopRun(PauseRun):
    """Kept for assistants written against the old contract: pauses the run as `account credits`."""

    def __init__(self, message: str) -> None:
        super().__init__("account credits")
        self.message = message


Scope = Literal["people", "batches", "single"]


class ItemSpec(TypedDict, total=False):
    person_id: str | None
    payload: dict[str, Any]
    label: str | None


@dataclass(frozen=True)
class AssistantSpec:
    key: str
    label: str
    description: str
    options_model: type[BaseModel]
    # (run_id, item row incl. `person` when it has one, options, principal or None) -> deps
    make_deps: Callable[[int, dict[str, Any], BaseModel, Principal | None], Awaitable[AssistantDeps]]
    run_item: Callable[[AssistantDeps], Awaitable[dict[str, Any]]]
    item_cap: Callable[[BaseModel], int]     # worst-case credits one item may spend
    scope: Scope = "people"
    permission: str = "people:write"         # what starting a run requires
    details: str = ""                        # the full functionality spec, for the UI's toggle
    selector_field: str | None = None        # people scope: "everyone with this field" bulk selector
    # non-people scopes: the request body -> the items of the run
    prepare: Callable[[dict[str, Any], BaseModel], Awaitable[list[ItemSpec]]] | None = None

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "description": self.description,
                "details": self.details, "scope": self.scope, "permission": self.permission,
                "selector_field": self.selector_field,
                "options_schema": self.options_model.model_json_schema()}


REGISTRY: dict[str, AssistantSpec] = {}


def register(spec: AssistantSpec) -> AssistantSpec:
    REGISTRY[spec.key] = spec
    return spec
