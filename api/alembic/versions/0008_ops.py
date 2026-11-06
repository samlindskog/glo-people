"""Owned operational telemetry: every request (the API's own, and nginx's over syslog) and every
warning or error the app logs, with a request id tying them together

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("host", sa.Text, nullable=False, server_default=""),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("route", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.Integer, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("client_ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("user_agent", sa.Text, nullable=False, server_default=""),
        sa.Column("request_id", sa.String(32)),
        sa.Column("attrs", postgresql.JSONB, nullable=False, server_default="{}"),
    )
    op.create_index("ix_request_log_ts", "request_log", ["ts"])
    op.create_index("ix_request_log_request", "request_log", ["request_id"])
    op.create_table(
        "app_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("logger", sa.Text, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("traceback", sa.Text),
        sa.Column("request_id", sa.String(32)),
    )
    op.create_index("ix_app_log_ts", "app_log", ["ts"])
    op.create_index("ix_app_log_request", "app_log", ["request_id"])


def downgrade() -> None:
    op.drop_table("app_log")
    op.drop_table("request_log")
