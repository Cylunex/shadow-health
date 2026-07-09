"""diet_photos 餐次照片表（图片上传功能）

Revision ID: 20260709u3
Revises: 20260706u2
Create Date: 2026-07-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260709u3"
down_revision: Union[str, None] = "20260706u2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "diet_photos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("log_date", sa.Date(), nullable=False),
        sa.Column("meal", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("meal IN ('早餐','午餐','晚餐','加餐')", name="ck_photo_meal"),
        schema="health",
    )
    op.create_index("idx_diet_photos_date", "diet_photos", ["log_date"], schema="health")


def downgrade() -> None:
    op.drop_index("idx_diet_photos_date", table_name="diet_photos", schema="health")
    op.drop_table("diet_photos", schema="health")
