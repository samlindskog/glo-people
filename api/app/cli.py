"""Admin commands: python -m app.cli {create-user,set-password,import,embed,properties,classify,
normalize,migrate-history}. import, embed and normalize run as assistant runs executed in this
process (visible on the Assistants page like any other run)."""

import argparse
import asyncio
import getpass
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app import (
    assistants,
    auth,
    db,
    embeddings,
    fields,
    mongo,
    observability,
    people,
    properties,
    wiring,
)
from app.assistants import embed as embed_assistant
from app.assistants import merge, reparse, runner
from app.assistants.base import REGISTRY
from app.assistants.embed import EmbedOptions
from app.assistants.merge import MergeOptions
from app.assistants.reparse import ReparseOptions
from app.permissions import is_role, roles


async def create_user(username: str, role: str, password: str | None) -> None:
    if not is_role(role):
        sys.exit(f"unknown role {role!r}; choose from {sorted(roles())}")
    password = password or _prompt_password()
    async with db.engine().begin() as conn:
        exists = (await conn.execute(select(db.users.c.id).where(db.users.c.username == username))
                  ).first()
        if exists:
            sys.exit(f"user {username!r} already exists (use set-password)")
        await conn.execute(db.users.insert().values(
            username=username, password_hash=auth.hash_password(password), role=role))
    print(f"created {username} ({role})")


async def set_password(username: str, password: str | None) -> None:
    password = password or _prompt_password()
    async with db.engine().begin() as conn:
        res = await conn.execute(update(db.users).where(db.users.c.username == username)
                                 .values(password_hash=auth.hash_password(password)))
    if res.rowcount == 0:
        sys.exit(f"no user {username!r}")
    print(f"password updated for {username}")


def _prompt_password() -> str:
    pw = getpass.getpass("Password (min 10 chars): ")
    if len(pw) < 10:
        sys.exit("password too short")
    if getpass.getpass("Repeat: ") != pw:
        sys.exit("passwords do not match")
    return pw


def _show(msg: dict) -> None:
    label = f" [{msg['item_id']}]" if msg.get("item_id") else ""
    print(f"  {msg['kind']:8}{label} {msg['message']}")


async def _run(spec_key: str, items: list, params: dict, label: str) -> dict:
    """Create a run and execute it in this process, printing its activity."""
    observability.setup()
    wiring.register_write_hooks()      # this process writes: its writes must embed too
    assistants.load_all()
    run = await runner.create_run(REGISTRY[spec_key], None, items, params, label=label)
    print(f"run {run['id']} ({spec_key}): {run['total']} item(s)")
    run = await runner.run_to_completion(run["id"], runner.worker_id("cli"), on_event=_show)
    print(f"run {run['id']}: {run['status']}, {run['done']} done, {run['failed']} failed")
    return run


async def import_file(path: str, tier: str, *, dry_run: bool, agent: bool, review: bool,
                      columns: dict[str, str] | None = None) -> None:
    """Merge a CSV's rows into the people database (a merge run executed here)."""
    await mongo.ensure_indexes()
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    options = MergeOptions(tier=tier, dry_run=dry_run, agent=agent, review=review)
    parsed, created, items = await merge.prepare_csv(text, Path(path).name, dry_run=dry_run,
                                                     columns=columns)
    mapped = [c for c in parsed.columns if c.get("alias") or c["role"] != "field"]
    if mapped:
        print("columns: " + ", ".join(f"{c['header']} -> {c['key'] if c['role'] == 'field' else c['role']}"
                                      for c in mapped))
    print(f"{path}: {parsed.total} rows, {len(parsed.rows)} with a name, {parsed.skipped} skipped")
    if created:
        print(f"new properties: {', '.join(created)}")
    if not items:
        sys.exit("nothing to merge")
    run = await _run("merge", items, {"options": options.model_dump(), "file": parsed.file},
                     parsed.file)
    totals = {"created": 0, "matched": 0, "skipped": 0, "failed": 0}
    for it in await runner.get_items(run["id"], limit=10_000):
        res = it.get("result") or {}
        if it["status"] == "failed":
            totals["failed"] += 1
        elif it["status"] == "skipped":
            totals["skipped"] += 1
        elif res.get("created"):
            totals["created"] += 1
        else:
            totals["matched"] += 1
    verb = "would be " if dry_run else ""
    print(f"{totals['created']} {verb}created, {totals['matched']} {verb}matched, "
          f"{totals['skipped']} skipped (ambiguous or no name), {totals['failed']} failed")


