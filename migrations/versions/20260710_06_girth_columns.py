"""body_metrics 加围度四列（胸/臂/大腿/臀，对标 Keep 围度追踪）

Revision ID: 20260710u6
Revises: 20260710u5
Create Date: 2026-07-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260710u6"
down_revision: Union[str, None] = "20260710u5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = ("chest_cm", "arm_cm", "thigh_cm", "hip_cm")


def upgrade() -> None:
    for col in _COLS:
        op.add_column("body_metrics", sa.Column(col, sa.Numeric(5, 1)), schema="health")


def downgrade() -> None:
    for col in _COLS:
        op.drop_column("body_metrics", col, schema="health")
