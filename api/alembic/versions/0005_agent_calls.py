"""the ledger of MCP tool calls: who called which tool, with what shape of arguments, the outcome
the agent saw and how long it took

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_calls",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("api_key_id", sa.Integer, sa.ForeignKey("api_keys.id", ondelete="SET NULL")),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("session", sa.String(64)),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("args", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("result", postgresql.JSONB),
        sa.Column("ok", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("error", sa.Text),
        sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_agent_calls_key_time", "agent_calls", ["api_key_id", "created_at"])
    op.create_index("ix_agent_calls_tool", "agent_calls", ["tool"])


def downgrade() -> None:
    op.drop_table("agent_calls")
