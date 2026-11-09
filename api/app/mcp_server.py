"""Public MCP server (streamable HTTP at /mcp), authenticated with per-account API keys."""

import json
import logging
import time
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from pydantic_ai.exceptions import AgentRunError
from starlette.types import ASGIApp, Receive, Scope, Send

from app import agent_log, auth, boards, fields, importer, ops, properties
from app import people as people_svc
from app.assistants import REGISTRY, credits, merge, runner
from app.assistants.ask import ask
from app.assistants.enrich import budget as enrich_budget
from app.assistants.enrich.models import EnrichOptions
from app.assistants.merge import MergeOptions
from app.auth import Principal
from app.config import get_settings
from app.history import Source
from app.tools import invoke
from app.tools import normalize as normalize_tool
from app.tools import store as store_tool
from app.tools.normalize import NormalizeOptions
from app.tools.plan import Tuning
from app.tools.plan import plan_search as plan_search_tool
from app.tools.search import (
    MAX_LIMIT,
    FieldFilter,
    SearchRequest,
    SemanticQuery,
    SortSpec,
    record_search,
    run_search,
)
from app.tools.store import StoreOptions

log = logging.getLogger(__name__)

WRITE_TOOLS = frozenset({"create_person", "merge_person", "merge_people", "enrich_person", "update_person", "delete_person",
                         "store_about_person", "normalize_person", "start_assistant", "pause_run",
                         "resume_run", "stop_run",
                         "upsert_board_company", "upsert_board_job", "delete_board_job",
                         "upsert_board_talent", "delete_board_talent", "create_board",
                         "update_board", "delete_board"})

INSTRUCTIONS = """\
The GLO people database: people in tiers ('member', 'plus_one', 'plus_two'), each a document of
properties whose values are never overwritten (every write appends a time-stamped, sourced entry to
the property's history). You act for a person who is not technical: do the lookups, previews and
follow-ups yourself and report in plain words.

Playbook
- Finding someone: find_people first (an email, phone, LinkedIn or GitHub URL, or a name: exact,
  canonical, deterministic; it says matched / ambiguous / nobody). search_people is for descriptions
  ("fintech founders in Lagos"): literal per-property filters (AND) plus semantic similarity over
  embeddable properties (cosine; default threshold 0.4, 0.35-0.5 is the useful range): long
  texts (bio, about, company_description, enrichment_passage = the whole provider profile) and
  short values most people have (role, company, industry, location); fields ["*"] with mode
  max compares one passage with all of them. `tier` takes one tier or a list, so one call covers
  members and plus-ones together; a semantic result carries `at_or_above`, how many of the hits
  reach a few tighter thresholds, so a wide result is narrowed by raising the threshold once rather
  than by paging. plan_search writes such a request from a question. Call list_properties once to
  see the properties, their types and canonical values. get_person reads one record; pass
  include_history=false unless you need the entries. get_people reads several records (up to 50
  ids) in one call: after a search, read the people you want to look at with it, not one by one.
- Adding information: one record → merge_person (matches a person by identifier or unique name, else
  creates one; known values are written as entered, long prose and unknown keys go to the storage
  agent; use dry_run first when unsure); several records → start_assistant("merge", records=[…]).
  Large CSV files are uploaded on the web page or with the CLI, not through this interface. A free
  text about a known person → store_about_person.
- Enriching: one person → enrich_person (LinkedIn profile through the data providers, their site,
  GitHub, the company; costs credits, capped by options.max_credits, 30 by default). Many people:
  plan_run("enrich", …) first — it says how many are eligible, how many are skipped as already
  enriched, the recent cost per person, the estimated credits, tokens and minutes, the balance
  (get_credits) and a recommended max_credits — tell the person, then start_assistant("enrich",
  …, options={"max_credits": <cap>}). A run pauses when its spend reaches the cap; resume_run with
  a higher one. The runner reserves the worst case per item in flight (reserved_per_item, ~16) and
  only dispatches while spend + reservations fit the cap, so use at least minimum_cap; `reserved`
  on get_credits and get_run is that worst case, not spend — judge cost by credits_per_done.
  Options for enrich: max_credits, and refresh when the person wants a recent enrichment redone.
  Never set providers yourself: the default (FullEnrich, 5 credits a match) is the right one;
  People Data Labs bills 60 a match and is only for when the person asks for it by name. plan_run,
  start_assistant and get_run show `cost.providers` with the price of each and `warnings` when a
  dear one is in; get_credits lists every provider's price. A paused run keeps the options it was
  started with (params.options on get_run); resume_run(options={…}) changes them.
- Long runs: do not wait inside the call. Note the run id, poll get_run every few minutes and report
  progress.percent, items_per_min, eta_min and credits_used; list_runs shows what is active.
  pause_run / stop_run if the person asks. When it finishes, summarise the items by status.
- Following up on an upload: the import answers with a run id; start_assistant(key, from_run=<that
  id>) runs another assistant over exactly the people it created or matched: "normalize" (typed values,
  record kind, broken names; free), "enrich" (data providers and sites; costs credits — always pass
  options.max_credits), "update" (the same values or tier on all of them, e.g. options={"tier":
  "plus_two"}; dry_run first). where=[filters] selects by properties instead; person_ids by id.
  wait_seconds (at most 50) lets a small run finish inside the call; otherwise follow it with
  get_run. No call blocks longer than 50 s: merge_person and start_assistant hand back the run
  (status running, run_id) and get_run finishes the story.
- Fixing a value: update_person appends the right value (null removes); to restore an earlier value,
  write it again — the history keeps every version with its source. Which entry is shown follows
  the trust of its source: a person's own edit (ui, api, mcp — yours) outranks an assistant's
  finding (agent), which outranks a bulk upload (import); newest wins within a class, and an entry
  marked `derived` (a fix or completion of the shown value) supersedes it. A write that does not
  outrank the shown value is recorded and reported as `kept`; get_person marks the shown entry
  (`shown`, `why`). normalize_person re-parses one record and repairs its identity data.
- Before any bulk write, dry_run; after one, read the run's items (get_run) and summarise counts.
- Boards (glojobs.app): a board is one person's private page — reachable only by its UUID link —
  listing open roles, people, or both, drawn from a shared catalogue. Call boards_guide once
  before touching them: it explains the catalogue-first order, how every talent entry and board
  is linked to a glo-people person (person_id, or a record that is matched before anything is
  created), the URLs (every board and item carries `url`), and what HTML survives.
Pass `agent` when you write on behalf of another automated agent: the entry stays attributed to your
key (kind "mcp") with that name in its reference."""


