"""The Firecrawl credit balance for the top bar (level 4 surface)."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.assistants import credits
from app.auth import Principal, require

router = APIRouter(prefix="/api", tags=["credits"])

Reader = Annotated[Principal, Depends(require("people:read"))]


@router.get("/credits")
async def credit_balance(_: Reader) -> dict | None:
    """Remaining Firecrawl credits for the team, the plan size, the billing period's end and the
    credits the running assistants of this process have reserved. Null when no key is set."""
    return await credits.summary()
