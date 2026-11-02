"""Tool (level 2): repair a record's identity data from the facts on the record. Not enrichment.

Mechanical fixes first (mojibake re-decoded, an all-lowercase name title-cased), then the repair
agent (defined here) judges the record: what it is (`record_kind`: Person, Company,
Unidentifiable, or a kind it allocates), whether the name is a real first and last name and, when
it is broken, the full name the record's own evidence gives verbatim; and values filed under the
wrong property. It never calls a data provider: a record whose name the evidence cannot complete
is enriched by the normalise assistant (the enrich ladder) before repair runs. The kind is common
sense: a human name, even a first name alone, is a Person; Unidentifiable is for records with
nothing identifying on them. Every fix is an appended history entry; nothing is deleted."""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import fields, history, names, properties, property_specs
from app.agents.common import run_agent
from app.history import Change, Source
from app.names import demojibake, is_full_name, title_case
from app.tools import Emit, ToolContext, ToolResult, silent
from app.urls import linkedin_profile

AGENT_NAME = "repair"
REQUEST_LIMIT = 4
KIND = "record_kind"
PERSON, COMPANY, UNIDENTIFIABLE = "Person", "Company", "Unidentifiable"
TEXT_LIKE = ("text", "keyword", "url", "contact", "list")
EVIDENCE_KEYS = property_specs.DESCRIBES_PERSON
EVIDENCE_CLIP = property_specs.DESCRIBE_CLIP


class RepairOptions(BaseModel):
    mechanical: bool = Field(True, description="Re-decode mojibake, title-case lowercase names")
    agent: bool = Field(True, description="Let the repair agent judge the record")
    max_edits: int = Field(3, ge=0, le=10, description="Misfiled values moved or removed per call")
    allocate: bool = Field(True, description="Let the agent add a new record kind")


NameSource = Literal["evidence", "unchanged"]


class RepairResult(ToolResult):
    kind: str | None = None
    name_source: NameSource | None = None
    signals: list[str] = Field(default_factory=list, description="What looked wrong beforehand")
    mechanical: list[str] = Field(default_factory=list, description="Mechanical fixes applied")
    agent_called: bool = False
    confidence: str | None = None
    note: str = ""
    rejected: list[str] = Field(default_factory=list,
                                description="Agent proposals the caps or rules did not allow")


# --- signals and mechanical fixes (pure) -----------------------------------------------------------

def _cur(doc: dict, key: str) -> Any:
    return history.current(doc.get(key))


def _slug(doc: dict) -> str | None:
    li = _cur(doc, "linkedin")
    url = linkedin_profile(li) if isinstance(li, str) else None
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url or "", re.IGNORECASE)
    return m.group(1) if m else None


def signals(doc: dict) -> list[str]:
    """What looks wrong with a record's identity: hints for the agent and for the result."""
    name = str(_cur(doc, "name") or "")
    out = []
    if not name.strip():
        out.append("no name")
        return out
    tokens = name.split()
    checks = [
        ("digits in name", re.search(r"\d", name)),
        ("handle in name", names.HANDLE.search(name)),
        ("email as name", names.EMAILISH.match(name.strip())),
        ("url in name", names.URLISH.search(name)),
        ("company words in name", names.COMPANY_WORDS.search(name)),
        ("mojibake", names.MOJIBAKE.search(name)),
        ("symbols in name", names.SYMBOLS.search(name)),
        ("five or more tokens", len(tokens) >= 5),
        ("single token", len(tokens) == 1),
        ("very short", len(name.strip()) <= 2),
        ("lowercase", name.islower()),
    ]
    out += [label for label, hit in checks if hit]
    slug = _slug(doc)
    if slug:
        flat = re.sub(r"[^a-z0-9]", "", slug.lower())
        if not any(t in flat for t in fields.fold(name).split() if len(t) >= 3):
            out.append("name absent from linkedin slug")
    if not any(_cur(doc, k) for k in property_specs.DESCRIBES_PERSON if k != "notes"):
        out.append("nothing identifying besides the name")
    return out


