"""Users (accounts:manage) and MCP API keys (api_keys:manage_own / api_keys:manage_all)."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app import activity, auth, db
from app.auth import Principal, require
from app.permissions import is_role, permissions_for

router = APIRouter(prefix="/api", tags=["accounts"])

Manager = Annotated[Principal, Depends(require("accounts:manage"))]
OwnKeys = Annotated[Principal, Depends(require("api_keys:manage_own"))]
AllKeys = Annotated[Principal, Depends(require("api_keys:manage_all"))]

USERNAME = r"^[a-z0-9][a-z0-9._-]{1,63}$"


class UserCreate(BaseModel):
    username: str = Field(pattern=USERNAME)
    password: str = Field(min_length=10, max_length=256)
    role: str


class UserPatch(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class PasswordIn(BaseModel):
    password: str = Field(min_length=10, max_length=256)


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


def _check_assignable(principal: Principal, role: str) -> None:
    """You may only grant roles whose permissions you hold yourself (no escalation)."""
    if not is_role(role):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown role {role!r}")
    if not permissions_for(role) <= principal.permissions:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"cannot grant role {role!r}: it has permissions you lack")


_USER_COLS = (db.users.c.id, db.users.c.username, db.users.c.role, db.users.c.is_active,
              db.users.c.created_at)


async def _user(user_id: int) -> dict:
    async with db.engine().connect() as conn:
        row = (await conn.execute(select(*_USER_COLS).where(db.users.c.id == user_id))
               ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return dict(row)


# --- users --------------------------------------------------------------------------------------

@router.get("/users")
async def list_users(_: Manager) -> list[dict]:
    last_seen = (select(db.activity.c.user_id, func.max(db.activity.c.created_at).label("last_seen"))
                 .group_by(db.activity.c.user_id).subquery())
    q = (select(*_USER_COLS, last_seen.c.last_seen)
         .join(last_seen, last_seen.c.user_id == db.users.c.id, isouter=True)
         .order_by(db.users.c.username))
    async with db.engine().connect() as conn:
        return [dict(r) for r in (await conn.execute(q)).mappings()]


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, principal: Manager) -> dict:
    _check_assignable(principal, body.role)
    try:
        async with db.engine().begin() as conn:
            uid = (await conn.execute(db.users.insert().values(
                username=body.username, password_hash=auth.hash_password(body.password),
                role=body.role).returning(db.users.c.id))).scalar_one()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already exists") from None
    await activity.record("user.create", principal, body.username, {"role": body.role})
    return await _user(uid)


@router.patch("/users/{user_id}")
async def patch_user(user_id: int, body: UserPatch, principal: Manager) -> dict:
    target = await _user(user_id)
    changes = body.model_dump(exclude_none=True)
    if user_id == principal.user_id and changes:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "you cannot change your own role or deactivate yourself")
    # Managing a user whose role outranks yours would let you lock them out.
    _check_assignable(principal, target["role"])
    if "role" in changes:
        _check_assignable(principal, changes["role"])
    if changes:
        async with db.engine().begin() as conn:
            await conn.execute(update(db.users).where(db.users.c.id == user_id).values(**changes))
            if changes.get("is_active") is False:
                await conn.execute(db.sessions.delete().where(db.sessions.c.user_id == user_id))
        await activity.record("user.update", principal, target["username"], changes)
    return await _user(user_id)


@router.post("/users/{user_id}/password", status_code=204)
async def set_password(user_id: int, body: PasswordIn, principal: Manager) -> None:
    target = await _user(user_id)
    if user_id != principal.user_id:
        _check_assignable(principal, target["role"])
    async with db.engine().begin() as conn:
        await conn.execute(update(db.users).where(db.users.c.id == user_id)
                           .values(password_hash=auth.hash_password(body.password)))
        await conn.execute(db.sessions.delete().where(db.sessions.c.user_id == user_id))
    await activity.record("user.password_reset", principal, target["username"])


# --- API keys -----------------------------------------------------------------------------------

_KEY_COLS = (db.api_keys.c.id, db.api_keys.c.user_id, db.api_keys.c.name, db.api_keys.c.prefix,
             db.api_keys.c.created_at, db.api_keys.c.last_used_at, db.api_keys.c.revoked_at)


async def _keys_for(user_id: int) -> list[dict]:
    q = select(*_KEY_COLS).where(db.api_keys.c.user_id == user_id).order_by(
        db.api_keys.c.revoked_at.is_not(None), db.api_keys.c.created_at.desc())
    async with db.engine().connect() as conn:
        return [dict(r) for r in (await conn.execute(q)).mappings()]


async def _issue(principal: Principal, user_id: int, name: str) -> dict:
    key, prefix, key_hash = auth.new_api_key()
    async with db.engine().begin() as conn:
        kid = (await conn.execute(db.api_keys.insert().values(
            user_id=user_id, name=name, prefix=prefix, key_hash=key_hash,
            created_by=principal.user_id).returning(db.api_keys.c.id))).scalar_one()
    await activity.record("apikey.create", principal, f"{prefix}", {"for_user": user_id,
                                                                    "name": name})
    async with db.engine().connect() as conn:
        row = (await conn.execute(select(*_KEY_COLS).where(db.api_keys.c.id == kid))
               ).mappings().one()
    return {**row, "key": key}


async def _revoke(principal: Principal, key_id: int, owner: int | None) -> None:
    q = update(db.api_keys).where(db.api_keys.c.id == key_id, db.api_keys.c.revoked_at.is_(None))
    if owner is not None:
        q = q.where(db.api_keys.c.user_id == owner)
    async with db.engine().begin() as conn:
        res = await conn.execute(q.values(revoked_at=datetime.now(UTC))
                                 .returning(db.api_keys.c.prefix))
        prefix = res.scalar_one_or_none()
    if prefix is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active key not found")
    await activity.record("apikey.revoke", principal, prefix)


@router.get("/api-keys")
async def my_keys(principal: OwnKeys) -> list[dict]:
    return await _keys_for(principal.user_id)


@router.post("/api-keys", status_code=201)
async def issue_my_key(body: KeyCreate, principal: OwnKeys) -> dict:
    return await _issue(principal, principal.user_id, body.name)


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_my_key(key_id: int, principal: OwnKeys) -> None:
    await _revoke(principal, key_id, owner=principal.user_id)


@router.get("/users/{user_id}/api-keys")
async def user_keys(user_id: int, _: AllKeys) -> list[dict]:
    await _user(user_id)
    return await _keys_for(user_id)


@router.post("/users/{user_id}/api-keys", status_code=201)
async def issue_user_key(user_id: int, body: KeyCreate, principal: AllKeys) -> dict:
    target = await _user(user_id)
    _check_assignable(principal, target["role"])
    return await _issue(principal, user_id, body.name)


@router.delete("/users/{user_id}/api-keys/{key_id}", status_code=204)
async def revoke_user_key(user_id: int, key_id: int, principal: AllKeys) -> None:
    await _revoke(principal, key_id, owner=user_id)