async def dedupe(apply: bool) -> None:
    """Documents sharing a LinkedIn URL or an email: merge the groups whose names agree (the
    oldest document kept), list the rest. Without --yes only lists."""
    await mongo.ensure_indexes()
    wiring.register_write_hooks()
    groups = await people.duplicate_groups()
    print(f"{len(groups)} duplicate group(s)")
    merged = listed = 0
    for g in groups:
        who = "; ".join(f"{p['name']} ({p['id']}, {p['tier']})" for p in g["people"])
        tag = "same name" if g["same_name"] else "DIFFERENT names"
        print(f"- {g['key']} {g['value']!r} [{tag}]: {who}")
        if not g["same_name"] or not apply:
            listed += 1
            continue
        keep, *drops = g["ids"]
        for drop in drops:
            res = await people.merge_documents(None, keep, drop)
            merged += 1
            print(f"    merged {drop} into {keep}: {', '.join(c.key for c in res.changes) or 'nothing new'}")
    print(f"{merged} merged, {listed} listed" + ("" if apply else " (dry run; --yes to merge the same-name groups)"))


async def embed(only: list[str] | None) -> None:
    options = EmbedOptions(fields=only)
    items = await embed_assistant.prepare({}, options)
    await _run("embed", items, {"options": options.model_dump()}, "embed")


async def normalize_people() -> None:
    options = ReparseOptions()
    items = await reparse.prepare({}, options)
    await _run("reparse", items, {"options": options.model_dump()}, "reparse")


async def drop_property(keys: list[str], yes: bool) -> None:
    """Remove properties that should not exist (test leftovers): the field on every document,
    its passage vectors, its registry entry and its name vector. Lists the holders first; writes
    only with --yes."""
    for key in keys:
        if properties.spec(key) is not None:
            sys.exit(f"{key} is defined in property_specs.py; remove it from the code instead")
        holders = await people.count({key: {"$exists": True}})
        known = await properties.is_known(key)
        print(f"{key}: {'in the registry' if known else 'not in the registry'}, "
              f"on {holders} document(s)")
    if not yes:
        print("dry run; add --yes to remove")
        return
    for key in keys:
        docs = await people.drop_field(key)
        await embeddings.delete_field(key)
        removed = await properties.remove(key)
        print(f"{key}: removed from {docs} document(s); registry entry "
              f"{'deleted' if removed else 'absent'}")


async def list_providers(alexandria: bool, categories: list[str], providers: list[str] | None,
                         limit: int) -> None:
    """The data providers the enrich ladder can ask (the `providers` run option), then, with
    --alexandria, the Firecrawl catalogue's live prices and inputs for the people-data tools (a
    free call): the place to read a candidate provider's price before it joins the table."""
    from app.assistants.enrich.providers import PROVIDERS
    from app.integrations.firecrawl import AsyncFirecrawl

    print("Providers of the enrich assistant (run option `providers`, in this order):")
    for spec in PROVIDERS.values():
        print(f"  {spec.key:16} {spec.label:14} {spec.source:34} {spec.credits} credits per match")
    if not alexandria:
        return
    client = AsyncFirecrawl()
    try:
        tools = await client.find_tools(providers=providers,
                                        categories=None if providers else categories, limit=limit)
    finally:
        await client.aclose()
    print(f"\nAlexandria catalogue: {len(tools)} tool(s) for {providers or categories}")
    for t in tools:
        per = " per record" if t.get("perRecord") else ""
        print(f"\n{t.get('provider')}/{t.get('capability')}  —  {t.get('creditsCost', '?')} "
              f"credits{per}")
        if desc := t.get("description"):
            print(f"  {' '.join(str(desc).split())[:300]}")
        if one_of := t.get("requiresOneOf"):
            print(f"  needs one of: {' | '.join(', '.join(group) for group in one_of)}")
        for o in t.get("options") or []:
            if isinstance(o, dict):
                flag = " (required)" if o.get("required") else ""
                desc = " ".join(str(o.get("description") or "").split())[:140]
                print(f"    {o.get('name')}{flag} [{o.get('type', '')}]: {desc}")