class GloMCP(MCPServer):
    """Hides write tools from keys whose owner lacks people:write, and records every tool call in
    the agent ledger (`app.agent_log`): who, which tool, the shape of the arguments, the outcome
    the agent saw, how long it took."""

    async def _handle_list_tools(self, ctx, params):
        ops.note(method="tools/list")
        result = await super()._handle_list_tools(ctx, params)
        principal = await _principal_from_request(ctx.request)
        if principal is None or not principal.can("people:write"):
            result.tools = [t for t in result.tools if t.name not in WRITE_TOOLS]
        return result

    async def _handle_call_tool(self, ctx, params):
        request = ctx.request
        headers = getattr(request, "headers", None) or {}
        session = headers.get("x-agent-session") or headers.get("mcp-session-id")
        principal = await _principal_from_request(request)
        t0 = time.monotonic()
        ops.note(tool=params.name, api_key_id=getattr(principal, "api_key_id", None), session=session)
        result = await super()._handle_call_tool(ctx, params)
        is_error = bool(getattr(result, "is_error", False))
        error = None
        payload: Any = None
        if is_error:
            content = getattr(result, "content", None) or []
            error = getattr(content[0], "text", None) if content else "error"
        else:
            payload = getattr(result, "structured_content", None)
            if payload is None:                       # a tool answering plain JSON text
                content = getattr(result, "content", None) or []
                text = getattr(content[0], "text", None) if content else None
                try:
                    payload = json.loads(text) if text else None
                except ValueError:
                    payload = None
        ops.note(ok=not is_error)
        await agent_log.record(agent_log.Call(
            tool=params.name, principal=principal, session=session, args=params.arguments or {},
            ok=not is_error, error=error, duration_ms=int((time.monotonic() - t0) * 1000),
            result=payload), full_args=get_settings().mcp_log_args)
        return result


mcp = GloMCP(name="glo-people", instructions=INSTRUCTIONS)

# The longest a tool call blocks on a run. Claude Code allows a tool call 60 s unless the user
# raises MCP_TOOL_TIMEOUT; claude.ai allows 240 s. A run still going after this comes back as
# `running` with its run_id, and the agent follows it with get_run.
MAX_WAIT_S = 50


