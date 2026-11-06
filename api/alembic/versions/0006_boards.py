"""glojobs.app boards: the shared catalogue of companies, jobs and talent, and the per-person
pages that list them by slug

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "board_companies",
        sa.Column("slug", sa.String(80), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("web", sa.Text, nullable=False, server_default=""),
        sa.Column("line", sa.Text, nullable=False, server_default=""),
        sa.Column("about_html", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_table(
        "board_jobs",
        sa.Column("slug", sa.String(120), primary_key=True),
        sa.Column("company_slug", sa.String(80),
                  sa.ForeignKey("board_companies.slug", ondelete="RESTRICT"), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("location", sa.Text, nullable=False, server_default=""),
        sa.Column("type", sa.Text, nullable=False, server_default=""),
        sa.Column("comp", sa.Text, nullable=False, server_default=""),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("html", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("source_url", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
    )
    op.create_index("ix_board_jobs_company", "board_jobs", ["company_slug"])
    op.create_table(
        "board_talent",
        sa.Column("slug", sa.String(120), primary_key=True),
        sa.Column("person_id", sa.String(24)),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("headline", sa.Text, nullable=False, server_default=""),
        sa.Column("location", sa.Text, nullable=False, server_default=""),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("html", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
    )
    op.create_index("ix_board_talent_person", "board_talent", ["person_id"])
    op.create_table(
        "boards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("person_id", sa.String(24), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("intro_html", sa.Text, nullable=False, server_default=""),
        sa.Column("job_slugs", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("talent_slugs", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("views", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
    )
    op.create_index("ix_boards_person", "boards", ["person_id"])


def downgrade() -> None:
    op.drop_table("boards")
    op.drop_table("board_talent")
    op.drop_table("board_jobs")
    op.drop_table("board_companies")
