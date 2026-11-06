import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from app import assistants, auth, db, mongo, observability, ops, properties, wiring
from app import people as people_svc
from app.assistants import runner
from app.config import get_settings
from app.glojobs import router as glojobs_router
from app.mcp_server import build_app as build_mcp_app
from app.mcp_server import mcp
from app.ops_mcp import build_app as build_ops_app
from app.ops_mcp import mcp as ops_mcp
from app.permissions import load_contract
from app.routers import accounts
from app.routers import assistants as assistants_router
from app.routers import auth as auth_router
from app.routers import boards as boards_router
from app.routers import credits as credits_router
from app.routers import invocations as invocations_router
from app.routers import people as people_router
from app.routers import properties as properties_router
from app.routers import search as search_router
from app.tools import invoke

# force: an imported library (FastMCP) has already installed a Rich handler that wraps every line
# at 80 columns, which makes the logs impossible to grep; one plain line per record instead.
logging.basicConfig(level=logging.INFO, force=True,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("app").setLevel(get_settings().log_level.upper())
logging.getLogger().addHandler(ops.Handler())      # every warning and error into app_log
observability.setup()

mcp_app = build_mcp_app()
ops_app = build_ops_app()
assistants.load_all()
wiring.register_write_hooks()      # what reacts to a person write; shared with the CLI


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
    # Prod never runs alembic on its own (make prod-migrate does, after a deploy), so a release
    # that adds tables must still come up before the migration: the steps that need the new
    # tables log an error and are retried by the migration commands instead of crashing the app.
    try:
        await properties.sync_specs()
    except Exception:
        logging.getLogger(__name__).exception(
            "sync_specs failed at startup; pending migration? run `alembic upgrade head` "
            "(make prod-migrate)")
    runner.start_scheduler(runner.worker_id("api"))
    try:
        await runner.adopt(all_api_runs=True)
    except Exception:
        logging.getLogger(__name__).exception(
            "adopting runs failed at startup; pending migration? run `alembic upgrade head` "
            "(make prod-migrate)")
    await ops.start()
    logging.getLogger(__name__).info("started %s", get_settings().git_sha)
    async with mcp.session_manager.run(), ops_mcp.session_manager.run():
        yield
    await ops.stop()
    await runner.stop_scheduler()
    await invoke.drain()
    await mongo.close()
    await db.dispose()


api = FastAPI(title="glo-people", lifespan=lifespan, docs_url="/api/docs",
              openapi_url="/api/openapi.json", redoc_url=None)
api.include_router(auth_router.router)
api.include_router(people_router.router)
api.include_router(invocations_router.router)
api.include_router(credits_router.router)
api.include_router(properties_router.router)
api.include_router(search_router.router)
api.include_router(accounts.router)
api.include_router(assistants_router.router)
api.include_router(boards_router.router)
# glojobs.app: the public boards (no auth; the UUID is the secret) and their stylesheet, fonts
# and script. The mount goes first so /glojobs/static/... never reaches the board routes.
api.mount("/glojobs/static", glojobs_router.static_files, name="glojobs-static")
api.include_router(glojobs_router.router)


@api.get("/api/health")
async def health() -> dict:
    # fake_agents lets end-to-end tests know whether agent flows are deterministic here.
    return {"ok": True, "fake_agents": get_settings().fake_agents}


def _route(scope: Scope) -> str:
    """The route template of a handled request: the path with each path parameter's value put
    back as its name, so rows group by endpoint."""
    route = scope["path"]
    for name, value in (scope.get("path_params") or {}).items():
        route = route.replace(str(value), "{" + name + "}", 1)
    return route


async def _dispatch(scope: Scope, receive: Receive, send: Send) -> None:
    """Route /mcp and /mcp/* to the MCP app, /ops/* to the ops app (paths rewritten), the rest
    to FastAPI."""
    path = scope["path"]
    if path == "/mcp" or path.startswith("/mcp/"):
        await mcp_app(ops.sub_scope(scope, "/mcp"), receive, send)
    elif path == "/ops" or path.startswith("/ops/"):
        await ops_app(ops.sub_scope(scope, "/ops"), receive, send)
    else:
        await api(scope, receive, send)


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    """Every HTTP request gets a request id (sent back as X-Request-Id) and a row in the request
    log once it is answered; the health check is left out."""
    if scope["type"] != "http":
        await api(scope, receive, send)
        return
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    rid = ops.begin()
    status = {"code": 0}
    started = time.perf_counter()

    async def send_with_id(message: dict) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
            message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", rid.encode())]}
        await send(message)

    try:
        await _dispatch(scope, receive, send_with_id)
    except BaseException:
        status["code"] = status["code"] or 500
        raise
    finally:
        if scope["path"] != "/api/health":
            client = scope.get("client") or ("", 0)
            path = scope["path"]
            route = ("/mcp" if path.startswith("/mcp") else "/ops" if path.startswith("/ops")
                     else _route(scope))
            ops.record_request(
                source="api", host=headers.get("host", ""), method=scope.get("method", ""),
                path=path + (("?" + scope["query_string"].decode("latin-1", "replace"))
                             if scope.get("query_string") else ""),
                route=route, status=status["code"],
                duration_ms=int((time.perf_counter() - started) * 1000),
                client_ip=headers.get("x-forwarded-for", "").split(",")[0].strip() or str(client[0]),
                user_agent=headers.get("user-agent", ""), request_id=rid, attrs=ops.notes())
