"""Tool (level 2): repair a record's identity data from the facts on the record. Not enrichment.

Mechanical fixes first (mojibake re-decoded, an all-lowercase name title-cased), then the repair
agent (defined here) judges the record: what it is (`record_kind`: Person, Company,
Unidentifiable, or a kind it allocates), whether the name is a real first and last name and, when
it is broken, the full name the record's own evidence gives verbatim; and values filed under the
wrong property. When the evidence yields no full name and the record has a LinkedIn URL, the
FullEnrich profile behind it is asked for the name only. A Person whose name stays partial or
broken is Unidentifiable. Every fix is an appended history entry; nothing is deleted."""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import fields, history, people, properties
from app.agents.common import run_agent
from app.auth import Principal
from app.history import Change, Source
from app.integrations import fullenrich
from app.integrations.firecrawl import AsyncFirecrawl, FirecrawlError, NotConfigured, NotFound
from app.tools import ToolResult
from app.urls import linkedin_profile

log = logging.getLogger(__name__)

AGENT_NAME = "repair"
REQUEST_LIMIT = 4
KIND = "record_kind"
PERSON, COMPANY, UNIDENTIFIABLE = "Person", "Company", "Unidentifiable"
MAX_NAME_CHARS = 80
TEXT_LIKE = ("text", "keyword", "url", "contact", "list")
EVIDENCE_KEYS = (("linkedin", 200), ("email", 200), ("emails", 200), ("github", 200),
                 ("website", 200), ("company", 200), ("role", 200), ("linkedin_headline", 300),
                 ("one_liner", 300), ("bio", 400), ("intro_bio", 300), ("notes", 200),
                 ("entity_type", 100), ("location", 100))
_COMPANY_WORDS = re.compile(r"\b(inc|llc|ltd|ventures?|capital|labs?|studio|partners|holdings|"
                            r"group|technologies|fund|gmbh|corp|company|co)\b", re.IGNORECASE)
_HANDLE = re.compile(r"@\w")
_URLISH = re.compile(r"https?:|www\.|\.(com|io|ai|co|org|net)\b", re.IGNORECASE)
_EMAILISH = re.compile(r"^[^@\s]+@[^@\s]+\.\w+$")
# UTF-8 bytes read as Latin-1 show up as A-tilde / A-circumflex followed by a C1/Latin-1 char.
_MOJIBAKE = re.compile(r"[\u00c3\u00c2][\u0080-\u00bf]")   # regex escapes, kept raw on purpose
_SYMBOLS = re.compile(r"[^\w\s.'’\-(),&/]", re.UNICODE)


class RepairOptions(BaseModel):
    mechanical: bool = Field(True, description="Re-decode mojibake, title-case lowercase names")
    agent: bool = Field(True, description="Let the repair agent judge the record")
    provider: bool = Field(True, description="Ask FullEnrich (5 credits) for the full name when "
                                             "the record has a LinkedIn URL and no full name")
    max_edits: int = Field(3, ge=0, le=10, description="Misfiled values moved or removed per call")
    allocate: bool = Field(True, description="Let the agent add a new record kind")


NameSource = Literal["evidence", "provider", "unchanged"]


class RepairResult(ToolResult):
    kind: str | None = None
    name_source: NameSource | None = None
    signals: list[str] = Field(default_factory=list, description="What looked wrong beforehand")
    mechanical: list[str] = Field(default_factory=list, description="Mechanical fixes applied")
    agent_called: bool = False
    provider_called: bool = False
    credits: int = 0
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
        ("handle in name", _HANDLE.search(name)),
        ("email as name", _EMAILISH.match(name.strip())),
        ("url in name", _URLISH.search(name)),
        ("company words in name", _COMPANY_WORDS.search(name)),
        ("mojibake", _MOJIBAKE.search(name)),
        ("symbols in name", _SYMBOLS.search(name)),
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
    identifying = ("linkedin", "email", "emails", "bio", "one_liner", "intro_bio", "website",
                   "company", "linkedin_headline")
    if not any(_cur(doc, k) for k in identifying):
        out.append("nothing identifying besides the name")
    return out


def demojibake(text: str) -> str:
    """UTF-8 that was read as Latin-1 back to what it was (Nin~o -> Niño style damage)."""
    if not _MOJIBAKE.search(text):
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return fixed if len(_MOJIBAKE.findall(fixed)) < len(_MOJIBAKE.findall(text)) else text


