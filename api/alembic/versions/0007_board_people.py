"""The people the boards refer to, as a table: real foreign keys from boards and talent entries
to a mirrored person row (kept by the write observer), one talent entry per person

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-06
"""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "people",
        sa.Column("id", sa.String(24), primary_key=True),
        sa.Column("name", sa.Text, nullable=False, server_default=""),
        sa.Column("tier", sa.String(16), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    # Every person a board or talent entry already names gets a row, name and tier blank until
    # the next write or `python -m app.cli boards-link` fills them from the document (a person
    # who no longer exists is dropped there, taking the rows that named them along).
    op.execute(
        "INSERT INTO people (id) SELECT DISTINCT person_id FROM boards "
        "UNION SELECT DISTINCT person_id FROM board_talent WHERE person_id IS NOT NULL "
        "ON CONFLICT DO NOTHING")
    op.create_foreign_key("fk_boards_person", "boards", "people", ["person_id"], ["id"],
                          ondelete="CASCADE")
    op.create_foreign_key("fk_board_talent_person", "board_talent", "people", ["person_id"],
                          ["id"], ondelete="CASCADE")
    # One entry per person. A duplicate would have to go by hand first; none existed on 2026-11-06.
    op.create_index("ux_board_talent_person", "board_talent", ["person_id"], unique=True,
                    postgresql_where=sa.text("person_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ux_board_talent_person", table_name="board_talent")
    op.drop_constraint("fk_board_talent_person", "board_talent", type_="foreignkey")
    op.drop_constraint("fk_boards_person", "boards", type_="foreignkey")
    op.drop_table("people")
