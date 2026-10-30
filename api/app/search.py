"""People search: literal per-property filters (Mongo) combined with per-property semantic
similarity (pgvector). The one engine: the same `SearchRequest` backs the REST endpoint, the UI,
the MCP tool and the planner agent, and `run_search` validates it against the property catalog."""

import json
import re
from typing import Annotated, Any, Literal

from bson import ObjectId
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text

from app import db, embeddings, fields, normalize, people, properties
from app.auth import Principal
from app.property_specs import TEXT_OPS

FilterOp = Literal["eq", "ne", "contains", "starts_with", "regex", "in", "exists",
                   "gt", "gte", "lt", "lte"]

MAX_LIMIT = 200


class FieldFilter(BaseModel):
    """A literal condition on one property. contains/starts_with/regex match the text as entered
    (case-insensitive; on lists, any element). On a typed property (number, date, boolean, enum,
    enum list) eq/ne/in and gt/gte/lt/lte compare the normalised value: pass a number, an ISO
    date, true/false or a canonical enum value (list_properties shows them). Text properties
    compare case-insensitively."""

    field: str = Field(description="Property key, e.g. 'name'. See list_properties.")
    op: FilterOp = Field("contains", description="Comparison operator; list_properties gives "
                                                 "each property's filter_ops")
    value: Any = Field(None, description="Value to compare. A list for 'in'; true/false for "
                                         "'exists'; a number or ISO date string for gt/gte/lt/lte.")


class FieldQuery(BaseModel):
    """A passage compared with one embeddable property."""

    field: str = Field(description="Embeddable property key, e.g. 'bio'")
    query: str = Field(min_length=1, max_length=2000, description="Passage to compare with it")


class SemanticQuery(BaseModel):
    """Similarity search against per-property embeddings: one passage for several properties
    (`query` + `fields`), a passage per property (`queries`), or both."""

    query: str | None = Field(None, min_length=1, max_length=2000,
                              description="Natural-language passage compared with every "
                                          "property in `fields`")
    fields: list[str] = Field(default_factory=list, max_length=20,
                              description="Embeddable property keys `query` is compared with, "
                                          "e.g. ['bio'] (see list_properties)")
    queries: list[FieldQuery] = Field(default_factory=list, max_length=20,
                                      description="A passage per property, e.g. "
                                                  "[{field: 'bio', query: 'payments founder'}]")
    mode: Literal["avg", "max"] = Field(
        "avg", description="avg: mean cosine similarity over the compared properties the person "
                           "has; max: best single property. A property's own score is its "
                           "best-matching passage across its history entries.")
    threshold: float = Field(0.4, ge=-1, le=1,
                             description="Only return people whose aggregate score >= threshold")
    require_all_fields: bool = Field(
        False, description="Only consider people who have every compared property")

    @model_validator(mode="after")
    def _some_passage(self) -> "SemanticQuery":
        if bool(self.query) != bool(self.fields):
            raise ValueError("semantic.query and semantic.fields go together")
        if not self.query and not self.queries:
            raise ValueError("semantic needs query + fields and/or queries")
        seen = set()
        for key in self.compared_fields():
            if key in seen:
                raise ValueError(f"property {key!r} is compared more than once")
            seen.add(key)
        return self

    def compared_fields(self) -> list[str]:
        return [*self.fields, *(q.field for q in self.queries)]

    def passages(self) -> list[tuple[str, str]]:
        """(property, passage) pairs, one per compared property."""
        return [*((f, self.query) for f in self.fields if self.query),
                *((q.field, q.query) for q in self.queries)]


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


_TEXT_ONLY = frozenset({"contains", "starts_with", "regex"})


def _typed(value: Any, prop: properties.Property, lookup: dict[str, str] | None) -> Any:
    """A filter value as stored in `field.norm.v`; ValueError when it cannot be normalised."""
    ptype = "enum" if prop.type == "enum_list" else prop.type
    parsed = normalize.parse(value, ptype, lookup)
    if parsed.value is None:
        hint = f"; one of {prop.values}" if prop.values else ""
        raise ValueError(f"{value!r} is not a {prop.type} value for {prop.key}{hint}")
    return parsed.value


def filter_to_mongo(f: FieldFilter, prop: properties.Property | None = None,
                    lookup: dict[str, str] | None = None) -> dict:
    """Mongo condition for one filter. Text operators (and `exists`) look at the text as entered
    (`field.v`); on a normalisable property the other operators compare the normalised value
    (`field.norm.v`) with the filter value normalised the same way. Without `prop` (a property
    not in the catalog) every operator works on the text."""
    v = f.value
    allowed = prop.filter_ops if prop else TEXT_OPS
    if f.op not in allowed:
        raise ValueError(f"operator {f.op!r} is not available for {f.field} "
                         f"({prop.type if prop else 'text'}); use one of {list(allowed)}")
    typed = prop is not None and prop.normalisable and f.op not in _TEXT_ONLY and f.op != "exists"
    text_path = f"{f.field}.v"
    path = f"{f.field}.norm.v" if typed else text_path
    match f.op:
        case "exists":
            present = v is None or bool(v)
            return {path: {"$exists": True, "$nin": [None, "", []]}} if present else \
                {"$or": [{path: {"$exists": False}}, {path: {"$in": [None, "", []]}}]}
        case "eq" | "ne":
            if typed:
                cond: Any = _typed(v, prop, lookup)
            else:
                cond = _ci(f"^{re.escape(v)}$") if isinstance(v, str) else v
            if f.op == "eq":
                return {path: cond}
            return {path: {"$not": cond} if isinstance(cond, dict) else {"$ne": cond}}
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
            if not values:
                return {"_id": None}
            if typed:
                return {path: {"$in": [_typed(x, prop, lookup) for x in values]}}
            ors = [_ci(f"^{re.escape(x)}$") if isinstance(x, str) else x for x in values]
            return {"$or": [{path: o} for o in ors]}
        case "gt" | "gte" | "lt" | "lte":
            return {path: {f"${f.op}": _typed(v, prop, lookup)}}
    raise ValueError(f"unsupported op {f.op}")


