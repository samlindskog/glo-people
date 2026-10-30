"""property name embeddings

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30
"""

import pgvector.sqlalchemy
import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

DIMS = 512


def upgrade() -> None:
    op.create_table(
        "property_embeddings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.HALFVEC(DIMS), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("property_embeddings")
