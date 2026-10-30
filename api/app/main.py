import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from app import auth, db, embeddings, jobs, mongo, observability, properties, workflows
from app import people as people_svc
from app.config import get_settings
from app.mcp_server import build_app as build_mcp_app
from app.mcp_server import mcp
from app.permissions import load_contract
from app.routers import accounts
from app.routers import agents as agents_router
from app.routers import auth as auth_router
from app.routers import people as people_router
from app.routers import properties as properties_router
from app.routers import workflows as workflows_router
from app.workflows import runner

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
observability.setup()

mcp_app = build_mcp_app()
workflows.load_all()
# Composition root: what reacts to a person write (people.py itself knows no consumer).
people_svc.on_write(embeddings.sync_person)


def check_dev_auto_login() -> None:
    """DEV_AUTO_LOGIN must never reach production: refuse to start with secure cookies on."""
    s = get_settings()
    if s.dev_auto_login and s.cookie_secure:
        raise RuntimeError("DEV_AUTO_LOGIN=1 with COOKIE_SECURE=true: refusing to start")


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_contract()
    check_dev_auto_login()
    if get_settings().dev_auto_login:
        await auth.ensure_dev_user()
        logging.getLogger(__name__).warning("DEV_AUTO_LOGIN is on: every request acts as 'dev'")
    await mongo.ensure_indexes()
    await people_svc.migrate_legacy()
    try:
        await properties.sync_specs()
    except Exception:
        # Typically a pending migration (property_embeddings). The catalog still works from the
        # registry and specs; `python -m app.cli embed` syncs the names once migrated.
        logging.getLogger(__name__).exception(
            "property sync failed at startup; run `alembic upgrade head` (make prod-migrate)")
    await runner.reconcile()
    async with mcp.session_manager.run():
        yield
    await runner.drain()
    await jobs.drain()
    await mongo.close()
    await db.dispose()


api = FastAPI(title="glo-people", lifespan=lifespan, docs_url="/api/docs",
              openapi_url="/api/openapi.json", redoc_url=None)
api.include_router(auth_router.router)
api.include_router(people_router.router)
api.include_router(properties_router.router)
api.include_router(agents_router.router)
api.include_router(accounts.router)
api.include_router(workflows_router.router)
observability.instrument_app(api)


@api.get("/api/health")
async def health() -> dict:
    return {"ok": True}


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    """Route /mcp and /mcp/* to the MCP app (with the path rewritten to /), the rest to FastAPI."""
    if scope["type"] == "http" and (scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
        sub = dict(scope)
        sub["path"] = scope["path"][len("/mcp"):] or "/"
        sub["raw_path"] = sub["path"].encode()
        await mcp_app(sub, receive, send)
        return
    await api(scope, receive, send)
