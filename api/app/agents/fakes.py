"""Deterministic stand-ins for every agent (FAKE_AGENTS=1): no model calls, predictable outputs
that exercise the same validators and operations. Used by end-to-end tests and offline demos."""

import re
from collections.abc import Callable

from pydantic_ai import Agent, ModelMessage, ModelResponse
from pydantic_ai.messages import (
    ModelRequest,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app import fields


def _prompt(messages: list[ModelMessage]) -> str:
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart):
                    return str(p.content)
    return ""


def _retried(messages: list[ModelMessage]) -> bool:
    """True once a validator has sent an output back (pydantic-ai returns it as a retry prompt or
    as the output tool's return part)."""
    return any(isinstance(p, RetryPromptPart | ToolReturnPart)
               for m in messages if isinstance(m, ModelRequest) for p in m.parts)


def _out(info: AgentInfo, data: dict) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, data)])


_HEADERS = ("Person", "Passage", "Properties", "Question", "Tier", "People", "Items")


def _section(prompt: str, name: str) -> str:
    """The text under one prompt header (up to the next known header)."""
    stop = "|".join(_HEADERS)
    m = re.search(rf"^{re.escape(name)}:\s*(.*?)(?=^(?:{stop}):|\Z)", prompt, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def planner(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A passage on `bio` with the question's words; `tier`/number hints become filters:
    "mrr over 10000" → mrr gte 10000, "stage seed" → stage eq Seed (first canonical match)."""
    prompt = _prompt(messages)
    question = _section(prompt, "Question")
    plan: dict = {"semantic": [{"field": "bio", "query": question or "person"}], "filters": [],
                  "rationale": "fake planner"}
    m = re.search(r"\b(mrr|raised_funds|messages)\s+(over|above|at least|>=?)\s+\$?(\d[\d,]*)",
                  question, re.IGNORECASE)
    if m:
        plan["filters"].append({"field": m.group(1).lower(), "op": "gte",
                                "value": int(m.group(3).replace(",", ""))})
    m = re.search(r"\bstage\s+(?:is\s+|=\s*)?([\w -]+?)(?:[.,;]|$)", question, re.IGNORECASE)
    if m:
        plan["filters"].append({"field": "stage", "op": "eq", "value": m.group(1).strip()})
    return _out(info, plan)


def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    prompt = _prompt(messages)
    people = [(pid, name) for name, pid in re.findall(r"^### (.+?) \(id ([0-9a-f]{24})", prompt, re.MULTILINE)]
    if not people:
        return _out(info, {"text": "No one in the data matches.", "citations": []})
    names = ", ".join(n for _, n in people)
    return _out(info, {"text": f"{len(people)} people match: {names}.",
                       "citations": [{"person_id": pid, "field": "bio", "value": n}
                                     for pid, n in people[:5]]})


def extract(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """`tag: text` lines become items (an existing key named by the tag is picked up); other
    sentences land on `notes`."""
    prompt = _prompt(messages)
    passage = _section(prompt, "Passage")
    keys = set(re.findall(r"^- ([a-z0-9_]+) \(", _section(prompt, "Properties"), re.MULTILINE))
    items = []
    for line in [ln.strip() for ln in re.split(r"[\n.]+", passage) if ln.strip()]:
        if ":" in line:
            tag, text = (s.strip() for s in line.split(":", 1))
            key = fields.slugify(tag)
            items.append({"tag": tag, "text": text or tag,
                          "existing_key": key if key in keys else None})
        else:
            items.append({"tag": "notes", "text": line, "existing_key": "notes" if "notes" in keys
                          else None})
    return _out(info, {"items": items[:8]})


def normalizer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Picks the first canonical value whose words appear in the text; else non-normalizable."""
    prompt = _prompt(messages)
    items = []
    for m in re.finditer(r"^- (\w+) \((\w+)(?: values: (.*?))?\): '(.*)'$", prompt, re.MULTILINE):
        field, _ptype, values, text = m.groups()
        folded = fields.fold(text)
        choice = None
        for v in (values or "").split(", "):
            if v and fields.fold(v) and fields.fold(v) in folded:
                choice = v
                break
        if choice:
            items.append({"field": field, "value": choice, "note": "fake match"})
        else:
            items.append({"field": field, "value": None, "non_normalizable": True,
                          "note": "fake: no match"})
    return _out(info, {"items": items})


FAKES: dict[str, Callable[[list[ModelMessage], AgentInfo], ModelResponse]] = {
    "agents.planner": planner,
    "agents.answer": answer,
    "agents.extract": extract,
    "agents.normalizer": normalizer,
}


def model_for(agent: Agent) -> FunctionModel:
    try:
        return FunctionModel(FAKES[agent.name or ""])
    except KeyError as e:
        raise RuntimeError(f"FAKE_AGENTS is on but {agent.name!r} has no fake") from e