async def _principal_from_request(request: Any) -> Principal | None:
    state = getattr(request, "state", None)
    principal = getattr(state, "principal", None) if state is not None else None
    if principal is not None:
        return principal
    headers = getattr(request, "headers", None) or {}
    scheme, _, token = (headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return await auth.principal_from_api_key(token.strip())


async def _principal(ctx: Context, permission: str) -> Principal:
    principal = await _principal_from_request(ctx.request_context.request)
    if principal is None:
        raise ToolError("not authenticated")
    if not principal.can(permission):
        raise ToolError(f"missing permission: {permission}")
    return principal


@mcp.tool()
async def list_properties(ctx: Context) -> list[dict]:
    """List every property on people records: key, label, type, canonical values (enum types),
    coverage count, and whether it is embeddable (usable in search_people semantic.fields)."""
    await _principal(ctx, "people:read")
    # The coverage scan reads every document (5-7 s on prod); the counts may be a minute old.
    props = await properties.catalog(await people_svc.coverage(max_age_s=store_tool.COVERAGE_MAX_AGE_S))
    return [p.model_dump(mode="json") for p in props]


@mcp.tool()
async def search_people(
    ctx: Context,
    tier: Annotated[fields.Tier | list[fields.Tier] | None,
                    Field(description="Restrict to one tier, or to any of several "
                                      "(e.g. ['member', 'plus_one']); default: every tier")] = None,
    filters: Annotated[list[FieldFilter] | None,
                       Field(description="Literal per-field filters, ANDed")] = None,
    semantic: Annotated[SemanticQuery | None,
                        Field(description="Semantic similarity over embedded fields")] = None,
    sort: Annotated[Literal["score"] | SortSpec,
                    Field(description="'score' or {field, dir}")] = "score",
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 25,
    page: Annotated[int, Field(ge=1)] = 1,
    fields: Annotated[list[str] | None,
                      Field(description="Only these properties in each hit's `person` (id, tier "
                                        "and name always come, the scores too); default: every "
                                        "current value")] = None,
) -> dict:
    """Tool: search people. Same parameters as the web UI and POST /api/search.

    Examples:
      filters=[{"field":"name","op":"contains","value":"okafor"},
               {"field":"mrr","op":"gte","value":10000}, {"field":"stage","op":"in","value":["Seed","Series A"]}]
      semantic={"query":"fintech founder","fields":["bio","one_liner"],"mode":"avg","threshold":0.25}
      semantic={"queries":[{"field":"bio","query":"payments infrastructure"},
                           {"field":"company_description","query":"merchant acquiring"}],"threshold":0.3}
      semantic={"query":"head of partnerships at a fintech","fields":["*"],"mode":"max","threshold":0.35}
    Short values (role, company, industry, location) and the whole provider profile
    (enrichment_passage) are embeddable too; "*" compares a passage with every embeddable property.
    Each result has id, tier, score (semantic only), field_scores and the person's properties
    (plus `norm`, the normalised values of typed properties). A semantic response also carries
    `at_or_above`: how many of `total` reach each of a few tighter thresholds, so when `total`
    is far above `limit`, raise `threshold` to the step that leaves what you can read instead
    of paging.
    """
    principal = await _principal(ctx, "people:read")
    try:
        req = SearchRequest(tier=tier, filters=filters or [], semantic=semantic, sort=sort,
                            limit=limit, page=page)
        resp = await run_search(req)
    except ValueError as e:
        raise ToolError(str(e)) from e
    resp.search_id = await record_search(principal, req, resp, "mcp")
    out = json.loads(resp.model_dump_json())
    if fields:
        # the properties live under each hit's `person`; the hit's own keys (id, tier, score,
        # field_scores) stay as they are
        keep = {"id", "tier", "name", *fields}
        for hit in out["results"]:
            hit["person"] = {k: v for k, v in hit["person"].items() if k in keep}
    return out


@mcp.tool()
async def get_person(ctx: Context, id: Annotated[str, Field(description="Person id")],
                     include_history: Annotated[bool, Field(description="Also return every "
                                                                        "property's entries")] = True
                     ) -> dict:
    """Fetch one person by id: the shown field values plus, by default, `history`
    ({field: [{v, ts, src, derived?, shown?, why?}], oldest first}; the entry marked `shown` is
    the one the value comes from), and `glojobs`: their boards (`boards`, each with its url) and
    the talent entry that lists them (`listed_on`, with their page url on every board)."""
    await _principal(ctx, "people:read")
    try:
        person = await people_svc.get(id, include_history=include_history)
    except people_svc.PersonError as e:
        raise ToolError(str(e)) from e
    if not person:
        raise ToolError("person not found")
    return {**person, "glojobs": await boards.presence(person["id"])}


MAX_GET_PEOPLE = 50


@mcp.tool()
async def get_people(ctx: Context,
                     ids: Annotated[list[str], Field(min_length=1, max_length=MAX_GET_PEOPLE,
                                                     description="Person ids, up to 50")],
                     include_history: Annotated[bool, Field(description="Also return every "
                                                                        "property's entries")] = False,
                     fields: Annotated[list[str] | None,
                                       Field(description="Only these properties per person (id, tier "
                                                         "and name always come); default: every "
                                                         "current value")] = None,
                     ) -> dict:
    """Fetch several people by id in one call: `people` in the order asked (the same record as
    get_person, without `glojobs`), `missing` for ids nobody has. Use it after a search instead of
    one get_person per hit."""
    await _principal(ctx, "people:read")
    wanted = list(dict.fromkeys(ids))
    try:
        docs = await people_svc.fetch(wanted)
    except people_svc.PersonError as e:
        raise ToolError(str(e)) from e
    found = {str(d["_id"]): people_svc.public(d, include_history=include_history) for d in docs}
    keep = {"id", "tier", "name", *(fields or [])} if fields else None
    out = []
    for pid in wanted:
        person = found.get(pid)
        if person is None:
            continue
        out.append({k: v for k, v in person.items() if k in keep} if keep else person)
    return {"people": out, "missing": [pid for pid in wanted if pid not in found]}


@mcp.tool()
async def find_people(
    ctx: Context,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    linkedin: Annotated[str | None, Field(description="LinkedIn profile URL, any spelling")] = None,
    github: str | None = None,
) -> dict:
    """Tool: who a record is. Compares the given identifiers (email, phone, LinkedIn or GitHub URL,
    on canonical forms) and the name against every person and answers like the merge assistant
    would: decision `matched` (person_id, tier: identifier | name | name+context, keys), `ambiguous`
    (candidates to choose between) or `new` (nobody). Deterministic and exact; use it before
    search_people when you have a name or an identifier."""
    await _principal(ctx, "people:read")
    values = {k: v for k, v in {"name": name, "email": email, "phone_number": phone,
                                "linkedin": linkedin, "github": github}.items() if v}
    if not values:
        raise ToolError("give a name or an identifier (email, phone, linkedin, github)")
    match = await importer.match_record(values)
    return match.public()


@mcp.tool()
async def create_person(
    ctx: Context,
    tier: fields.Tier,
    fields: Annotated[dict[str, Any], Field(description="Property key → value; 'name' required. "
                                                        "Keys must exist (list_properties).")],
    agent: Annotated[str | None, Field(description="Name of the automated agent making this "
                                                   "change, if any; noted in the entry source")] = None,
) -> dict:
    """Create a person (requires people:write). Only existing properties can be set. To add a
    record that may already be in the database (matched by email, phone, LinkedIn, GitHub or
    name), use merge_person instead."""
    principal = await _principal(ctx, "people:write")
    try:
        res = await people_svc.create(principal, tier, fields,
                                      source=Source.for_mcp(principal, agent))
    except people_svc.PersonError as e:
        raise ToolError(str(e)) from e
    return res.person


@mcp.tool()
async def merge_person(
    ctx: Context,
    record: Annotated[dict[str, Any], Field(description="Property key → value of the incoming "
                                                        "record; 'name' required for a person "
                                                        "that may not exist yet; a 'person_id' "
                                                        "names the person outright")],
    options: Annotated[MergeOptions | None,
                       Field(description="tier (for a created person), max_new_properties, "
                                         "agent, review, dry_run")] = None,
) -> dict:
    """Assistant: merge one incoming record into the people database (requires people:write).
    The record is matched to a person by a shared identifier (email, phone, LinkedIn or GitHub
    URL, compared on canonical forms) or by a unique or corroborated name, else a person is
    created. Values for known properties that are not long prose are written as entered (list
    items appended, typed values normalised); long prose and unknown keys go to the storage
    agent. Several plausible people: nothing is written and the candidates are returned
    (status 'skipped'). Returns the match, what was written and filed, the review and every
    change; a dry run returns the match and the plan. A merge still going after 50 s comes back
    with status 'running' and its run_id: get_run shows the rest."""
    principal = await _principal(ctx, "people:write")
    try:
        return await merge.merge_one(record, options or MergeOptions(), principal,
                                     Source.for_mcp(principal), timeout=MAX_WAIT_S)
    except (ValueError, TypeError) as e:
        raise ToolError(str(e)) from e


@mcp.tool()
async def enrich_person(
    ctx: Context,
    id: Annotated[str, Field(description="Person id")],
    options: Annotated[EnrichOptions | None,
                       Field(description="max_credits (default 30 for one person) and refresh. "
                                         "Leave providers unset (FullEnrich, 5 credits a match) "
                                         "unless People Data Labs (60 a match) was asked for by "
                                         "name.")] = None,
) -> dict:
    """Assistant: enrich one person now (requires people:write; costs Firecrawl credits, capped
    by options.max_credits). Resolves their LinkedIn profile through the data providers, reads
    their own site, summarises a known GitHub account and the company's site, files the profile's
    facts through the storage agent and closes with the review. A person enriched recently is
    skipped unless options.refresh. Returns what was written and filed, the providers asked, the
    credits spent, the review and every change. For many people use start_assistant("enrich", …)."""
    from app.assistants import enrich as enrich_assistant
    principal = await _principal(ctx, "people:write")
    opts = options or EnrichOptions(max_credits=30)
    person = await people_svc.get(id, include_history=False)
    if person is None:
        raise ToolError("person not found")
    items = [{"person_id": id, "payload": {}, "label": person.get("name") or id}]
    try:
        res = await runner.run_one(enrich_assistant.SPEC, principal, items,
                                   {"options": opts.model_dump(mode="json")})
    except ValueError as e:
        raise ToolError(str(e)) from e
    changes = res.get("changes") or []
    written = sorted({c["key"] for c in changes if c.get("action") in ("set", "append")})
    keep = ("run_id", "item_id", "status", "error", "credits", "text", "linkedin", "providers",
            "storage", "stored", "not_found", "skipped", "review", "kept")
    return {**{k: res.get(k) for k in keep if k in res}, "written": written, "changes": changes,
            "warnings": enrich_budget.describe(opts)["warnings"]}


@mcp.tool()
async def merge_people(
    ctx: Context,
    keep_id: Annotated[str, Field(description="The person to keep")],
    drop_id: Annotated[str, Field(description="The duplicate to fold into it and delete")],
) -> dict:
    """Fold a duplicate document into another (requires people:write): every entry of the dropped
    person joins the kept one's histories with its own time and source, then the duplicate is
    deleted. Use it when find_people reports two people who are one (the same LinkedIn URL or
    email). Returns the kept person and the changes."""
    principal = await _principal(ctx, "people:write")
    try:
        res = await people_svc.merge_documents(principal, keep_id, drop_id)
    except people_svc.PersonError as e:
        raise ToolError(str(e)) from e
    return {"person": res.person, "changes": [c.model_dump(mode="json") for c in res.changes]}


@mcp.tool()
async def update_person(
    ctx: Context,
    id: str,
    fields: Annotated[dict[str, Any],
                      Field(description="Property key → new value (keys must exist, see "
                                        "list_properties). Each value is appended as a new "
                                        "time-stamped entry in the property's history (earlier "
                                        "values are kept) and, as a person's own edit, becomes "
                                        "the shown value; the shown value is a no-op; null "
                                        "records a removal. Write an earlier value again to "
                                        "restore it.")],
    tier: fields.Tier | None = None,
    agent: Annotated[str | None, Field(description="Name of the automated agent making this "
                                                   "change, if any; noted in the entry source")] = None,
) -> dict:
    """Append new values to a person's properties (requires people:write)."""
    principal = await _principal(ctx, "people:write")
    try:
        res = await people_svc.update(principal, id, fields, tier,
                                      source=Source.for_mcp(principal, agent))
    except people_svc.PersonError as e:
        raise ToolError(str(e)) from e
    if res is None:
        raise ToolError("person not found")
    return res.person


@mcp.tool()
async def plan_search(
    ctx: Context,
    question: Annotated[str, Field(min_length=1, max_length=2000,
                                   description="A question about who is in the database")],
    tier: Annotated[fields.Tier | None, Field(description="Restrict to one "
                                                                            "tier")] = None,
    filters: Annotated[list[FieldFilter] | None,
                       Field(description="Extra literal filters, ANDed with the plan")] = None,
    options: Annotated[Tuning | None, Field(description="k, mode, threshold")] = None,
) -> dict:
    """Tool: the executable search for a question. An agent reads the property catalog and turns
    the question into typed filters (canonical enum values, numbers, ISO dates) and a passage per
    embeddable property; the result is a search request you run with search_people, plus the
    planner's rationale. Nothing is executed or written."""
    await _principal(ctx, "people:read")
    try:
        result = await plan_search_tool(question, tier, filters or [], options or Tuning())
    except (ValueError, AgentRunError) as e:
        raise ToolError(str(e)) from e
    return json.loads(result.model_dump_json())


@mcp.tool()
async def ask_people(
    ctx: Context,
    question: Annotated[str, Field(min_length=1, max_length=2000,
                                   description="A question about who is in the database")],
    tier: Annotated[fields.Tier | None, Field(description="Restrict to one "
                                                                            "tier")] = None,
    filters: Annotated[list[FieldFilter] | None,
                       Field(description="Extra literal filters, ANDed with the plan")] = None,
    options: Annotated[Tuning | None, Field(description="k, mode, threshold")] = None,
) -> dict:
    """Assistant: answer a question about people in text. Plans the search (plan_search), runs
    it, and answers from the hits with citations. Returns text, citations, the executed search
    request (re-runnable with search_people) and the hits. For structured results use the tools
    instead."""
    principal = await _principal(ctx, "people:read")
    try:
        result = await ask(question, options or Tuning(), principal, tier, filters or [],
                           source="mcp")
    except (ValueError, AgentRunError) as e:
        raise ToolError(str(e)) from e
    return json.loads(result.model_dump_json())


@mcp.tool()
async def store_about_person(
    ctx: Context,
    id: str,
    passage: Annotated[str, Field(min_length=1, max_length=fields.MAX_TEXT_CHARS,
                                  description="Free text about the person (notes, a message, "
                                              "a profile)")],
    agent: Annotated[str | None, Field(description="Name of the automated agent supplying the "
                                                   "passage, if any")] = None,
    options: Annotated[StoreOptions | None,
                       Field(description="max_new, near_duplicate, overflow")] = None,
) -> dict:
    """Tool: store what a passage says about a person (requires people:write). An agent reads
    the passage against the person's current values and the property catalog and files one
    value per property (typed values in the type's form, list items appended, contradictions
    marked), creates new properties only for recurring kinds of fact (at most `max_new`;
    near-duplicates are redirected), then the normaliser maps what stayed unmapped. One shot;
    returns every change (set, append, normalized, flagged, property_created) plus what was
    confirmed, ignored and in conflict."""
    principal = await _principal(ctx, "people:write")
    src = Source.for_mcp(principal, f"store{'/' + agent.strip() if agent and agent.strip() else ''}")
    result = await _run_tool(ctx, store_tool.store, id, src, principal,
                             passage=passage, opts=options or StoreOptions())
    assert isinstance(result, store_tool.StoreResult)
    return json.loads(result.model_dump_json())


@mcp.tool()
async def normalize_person(
    ctx: Context,
    id: str,
    options: Annotated[NormalizeOptions | None,
                       Field(description="allocate, max_new_values, repair, force")] = None,
) -> dict:
    """Tool: re-parse a person's typed properties and have an agent decide the values the parser
    could not map (requires people:write). For enum properties the agent merges a text into an
    existing value (recorded as an alias), allocates a new canonical value when the text names a
    category the list lacks and fits the person (within options), or drops it. For a record
    without a record_kind (or with force) the repair step fixes broken identity data from the
    record's own facts
    and classifies it as Person, Company or Unidentifiable. Returns every change, the merges,
    allocations and rejections, what is still unmapped, and the repair result."""
    principal = await _principal(ctx, "people:write")
    options = options or NormalizeOptions()
    result = await _run_tool(ctx, normalize_tool.normalize, id,
                             Source("agent", "normalizer", f"key:{principal.api_key_id}"),
                             principal, opts=options)
    return json.loads(result.model_dump_json())


async def _run_tool(ctx: Context, tool, person_id: str, source: Source, principal: Principal, *,
                    firecrawl=None, **kwargs):
    """Run a tool on one record for an MCP call: its activity is forwarded to the client as log
    messages while it runs, and the result is returned when it has committed."""
    try:
        tctx = await invoke.make_context(person_id, source, principal, firecrawl=firecrawl,
                                         owns_firecrawl=firecrawl is not None)
    except ValueError as e:
        raise ToolError(str(e)) from e
    if tctx is None:
        if firecrawl is not None:
            await firecrawl.aclose()
        raise ToolError("person not found")
    inv = invoke.invoke(tool, tctx, **kwargs)
    async for ev in inv.subscribe():
        if ev.kind != "result":
            try:                 # a progress notification per event; nothing when the client sent no token
                await ctx.report_progress(ev.seq, None, f"{ev.kind}: {ev.message}")
            except Exception:                         # the client must never fail the tool
                log.debug("could not forward a tool event to the MCP client", exc_info=True)
    try:
        return await inv.wait()
    except (ValueError, AgentRunError, invoke.Stopped) as e:
        raise ToolError(str(e)) from e


@mcp.tool()
async def list_assistants(ctx: Context) -> list[dict]:
    """Assistant: the assistants that run over records as a run (key, what they do, their
    options schema, and the selector field for 'everyone with this property')."""
    await _principal(ctx, "people:read")
    return [spec.public() for spec in REGISTRY.values()]


@mcp.tool()
async def start_assistant(
    ctx: Context,
    key: Annotated[str, Field(description="The assistant key (see list_assistants)")],
    person_ids: Annotated[list[str] | None, Field(description="The people to run it for")] = None,
    from_run: Annotated[int | None, Field(description="Or: the people another run touched (the "
                                                      "merge run of an upload: everyone it "
                                                      "created or matched)")] = None,
    where: Annotated[list[FieldFilter] | None,
                     Field(description="Or: everyone matching these filters (ANDed; `tier` "
                                       "narrows)")] = None,
    all_with_field: Annotated[str | None, Field(description="Or: everyone whose property is set")] = None,
    records: Annotated[list[dict[str, Any]] | None,
                       Field(description="Or, for merge: the incoming records (property key → "
                                         "value each)")] = None,
    tier: fields.Tier | None = None,
    options: Annotated[dict[str, Any] | None, Field(description="Per the options schema")] = None,
    label: Annotated[str | None, Field(description="A name for the run")] = None,
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_S,
                                       description="Block up to this long for the run to finish "
                                                   "and return it done, with its items")] = 0,
) -> dict:
    """Assistant: start a run (requires people:write). Returns the run with its items; follow it
    with get_run, pause_run / resume_run / stop_run control it. A run pauses by itself when it
    reaches its credit cap (resume with a higher max_credits) or when the Firecrawl account is
    empty."""
    from app.routers.assistants import RunRequest, _people_items
    spec = REGISTRY.get(key)
    if spec is None:
        raise ToolError(f"unknown assistant {key!r}; list_assistants names them")
    principal = await _principal(ctx, spec.permission)
    try:
        body = RunRequest(person_ids=person_ids, from_run=from_run, where=where,
                          all_with_field=all_with_field, records=records, tier=tier,
                          options=options or {}, label=label)
        opts = spec.options_model.model_validate(body.options)
        if spec.scope == "people":
            items = await _people_items(body)
        elif spec.prepare is not None:
            items = await spec.prepare(body.model_dump(), opts)
        else:
            raise ToolError(f"{key} cannot be started this way")
    except (ValueError, HTTPException) as e:
        raise ToolError(getattr(e, "detail", str(e))) from e
    label = label or (items[0].get("label") if len(items) == 1 else None)
    run = await runner.create_run(spec, principal, items, {
        "options": opts.model_dump(mode="json"), "selector": all_with_field, "from_run": from_run,
        "where": [f.model_dump() for f in where or []] or None, "tier": tier}, label=label)
    await runner.start(run["id"], principal)
    if wait_seconds:
        try:
            await runner.wait(run["id"], timeout=wait_seconds)
        except TimeoutError:
            pass                                   # still running: the caller follows it with get_run
    return await _run_view(run["id"])