async def _lookup(prop: properties.Property | None) -> dict[str, str] | None:
    if prop is None or prop.type not in ("enum", "enum_list"):
        return None
    return await properties.enum_lookup(prop.key)


async def mongo_query(req: SearchRequest) -> dict:
    """The Mongo filter for a request's literal part; ValueError names an unknown property."""
    cat = {p.key: p for p in await properties.catalog()}
    clauses = []
    for f in req.filters:
        if f.field not in cat:
            raise ValueError(f"unknown property {f.field!r} in filters (see the catalog)")
        clauses.append(filter_to_mongo(f, cat[f.field], await _lookup(cat[f.field])))
    if req.tier:
        clauses.append({"tier": req.tier})
    return {"$and": clauses} if clauses else {}


def sort_path(key: str, cat: dict[str, properties.Property]) -> str:
    """Normalisable properties sort by their normalised value, the rest by the text."""
    prop = cat.get(key)
    return f"{key}.norm.v" if prop and prop.normalisable else f"{key}.v"


# --- semantic scoring ---------------------------------------------------------------------------

# Each compared property has its own passage vector (`q`); a property's score is its best passage
# across the person's history entries; the person's score aggregates over the compared properties
# they have.
_SCORE_SQL = """
WITH q(field, qvec) AS (VALUES {values}),
per_field AS (
    SELECT fe.mongo_id, fe.field, max(1 - (fe.embedding <=> q.qvec)) AS sim
    FROM field_embeddings fe
    JOIN q ON q.field = fe.field
    WHERE TRUE
      {tier_clause}
      {ids_clause}
    GROUP BY fe.mongo_id, fe.field
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
    pairs = sem.passages()
    vectors = await embeddings.embed_queries([p for _, p in pairs])
    params: dict[str, Any] = {"threshold": sem.threshold}
    values = []
    for i, ((field, _), vec) in enumerate(zip(pairs, vectors, strict=True)):
        params[f"f{i}"], params[f"v{i}"] = field, str(vec)
        values.append(f"(CAST(:f{i} AS text), CAST(:v{i} AS halfvec))")
    sql = _SCORE_SQL.format(
        values=", ".join(values),
        agg="avg" if sem.mode == "avg" else "max",
        tier_clause="AND fe.tier = :tier" if tier else "",
        ids_clause="AND fe.mongo_id = ANY(:ids)" if ids is not None else "",
        all_clause="AND count(*) = :nfields" if sem.require_all_fields else "",
    )
    if tier:
        params["tier"] = tier
    if ids is not None:
        params["ids"] = ids
    if sem.require_all_fields:
        params["nfields"] = len(pairs)
    async with db.engine().connect() as conn:
        rows = (await conn.execute(text(sql), params)).all()
    out = []
    for r in rows:
        fs = json.loads(r.field_scores) if isinstance(r.field_scores, str) else r.field_scores
        out.append((r.mongo_id, float(r.score), {k: float(v) for k, v in fs.items()}))
    return out


# --- orchestration ------------------------------------------------------------------------------

async def validate(req: SearchRequest, cat: dict[str, properties.Property]) -> None:
    """Everything the request names must be in the catalog: filter and sort properties exist,
    compared properties are embeddable. Filter operators and values are checked in mongo_query."""
    if req.semantic is not None:
        for key in req.semantic.compared_fields():
            prop = cat.get(key)
            if prop is None:
                raise ValueError(f"unknown property {key!r} in semantic (see the catalog)")
            if not prop.embeddable:
                raise ValueError(f"property {key!r} is {prop.type}, not embeddable; semantic "
                                 "search compares text and list properties")
    if req.sort != "score" and req.sort.field not in cat:
        raise ValueError(f"unknown property {req.sort.field!r} in sort")


async def run_search(req: SearchRequest) -> SearchResponse:
    cat = {p.key: p for p in await properties.catalog()}
    await validate(req, cat)
    query = await mongo_query(req)
    skip = (req.page - 1) * req.limit

    if req.semantic is None:
        sort = [("name.v", 1)] if req.sort == "score" else \
            [(sort_path(req.sort.field, cat), 1 if req.sort.dir == "asc" else -1), ("_id", 1)]
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
                                 sort=[(sort_path(req.sort.field, cat), direction), ("_id", 1)],
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
        sem = req.semantic
        out["semantic"] = {"mode": sem.mode, "threshold": sem.threshold}
        if sem.query:
            out["semantic"].update(query=sem.query, fields=sem.fields)
        if sem.queries:
            out["semantic"]["queries"] = [f"{q.field}: {q.query}" for q in sem.queries]
    return out