def mechanical_fixes(doc: dict) -> dict[str, str]:
    """{key: fixed text} for the mechanical repairs a document needs."""
    out: dict[str, str] = {}
    for key in ("name", "company", "location", "bio"):
        value = _cur(doc, key)
        if isinstance(value, str) and value != (fixed := demojibake(value)):
            out[key] = fixed
    name = out.get("name", _cur(doc, "name"))
    if isinstance(name, str) and name.strip() and not names.looks_broken(name) \
            and name != (cased := title_case(name)):
        out["name"] = cased
    return out


# --- the repair agent ------------------------------------------------------------------------------

Confidence = Literal["high", "medium", "low"]


class Misfiled(BaseModel):
    key: str = Field(description="The property holding a value that belongs elsewhere")
    move_to: str | None = Field(None, description="The text property to append it to (notes "
                                                  "when unsure), or null to just remove it")


class Verdict(BaseModel):
    kind: str = Field(description="Person, Company, Unidentifiable, or a new kind that fits")
    name: str | None = Field(None, description="The person's first and last name when the "
                                               "current name is broken and the evidence gives "
                                               "the full name verbatim; otherwise null")
    name_evidence: str = Field("", description="Where the name comes from, in a few words")
    name_complete: bool = Field(description="After your answer, is the name a first and a last "
                                            "name (or, for a company, the company's name)?")
    misfiled: list[Misfiled] = Field(default_factory=list, max_length=5)
    confidence: Confidence = "medium"
    note: str = ""


@dataclass
class RepairDeps:
    current_name: str
    evidence: str                       # folded text a proposed name must come from
    present: dict[str, str]             # property key -> type, for what the person has
    catalog: dict[str, str]             # property key -> type
    max_edits: int
    kinds: list[str] = field(default_factory=list)


agent = Agent(
    name=f"agents.{AGENT_NAME}",
    deps_type=RepairDeps,
    output_type=Verdict,
    retries=2,
    instructions="""\
You check one record of a people database for broken identity data and repair only what the
record's own facts establish. You see the current name and its earlier values, hints about what
looks wrong, the evidence on the record (LinkedIn URL and slug, emails, website, company, role,
headline, bio, intro, notes, entity type, source file) and the kinds of record.

Decide the kind by common sense. Person for a human: a human name is enough, even a first name
alone ("Vlad", "Jack M."), as is a headline, bio or job history that describes a person. Company
for a firm, fund or organisation recorded as if it were a person (its name is the company's and
counts as complete). Unidentifiable only when nothing on the record names a human or an
organisation: an empty, random or generic string ("E", "Click", "Store eo39uo") with no telling
evidence. Another kind only when it clearly fits and the list lacks it.

A proposed name must be a first and last name. Propose one when the current name is broken (a
handle, an email, a company, a role or company suffix such as "CEO MyBot" or "_Founder of X", a
parenthetical) or incomplete (a first name only) and the evidence gives the full name verbatim:
the first and last name inside the current name itself ("Andres Niño CEO MyBot" → "Andres Niño"),
a headline or about text that states it ("I'm Jack Mathews, …"), an intro, an email local part
such as "polly.israni", a LinkedIn slug such as "brad-topliff". Never guess or complete a surname;
an initial is not a surname. When the evidence gives no full name, leave the name and say
name_complete is false. Mojibake and casing are already fixed; do not re-propose the current name
unchanged.

Misfiled: a value that plainly belongs to another property (prose in website, a URL in company):
name the property and where to move it (notes when unsure), at most a few.""",
)


