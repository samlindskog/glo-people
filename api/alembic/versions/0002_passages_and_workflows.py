"""one embedding per history passage; workflow runs

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows (one per person+field) stay valid under the wider key: no re-embedding needed.
    op.drop_constraint("field_embeddings_pkey", "field_embeddings", type_="primary")
    op.create_primary_key("field_embeddings_pkey", "field_embeddings",
                          ["mongo_id", "field", "content_hash"])
    op.execute("CREATE INDEX IF NOT EXISTS ix_field_embeddings_hnsw ON field_embeddings "
               "USING hnsw (embedding halfvec_cosine_ops)")

    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("workflow", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("params", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("done", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("credits_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("summary", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("error", sa.Text),
    )
    op.create_index("ix_workflow_runs_created", "workflow_runs", ["created_at"])
    op.create_table(
        "workflow_run_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("workflow", sa.String(64), nullable=False),
        sa.Column("person_id", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("error", sa.Text),
        sa.Column("credits", sa.Integer, nullable=False, server_default="0"),
        sa.Column("trace_id", sa.String(32)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_workflow_run_items_run_status", "workflow_run_items", ["run_id", "status"])
    op.create_index("ix_workflow_run_items_person", "workflow_run_items",
                    ["workflow", "person_id", "finished_at"])


def downgrade() -> None:
    op.drop_table("workflow_run_items")
    op.drop_table("workflow_runs")
    op.execute("DROP INDEX IF EXISTS ix_field_embeddings_hnsw")
    # Keep only the newest passage per (person, field) before narrowing the key again.
    op.execute("""
        DELETE FROM field_embeddings a USING field_embeddings b
        WHERE a.mongo_id = b.mongo_id AND a.field = b.field
          AND (a.updated_at, a.ctid) < (b.updated_at, b.ctid)
    """)
    op.drop_constraint("field_embeddings_pkey", "field_embeddings", type_="primary")
    op.create_primary_key("field_embeddings_pkey", "field_embeddings", ["mongo_id", "field"])
