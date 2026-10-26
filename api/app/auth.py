"""Passwords, browser sessions, API keys and the `require(permission)` dependency."""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select, update

from app import db
from app.config import get_settings
from app.permissions import API_KEY_FORBIDDEN, permissions_for

SESSION_COOKIE = "glo_session"
API_KEY_PREFIX = "glo_"

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    role: str
    permissions: frozenset[str]
    api_key_id: int | None = None
    ip: str | None = field(default=None, compare=False)

    @property
    def via_api_key(self) -> bool:
        return self.api_key_id is not None

    def can(self, permission: str) -> bool:
        if self.via_api_key and permission in API_KEY_FORBIDDEN:
            return False
        return permission in self.permissions


def client_ip(request: Request) -> str | None:
    # nginx sets X-Real-IP from CF-Connecting-IP in production.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else None)


# --- sessions ---------------------------------------------------------------------------------

async def create_session(user_id: int, ip: str | None, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(days=get_settings().session_days)
    async with db.engine().begin() as conn:
        await conn.execute(db.sessions.insert().values(
            token_hash=sha256(token), user_id=user_id, expires_at=expires, ip=ip,
            user_agent=(user_agent or "")[:500]))
    return token


async def delete_session(token: str) -> None:
    async with db.engine().begin() as conn:
        await conn.execute(db.sessions.delete().where(db.sessions.c.token_hash == sha256(token)))


async def principal_from_session(token: str, ip: str | None) -> Principal | None:
    q = (select(db.users).join(db.sessions, db.sessions.c.user_id == db.users.c.id)
         .where(db.sessions.c.token_hash == sha256(token),
                db.sessions.c.expires_at > datetime.now(UTC),
                db.users.c.is_active))
    async with db.engine().connect() as conn:
        user = (await conn.execute(q)).mappings().first()
    if not user:
        return None
    return Principal(user["id"], user["username"], user["role"], permissions_for(user["role"]),
                     ip=ip)


# --- API keys ---------------------------------------------------------------------------------

def new_api_key() -> tuple[str, str, str]:
    """Returns (full key shown once, lookup prefix, sha256 of the full key)."""
    prefix = secrets.token_hex(4)
    key = f"{API_KEY_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
    return key, prefix, sha256(key)


async def principal_from_api_key(key: str, ip: str | None = None) -> Principal | None:
    if not key.startswith(API_KEY_PREFIX):
        return None
    prefix = key[len(API_KEY_PREFIX):].split("_", 1)[0]
    q = (select(db.users, db.api_keys.c.id.label("key_id"), db.api_keys.c.key_hash)
         .join(db.api_keys, db.api_keys.c.user_id == db.users.c.id)
         .where(db.api_keys.c.prefix == prefix, db.api_keys.c.revoked_at.is_(None),
                db.users.c.is_active))
    async with db.engine().begin() as conn:
        row = (await conn.execute(q)).mappings().first()
        if not row or not hmac.compare_digest(row["key_hash"], sha256(key)):
            return None
        await conn.execute(update(db.api_keys).where(db.api_keys.c.id == row["key_id"])
                           .values(last_used_at=datetime.now(UTC)))
    return Principal(row["id"], row["username"], row["role"], permissions_for(row["role"]),
                     api_key_id=row["key_id"], ip=ip)


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


# --- dependencies -----------------------------------------------------------------------------

async def current_principal(request: Request) -> Principal:
    ip = client_ip(request)
    if token := bearer_token(request):
        principal = await principal_from_api_key(token, ip)
    elif cookie := request.cookies.get(SESSION_COOKIE):
        principal = await principal_from_session(cookie, ip)
    else:
        principal = None
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return principal


def require(*permissions: str):
    async def dependency(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        missing = [p for p in permissions if not principal.can(p)]
        if missing:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission: {missing[0]}")
        return principal

    return dependency


# --- login throttling (single API worker, so in-memory is enough) -------------------------------

class LoginThrottle:
    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str) -> deque[float]:
        window = get_settings().login_window_seconds
        q = self._failures[key]
        cutoff = time.monotonic() - window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, *keys: str) -> bool:
        limit = get_settings().login_max_failures
        return any(len(self._recent(k)) >= limit for k in keys)

    def fail(self, *keys: str) -> None:
        now = time.monotonic()
        for k in keys:
            self._recent(k).append(now)

    def reset(self, *keys: str) -> None:
        for k in keys:
            self._failures.pop(k, None)


login_throttle = LoginThrottle()