@agent.output_validator
async def _grounded(ctx: RunContext[RepairDeps], v: Verdict) -> Verdict:
    problems = []
    kind = " ".join(v.kind.split())
    if not fields.fold(kind) or len(kind) > properties.MAX_ENUM_VALUE_CHARS:
        problems.append("kind must be a short label")
    if v.name is not None:
        name = " ".join(v.name.split())
        toks = names.tokens(name)
        evidence_tokens = set(ctx.deps.evidence.split())
        if not is_full_name(name):
            problems.append("a proposed name needs a first and a last name (or null)")
        elif fields.fold(name) == fields.fold(ctx.deps.current_name):
            problems.append("the proposed name is the current one; answer null")
        elif not all(t in evidence_tokens for t in toks):
            problems.append(f"{name!r} is not in the evidence verbatim; propose only names the "
                            "record gives, or null")
        v = v.model_copy(update={"name": name})
    if len(v.misfiled) > ctx.deps.max_edits:
        problems.append(f"at most {ctx.deps.max_edits} misfiled values")
    for m in v.misfiled:
        if m.key not in ctx.deps.present:
            problems.append(f"misfiled {m.key!r}: the person has no such value")
        elif ctx.deps.present[m.key] not in TEXT_LIKE or m.key == "name":
            problems.append(f"misfiled {m.key!r}: only text-like properties can be moved")
        if m.move_to is not None and ctx.deps.catalog.get(m.move_to) not in TEXT_LIKE:
            problems.append(f"move_to {m.move_to!r} must be a text-like property or null")
        if m.move_to == m.key:
            problems.append(f"misfiled {m.key!r}: move_to must differ")
    if problems:
        raise ModelRetry("; ".join(problems))
    return v


def _evidence(doc: dict) -> tuple[str, str]:
    """(readable evidence block for the agent, folded evidence text for the validator)."""
    lines, folded = [], []
    name = str(_cur(doc, "name") or "")
    earlier = [str(e["v"]) for e in history.entries(doc.get("name"))
               if e.get("v") not in (None, name)]
    folded.append(fields.fold(name))
    if earlier:
        lines.append(f"earlier names: {'; '.join(earlier[-3:])}")
        folded += [fields.fold(e) for e in earlier]
    slug = _slug(doc)
    if slug:
        lines.append(f"linkedin slug: {slug}")
        folded.append(fields.fold(slug.replace("-", " ").replace("_", " ")))
    for key in EVIDENCE_KEYS:
        value = _cur(doc, key)
        if value in (None, "", []):
            continue
        text = fields.text_of(value)[:EVIDENCE_CLIP.get(key, fields.MAX_TEXT_CHARS)]
        lines.append(f"{key}: {text}")
        folded.append(fields.fold(text))
        if key in ("email", "emails"):
            for addr in re.findall(r"[^\s;,@]+@", text):
                folded.append(fields.fold(re.sub(r"[._\-]+", " ", addr[:-1])))
    src = (doc.get("source") or {}).get("file")
    if src:
        lines.append(f"source file: {src}")
    return "\n".join(lines) or "(nothing else on the record)", " ".join(folded)


async def _judge(doc: dict, sigs: list[str], opts: RepairOptions, emit: Emit = silent) -> Verdict:
    block, folded = _evidence(doc)
    cat = {p.key: p.type for p in await properties.catalog()}
    present = {k: cat[k] for k in doc if k in cat and _cur(doc, k) not in (None, "", [])}
    kinds = await properties.enum_values(KIND)
    current_kind = _cur(doc, KIND)
    kind_note = (" (set earlier, while the name was incomplete; judge afresh)"
                 if current_kind == UNIDENTIFIABLE and is_full_name(str(_cur(doc, "name") or ""))
                 else "")
    prompt = (f"Name: {_cur(doc, 'name') or '(none)'}\n"
              f"Current record kind: {current_kind or '(not set)'}{kind_note}\n"
              f"Kinds: {', '.join(kinds)}\n"
              f"Hints: {'; '.join(sigs) or 'nothing flagged'}\n\n"
              f"Evidence:\n{block}")
    deps = RepairDeps(current_name=str(_cur(doc, "name") or ""), evidence=folded, present=present,
                      catalog=cat, max_edits=opts.max_edits, kinds=kinds)
    return await run_agent(agent, prompt, deps, request_limit=REQUEST_LIMIT, on_event=emit)


# --- the tool -----------------------------------------------------------------------------------------

