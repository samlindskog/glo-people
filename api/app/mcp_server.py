"""Public MCP server (streamable HTTP at /mcp), authenticated with per-account API keys."""

import json
import logging
import time
from typing import Annotated, Any, Literal

import logfire
from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from pydantic_ai.exceptions import AgentRunError
from starlette.types import ASGIApp, Receive, Scope, Send

from app import agent_log, auth, fields, importer, properties
from app import people as people_svc
from app.assistants import REGISTRY, credits, merge, runner
from app.assistants.ask import ask
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
                         "resume_run", "stop_run"})

INSTRUCTIONS = """\
The GLO people database: people in tiers ('member', 'plus_one', 'plus_two'), each a document of
properties whose values are never overwritten (every write appends a time-stamped, sourced entry to
the property's history). You act for a person who is not technical: do the lookups, previews and
follow-ups yourself and report in plain words.

Playbook
- Finding someone: find_people first (an email, phone, LinkedIn or GitHub URL, or a name: exact,
  canonical, deterministic; it says matched / ambiguous / nobody). search_people is for descriptions
  ("fintech founders in Lagos"): literal per-property filters (AND) plus semantic similarity over
  embeddable properties (cosine; default threshold 0.4, 0.35-0.5 is the useful range). Call
  list_properties once to see the properties, their types and canonical values. get_person reads one
  record; pass include_history=false unless you need the entries.
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
- Long runs: do not wait inside the call. Note the run id, poll get_run every few minutes and report
  progress.percent, items_per_min, eta_min and credits_used; list_runs shows what is active.
  pause_run / stop_run if the person asks. When it finishes, summarise the items by status.
- Following up on an upload: the import answers with a run id; start_assistant(key, from_run=<that
  id>) runs another assistant over exactly the people it created or matched: "normalize" (typed values,
  record kind, broken names; free), "enrich" (data providers and sites; costs credits — always pass
  options.max_credits), "update" (the same values or tier on all of them, e.g. options={"tier":
  "plus_two"}; dry_run first). where=[filters] selects by properties instead; person_ids by id.
  wait_seconds lets a small run finish inside the call; otherwise follow it with get_run.
- Fixing a value: update_person appends the right value (null removes); to restore an earlier value,
  write it again — the history keeps every version with its source. Which entry is shown follows
  the trust of its source: a person's own edit (ui, api, mcp — yours) outranks an assistant's
  finding (agent), which outranks a bulk upload (import); newest wins within a class, and an entry
  marked `derived` (a fix or completion of the shown value) supersedes it. A write that does not
  outrank the shown value is recorded and reported as `kept`; get_person marks the shown entry
  (`shown`, `why`). normalize_person re-parses one record and repairs its identity data.
- Before any bulk write, dry_run; after one, read the run's items (get_run) and summarise counts.
Pass `agent` when you write on behalf of another automated agent: the entry stays attributed to your
key (kind "mcp") with that name in its reference."""


class GloMCP(MCPServer):
    """Hides write tools from keys whose owner lacks people:write, and records every tool call in
    the agent ledger (`app.agent_log`): who, which tool, the shape of the arguments, the outcome
    the agent saw, how long it took."""

    async def _handle_list_tools(self, ctx, params):
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
        with logfire.span("mcp {tool}", tool=params.name, api_key_id=getattr(principal, "api_key_id", None),
                          session=session) as span:
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
            span.set_attribute("ok", not is_error)
        await agent_log.record(agent_log.Call(
            tool=params.name, principal=principal, session=session, args=params.arguments or {},
            ok=not is_error, error=error, duration_ms=int((time.monotonic() - t0) * 1000),
            result=payload), full_args=get_settings().mcp_log_args)
        return result


mcp = GloMCP(name="glo-people", instructions=INSTRUCTIONS)


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
    props = await properties.catalog(await people_svc.coverage())
    return [p.model_dump(mode="json") for p in props]