def title_case(name: str) -> str:
    """"elizabeth hsu" -> "Elizabeth Hsu"; only for names that are entirely lowercase and look
    like names (no handle, email, URL or digits: those are for the agent)."""
    if not name.islower() or _HANDLE.search(name) or _URLISH.search(name) or re.search(r"\d", name):
        return name
    return re.sub(r"(^|[\s\-'’])(\w)", lambda m: m.group(1) + m.group(2).upper(), name)


def mechanical_fixes(doc: dict) -> dict[str, str]:
    """{key: fixed text} for the mechanical repairs a document needs."""
    out: dict[str, str] = {}
    for key in ("name", "company", "location", "bio"):
        value = _cur(doc, key)
        if isinstance(value, str) and value != (fixed := demojibake(value)):
            out[key] = fixed
    name = out.get("name", _cur(doc, "name"))
    if isinstance(name, str) and name.strip() and name != (cased := title_case(name)):
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
    name_evidence: str = Field("", max_length=200,
                               description="Where the name comes from, in a few words")
    name_complete: bool = Field(description="After your answer, is the name a first and a last "
                                            "name (or, for a company, the company's name)?")
    misfiled: list[Misfiled] = Field(default_factory=list, max_length=5)
    confidence: Confidence = "medium"
    note: str = Field("", max_length=200)


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