_RESULT_KEYS = ("person_id", "created", "written", "filed", "kept", "text", "dry_run")


def _item_view(it: dict) -> dict:
    """An item for an agent: its state plus the result's summary, not the full result."""
    out = {k: it.get(k) for k in ("id", "person_id", "label", "status", "credits", "error")}
    res = it.get("result") or {}
    summary = {k: res[k] for k in _RESULT_KEYS if k in res}
    if isinstance(res.get("match"), dict):
        summary["match"] = {k: res["match"].get(k) for k in ("decision", "tier", "keys")}
        if res["match"].get("decision") == "ambiguous":
            summary["match"]["candidates"] = res["match"].get("candidates")
    if summary:
        out["result"] = summary
    return out


def _cost_view(run: dict) -> dict:
    """What the run's stored options cost (`cost`) and the warnings they carry, for a reader
    deciding whether to resume or repeat it; empty for an assistant that spends nothing."""
    spec = REGISTRY.get(run["workflow"])
    if spec is None or spec.describe_cost is None:
        return {}
    try:
        cost = dict(spec.describe_cost(spec.options_model.model_validate(
            (run.get("params") or {}).get("options") or {})))
    except ValueError:
        return {}
    cost.pop("comparable", None)
    return {"warnings": cost.pop("warnings", []), "cost": cost}


