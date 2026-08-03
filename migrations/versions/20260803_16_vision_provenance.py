"""识图结果增加可追溯元数据

Revision ID: 20260803u16
Revises: 20260803u15
Create Date: 2026-08-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260803u16"
down_revision: Union[str, None] = "20260803u15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "diet_logs",
        sa.Column("provenance", postgresql.JSONB(), nullable=True),
        schema="health",
    )
    op.add_column(
        "diet_photos",
        sa.Column("analysis", postgresql.JSONB(), nullable=True),
        schema="health",
    )
    op.add_column(
        "lab_results",
        sa.Column("provenance", postgresql.JSONB(), nullable=True),
        schema="health",
    )


def downgrade() -> None:
    op.drop_column("lab_results", "provenance", schema="health")
    op.drop_column("diet_photos", "analysis", schema="health")
    op.drop_column("diet_logs", "provenance", schema="health")