async def repair(ctx: ToolContext, opts: RepairOptions | None = None) -> RepairResult:
    """Repair one record's identity data, staged on the context's transaction."""
    opts = opts or RepairOptions()
    doc = ctx.tx.view
    result = RepairResult(person_id=ctx.person_id, signals=signals(doc))
    if result.signals:
        ctx.emit("step", "looks wrong: " + "; ".join(result.signals), signals=result.signals)

    if opts.mechanical and (fixes := mechanical_fixes(doc)):
        await ctx.stage(fixes, src=Source("system", "repair"), derived=fixes)
        result.mechanical = sorted(fixes)
        ctx.emit("write", "mechanical fixes: " + ", ".join(result.mechanical), keys=result.mechanical)
        doc = ctx.tx.view
    if not opts.agent:
        result.changes = ctx.tx.changes()
        return result

    current_name = str(_cur(doc, "name") or "")
    li = _cur(doc, "linkedin")
    if _cur(doc, KIND) is None and not result.signals and is_full_name(current_name) \
            and isinstance(li, str) and linkedin_profile(li):
        # the deterministic case, no model call: a first-and-last name behind a LinkedIn profile
        # URL with nothing looking wrong identifies a person (an import of thousands of such rows
        # must not consult the agent for each)
        await ctx.stage({KIND: PERSON})
        result.kind, result.name_source = PERSON, "unchanged"
        result.note = "A full name and a LinkedIn profile identify a person."
        result.changes = ctx.tx.changes()
        ctx.emit("step", f"record kind: {PERSON} (full name behind a LinkedIn profile)",
                 record_kind=PERSON)
        return result

    result.agent_called = True
    verdict = await _judge(doc, result.signals, opts, ctx.emit)
    result.confidence, result.note = verdict.confidence, verdict.note
    name: str | None = None
    name_source: NameSource = "unchanged"
    if verdict.name and verdict.confidence != "low":
        name, name_source = verdict.name, "evidence"
    elif verdict.name:
        result.rejected.append(f"name {verdict.name!r} (low confidence)")
    kind_proposed = verdict.kind
    overruled = False
    if fields.fold(kind_proposed) == fields.fold(UNIDENTIFIABLE) and is_full_name(current_name) \
            and isinstance(li, str) and linkedin_profile(li):
        # a first-and-last name behind a LinkedIn profile URL identifies a person, whatever the
        # agent made of an earlier Unidentifiable mark: a rule's correction, so it supersedes
        kind_proposed, overruled = PERSON, True
        result.note = f"{result.note} A full name and a LinkedIn profile identify a person.".strip()
    result.name_source = name_source

    kind, allocated = await properties.resolve_enum_value(
        KIND, kind_proposed, opts.allocate, src=f"agent:{AGENT_NAME} {ctx.source.ref or ''}".strip())
    if kind is None:
        result.rejected.append(f"kind {kind_proposed!r} (allocation off)")
        kind = PERSON if names.tokens(current_name) else UNIDENTIFIABLE
    if allocated:
        ctx.tx.record_catalog(Change(key=KIND, action="enum_value_added", value=kind))
        ctx.emit("note", f"new record kind {kind!r}", value=kind)
    result.kind = kind

    values: dict[str, Any] = {}
    moves: dict[str, str] = {}
    if _cur(doc, KIND) != kind:
        values[KIND] = kind
    for m in verdict.misfiled[:opts.max_edits]:
        text = fields.text_of(_cur(doc, m.key))
        if not text or m.key in values:
            continue
        if m.move_to:
            moves[m.move_to] = f"{moves[m.move_to]}\n{text}" if m.move_to in moves else text
        values[m.key] = None
        ctx.emit("note", f"{m.key} misfiled: " + (f"moved to {m.move_to}" if m.move_to else "removed"),
                 key=m.key, move_to=m.move_to)
    if name:
        values["name"] = name
    if values:          # corrections of broken data supersede what is shown; the kind competes unless
        await ctx.stage(values, derived=[k for k in values if k != KIND or overruled])   # a rule set it
    if moves:
        await ctx.stage(moves, append=set(moves))      # the text joins the target as new lines
    result.changes = ctx.tx.changes()
    ctx.emit("step", f"record kind: {kind}" + (f"; name → {name} ({name_source})" if name else ""),
             record_kind=kind, name=name, name_source=name_source)
    return result
