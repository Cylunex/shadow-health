"""diet_logs 加脂肪/碳水两列（对标薄荷/MyFitnessPal 三大营养素完整记录）

Revision ID: 20260710u7
Revises: 20260710u6
Create Date: 2026-07-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260710u7"
down_revision: Union[str, None] = "20260710u6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("diet_logs", sa.Column("fat_g", sa.Numeric(5, 1)), schema="health")
    op.add_column("diet_logs", sa.Column("carb_g", sa.Numeric(6, 1)), schema="health")


def downgrade() -> None:
    op.drop_column("diet_logs", "carb_g", schema="health")
    op.drop_column("diet_logs", "fat_g", schema="health")
