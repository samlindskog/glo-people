from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from app import activity, auth, db
from app.auth import Principal, current_principal
from app.config import get_settings
from app.permissions import load_contract

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


def me_payload(p: Principal) -> dict:
    return {"id": p.user_id, "username": p.username, "role": p.role,
            "permissions": sorted(x for x in p.permissions if p.can(x)),
            "via_api_key": p.via_api_key}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict:
    ip = auth.client_ip(request)
    username = body.username.strip().lower()
    keys = (f"ip:{ip}", f"user:{username}")
    if auth.login_throttle.blocked(*keys):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many failed attempts, try again later")
    async with db.engine().connect() as conn:
        user = (await conn.execute(select(db.users).where(db.users.c.username == username))
                ).mappings().first()
    ok = user is not None and auth.verify_password(user["password_hash"], body.password)
    if not ok or not user["is_active"]:
        auth.login_throttle.fail(*keys)
        await activity.record("auth.login_failed", None, username, ip=ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    auth.login_throttle.reset(*keys)

    token = await auth.create_session(user["id"], ip, request.headers.get("user-agent"))
    s = get_settings()
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=s.session_days * 86400,
                        httponly=True, secure=s.cookie_secure, samesite="lax", path="/")
    principal = await auth.principal_from_session(token, ip)
    await activity.record("auth.login", principal)
    return me_payload(principal)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    if token := request.cookies.get(auth.SESSION_COOKIE):
        principal = await auth.principal_from_session(token, auth.client_ip(request))
        await auth.delete_session(token)
        if principal:
            await activity.record("auth.logout", principal)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")


@router.get("/me")
async def me(principal: Annotated[Principal, Depends(current_principal)]) -> dict:
    return me_payload(principal)


@router.get("/contract")
async def contract() -> dict:
    """The shared roles/permissions contract (also bundled into the web app)."""
    return load_contract()
