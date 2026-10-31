"""Tool invocations over HTTP (level 4 surface): the handle a surface gets back from starting a
tool on one record. Read its state and result, follow its activity as it happens (Server-Sent
Events, replayable from `Last-Event-ID`), stop it before it commits."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sse_starlette.sse import EventSourceResponse

from app.auth import Principal, require
from app.tools import invoke

router = APIRouter(prefix="/api/invocations", tags=["invocations"])

Reader = Annotated[Principal, Depends(require("people:read"))]
Writer = Annotated[Principal, Depends(require("people:write"))]

PING_SECONDS = 15        # keeps the proxy's read timeout from cutting a quiet stream


def _get(invocation_id: str) -> invoke.Invocation:
    inv = invoke.get(invocation_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invocation not found (or expired)")
    return inv


@router.get("/{invocation_id}")
async def get_invocation(invocation_id: str, _: Reader) -> dict:
    """The invocation's state, and its result once done."""
    return _get(invocation_id).public(include_result=True)


@router.get("/{invocation_id}/events")
async def invocation_events(invocation_id: str, _: Reader,
                            last_event_id: Annotated[str | None, Header()] = None):
    """The tool's activity as Server-Sent Events: every event so far, then live ones, ending with
    the `result` event. `Last-Event-ID` resumes after an event sequence number."""
    inv = _get(invocation_id)
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

    async def events():
        async for ev in inv.subscribe(after):
            yield {"event": ev.kind, "id": str(ev.seq), "data": ev.model_dump_json()}

    return EventSourceResponse(events(), ping=PING_SECONDS, headers={"X-Accel-Buffering": "no"})


@router.post("/{invocation_id}/stop")
async def stop_invocation(invocation_id: str, _: Writer) -> dict:
    """Stop the tool before it commits: nothing is written. 409 when it already returned."""
    inv = _get(invocation_id)
    if not inv.stop():
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "too late to stop: the tool has finished" if inv.status != "running"
                            else "too late to stop: the tool is committing")
    return inv.public()
