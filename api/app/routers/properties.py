from typing import Annotated

from fastapi import APIRouter, Depends

from app import people, properties
from app.auth import Principal, require

router = APIRouter(prefix="/api", tags=["properties"])

Reader = Annotated[Principal, Depends(require("people:read"))]


@router.get("/properties")
async def list_properties(_: Reader) -> list[properties.Property]:
    """The property catalog with coverage. Read-only: properties are created by imports and the
    storage agent, typed in source code (app.property_specs)."""
    return await properties.catalog(await people.coverage())
