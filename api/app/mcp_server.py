"""Public MCP server (streamable HTTP at /mcp), authenticated with per-account API keys."""

import json
import logging
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from pydantic_ai.exceptions import AgentRunError
from starlette.types import ASGIApp, Receive, Scope, Send

from app import activity, auth, fields, properties
from app import people as people_svc
from app.assistants import REGISTRY, merge, runner
from app.assistants.ask import ask
from app.assistants.merge import MergeOptions
from app.auth import Principal
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
    describe,
    record_search,
    run_search,
)
from app.tools.store import StoreOptions

log = logging.getLogger(__name__)

WRITE_TOOLS = frozenset({"create_person", "merge_person", "update_person", "delete_person",
                         "store_about_person", "normalize_person", "start_assistant", "pause_run",
                         "resume_run", "stop_run"})

INSTRUCTIONS = """\
Search and read the GLO people database. People are documents in two tiers ('member' and
'plus_one') made of properties. Call list_properties first to see which properties exist, their
type, canonical values (enum types), coverage, and which are embeddable for semantic search.
search_people combines literal per-property filters (AND) with optional semantic similarity over
one or more embeddable properties, aggregated by avg or max and cut off at a threshold (cosine
similarity; default 0.4. Unrelated text scores around 0.2-0.3, so 0.35-0.5 is the useful range).
A property's semantic score is its best-matching passage across its history. Values are never
overwritten: create_person and update_person append time-stamped entries (with their source) to
each property's history, and the latest entry is the current value; get_person returns the
current values plus `history`. Only existing properties can be written directly; new properties
come only from store_about_person.

Two kinds of operation. Tools act on one record, one shot, and return structured results for
programs: search_people (run a search), plan_search (question → an executable search request),
store_about_person (file a passage into the person's properties; returns every change),
normalize_person (typed values; returns every change); while they run, their activity arrives as
progress notifications. Assistants answer in text for a human (ask_people: plan, search, answer
with citations) or run over many records as a run you can follow, pause, resume with other
options and stop: list_assistants, start_assistant, get_run, pause_run, resume_run, stop_run.
Pass `agent` when you write on behalf of an automated agent: the entry stays attributed to your
key (kind "mcp") with the agent name noted in its reference."""


class GloMCP(MCPServer):
    """Hides write tools from keys whose owner lacks people:write."""

    async def _handle_list_tools(self, ctx, params):
        result = await super()._handle_list_tools(ctx, params)
        principal = await _principal_from_request(ctx.request)
        if principal is None or not principal.can("people:write"):
            result.tools = [t for t in result.tools if t.name not in WRITE_TOOLS]
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
    principal = await _principal(ctx, "people:read")
    await activity.record("mcp.list_properties", principal)
    props = await properties.catalog(await people_svc.coverage())
    return [p.model_dump(mode="json") for p in props]