async def _run_view(run_id: int, events: int = 0) -> dict:
    run = await runner.get_run(run_id)
    if run is None:
        raise ToolError("run not found")
    items = await runner.get_items(run_id, limit=500)
    recent = (await runner.events(run_id, limit=100_000))[-events:] if events else []
    return {**run, **_cost_view(run), "progress": await runner.progress(run),
            "items": [_item_view(it) for it in items],
            "events": [{k: e.get(k) for k in ("id", "ts", "kind", "message", "item_id")}
                       for e in recent]}


@mcp.tool()
async def plan_run(
    ctx: Context,
    key: Annotated[str, Field(description="The assistant key (enrich, normalize, update)")],
    person_ids: list[str] | None = None,
    from_run: int | None = None,
    where: list[FieldFilter] | None = None,
    all_with_field: str | None = None,
    tier: fields.Tier | None = None,
    options: Annotated[dict[str, Any] | None, Field(description="The options you would start with "
                                                                "(refresh changes what enrich "
                                                                "skips)")] = None,
) -> dict:
    """Assistant: estimate a run before starting it, with the same selectors as start_assistant.
    Returns the people selected; for enrich, how many have a LinkedIn URL or a site to work from,
    how many it would skip because they were enriched within the TTL, and the eligible count; the
    recent cost of one item (credits, model tokens, seconds, from the last 90 days of runs); the
    estimated credits, tokens and minutes; the Firecrawl balance and whether the estimate fits;
    and a recommended options.max_credits; `cost` names the providers the options would ask with
    the price of each, and `warnings` says when one of them should not be in (People Data Labs,
    60 a match, unless asked for by name). Writes nothing and costs nothing. Call it before any
    run over many people and tell the person what it will cost."""
    from app.routers.assistants import RunRequest, plan
    spec = REGISTRY.get(key)
    if spec is None:
        raise ToolError(f"unknown assistant {key!r}; list_assistants names them")
    if spec.scope != "people":
        raise ToolError(f"{key} is not run over people")
    await _principal(ctx, "people:read")
    try:
        body = RunRequest(person_ids=person_ids, from_run=from_run, where=where,
                          all_with_field=all_with_field, tier=tier, options=options or {})
        spec.options_model.model_validate(body.options)
        return await plan(spec, body)
    except (ValueError, HTTPException) as e:
        raise ToolError(getattr(e, "detail", str(e))) from e