@mcp.tool()
async def search_people(
    ctx: Context,
    tier: Annotated[fields.Tier | None,
                    Field(description="Restrict to one tier")] = None,
    filters: Annotated[list[FieldFilter] | None,
                       Field(description="Literal per-field filters, ANDed")] = None,
    semantic: Annotated[SemanticQuery | None,
                        Field(description="Semantic similarity over embedded fields")] = None,
    sort: Annotated[Literal["score"] | SortSpec,
                    Field(description="'score' or {field, dir}")] = "score",
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 25,
    page: Annotated[int, Field(ge=1)] = 1,
    fields: Annotated[list[str] | None,
                      Field(description="Only these properties in each result (id, tier, name and "
                                        "the scores always come); default: every current value")] = None,
) -> dict:
    """Tool: search people. Same parameters as the web UI and POST /api/search.

    Examples:
      filters=[{"field":"name","op":"contains","value":"okafor"},
               {"field":"mrr","op":"gte","value":10000}, {"field":"stage","op":"in","value":["Seed","Series A"]}]
      semantic={"query":"fintech founder","fields":["bio","one_liner"],"mode":"avg","threshold":0.25}
      semantic={"queries":[{"field":"bio","query":"payments infrastructure"},
                           {"field":"company_description","query":"merchant acquiring"}],"threshold":0.3}
    Each result has id, tier, score (semantic only), field_scores and the person's properties
    (plus `norm`, the normalised values of typed properties).
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
        keep = {"id", "tier", "name", "score", "field_scores", *fields}
        out["results"] = [{k: v for k, v in r.items() if k in keep} for r in out["results"]]
    return out


@mcp.tool()
async def get_person(ctx: Context, id: Annotated[str, Field(description="Person id")],
                     include_history: Annotated[bool, Field(description="Also return every "
                                                                        "property's entries")] = True
                     ) -> dict:
    """Fetch one person by id: the shown field values plus, by default, `history`
    ({field: [{v, ts, src, derived?, shown?, why?}], oldest first}; the entry marked `shown` is
    the one the value comes from)."""
    await _principal(ctx, "people:read")
    try:
        person = await people_svc.get(id, include_history=include_history)
    except people_svc.PersonError as e:
        raise ToolError(str(e)) from e
    if not person:
        raise ToolError("person not found")
    return person


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
    change; a dry run returns the match and the plan."""
    principal = await _principal(ctx, "people:write")
    try:
        return await merge.merge_one(record, options or MergeOptions(), principal,
                                     Source.for_mcp(principal))
    except (ValueError, TypeError) as e:
        raise ToolError(str(e)) from e


@mcp.tool()
async def enrich_person(
    ctx: Context,
    id: Annotated[str, Field(description="Person id")],
    options: Annotated[EnrichOptions | None,
                       Field(description="max_credits (default 30 for one person), providers, "
                                         "provider_policy, refresh, max_new_properties")] = None,
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
    return {**{k: res.get(k) for k in keep if k in res}, "written": written, "changes": changes}


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
    wait_seconds: Annotated[int, Field(ge=0, le=120,
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


async def _run_view(run_id: int, events: int = 0) -> dict:
    run = await runner.get_run(run_id)
    if run is None:
        raise ToolError("run not found")
    items = await runner.get_items(run_id, limit=500)
    recent = (await runner.events(run_id, limit=100_000))[-events:] if events else []
    return {**run, "progress": await runner.progress(run), "items": [_item_view(it) for it in items],
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
    and a recommended options.max_credits. Writes nothing and costs nothing. Call it before any
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
    opted in); nothing else does. Null
    when no provider key is set."""
    await _principal(ctx, "people:read")
    return {"credits": await credits.summary()}


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
    text) and its latest activity. Poll it every few minutes for a long run and report the
    progress in plain words."""
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
    """Assistant: continue a paused run, optionally with other options (requires people:write)."""
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
        await self.app(scope, receive, send)


def build_app() -> ASGIApp:
    starlette_app = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        # Auth is a bearer key, not ambient cookies, so DNS-rebinding protection adds nothing and
        # would reject the public hostname.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return APIKeyAuth(starlette_app)