@mcp.tool()
async def search_people(
    ctx: Context,
    tier: Annotated[Literal["member", "plus_one"] | None,
                    Field(description="Restrict to one tier")] = None,
    filters: Annotated[list[FieldFilter] | None,
                       Field(description="Literal per-field filters, ANDed")] = None,
    semantic: Annotated[SemanticQuery | None,
                        Field(description="Semantic similarity over embedded fields")] = None,
    sort: Annotated[Literal["score"] | SortSpec,
                    Field(description="'score' or {field, dir}")] = "score",
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 25,
    page: Annotated[int, Field(ge=1)] = 1,
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
    await activity.record("mcp.search", principal, str(resp.search_id),
                          {**describe(req), "total": resp.total})
    return json.loads(resp.model_dump_json())


@mcp.tool()
async def get_person(ctx: Context, id: Annotated[str, Field(description="Person id")]) -> dict:
    """Fetch one person by id: current field values plus `history`
    ({field: [{v, ts, src}], oldest first})."""
    principal = await _principal(ctx, "people:read")
    try:
        person = await people_svc.get(id)
    except people_svc.PersonError as e:
        raise ToolError(str(e)) from e
    if not person:
        raise ToolError("person not found")
    await activity.record("mcp.get_person", principal, id)
    return person


@mcp.tool()
async def create_person(
    ctx: Context,
    tier: Literal["member", "plus_one"],
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
async def update_person(
    ctx: Context,
    id: str,
    fields: Annotated[dict[str, Any],
                      Field(description="Property key → new value (keys must exist, see "
                                        "list_properties). Each value is appended as a new "
                                        "time-stamped entry in the property's history (earlier "
                                        "values are kept); the current value is a no-op; null "
                                        "records a removal.")],
    tier: Literal["member", "plus_one"] | None = None,
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
    tier: Annotated[Literal["member", "plus_one"] | None, Field(description="Restrict to one "
                                                                            "tier")] = None,
    filters: Annotated[list[FieldFilter] | None,
                       Field(description="Extra literal filters, ANDed with the plan")] = None,
    options: Annotated[Tuning | None, Field(description="k, mode, threshold")] = None,
) -> dict:
    """Tool: the executable search for a question. An agent reads the property catalog and turns
    the question into typed filters (canonical enum values, numbers, ISO dates) and a passage per
    embeddable property; the result is a search request you run with search_people, plus the
    planner's rationale. Nothing is executed or written."""
    principal = await _principal(ctx, "people:read")
    try:
        result = await plan_search_tool(question, tier, filters or [], options or Tuning())
    except (ValueError, AgentRunError) as e:
        raise ToolError(str(e)) from e
    await activity.record("mcp.plan", principal, None, {"question": question})
    return json.loads(result.model_dump_json())


@mcp.tool()
async def ask_people(
    ctx: Context,
    question: Annotated[str, Field(min_length=1, max_length=2000,
                                   description="A question about who is in the database")],
    tier: Annotated[Literal["member", "plus_one"] | None, Field(description="Restrict to one "
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
    await activity.record("mcp.ask", principal, str(result.search_id),
                          {"question": question, "total": result.total})
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
    await activity.record("mcp.store", principal, id, {
        "changes": [{"key": c.key, "action": c.action} for c in result.changes],
        "created": result.created_properties})
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
    all_with_field: Annotated[str | None, Field(description="Or: everyone whose property is set")] = None,
    records: Annotated[list[dict[str, Any]] | None,
                       Field(description="Or, for merge: the incoming records (property key → "
                                         "value each)")] = None,
    tier: Literal["member", "plus_one"] | None = None,
    options: Annotated[dict[str, Any] | None, Field(description="Per the options schema")] = None,
    label: Annotated[str | None, Field(description="A name for the run")] = None,
) -> dict:
    """Assistant: start a run (requires people:write). Returns the run; follow it with get_run,
    pause_run / resume_run / stop_run control it. A run pauses by itself when it reaches its
    credit cap (resume with a higher max_credits) or when the Firecrawl account is empty."""
    from app.routers.assistants import RunRequest, _people_items
    spec = REGISTRY.get(key)
    if spec is None:
        raise ToolError(f"unknown assistant {key!r}")
    principal = await _principal(ctx, spec.permission)
    try:
        body = RunRequest(person_ids=person_ids, all_with_field=all_with_field, records=records,
                          tier=tier, options=options or {}, label=label)
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
        "options": opts.model_dump(mode="json"), "selector": all_with_field, "tier": tier},
        label=label)
    await runner.start(run["id"], principal)
    return await runner.get_run(run["id"])


@mcp.tool()
async def get_run(ctx: Context, run_id: int,
                  events: Annotated[int, Field(ge=0, le=200, description="Latest events")] = 20) -> dict:
    """Assistant: a run's state (status, counts, credits, pause reason), its items' statuses and
    its latest activity."""
    await _principal(ctx, "people:read")
    run = await runner.get_run(run_id)
    if run is None:
        raise ToolError("run not found")
    items = await runner.get_items(run_id, limit=500)
    recent = (await runner.events(run_id, limit=100_000))[-events:] if events else []
    return {**run, "items": [{k: it.get(k) for k in ("id", "person_id", "label", "status",
                                                     "credits", "error")} for it in items],
            "events": [{k: e.get(k) for k in ("id", "ts", "kind", "message", "item_id")}
                       for e in recent]}


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
        if scope["type"] != "http":
            await self.app(scope, receive, send)
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
