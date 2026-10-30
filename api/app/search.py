"""People search: literal per-field filters (Mongo) combined with per-field semantic similarity
(pgvector). The same `SearchRequest` model backs the REST endpoint, the UI and the MCP tool."""

import json
import re
from typing import Annotated, Any, Literal

from bson import ObjectId
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from app import db, embeddings, fields, people, properties
from app.auth import Principal

FilterOp = Literal["eq", "ne", "contains", "starts_with", "regex", "in", "exists",
                   "gt", "gte", "lt", "lte"]

MAX_LIMIT = 200


class FieldFilter(BaseModel):
    """A literal condition on one field. String matching (eq, contains, starts_with, regex, in)
    is case-insensitive; on list fields it matches any element."""

    field: str = Field(description="Property key, e.g. 'name'. See list_properties.")
    op: FilterOp = Field("contains", description="Comparison operator")
    value: Any = Field(None, description="Value to compare. A list for 'in'; true/false for "
                                         "'exists'; a number or ISO date string for gt/gte/lt/lte.")


class SemanticQuery(BaseModel):
    """Similarity search against per-field embeddings."""

    query: str = Field(min_length=1, max_length=2000, description="Natural-language query")
    fields: list[str] = Field(min_length=1, max_length=20,
                              description="Embeddable property keys to compare against, e.g. "
                                          "['bio'] (see list_properties)")
    mode: Literal["avg", "max"] = Field(
        "avg", description="avg: mean cosine similarity over the listed fields the person has; "
                           "max: best single field. A field's own score is its best-matching "
                           "passage across the field's history entries.")
    threshold: float = Field(0.4, ge=-1, le=1,
                             description="Only return people whose aggregate score >= threshold")
    require_all_fields: bool = Field(
        False, description="Only consider people who have every listed field")


class SortSpec(BaseModel):
    field: str
    dir: Literal["asc", "desc"] = "asc"


class SearchRequest(BaseModel):
    tier: Literal["member", "plus_one"] | None = Field(None, description="Restrict to one tier")
    filters: list[FieldFilter] = Field(default_factory=list,
                                       description="Literal filters, combined with AND")
    semantic: SemanticQuery | None = None
    sort: Literal["score"] | SortSpec = Field(
        "score", description="'score' (semantic relevance, else name) or {field, dir}")
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 25
    page: Annotated[int, Field(ge=1)] = 1

    @field_validator("filters")
    @classmethod
    def _filter_fields(cls, v: list[FieldFilter]) -> list[FieldFilter]:
        for f in v:
            if f.field not in ("name",) and not fields.valid_key(f.field):
                raise ValueError(f"invalid field key {f.field!r}")
        return v


class SearchHit(BaseModel):
    id: str
    tier: str
    score: float | None = None
    field_scores: dict[str, float] = Field(default_factory=dict)
    person: dict[str, Any]


class SearchResponse(BaseModel):
    total: int
    page: int
    limit: int
    results: list[SearchHit]
    search_id: int | None = None


# --- literal filters → Mongo ------------------------------------------------------------------

def _ci(pattern: str) -> dict:
    return {"$regex": pattern, "$options": "i"}


def _num_or_str(value: Any, ftype: str) -> Any:
    if ftype == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def filter_to_mongo(f: FieldFilter, ftype: str = "text") -> dict:
    """Mongo condition for one filter, against the field's current value (`field.v`)."""
    v = f.value
    path = f"{f.field}.v"
    match f.op:
        case "exists":
            present = v is None or bool(v)
            return {path: {"$exists": True, "$nin": [None, "", []]}} if present else \
                {"$or": [{path: {"$exists": False}}, {path: {"$in": [None, "", []]}}]}
        case "eq" | "ne":
            if isinstance(v, str) and ftype != "number":
                cond = _ci(f"^{re.escape(v)}$")
            else:
                cond = _num_or_str(v, ftype)
            return {path: cond} if f.op == "eq" else {path: {"$not": cond} if
                                                         isinstance(cond, dict) else {"$ne": cond}}
        case "contains":
            return {path: _ci(re.escape(str(v or "")))}
        case "starts_with":
            return {path: _ci(f"^{re.escape(str(v or ''))}")}
        case "regex":
            try:
                re.compile(str(v))
            except re.error as e:
                raise ValueError(f"invalid regex for {f.field}: {e}") from e
            return {path: _ci(str(v))}
        case "in":
            values = v if isinstance(v, list) else [v]
            ors = [_ci(f"^{re.escape(x)}$") if isinstance(x, str) and ftype != "number"
                   else _num_or_str(x, ftype) for x in values]
            return {"$or": [{path: o} for o in ors]} if ors else {"_id": None}
        case "gt" | "gte" | "lt" | "lte":
            return {path: {f"${f.op}": _num_or_str(v, ftype)}}
    raise ValueError(f"unsupported op {f.op}")


