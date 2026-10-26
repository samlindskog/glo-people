import logging
from typing import Any

from app import db
from app.auth import Principal

log = logging.getLogger(__name__)


async def record(action: str, principal: Principal | None = None, target: str | None = None,
                 meta: dict[str, Any] | None = None, ip: str | None = None) -> None:
    """Append to the activity log. Never lets a logging failure break the request."""
    try:
        async with db.engine().begin() as conn:
            await conn.execute(db.activity.insert().values(
                action=action,
                user_id=principal.user_id if principal else None,
                api_key_id=principal.api_key_id if principal else None,
                target=target,
                meta=meta or {},
                ip=ip or (principal.ip if principal else None),
            ))
    except Exception:
        log.exception("failed to record activity %s", action)
