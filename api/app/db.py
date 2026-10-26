from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import get_settings

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(64), nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("role", String(32), nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

sessions = Table(
    "sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("ip", String(64)),
    Column("user_agent", Text),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(100), nullable=False),
    Column("prefix", String(16), nullable=False, unique=True),
    Column("key_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("created_by", Integer, ForeignKey("users.id", ondelete="SET NULL")),
    Column("last_used_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
)

searches = Table(
    "searches",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("api_key_id", Integer, ForeignKey("api_keys.id", ondelete="SET NULL")),
    Column("source", String(8), nullable=False),  # ui | api | mcp
    Column("params", JSONB, nullable=False),
    Column("result_ids", ARRAY(Text), nullable=False),
    Column("result_count", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_searches_user_created", "user_id", "created_at"),
)

activity = Table(
    "activity",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="SET NULL")),
    Column("api_key_id", Integer, ForeignKey("api_keys.id", ondelete="SET NULL")),
    Column("action", String(64), nullable=False),
    Column("target", Text),
    Column("meta", JSONB, nullable=False, server_default="{}"),
    Column("ip", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_activity_created", "created_at"),
    Index("ix_activity_user_created", "user_id", "created_at"),
)

field_embeddings = Table(
    "field_embeddings",
    metadata,
    Column("mongo_id", String(24), nullable=False),
    Column("field", String(128), nullable=False),
    Column("tier", String(32), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("embedding", HALFVEC(get_settings().embedding_dims), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("mongo_id", "field"),
    Index("ix_field_embeddings_field_tier", "field", "tier"),
)

_engine: AsyncEngine | None = None


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_size=5, max_overflow=5)
    return _engine


async def dispose() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