async def run_report(run_id: int, show_items: bool, show_events: bool) -> None:
    """What happened in one run, for finding bugs after the fact: status and credits, items by
    status, each step's outcomes, the skipped reasons and provider statuses as histograms, what
    the storage step filed, dropped or created, failures with their errors; `--items` adds one
    line per item, `--events` the error-like events."""
    from collections import Counter

    from app.assistants import runner

    run = await runner.get_run(run_id)
    if run is None:
        sys.exit(f"no run {run_id}")
    items: list[dict] = []
    after = 0
    while True:
        page = await runner.get_items(run_id, after=after, limit=500)
        items += page
        if len(page) < 500:
            break
        after = page[-1]["id"]
    print(f"run {run_id} ({run['workflow']}): {run['status']}, {run.get('credits_used', 0)} credits, "
          f"{len(items)} items, started {run.get('started_at')}, finished {run.get('finished_at')}")
    print(f"  params: {json.dumps(run.get('params'), ensure_ascii=False)[:300]}")
    by_status = Counter(i["status"] for i in items)
    print("  items by status:", dict(by_status))
    credits = sorted((i.get("credits") or 0 for i in items), reverse=True)
    if credits:
        print(f"  credits per item: max {credits[0]}, median {credits[len(credits) // 2]}, "
              f"zero for {sum(1 for c in credits if c == 0)}")
    results = [(i, i.get("result") or {}) for i in items]
    steps = Counter()
    providers = Counter()
    skipped = Counter()
    stored = Counter()
    dropped = Counter()
    created = Counter()
    storage = Counter()
    for _, res in results:
        for step in ("linkedin", "github", "company", "storage", "repair"):
            if step in res and isinstance(res[step], str):
                steps[(step, res[step])] += 1
        for key, status in (res.get("providers") or {}).items():
            providers[(key, status)] += 1
        for s in res.get("skipped") or []:
            skipped[(s["step"], re.sub(r"https?://\S+", "<url>", s["reason"])[:110])] += 1
        for key in res.get("stored") or {}:
            stored[key] += 1
        for label in res.get("dropped") or []:
            dropped[label] += 1
        for key in res.get("created_properties") or []:
            created[key] += 1
        if "storage" in res:
            storage[res["storage"]] += 1
    if steps:
        print("  step outcomes:")
        for (step, outcome), n in sorted(steps.items()):
            print(f"    {step:9} {outcome:12} {n}")
    if providers:
        print("  providers:", {f"{k}:{s}": n for (k, s), n in sorted(providers.items())})
    if stored:
        print("  properties filed by the storage agent:", dict(stored.most_common()))
    if dropped:
        print("  facts dropped (no property, none allowed):", dict(dropped.most_common(20)))
    if created:
        print("  properties created:", dict(created))
    if skipped:
        print("  skipped reasons:")
        for (step, reason), n in skipped.most_common(25):
            print(f"    {n:4}  {step:9} {reason}")
    failed = [i for i in items if i["status"] == "failed" or i.get("error")]
    if failed:
        print("  failures:")
        for i in failed:
            print(f"    item {i['id']} {i.get('label') or i.get('person_id')}: {str(i.get('error'))[:200]}")
    if show_items:
        print("  items:")
        for i, res in results:
            print(f"    {i['id']:5} {i['status']:8} {i.get('credits') or 0:3}cr  "
                  f"{(i.get('label') or i.get('person_id') or '')[:28]:28} {(res.get('text') or '')[:150]}")
    if show_events:
        after = 0
        bad = 0
        while True:
            evs = await runner.events(run_id, after=after, limit=1000)
            for e in evs:
                msg = e.get("message") or ""
                if e.get("kind") in ("error", "failed", "skip") or "fail" in msg \
                        or "error" in msg.lower() or "rejected" in msg:
                    reason = (e.get("data") or {}).get("reason") or ""
                    print(f"    event {e['id']} {e.get('kind')}: {msg[:160]}"
                          + (f"  — {reason[:200]}" if reason else ""))
                    bad += 1
            if len(evs) < 1000:
                break
            after = evs[-1]["id"]
        print(f"  error-like events: {bad}")


