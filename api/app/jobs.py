"""Tiny in-process background job runner (the API runs a single worker)."""

import asyncio
import itertools
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

_ids = itertools.count(1)
_jobs: deque[dict] = deque(maxlen=50)
_tasks: set[asyncio.Task] = set()


def spawn(name: str, fn: Callable[[dict], Awaitable[Any]]) -> dict:
    """Run fn(job) in the background; fn may update job["progress"]."""
    job = {"id": next(_ids), "name": name, "status": "running", "progress": 0,
           "result": None, "error": None, "started_at": datetime.now(UTC).isoformat(),
           "finished_at": None}
    _jobs.appendleft(job)

    async def runner() -> None:
        try:
            job["result"] = await fn(job)
            job["status"] = "done"
        except Exception as e:
            log.exception("job %s failed", name)
            job["status"], job["error"] = "failed", str(e)
        finally:
            job["finished_at"] = datetime.now(UTC).isoformat()

    task = asyncio.create_task(runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


def recent() -> list[dict]:
    return list(_jobs)


async def drain() -> None:
    """Wait for running jobs (used by tests and the CLI)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