async def mongo_query(req: SearchRequest) -> dict:
    types = {p.key: p.type for p in await properties.catalog()}
    clauses = [filter_to_mongo(f, types.get(f.field, "text")) for f in req.filters]
    if req.tier:
        clauses.append({"tier": req.tier})
    return {"$and": clauses} if clauses else {}


# --- semantic scoring ---------------------------------------------------------------------------

_SCORE_SQL = """
WITH per_field AS (
    SELECT mongo_id, field, max(1 - (embedding <=> CAST(:q AS halfvec))) AS sim
    FROM field_embeddings
    WHERE field = ANY(:fields)
      {tier_clause}
      {ids_clause}
    GROUP BY mongo_id, field
)
SELECT mongo_id,
       {agg}(sim) AS score,
       jsonb_object_agg(field, round(sim::numeric, 4)) AS field_scores,
       count(*) AS n
FROM per_field
GROUP BY mongo_id
HAVING {agg}(sim) >= :threshold
   {all_clause}
ORDER BY score DESC, mongo_id
"""


async def semantic_scores(sem: SemanticQuery, tier: str | None,
                          ids: list[str] | None) -> list[tuple[str, float, dict]]:
    vector = await embeddings.embed_query(sem.query)
    sql = _SCORE_SQL.format(
        agg="avg" if sem.mode == "avg" else "max",
        tier_clause="AND tier = :tier" if tier else "",
        ids_clause="AND mongo_id = ANY(:ids)" if ids is not None else "",
        all_clause="AND count(*) = :nfields" if sem.require_all_fields else "",
    )
    params: dict[str, Any] = {"q": str(vector), "fields": list(dict.fromkeys(sem.fields)),
                              "threshold": sem.threshold}
    if tier:
        params["tier"] = tier
    if ids is not None:
        params["ids"] = ids
    if sem.require_all_fields:
        params["nfields"] = len(params["fields"])
    async with db.engine().connect() as conn:
        rows = (await conn.execute(text(sql), params)).all()
    out = []
    for r in rows:
        fs = json.loads(r.field_scores) if isinstance(r.field_scores, str) else r.field_scores
        out.append((r.mongo_id, float(r.score), {k: float(v) for k, v in fs.items()}))
    return out


# --- orchestration ------------------------------------------------------------------------------

async def run_search(req: SearchRequest) -> SearchResponse:
    query = await mongo_query(req)
    skip = (req.page - 1) * req.limit

    if req.semantic is None:
        sort = [("name.v", 1)] if req.sort == "score" else \
            [(f"{req.sort.field}.v", 1 if req.sort.dir == "asc" else -1), ("_id", 1)]
        total = await people.count(query)
        docs = await people.find(query, sort=sort, skip=skip, limit=req.limit)
        return SearchResponse(total=total, page=req.page, limit=req.limit,
                              results=[SearchHit(id=str(d["_id"]), tier=d.get("tier", ""),
                                                 person=people.public(d)) for d in docs])

    ids = None
    if req.filters:
        ids = await people.find_ids(query)
    scored = await semantic_scores(req.semantic, req.tier, ids)
    by_id = {mid: (score, fs) for mid, score, fs in scored}
    total = len(scored)

    if req.sort == "score":
        page_ids = [mid for mid, _, _ in scored[skip:skip + req.limit]]
        docs = await people.fetch(page_ids)
        order = {mid: i for i, mid in enumerate(page_ids)}
        docs.sort(key=lambda d: order[str(d["_id"])])
    else:
        direction = 1 if req.sort.dir == "asc" else -1
        docs = await people.find({"_id": {"$in": [ObjectId(m) for m in by_id]}},
                                 sort=[(f"{req.sort.field}.v", direction), ("_id", 1)],
                                 skip=skip, limit=req.limit)

    results = []
    for d in docs:
        score, fs = by_id[str(d["_id"])]
        results.append(SearchHit(id=str(d["_id"]), tier=d.get("tier", ""), score=round(score, 4),
                                 field_scores=fs, person=people.public(d)))
    return SearchResponse(total=total, page=req.page, limit=req.limit, results=results)


async def record_search(principal: Principal, req: SearchRequest, resp: SearchResponse,
                        source: str) -> int:
    async with db.engine().begin() as conn:
        row = await conn.execute(db.searches.insert().values(
            user_id=principal.user_id, api_key_id=principal.api_key_id, source=source,
            params=req.model_dump(mode="json", exclude_none=True),
            result_ids=[r.id for r in resp.results], result_count=resp.total,
        ).returning(db.searches.c.id))
        return row.scalar_one()


def describe(req: SearchRequest) -> dict:
    """Compact summary of a search for the activity log."""
    out: dict[str, Any] = {}
    if req.tier:
        out["tier"] = req.tier
    if req.filters:
        out["filters"] = [f"{f.field} {f.op} {f.value!r}" for f in req.filters]
    if req.semantic:
        out["semantic"] = {"query": req.semantic.query, "fields": req.semantic.fields,
                           "mode": req.semantic.mode, "threshold": req.semantic.threshold}
    return out
