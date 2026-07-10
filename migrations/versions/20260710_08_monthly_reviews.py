"""monthly_reviews 表（月报：惰性快照 + 手写复盘，照 weekly_reviews 抄）

Revision ID: 20260710u8
Revises: 20260710u7
Create Date: 2026-07-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260710u8"
down_revision: Union[str, None] = "20260710u7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "monthly_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("metrics_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("extract(day FROM month_start) = 1", name="ck_month_first_day"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("month_start"),
        schema="health",
    )
    # 非 ORM 写入也维护 updated_at（与迁移 02 的共享触发器一致）
    op.execute(
        """
        CREATE TRIGGER trg_monthly_reviews_updated_at
        BEFORE UPDATE ON health.monthly_reviews
        FOR EACH ROW EXECUTE FUNCTION health.set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_monthly_reviews_updated_at ON health.monthly_reviews;"
    )
    op.drop_table("monthly_reviews", schema="health")
