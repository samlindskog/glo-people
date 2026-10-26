import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from app import db, jobs, mongo
from app.mcp_server import build_app as build_mcp_app
from app.mcp_server import mcp
from app.permissions import load_contract
from app.routers import accounts, people
from app.routers import auth as auth_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

mcp_app = build_mcp_app()


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_contract()
    await mongo.ensure_indexes()
    async with mcp.session_manager.run():
        yield
    await jobs.drain()
    await mongo.close()
    await db.dispose()


api = FastAPI(title="glo-people", lifespan=lifespan, docs_url="/api/docs",
              openapi_url="/api/openapi.json", redoc_url=None)
api.include_router(auth_router.router)
api.include_router(people.router)
api.include_router(accounts.router)


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
