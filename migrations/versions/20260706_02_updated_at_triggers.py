"""set_updated_at 共享触发器（设计文档 §3.6：非 ORM 写入也维护 updated_at）

Revision ID: 20260706u2
Revises: 117efa139a13
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260706u2"
down_revision: Union[str, None] = "117efa139a13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = [
    "body_metrics",
    "daily_activity",
    "diet_logs",
    "plan_enrollments",
    "workout_logs",
    "habit_logs",
    "weekly_reviews",
    "app_settings",
]


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION health.set_updated_at() RETURNS trigger AS $$
        BEGIN
          NEW.updated_at = now();
          RETURN NEW;
        END $$ LANGUAGE plpgsql;
        """
    )
    for table in TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON health.{table}
            FOR EACH ROW EXECUTE FUNCTION health.set_updated_at();
            """
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON health.{table};")
    op.execute("DROP FUNCTION IF EXISTS health.set_updated_at();")
