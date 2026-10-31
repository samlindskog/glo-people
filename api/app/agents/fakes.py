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


_HEADERS = ("Person", "Current values", "Passage", "Properties", "Question", "Tier", "People",
            "Items")


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


def storage(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """`key: value` lines write to that property (a list type gets the items split on ';',
    appended); `new <words>: value` proposes a property; a value equal to the current one is
    confirmed; other sentences go to notes; lines starting with 'hi' are ignored."""
    prompt = _prompt(messages)
    passage = _section(prompt, "Passage")
    props = dict(re.findall(r"^- ([a-z0-9_]+) \((\w+)", _section(prompt, "Properties"), re.MULTILINE))
    current = dict(re.findall(r"^([a-z0-9_]+): (.*?)(?:  \[|$)", _section(prompt, "Current values"), re.MULTILINE))
    out: dict = {"writes": [], "new_properties": [], "confirmed": [], "ignored": []}
    for line in [ln.strip() for ln in re.split(r"(?<=[^0-9])\.\s+|\n+", passage) if ln.strip()]:
        line = line.rstrip(".")
        if line.lower().startswith("hi") and ":" not in line:
            out["ignored"].append(line)
            continue
        if line.lower().startswith("new ") and ":" in line:
            words, value = (s.strip() for s in line[4:].split(":", 1))
            key = fields.slugify(words)
            if key in props:        # it exists after all: write to it, as the real agent would
                out["writes"].append({"key": key, "value": value, "action": "set",
                                      "conflict": key in current, "evidence": line})
            else:
                out["new_properties"].append({"key": key, "label": words.capitalize(),
                                              "description": f"The person's {words}",
                                              "value": value, "evidence": line})
            continue
        key, value = (s.strip() for s in line.split(":", 1)) if ":" in line else ("notes", line)
        if key not in props:
            key, value = "notes", line
        if current.get(key, "").strip() == value:
            out["confirmed"].append(key)
            continue
        if props[key] in ("list", "enum_list"):
            out["writes"].append({"key": key, "value": [v.strip() for v in value.split(";")],
                                  "action": "append", "evidence": line})
        else:
            out["writes"].append({"key": key, "value": value, "action": "set",
                                  "conflict": key in current, "evidence": line})
    return _out(info, out)


def normalizer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Per item: merge into the first canonical value whose words appear in the text; else, for
    enum types, allocate the text itself when it looks like a label (a capitalised word or two,
    no commas); else non-normalizable (an industry list, a remark)."""
    prompt = _prompt(messages)
    items = []
    for m in re.finditer(r"^- (\w+) \((\w+)(?: values: (.*?))?\): '(.*)'$", prompt, re.MULTILINE):
        field, ptype, values, text = m.groups()
        folded = fields.fold(text)
        choice = None
        for v in (values or "").split(", "):
            if v and fields.fold(v) and fields.fold(v) in folded:
                choice = v
                break
        if choice:
            items.append({"field": field, "value": [choice] if ptype == "enum_list" else choice,
                          "note": "fake match"})
        elif ptype in ("enum", "enum_list") and re.fullmatch(r"[A-Z][\w/&' -]{0,30}", text) \
                and "," not in text:
            items.append({"field": field, "value": [text] if ptype == "enum_list" else text,
                          "note": "fake: new value"})
        else:
            items.append({"field": field, "value": None, "non_normalizable": True,
                          "note": "fake: no match"})
    return _out(info, {"items": items})


def site_reader(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A site is the person's own when the prompt names them in its first lines; no links."""
    prompt = _prompt(messages)
    person = _section(prompt, "Person").split("\n")[0].strip()
    home = prompt.split("# Home page", 1)[-1][:600].lower()
    personal = bool(person) and person.lower() in home
    return _out(info, {"is_personal": personal, "github_urls": [], "related_links": [],
                       "one_liner": None})


def company(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Never resolves a company: low confidence, nothing fetched, nothing written."""
    return _out(info, {"website": None, "summary": None, "confidence": "low",
                       "evidence_url": None})


def repair(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Kind Company for company words in the name, else Person. A name is proposed when the
    current one is broken and the evidence yields a full one: the text before " - ", a
    parenthetical stripped, or an email local part with a dot ("polly.israni" -> Polly Israni).
    name_complete = the resulting name has two tokens. A website value without a dot is
    misfiled: move it to notes."""
    prompt = _prompt(messages)
    name = re.search(r"^Name: (.*)$", prompt, re.MULTILINE).group(1).strip()
    evidence = prompt.split("Evidence:", 1)[-1]
    kind = "Company" if re.search(r"\b(inc|llc|ltd|ventures?|capital|labs?|fund)\b", name,
                                  re.IGNORECASE) else "Person"
    proposed = None
    if " - " in name:
        proposed = name.split(" - ", 1)[0].strip()
    elif "(" in name:
        proposed = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()
    elif "@" in name:
        local = name.split("@", 1)[0]
        if "." in local:
            proposed = " ".join(w.capitalize() for w in local.split("."))
    else:
        m = re.search(r"^email: ([^@\s]+)@", evidence, re.MULTILINE)
        if len(name.split()) < 2 and m and "." in m.group(1):
            proposed = " ".join(w.capitalize() for w in m.group(1).split("."))
    if proposed and (len(proposed.split()) < 2 or fields.fold(proposed) == fields.fold(name)):
        proposed = None
    final = proposed or name
    complete = kind == "Company" or len(final.split()) >= 2
    misfiled = []
    site = re.search(r"^website: (.*)$", evidence, re.MULTILINE)
    if site and not re.search(r"\.[a-z]{2,}(/|$|\s)", site.group(1), re.IGNORECASE):
        misfiled.append({"key": "website", "move_to": "notes"})   # prose, not a URL
    return _out(info, {"kind": kind, "name": proposed, "name_evidence": "fake", "name_complete": complete,
                       "misfiled": misfiled, "confidence": "high", "note": "fake repair"})


FAKES: dict[str, Callable[[list[ModelMessage], AgentInfo], ModelResponse]] = {
    "agents.planner": planner,
    "agents.answer": answer,
    "agents.storage": storage,
    "agents.normalizer": normalizer,
    "agents.site_reader": site_reader,
    "agents.company": company,
    "agents.repair": repair,
}


def model_for(agent: Agent) -> FunctionModel:
    try:
        return FunctionModel(FAKES[agent.name or ""])
    except KeyError as e:
        raise RuntimeError(f"FAKE_AGENTS is on but {agent.name!r} has no fake") from e
