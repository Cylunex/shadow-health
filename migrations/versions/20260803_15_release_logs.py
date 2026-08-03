"""“起飞”从训练记录迁移为独立释放记录

Revision ID: 20260803u15
Revises: 20260712u14
Create Date: 2026-08-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260803u15"
down_revision: Union[str, None] = "20260712u14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_RELEASE_PREDICATE = """
lower(btrim(coalesce(session_type, ''))) = '起飞'
OR lower(btrim(coalesce(detail->>'title', ''))) = '起飞'
"""


def upgrade() -> None:
    op.create_table(
        "release_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("log_date", sa.Date(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_min", sa.Integer(), nullable=True),
        sa.Column("calories", sa.Integer(), nullable=True),
        sa.Column("avg_hr", sa.Integer(), nullable=True),
        sa.Column("max_hr", sa.Integer(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "source IN ('manual','samsung_zip','samsung_direct')",
            name="ck_release_source",
        ),
        sa.CheckConstraint("source = 'manual' OR external_id IS NOT NULL", name="ck_release_ext_required"),
        schema="health",
    )
    op.create_index("idx_release_logs_date", "release_logs", ["log_date"], schema="health")
    op.create_index(
        "ux_release_logs_ext",
        "release_logs",
        ["source", "external_id"],
        unique=True,
        schema="health",
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.execute(sa.text(f"""
        INSERT INTO health.release_logs
            (log_date, started_at, duration_min, calories, avg_hr, max_hr,
             detail, source, external_id, notes, created_at, updated_at)
        SELECT log_date, started_at, duration_min, calories, avg_hr, max_hr,
               detail, source, external_id, notes, created_at, updated_at
          FROM health.workout_logs
         WHERE {_RELEASE_PREDICATE}
        ON CONFLICT (source, external_id) WHERE external_id IS NOT NULL DO NOTHING
    """))
    op.execute(sa.text(f"DELETE FROM health.workout_logs WHERE {_RELEASE_PREDICATE}"))


def downgrade() -> None:
    op.execute(sa.text("""
        INSERT INTO health.workout_logs
            (log_date, started_at, session_type, duration_min, calories, avg_hr, max_hr,
             detail, source, external_id, notes, created_at, updated_at)
        SELECT log_date, started_at, '起飞', duration_min, calories, avg_hr, max_hr,
               detail, source, external_id, notes, created_at, updated_at
          FROM health.release_logs
        ON CONFLICT (source, external_id) WHERE external_id IS NOT NULL DO NOTHING
    """))
    op.drop_table("release_logs", schema="health")