async def migrate_history(dry_run: bool) -> None:
    n = await people.migrate_legacy(dry_run=dry_run)
    print(f"{'would migrate' if dry_run else 'migrated'} {n} people documents to field history")


async def list_properties() -> None:
    await properties.sync_specs()
    for p in await properties.catalog(await people.coverage()):
        flags = "".join(("E" if p.embeddable else "-", "N" if p.normalisable else "-"))
        values = f"  [{len(p.values)} values]" if p.values else ""
        print(f"{p.key:32} {p.type:9} {flags} {p.source:6} count={p.count}{values}")


# --- classify: suggest specs from the data (prints Python; writes nothing) ------------------------

_NUMBER = re.compile(r"^[<>~]?\s*[-$€£]?\s*\d[\d,]*(\.\d+)?\s*[kKmMbB]?\+?$")
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{1,2}[/ -]\d{1,2}[/ -]\d{2,4}|\d{1,2} [A-Za-z]{3,9} \d{4}|"
                   r"[A-Za-z]{3,9} \d{4})$")
_URL = re.compile(r"^(https?://|www\.)|\.[a-z]{2,}(/|$)", re.IGNORECASE)
_CONTACT = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$|^\+?[\d\s().-]{7,}(\s*\(.*\))?$", re.IGNORECASE)
_BOOL = {"yes", "no", "true", "false", "y", "n"}


def suggest_spec(values: list[tuple[Any, int]], total: int) -> tuple[str, tuple[str, ...]]:
    """(type, enum values) for a property given its (value, count) pairs, most common first."""
    if not values:
        return "text", ()
    strs = [(str(v).strip(), n) for v, n in values if str(v).strip()]
    n = sum(c for _, c in strs) or 1
    share = lambda pred: sum(c for s, c in strs if pred(s)) / n
    lowered = {s.lower() for s, _ in strs}
    if lowered <= _BOOL:
        return "boolean", ()
    if share(lambda s: bool(_NUMBER.match(s))) >= 0.9:
        return "number", ()
    if share(lambda s: bool(_DATE.match(s))) >= 0.9:
        return "date", ()
    if share(lambda s: bool(_CONTACT.match(s))) >= 0.8:
        return "contact", ()
    if share(lambda s: bool(_URL.search(s)) and " " not in s) >= 0.8:
        return "url", ()
    listy = any(isinstance(v, list) for v, _ in values)
    distinct = len(strs)
    if distinct <= 25 and total >= 20 and distinct <= 0.2 * total:
        return ("enum_list" if listy else "enum"), tuple(s for s, _ in strs)
    if listy:
        return "list", ()
    avg = statistics.fmean(len(s) for s, _ in strs)
    return ("keyword" if avg < 40 else "text"), ()


