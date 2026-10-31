"""In-process publish/subscribe (level 0): the live side of event streams. A publisher puts a
dict on a topic; every attached queue of that topic gets it. Nothing is kept: a reader that
wants history attaches first, replays it from where it is persisted, then drains its queue. The
API runs one worker, so one process-wide hub is the whole fan-out."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

_subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)


def publish(topic: str, event: dict[str, Any]) -> None:
    for q in _subscribers.get(topic, ()):
        q.put_nowait(event)


def attach(topic: str) -> asyncio.Queue:
    """A queue that receives every event published on `topic` from now on."""
    q: asyncio.Queue = asyncio.Queue()
    _subscribers[topic].append(q)
    return q


def detach(topic: str, q: asyncio.Queue) -> None:
    subs = _subscribers.get(topic)
    if subs and q in subs:
        subs.remove(q)
        if not subs:
            del _subscribers[topic]


async def subscribe(topic: str) -> AsyncIterator[dict[str, Any]]:
    """Events published on `topic` from now on, until the consumer stops iterating."""
    q = attach(topic)
    try:
        while True:
            yield await q.get()
    finally:
        detach(topic, q)


def listeners(topic: str) -> int:
    return len(_subscribers.get(topic, ()))
