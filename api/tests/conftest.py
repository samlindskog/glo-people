import os

# Point the app at throwaway databases before anything imports settings.
_pg = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://glo:glo@localhost:5432/glo_test")
os.environ["DATABASE_URL"] = _pg
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "glo_test"
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["LOGFIRE_SEND_TO_LOGFIRE"] = "false"

import asyncio
import sys
from pathlib import Path

import asyncpg
import httpx
import pytest
from pydantic_ai import models as ai_models
from sqlalchemy import text

from app import auth, db, mongo, properties
from app.main import api, app, lifespan

ai_models.ALLOW_MODEL_REQUESTS = False   # no accidental LLM calls from tests

PASSWORD = "correct-horse-battery"


async def _ensure_database() -> None:
    dsn = _pg.replace("+asyncpg", "")
    base, name = dsn.rsplit("/", 1)
    conn = await asyncpg.connect(f"{base}/postgres")
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
async def _schema():
    await _ensure_database()
    async with db.engine().begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await db.dispose()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "alembic", "upgrade", "head", cwd=Path(__file__).parents[1],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    assert proc.returncode == 0, out.decode()
    # Enter and exit the app lifespan in one task: the MCP session manager's anyio task group
    # must not be exited from a different task than the one that entered it.
    ready, stop = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with lifespan(api):
            ready.set()
            await stop.wait()

    task = asyncio.create_task(hold())
    await ready.wait()
    yield
    stop.set()
    await task


@pytest.fixture(autouse=True)
async def _clean():
    async with db.engine().begin() as conn:
        await conn.execute(text(
            "TRUNCATE users, sessions, api_keys, searches, activity, field_embeddings, "
            "property_embeddings RESTART IDENTITY CASCADE"))
    await mongo.db().people.delete_many({})
    await mongo.db().fields.delete_many({})
    properties.reset()
    await properties.sync_specs()
    auth.login_throttle._failures.clear()
    yield


async def make_user(username: str, role: str) -> int:
    async with db.engine().begin() as conn:
        return (await conn.execute(db.users.insert().values(
            username=username, password_hash=auth.hash_password(PASSWORD), role=role
        ).returning(db.users.c.id))).scalar_one()


@pytest.fixture
def client_factory():
    clients = []

    async def make(username: str | None = None, role: str = "reader") -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        if username:
            await make_user(username, role)
            r = await c.post("/api/auth/login", json={"username": username, "password": PASSWORD})
            assert r.status_code == 200, r.text
        return c

    yield make


@pytest.fixture
async def writer(client_factory):
    return await client_factory("wendy", "writer")


@pytest.fixture
async def reader(client_factory):
    return await client_factory("rita", "reader")
