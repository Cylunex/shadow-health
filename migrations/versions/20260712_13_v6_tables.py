"""meal_templates（组合菜谱）+ achievements（成就首达记录）——V6 批次 P5/P8

- meal_templates：把某天某餐存成可复用组合（items JSONB：food_id 行存
  food_id+amount_g 记录时按食物库现值重算；free_text 行原样快照），一键整组记录。
- achievements：成就「首次达成」落档（key 主键幂等）——成就本体按数据实时计算，
  这张表只记「哪天第一次达成」，供徽章墙显示日期与 digest 庆祝新达成。

Revision ID: 20260712u13
Revises: 20260712u12
Create Date: 2026-07-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260712u13"
down_revision: Union[str, None] = "20260712u12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "meal_templates",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        schema="health",
    )
    op.create_table(
        "achievements",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("earned_on", sa.Date(), nullable=False),
        schema="health",
    )


def downgrade() -> None:
    op.drop_table("achievements", schema="health")
    op.drop_table("meal_templates", schema="health")