async def classify(include_typed: bool) -> None:
    counts = await people.coverage()
    for prop in await properties.catalog(counts):
        if properties.spec(prop.key) and not include_typed:
            continue
        # value_counts unwinds lists; we also need to know whether the raw values are lists
        raw = await people.find({f"{prop.key}.v": {"$type": "array"}}, limit=1,
                                projection={"_id": 1})
        values = await people.value_counts(prop.key, 200)
        if raw:
            values = [([v], c) for v, c in values]
        ptype, enum = suggest_spec(values, prop.count)
        examples = "; ".join(str(v if not isinstance(v, list) else v[0])[:40]
                             for v, _ in values[:3])
        current = f" (now {prop.type})" if properties.spec(prop.key) else ""
        spec = f'Spec("{ptype}", {enum!r})' if enum else f'Spec("{ptype}")'
        print(f'    "{prop.key}": {spec},  # n={prop.count} distinct={len(values)}{current}'
              f"  e.g. {examples}")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    cu = sub.add_parser("create-user")
    cu.add_argument("username")
    cu.add_argument("--role", default="reader")
    cu.add_argument("--password", help="omit to be prompted")
    sp = sub.add_parser("set-password")
    sp.add_argument("username")
    sp.add_argument("--password")
    im = sub.add_parser("import", help="merge a CSV's rows into the people database (the merge "
                                       "assistant: matched by identifier or name, else created)")
    im.add_argument("file")
    im.add_argument("--tier", required=True, choices=fields.TIERS,
                    help="for the people the file creates; a matched person keeps theirs")
    im.add_argument("--dry-run", action="store_true", help="match and plan only, write nothing")
    im.add_argument("--no-agent", action="store_true",
                    help="write every known value as entered; nothing goes to the storage agent")
    im.add_argument("--no-review", action="store_true", help="skip the closing review")
    im.add_argument("--map", action="append", default=[], metavar="HEADER=KEY",
                    help="map a column to a property key, 'id' or 'skip' (repeatable)")
    dd = sub.add_parser("dedupe", help="merge documents that share a LinkedIn URL or an email "
                                       "(same name: merged, oldest kept; else listed)")
    dd.add_argument("--yes", action="store_true", help="without it: list the groups only")
    em = sub.add_parser("embed", help="sync property-name vectors and backfill passage vectors")
    em.add_argument("--field", action="append", help="only these fields (repeatable)")
    sub.add_parser("properties", help="the property catalog with coverage")
    cl = sub.add_parser("classify", help="suggest property_specs entries from the data")
    cl.add_argument("--all", action="store_true", help="include properties that have a spec")
    nz = sub.add_parser("normalize", help="(re)parse the normalised values of typed properties")
    nz.add_argument("--all", action="store_true", required=True, help="every document")
    mh = sub.add_parser("migrate-history", help="wrap legacy flat field values into histories")
    mh.add_argument("--dry-run", action="store_true")
    dp = sub.add_parser("drop-property", help="delete properties (documents, vectors, registry)")
    dp.add_argument("keys", nargs="+")
    dp.add_argument("--yes", action="store_true", help="without it: list the holders only")
    rr = sub.add_parser("run", help="what happened in one run: outcomes, skipped reasons, "
                                    "what the storage agent filed, failures")
    rr.add_argument("run_id", type=int)
    rr.add_argument("--items", action="store_true", help="one line per item")
    rr.add_argument("--events", action="store_true", help="the error-like events")
    pv = sub.add_parser("providers", help="the enrich ladder's data providers; --alexandria adds "
                                          "the Firecrawl catalogue's live prices and inputs (free)")
    pv.add_argument("--alexandria", action="store_true")
    pv.add_argument("--category", action="append", default=None,
                    help="catalogue category (repeatable; default: people)")
    pv.add_argument("--provider", action="append", default=None,
                    help="only these providers (repeatable)")
    pv.add_argument("--limit", type=int, default=50)
    args = p.parse_args()

    async def run() -> None:
        try:
            match args.cmd:
                case "create-user":
                    await create_user(args.username.lower(), args.role, args.password)
                case "set-password":
                    await set_password(args.username.lower(), args.password)
                case "import":
                    mapping = dict(m.split("=", 1) for m in args.map if "=" in m) or None
                    await import_file(args.file, args.tier, dry_run=args.dry_run,
                                      agent=not args.no_agent, review=not args.no_review,
                                      columns=mapping)
                case "dedupe":
                    await dedupe(args.yes)
                case "embed":
                    await embed(args.field)
                case "properties":
                    await list_properties()
                case "classify":
                    await classify(args.all)
                case "normalize":
                    await normalize_people()
                case "migrate-history":
                    await migrate_history(args.dry_run)
                case "drop-property":
                    await drop_property(args.keys, args.yes)
                case "run":
                    await run_report(args.run_id, args.items, args.events)
                case "providers":
                    await list_providers(args.alexandria, args.category or ["people"],
                                         args.provider, args.limit)
        finally:
            await mongo.close()
            await db.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