@mcp.tool()
async def get_credits(ctx: Context) -> dict:
    """The Firecrawl credit balance: remaining, plan size, the billing period's end and `reserved`
    — the worst case the items in flight right now may still spend (about 16 per enrich item),
    NOT what they spent: actual spend is on each finished item (`credits`) and on the run
    (`credits_used`, `progress.credits_per_done`; typically 2–7 per person). Enrichment spends
    credits (FullEnrich 5 per match, a site read 1, GitHub 5; People Data Labs 60, only when
    asked for by name); nothing else does. `prices` lists every data provider with its listed
    price per match and the last bill seen. `credits` is null when no provider key is set."""
    await _principal(ctx, "people:read")
    return {"credits": await credits.summary(), "prices": enrich_budget.provider_prices()}


@mcp.tool()
async def list_runs(ctx: Context,
                    status: Annotated[Literal["active", "queued", "running", "pausing", "paused",
                                              "done", "cancelled"] | None,
                                      Field(description="active = not finished")] = None,
                    limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict:
    """Assistant: the recent runs (newest first) with their status, counts, credits and label;
    `active` lists what is running or paused right now."""
    await _principal(ctx, "people:read")
    page = await runner.list_runs(limit, 0, status)
    return {"runs": [{k: r.get(k) for k in ("id", "workflow", "label", "status", "total", "done",
                                            "failed", "credits_used", "pause_reason", "username",
                                            "created_at", "finished_at")} for r in page["items"]]}


@mcp.tool()
async def get_run(ctx: Context, run_id: int,
                  events: Annotated[int, Field(ge=0, le=200, description="Latest events")] = 20) -> dict:
    """Assistant: a run's state (status, counts, credits, pause reason), its progress (percent,
    items per minute, the estimated minutes left, credits per done item, model tokens so far),
    its items (status, the person each one created or matched, what it wrote, its one-line
    text), its latest activity, and for enrich the options it runs with (params.options), what
    they cost (`cost.providers`) and `warnings`. Poll it every few minutes for a long run and
    report the progress in plain words."""
    await _principal(ctx, "people:read")
    return await _run_view(run_id, events)


@mcp.tool()
async def pause_run(ctx: Context, run_id: int) -> dict:
    """Assistant: stop taking new items; in-flight ones finish (requires people:write)."""
    principal = await _principal(ctx, "people:write")
    if not await runner.pause(run_id, principal):
        raise ToolError("run is not running")
    return await runner.get_run(run_id)


@mcp.tool()
async def resume_run(ctx: Context, run_id: int,
                     options: Annotated[dict[str, Any] | None,
                                        Field(description="Options to change, e.g. max_credits")] = None
                     ) -> dict:
    """Assistant: continue a paused run, optionally with other options (requires people:write).
    A paused run keeps the options it was started with — read get_run's params.options, cost and
    warnings first; pass options to change them (a higher max_credits, or providers=["fullenrich"]
    for a run that was started with a dear provider)."""
    principal = await _principal(ctx, "people:write")
    try:
        ok = await runner.resume(run_id, options, principal)
    except ValueError as e:
        raise ToolError(str(e)) from e
    if not ok:
        raise ToolError("run is not paused")
    return await runner.get_run(run_id)


@mcp.tool()
async def stop_run(ctx: Context, run_id: int) -> dict:
    """Assistant: stop a run for good; in-flight tools are stopped before they write, queued
    items are skipped (requires people:write)."""
    principal = await _principal(ctx, "people:write")
    if not await runner.stop(run_id, principal):
        raise ToolError("run is not running")
    return await runner.get_run(run_id)


@mcp.tool()
async def delete_person(ctx: Context, id: str) -> dict:
    """Delete a person (requires people:write)."""
    principal = await _principal(ctx, "people:write")
    try:
        deleted = await people_svc.delete(principal, id)
    except people_svc.PersonError as e:
        raise ToolError(str(e)) from e
    if not deleted:
        raise ToolError("person not found")
    return {"deleted": id}


# --- glojobs.app boards --------------------------------------------------------------------------

def _board_error(e: Exception) -> ToolError:
    return ToolError(str(e))


@mcp.tool()
async def boards_guide(ctx: Context) -> dict:
    """Tool: read this before working with the glojobs.app boards — what a board is, the order of
    calls (catalogue, then board), how talent entries and boards are linked to glo-people people
    (person_id, or a record matched before anything is created), the URL scheme, what HTML
    survives."""
    await _principal(ctx, "people:read")
    return {"guide": boards.GUIDE}


@mcp.tool()
async def list_board_companies(ctx: Context) -> list[dict]:
    """Tool: the companies of the glojobs.app job catalogue (slug, name, site, one-liner)."""
    await _principal(ctx, "people:read")
    return await boards.list_companies()


@mcp.tool()
async def upsert_board_company(
    ctx: Context,
    slug: Annotated[str, Field(description="URL key, e.g. 'fastlane'; lower case and hyphens")],
    name: str,
    web: Annotated[str, Field(description="The site's host, e.g. usefastlane.ai")] = "",
    line: Annotated[str, Field(description="One line under the name on the board")] = "",
    about_html: Annotated[str, Field(description="A paragraph or two about the company as HTML "
                                                 "(p, ul/li, strong, a); shown on every role page")] = "",
) -> dict:
    """Tool: create or replace a company of the job catalogue (requires people:write). Returns
    the stored row and `stripped`: tags the sanitiser dropped from the about text."""
    await _principal(ctx, "people:write")
    try:
        return await boards.upsert_company(slug, name=name, web=web, line=line, about_html=about_html)
    except boards.BoardError as e:
        raise _board_error(e) from e


@mcp.tool()
async def list_board_jobs(ctx: Context, company: str | None = None,
                          status: Annotated[str | None, Field(description="open | closed")] = None,
                          q: Annotated[str | None, Field(description="Text in the title, company, "
                                                                     "location or summary")] = None
                          ) -> list[dict]:
    """Tool: the glojobs.app job catalogue (every job's slug, company, title, location, type,
    pay, summary and status; the HTML body too)."""
    await _principal(ctx, "people:read")
    return await boards.list_jobs(company, status, q)


@mcp.tool()
async def get_board_job(ctx: Context, slug: str) -> dict:
    """Tool: one job of the catalogue with the boards that list it."""
    await _principal(ctx, "people:read")
    job = await boards.get_job(slug)
    if not job:
        raise ToolError("job not found")
    return {**job, "boards": await boards.boards_listing("job_slugs", slug)}


@mcp.tool()
async def upsert_board_job(
    ctx: Context,
    company: Annotated[str, Field(description="The company's slug (upsert_board_company first)")],
    title: str,
    slug: Annotated[str | None, Field(description="URL key; default '<company>-<title>' slugified. "
                                                  "Pass the existing slug to replace a job")] = None,
    location: str = "",
    type: Annotated[str, Field(description="Full-time, Internship, Contract, …")] = "",
    comp: Annotated[str, Field(description="Pay as shown, e.g. '$150K–$275K base + equity'")] = "",
    summary: Annotated[str, Field(description="One or two sentences for the list and link "
                                              "previews")] = "",
    html: Annotated[str, Field(description="The job description as HTML: h2 section headings "
                                           "(What you'll do, What we're looking for, "
                                           "Compensation), p, ul/li, strong, a. Scripts, "
                                           "styles, images and attributes other than href are "
                                           "dropped and reported in `stripped`")] = "",
    status: Annotated[str, Field(description="open | closed; a closed job leaves every board")] = "open",
    source_url: Annotated[str, Field(description="The original posting, linked from the page")] = "",
) -> dict:
    """Tool: create or replace a job of the catalogue (requires people:write). Returns the stored
    row and `stripped` (tags dropped from the HTML)."""
    principal = await _principal(ctx, "people:write")
    try:
        return await boards.upsert_job(slug, company=company, title=title, location=location,
                                       type=type, comp=comp, summary=summary, html=html,
                                       status=status, source_url=source_url,
                                       user_id=principal.user_id)
    except boards.BoardError as e:
        raise _board_error(e) from e


@mcp.tool()
async def delete_board_job(ctx: Context, slug: str) -> dict:
    """Tool: delete a job from the catalogue and from every board (requires people:write)."""
    await _principal(ctx, "people:write")
    if not await boards.delete_job(slug):
        raise ToolError("job not found")
    return {"deleted": slug}


@mcp.tool()
async def list_board_talent(ctx: Context,
                            status: Annotated[str | None, Field(description="active | hidden")] = None,
                            q: str | None = None, person_id: str | None = None) -> list[dict]:
    """Tool: the glojobs.app talent catalogue (people shown on talent boards: slug, name,
    headline, location, summary, HTML profile, the linked person_id)."""
    await _principal(ctx, "people:read")
    return await boards.list_talent(status, q, person_id)


@mcp.tool()
async def get_board_talent(ctx: Context, slug: str) -> dict:
    """Tool: one talent entry with the boards that list it."""
    await _principal(ctx, "people:read")
    row = await boards.get_talent(slug)
    if not row:
        raise ToolError("talent not found")
    return {**row, "boards": await boards.boards_listing("talent_slugs", slug)}


@mcp.tool()
async def upsert_board_talent(
    ctx: Context,
    slug: Annotated[str | None, Field(description="URL key; default the name slugified. Pass the "
                                                  "existing slug to replace an entry")] = None,
    person_id: Annotated[str | None, Field(description="The glo-people record; every field left "
                                                       "out is drafted from it (name, role at "
                                                       "company, location, one-liner, bio and "
                                                       "about as the profile)")] = None,
    person: Annotated[dict[str, Any] | None,
                      Field(description="Instead of person_id: a record {name, email, company, "
                                        "role, location, …}. Matched by identifier or unique "
                                        "name first: a match is an error naming the person_id "
                                        "to pass; only a stranger is created")] = None,
    tier: Annotated[fields.Tier, Field(description="Tier for a person `person` creates")] = "plus_two",
    name: str | None = None,
    headline: Annotated[str | None, Field(description="Role at company")] = None,
    location: str | None = None,
    summary: Annotated[str | None, Field(description="One or two sentences for the card")] = None,
    html: Annotated[str | None, Field(description="The profile as HTML: h2, p, ul/li, strong, a")] = None,
    status: Annotated[str | None, Field(description="active | hidden")] = None,
) -> dict:
    """Tool: create or replace a talent entry (requires people:write). Every entry is one
    glo-people person's: pass person_id, or person (see boards_guide). Returns the stored row
    and `stripped`."""
    principal = await _principal(ctx, "people:write")
    try:
        return await boards.upsert_talent(slug, name=name, person_id=person_id, person=person,
                                          headline=headline, location=location, summary=summary,
                                          html=html, status=status, user_id=principal.user_id,
                                          principal=principal, source=Source.for_mcp(principal),
                                          tier=tier)
    except boards.BoardError as e:
        raise _board_error(e) from e


@mcp.tool()
async def delete_board_talent(ctx: Context, slug: str) -> dict:
    """Tool: delete a talent entry from the catalogue and from every board (requires
    people:write)."""
    await _principal(ctx, "people:write")
    if not await boards.delete_talent(slug):
        raise ToolError("talent not found")
    return {"deleted": slug}


@mcp.tool()
async def list_boards(ctx: Context, person_id: str | None = None) -> list[dict]:
    """Tool: every glojobs.app board (or one person's): id, person, kind, title, url, item
    counts, views and when it was last opened."""
    await _principal(ctx, "people:read")
    return await boards.list_boards(person_id)


@mcp.tool()
async def get_board(ctx: Context, id: str) -> dict:
    """Tool: one board with its items resolved (open jobs grouped by company, active talent),
    its url, views and last_viewed_at."""
    await _principal(ctx, "people:read")
    try:
        row = await boards.get_board(id)
    except boards.NotFound:
        row = None
    if not row:
        raise ToolError("board not found")
    return await boards.resolve(row)


@mcp.tool()
async def create_board(
    ctx: Context,
    kind: Annotated[str, Field(description="jobs | talent | both")],
    person_id: Annotated[str | None, Field(description="Whose page it is (find_people first)")] = None,
    person: Annotated[dict[str, Any] | None,
                      Field(description="Instead of person_id: a record {name, email, company, "
                                        "role, …}. Matched by identifier or unique name first: "
                                        "a match is an error naming the person_id to pass; "
                                        "only a stranger is created")] = None,
    title: Annotated[str | None, Field(description="Page title; default 'Roles picked for "
                                                   "<first name>'")] = None,
    intro_html: Annotated[str, Field(description="A short note at the top as HTML (p, strong, a)")] = "",
    job_slugs: Annotated[list[str] | None, Field(description="Jobs in display order")] = None,
    talent_slugs: Annotated[list[str] | None, Field(description="Talent in display order")] = None,
    tier: Annotated[fields.Tier, Field(description="Tier for a person `person` creates")] = "plus_two",
) -> dict:
    """Tool: a new board for a person (requires people:write). Returns the board with its public
    url (hand that to the person) and `stripped` for the intro."""
    principal = await _principal(ctx, "people:write")
    try:
        return await boards.create_board(person_id=person_id, person=person, kind=kind, title=title,
                                         intro_html=intro_html, job_slugs=job_slugs,
                                         talent_slugs=talent_slugs, user_id=principal.user_id,
                                         principal=principal, source=Source.for_mcp(principal),
                                         tier=tier)
    except boards.BoardError as e:
        raise _board_error(e) from e


@mcp.tool()
async def update_board(
    ctx: Context,
    id: str,
    kind: Annotated[str | None, Field(description="jobs | talent | both")] = None,
    title: Annotated[str | None, Field(description="'' clears it back to the default")] = None,
    intro_html: str | None = None,
    job_slugs: Annotated[list[str] | None, Field(description="Replaces the job list")] = None,
    talent_slugs: Annotated[list[str] | None, Field(description="Replaces the talent list")] = None,
    add_jobs: list[str] | None = None,
    remove_jobs: list[str] | None = None,
    add_talent: list[str] | None = None,
    remove_talent: list[str] | None = None,
    enabled: Annotated[bool | None, Field(description="false turns the link off (404) without "
                                                      "deleting the board")] = None,
    person_id: Annotated[str | None, Field(description="Move the board to another person")] = None,
) -> dict:
    """Tool: change a board (requires people:write): its lists (replace, or add / remove in
    place), kind, title, intro, person or enabled flag. Returns the board."""
    await _principal(ctx, "people:write")
    try:
        row = await boards.update_board(id, kind=kind, title=title, intro_html=intro_html,
                                        job_slugs=job_slugs, talent_slugs=talent_slugs,
                                        add_jobs=add_jobs, remove_jobs=remove_jobs,
                                        add_talent=add_talent, remove_talent=remove_talent,
                                        enabled=enabled, person_id=person_id)
    except boards.BoardError as e:
        raise _board_error(e) from e
    if not row:
        raise ToolError("board not found")
    return row


@mcp.tool()
async def delete_board(ctx: Context, id: str) -> dict:
    """Tool: delete a board; its link stops working (requires people:write)."""
    await _principal(ctx, "people:write")
    try:
        ok = await boards.delete_board(id)
    except boards.NotFound:
        ok = False
    if not ok:
        raise ToolError("board not found")
    return {"deleted": id}


class APIKeyAuth:
    """ASGI wrapper: rejects requests without a valid API key before they reach the MCP app."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":                   # no other transport: a websocket is refused
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        scheme, _, token = headers.get("authorization", "").partition(" ")
        ip = headers.get("x-real-ip") or (scope.get("client") or (None,))[0]
        principal = None
        if scheme.lower() == "bearer" and token.strip():
            principal = await auth.principal_from_api_key(token.strip(), ip)
        if principal is None or not principal.can("people:read"):
            body = b'{"error":"invalid or missing API key"}'
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="glo-people"'),
                (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})["principal"] = principal
        if scope.get("method") == "POST":
            receive = await _noting_method(receive)
        await self.app(scope, receive, send)


MAX_PEEK_BYTES = 1 << 20


async def _noting_method(receive: Receive) -> Receive:
    """Read the request body, put its JSON-RPC method on the request's ops row (`attrs.method`:
    initialize, tools/list, tools/call, ping…), and hand the body back unread. Without this the
    request log knows a tool call from a handshake but nothing else about what the client sends,
    e.g. the POST the claude.ai connector holds open for minutes."""
    messages: list[dict] = []
    body = b""
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body") or len(body) > MAX_PEEK_BYTES:
            break
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if isinstance(parsed, list):
        methods = [m.get("method") for m in parsed if isinstance(m, dict) and m.get("method")]
        if methods:
            ops.note(method=methods[0] if len(methods) == 1 else ",".join(methods))

    async def replay() -> dict:
        if messages:
            return messages.pop(0)
        return await receive()

    return replay


def build_app() -> ASGIApp:
    starlette_app = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        # A call that finishes within the SDK's 15 s window still gets a plain JSON reply; a
        # longer one commits to an SSE stream with a keepalive ping every 15 s. Cloudflare cuts a
        # proxied response after 100 s without bytes and nginx after 300 s, which is what a
        # 120 s merge_person looked like from Claude: "the MCP timed out".
        json_response=False,
        # Auth is a bearer key, not ambient cookies, so DNS-rebinding protection adds nothing and
        # would reject the public hostname.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return APIKeyAuth(starlette_app)