Decide the kind: Person for a human; Company for a firm, fund or organisation recorded as if it
were a person (its name is the company's and counts as complete); Unidentifiable when nothing on
the record says who or what this is; another kind only when it clearly fits and the list lacks it.

The name must be a first and last name. Propose a new name only when the current one is broken
(a handle, an email, a company, a role or company suffix such as "CEO MyBot" or "_Founder of X",
a parenthetical) and the evidence gives the full name verbatim: the first and last name inside
the current name itself ("Andres Niño CEO MyBot" → "Andres Niño"), a bio that opens with it, an
intro, an email local part such as "polly.israni", a LinkedIn slug such as "brad-topliff". Never
guess or complete a surname; an initial is not a surname. When the name is just a first name,
leave it and say name_complete is false; something else will ask the LinkedIn profile. Mojibake
and casing are already fixed; do not re-propose the current name unchanged.

Misfiled: a value that plainly belongs to another property (prose in website, a URL in company):
name the property and where to move it (notes when unsure), at most a few.""",
)


def _tokens(name: str) -> list[str]:
    return fields.fold(name).split()


def is_full_name(name: str) -> bool:
    """A first and a last name: two or more tokens, the last one a word of two or more letters
    (so "Polly I." and "Ada O" do not count)."""
    toks = _tokens(name)
    return len(toks) >= 2 and len(toks[-1]) >= 2 and not toks[-1].isdigit()


@agent.output_validator
async def _grounded(ctx: RunContext[RepairDeps], v: Verdict) -> Verdict:
    problems = []
    kind = " ".join(v.kind.split())
    if not fields.fold(kind) or len(kind) > properties.MAX_ENUM_VALUE_CHARS:
        problems.append("kind must be a short label")
    if v.name is not None:
        name = " ".join(v.name.split())
        toks = _tokens(name)
        evidence_tokens = set(ctx.deps.evidence.split())
        if not is_full_name(name) or len(name) > MAX_NAME_CHARS:
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
    for key, limit in EVIDENCE_KEYS:
        value = _cur(doc, key)
        if value in (None, "", []):
            continue
        text = fields.text_of(value)[:limit]
        lines.append(f"{key}: {text}")
        folded.append(fields.fold(text))
        if key in ("email", "emails"):
            for addr in re.findall(r"[^\s;,@]+@", text):
                folded.append(fields.fold(re.sub(r"[._\-]+", " ", addr[:-1])))
    src = (doc.get("source") or {}).get("file")
    if src:
        lines.append(f"source file: {src}")
    return "\n".join(lines) or "(nothing else on the record)", " ".join(folded)


async def _judge(doc: dict, sigs: list[str], opts: RepairOptions) -> Verdict:
    block, folded = _evidence(doc)
    cat = {p.key: p.type for p in await properties.catalog()}
    present = {k: cat[k] for k in doc if k in cat and _cur(doc, k) not in (None, "", [])}
    kinds = await properties.enum_values(KIND)
    prompt = (f"Name: {_cur(doc, 'name') or '(none)'}\n"
              f"Current record kind: {_cur(doc, KIND) or '(not set)'}\n"
              f"Kinds: {', '.join(kinds)}\n"
              f"Hints: {'; '.join(sigs) or 'nothing flagged'}\n\n"
              f"Evidence:\n{block}")
    deps = RepairDeps(current_name=str(_cur(doc, "name") or ""), evidence=folded, present=present,
                      catalog=cat, max_edits=opts.max_edits, kinds=kinds)
    return await run_agent(agent, prompt, deps, request_limit=REQUEST_LIMIT)


async def _provider_name(doc: dict, firecrawl: AsyncFirecrawl,
                         result: RepairResult) -> str | None:
    """The full name from the FullEnrich profile behind the record's LinkedIn URL, or None."""
    li = _cur(doc, "linkedin")
    url = linkedin_profile(li) if isinstance(li, str) else None
    if not url:
        return None
    before = firecrawl.budget.credits
    profile = None
    try:
        profile = await fullenrich.match(firecrawl, url)
    except NotFound:
        pass
    except NotConfigured as e:
        result.note = f"{result.note} provider skipped: {e}".strip()
        return None
    except FirecrawlError as e:
        result.note = f"{result.note} provider failed: {e}".strip()
        return None
    finally:
        result.provider_called = True
        result.credits += firecrawl.budget.credits - before
    name = " ".join((profile.full_name or "").split()) if profile else ""
    return name if is_full_name(name) else None


# --- the tool -----------------------------------------------------------------------------------------

async def repair(person_id: str, principal: Principal | None, source: Source,
                 opts: RepairOptions | None = None,
                 firecrawl: AsyncFirecrawl | None = None) -> RepairResult | None:
    """Repair one record's identity data. None if the person does not exist."""
    opts = opts or RepairOptions()
    docs = await people.fetch([person_id])
    if not docs:
        return None
    doc = docs[0]
    result = RepairResult(person_id=person_id, signals=signals(doc))

    if opts.mechanical and (fixes := mechanical_fixes(doc)):
        write = await people.update(principal, person_id, fixes, source=Source("system", "repair"))
        if write is not None:
            result.changes += write.changes
            result.mechanical = sorted(fixes)
            doc = (await people.fetch([person_id]))[0]
    if not opts.agent:
        return result

    result.agent_called = True
    verdict = await _judge(doc, result.signals, opts)
    result.confidence, result.note = verdict.confidence, verdict.note
    current_name = str(_cur(doc, "name") or "")
    name: str | None = None
    name_source: NameSource = "unchanged"
    if verdict.name and verdict.confidence != "low":
        name, name_source = verdict.name, "evidence"
    elif verdict.name:
        result.rejected.append(f"name {verdict.name!r} (low confidence)")
    complete = verdict.name_complete or name is not None
    kind_proposed = verdict.kind
    if fields.fold(kind_proposed) == fields.fold(PERSON) and not complete:
        if opts.provider and firecrawl is not None:
            provider_name = await _provider_name(doc, firecrawl, result)
            if provider_name and fields.fold(provider_name) != fields.fold(current_name):
                name, name_source, complete = provider_name, "provider", True
            elif provider_name:
                complete = True
        if not complete:
            kind_proposed = UNIDENTIFIABLE
    result.name_source = name_source

    kind, allocated = await properties.resolve_enum_value(
        KIND, kind_proposed, opts.allocate, src=f"agent:{AGENT_NAME} {source.ref or ''}".strip())
    if kind is None:
        result.rejected.append(f"kind {kind_proposed!r} (allocation off)")
        kind = PERSON if complete else UNIDENTIFIABLE
    if allocated:
        result.changes.append(Change(key=KIND, action="enum_value_added", value=kind))
    result.kind = kind

    values: dict[str, Any] = {}
    if _cur(doc, KIND) != kind:
        values[KIND] = kind
    for m in verdict.misfiled[:opts.max_edits]:
        text = fields.text_of(_cur(doc, m.key))
        if not text or m.key in values:
            continue
        if m.move_to:
            target = _cur(doc, m.move_to)
            values[m.move_to] = f"{fields.text_of(target)}\n{text}" if target else text
        values[m.key] = None
    if name and name_source == "provider":
        write = await people.update(principal, person_id, {"name": name},
                                    source=Source("agent", f"{AGENT_NAME}/fullenrich", source.ref))
        if write is not None:
            result.changes += write.changes
    elif name:
        values["name"] = name
    if values:
        write = await people.update(principal, person_id, values, source=source)
        if write is not None:
            result.changes += write.changes
    return result
