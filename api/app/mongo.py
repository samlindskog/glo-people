from pymongo import ASCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.config import get_settings

_client: AsyncMongoClient | None = None


def db() -> AsyncDatabase:
    global _client
    if _client is None:
        _client = AsyncMongoClient(get_settings().mongo_url, tz_aware=True)
    return _client[get_settings().mongo_db]


async def ensure_indexes() -> None:
    people = db().people
    await people.create_index([("source.file", ASCENDING), ("source.source_id", ASCENDING)],
                              unique=True,
                              partialFilterExpression={"source.source_id": {"$exists": True}})
    await people.create_index([("tier", ASCENDING)])
    await people.create_index([("name.v", ASCENDING)])


async def close() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
