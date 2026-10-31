"""assistant runs that pause, resume and survive restarts: worker/heartbeat on runs, items that
are rows or batches (nullable person, payload, attempts), and the run events table

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("worker", sa.String(64)))
    op.add_column("workflow_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("workflow_runs", sa.Column("label", sa.Text))
    op.add_column("workflow_runs", sa.Column("pause_reason", sa.Text))

    op.alter_column("workflow_run_items", "person_id", existing_type=sa.String(24), nullable=True)
    op.add_column("workflow_run_items", sa.Column("payload", postgresql.JSONB, nullable=False,
                                                  server_default="{}"))
    op.add_column("workflow_run_items", sa.Column("label", sa.Text))
    op.add_column("workflow_run_items", sa.Column("attempts", sa.Integer, nullable=False,
                                                  server_default="0"))

    op.create_table(
        "workflow_run_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("workflow_run_items.id", ondelete="CASCADE")),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("data", postgresql.JSONB, nullable=False, server_default="{}"),
    )
    op.create_index("ix_workflow_run_events_run", "workflow_run_events", ["run_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_workflow_run_events_run", "workflow_run_events")
    op.drop_table("workflow_run_events")
    op.drop_column("workflow_run_items", "attempts")
    op.drop_column("workflow_run_items", "label")
    op.drop_column("workflow_run_items", "payload")
    op.execute("DELETE FROM workflow_run_items WHERE person_id IS NULL")
    op.alter_column("workflow_run_items", "person_id", existing_type=sa.String(24), nullable=False)
    op.drop_column("workflow_runs", "pause_reason")
    op.drop_column("workflow_runs", "label")
    op.drop_column("workflow_runs", "heartbeat_at")
    op.drop_column("workflow_runs", "worker")
