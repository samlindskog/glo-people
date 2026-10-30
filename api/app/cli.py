"""Admin commands: python -m app.cli {create-user,set-password,import,embed,fields,migrate-history}."""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from sqlalchemy import select, update

from app import auth, db, embeddings, fields, importer, mongo, people
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


async def import_file(path: str, tier: str, embed: bool) -> None:
    await mongo.ensure_indexes()
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    result = await importer.import_csv(text, Path(path).name, tier)
    print(f"{path}: {result.rows} rows, {result.inserted} inserted, {result.updated} updated, "
          f"{result.skipped} skipped (no name)")
    if result.new_fields:
        print(f"new fields: {', '.join(result.new_fields)}")
    if embed:
        n = await embeddings.sync_people(result.ids)
        print(f"embedded {n} field values")


async def embed(only: list[str] | None) -> None:
    keys = set(only) if only else None
    n = await embeddings.backfill(keys, progress=lambda n: print(f"  {n} vectors", end="\r"))
    print(f"\nembedded {n} field values")


async def migrate_history(dry_run: bool) -> None:
    n = await people.migrate_legacy(dry_run=dry_run)
    print(f"{'would migrate' if dry_run else 'migrated'} {n} people documents to field history")


async def list_fields() -> None:
    await fields.refresh_counts()
    for f in await fields.list_fields():
        print(f"{f['key']:32} {f['type']:7} embed={'y' if f['embed'] else 'n'} count={f['count']}")


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
    em = sub.add_parser("embed")
    em.add_argument("--field", action="append", help="only these fields (repeatable)")
    sub.add_parser("fields")
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
                case "fields":
                    await list_fields()
                case "migrate-history":
                    await migrate_history(args.dry_run)
        finally:
            await mongo.close()
            await db.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
