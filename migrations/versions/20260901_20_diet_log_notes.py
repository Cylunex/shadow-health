"""Add a human-readable notes field to diet logs.

Revision ID: 20260901u20
Revises: 20260831u19
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260901u20"
down_revision: Union[str, None] = "20260831u19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing records intentionally remain NULL. Food names may legitimately
    # contain parentheses, so no heuristic backfill is safe here.
    op.add_column(
        "diet_logs", sa.Column("notes", sa.Text(), nullable=True), schema="health"
    )
    op.create_check_constraint(
        "ck_diet_notes_length",
        "diet_logs",
        "notes IS NULL OR char_length(notes) <= 1000",
        schema="health",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_diet_notes_length", "diet_logs", schema="health", type_="check"
    )
    op.drop_column("diet_logs", "notes", schema="health")
