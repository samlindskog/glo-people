"""Admin commands: python -m app.cli {create-user,set-password,import,embed,properties,classify,
normalize,migrate-history}. import, embed and normalize run as assistant runs executed in this
process (visible on the Assistants page like any other run)."""

import argparse
import asyncio
import getpass
import os
import re
import statistics
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app import assistants, auth, db, fields, mongo, observability, people, properties
from app.assistants import embed as embed_assistant
from app.assistants import import_csv, reparse, runner
from app.assistants.base import REGISTRY
from app.assistants.embed import EmbedOptions
from app.assistants.import_csv import ImportOptions
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
    assistants.load_all()
    run = await runner.create_run(REGISTRY[spec_key], None, items, params, label=label)
    print(f"run {run['id']} ({spec_key}): {run['total']} item(s)")
    run = await runner.run_to_completion(run["id"], f"cli:{os.getpid()}", on_event=_show)
    print(f"run {run['id']}: {run['status']}, {run['done']} done, {run['failed']} failed")
    return run


async def import_file(path: str, tier: str, embed: bool) -> None:
    await mongo.ensure_indexes()
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    options = ImportOptions(tier=tier, embed=embed)
    parsed, created, items = await import_csv.prepare_csv(text, Path(path).name, options)
    print(f"{path}: {parsed.total} rows, {len(parsed.rows)} with a name, {parsed.skipped} skipped")
    if created:
        print(f"new properties: {', '.join(created)}")
    if not items:
        sys.exit("nothing to import")
    run = await _run("import", items, {"options": options.model_dump(), "file": parsed.file},
                     parsed.file)
    totals = {"inserted": 0, "updated": 0, "embedded": 0}
    for it in await runner.get_items(run["id"], limit=10_000):
        for k in totals:
            totals[k] += int((it.get("result") or {}).get(k) or 0)
    print(f"{totals['inserted']} inserted, {totals['updated']} updated, "
          f"{totals['embedded']} vectors")


async def embed(only: list[str] | None) -> None:
    options = EmbedOptions(fields=only)
    items = await embed_assistant.prepare({}, options)
    await _run("embed", items, {"options": options.model_dump()}, "embed")


async def normalize_people() -> None:
    options = ReparseOptions()
    items = await reparse.prepare({}, options)
    await _run("reparse", items, {"options": options.model_dump()}, "reparse")


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
    im = sub.add_parser("import")
    im.add_argument("file")
    im.add_argument("--tier", required=True, choices=fields.TIERS)
    im.add_argument("--no-embed", action="store_true")
    em = sub.add_parser("embed", help="sync property-name vectors and backfill passage vectors")
    em.add_argument("--field", action="append", help="only these fields (repeatable)")
    sub.add_parser("properties", help="the property catalog with coverage")
    cl = sub.add_parser("classify", help="suggest property_specs entries from the data")
    cl.add_argument("--all", action="store_true", help="include properties that have a spec")
    nz = sub.add_parser("normalize", help="(re)parse the normalised values of typed properties")
    nz.add_argument("--all", action="store_true", required=True, help="every document")
    mh = sub.add_parser("migrate-history", help="wrap legacy flat field values into histories")
    mh.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    async def run() -> None:
        try:
            match args.cmd:
                case "create-user":
                    await create_user(args.username.lower(), args.role, args.password)
                case "set-password":
                    await set_password(args.username.lower(), args.password)
                case "import":
                    await import_file(args.file, args.tier, not args.no_embed)
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
        finally:
            await mongo.close()
            await db.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
