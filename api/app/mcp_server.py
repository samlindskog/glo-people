"""Public MCP server (streamable HTTP at /mcp), authenticated with per-account API keys."""

import json
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from pydantic_ai.exceptions import AgentRunError
from starlette.types import ASGIApp, Receive, Scope, Send

from app import activity, auth, properties
from app import people as people_svc
from app.agents import normalize_person as normalize_person_op
from app.agents import store
from app.agents.ask import AskOptions, ask
from app.agents.store import StoreOptions
from app.auth import Principal
from app.history import Source
from app.search import (
    MAX_LIMIT,
    FieldFilter,
    SearchRequest,
    SemanticQuery,
    SortSpec,
    describe,
    record_search,
    run_search,
)

WRITE_TOOLS = frozenset({"create_person", "update_person", "delete_person", "store_about_person",
                         "normalize_person"})

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
come only from store_about_person, which has an agent split a passage into statements and file
each under the best-matching property. ask_people plans a search from a question (passages per
property plus typed filters), runs it and answers with citations. normalize_person re-parses a
person's typed values and asks an agent about what the parser could not map. Pass `agent` when
you write on behalf of an automated agent: the entry stays attributed to your key (kind "mcp")
with the agent name noted in its reference."""


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
    """Search people. Same parameters as the web UI and POST /api/search.

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
    """Create a person (requires people:write). Only existing properties can be set."""
    principal = await _principal(ctx, "people:write")
    try:
        return await people_svc.create(principal, tier, fields,
                                       source=Source.for_mcp(principal, agent))
    except people_svc.PersonError as e:
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
        person = await people_svc.update(principal, id, fields, tier,
                                         source=Source.for_mcp(principal, agent))
    except people_svc.PersonError as e:
        raise ToolError(str(e)) from e
    if not person:
        raise ToolError("person not found")
    return person


@mcp.tool()
async def ask_people(
    ctx: Context,
    question: Annotated[str, Field(min_length=1, max_length=2000,
                                   description="A question about who is in the database")],
    tier: Annotated[Literal["member", "plus_one"] | None, Field(description="Restrict to one "
                                                                            "tier")] = None,
    filters: Annotated[list[FieldFilter] | None,
                       Field(description="Extra literal filters, ANDed with the plan")] = None,
    options: Annotated[AskOptions | None, Field(description="k, mode, threshold")] = None,
) -> dict:
    """Answer a question about people: an agent plans a search over the property catalog (a
    passage per embeddable property, typed filters), the search runs, and an agent answers from
    the hits with citations. Returns answer, citations, the plan, the executed search request
    (re-runnable with search_people) and the hits."""
    principal = await _principal(ctx, "people:read")
    try:
        result = await ask(question, options or AskOptions(), principal, tier, filters or [],
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
    passage: Annotated[str, Field(min_length=1, max_length=20_000,
                                  description="Free text about the person (notes, a message, "
                                              "a profile)")],
    agent: Annotated[str | None, Field(description="Name of the automated agent supplying the "
                                                   "passage, if any")] = None,
    options: Annotated[StoreOptions | None,
                       Field(description="threshold, near_duplicate, max_new")] = None,
) -> dict:
    """Store what a passage says about a person (requires people:write). An agent splits it into
    tagged statements; each lands on the existing property whose name fits best, or on a new
    free-text property (at most `max_new` per passage). Returns what was written and why."""
    principal = await _principal(ctx, "people:write")
    src = Source.for_mcp(principal, f"store{'/' + agent.strip() if agent and agent.strip() else ''}")
    try:
        report = await store.store(id, passage, options or StoreOptions(), principal, src)
    except (ValueError, AgentRunError) as e:
        raise ToolError(str(e)) from e
    if report is None:
        raise ToolError("person not found")
    await activity.record("mcp.store", principal, id, {
        "items": [{"tag": i.tag, "action": i.action, "key": i.key} for i in report.items],
        "created": report.created})
    return json.loads(report.model_dump_json())


@mcp.tool()
async def normalize_person(ctx: Context, id: str) -> dict:
    """Re-parse a person's typed properties and have an agent decide the values the parser could
    not map (requires people:write). Returns what was parsed, mapped and left."""
    principal = await _principal(ctx, "people:write")
    try:
        report = await normalize_person_op.run(
            id, principal, Source("agent", "normalizer", f"key:{principal.api_key_id}"))
    except (ValueError, AgentRunError) as e:
        raise ToolError(str(e)) from e
    if report is None:
        raise ToolError("person not found")
    return json.loads(report.model_dump_json())


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
