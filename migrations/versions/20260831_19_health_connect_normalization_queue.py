"""Add auditable Health Connect normalization, revisions and field sources.

Revision ID: 20260831u19
Revises: 20260830u18
Create Date: 2026-08-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260831u19"
down_revision: Union[str, None] = "20260830u18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "daily_activity",
        sa.Column(
            "field_sources",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="health",
    )
    op.execute(
        """
        UPDATE health.daily_activity
        SET field_sources = jsonb_strip_nulls(jsonb_build_object(
          'steps', CASE WHEN steps IS NOT NULL THEN source END,
          'distance_m', CASE WHEN distance_m IS NOT NULL THEN source END,
          'active_kcal', CASE WHEN active_kcal IS NOT NULL THEN source END,
          'hr_min', CASE WHEN hr_min IS NOT NULL THEN source END,
          'hr_avg', CASE WHEN hr_avg IS NOT NULL THEN source END,
          'hr_max', CASE WHEN hr_max IS NOT NULL THEN source END
        ))
        """
    )
    op.add_column(
        "import_raw", sa.Column("pending_reason", sa.Text(), nullable=True), schema="health"
    )
    op.add_column(
        "import_raw",
        sa.Column(
            "normalization_attempts", sa.Integer(), server_default="0", nullable=False
        ),
        schema="health",
    )
    op.add_column(
        "import_raw",
        sa.Column("last_normalization_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="health",
    )
    op.create_check_constraint(
        "ck_import_raw_normalization_attempts",
        "import_raw",
        "normalization_attempts >= 0",
        schema="health",
    )
    op.create_index(
        "ix_import_raw_pending_reason",
        "import_raw",
        ["source", "record_type", "pending_reason"],
        schema="health",
        postgresql_where=sa.text("parse_status IN ('pending','failed')"),
    )
    op.create_table(
        "import_raw_revisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("import_raw_id", sa.BigInteger(), nullable=False),
        sa.Column("record_version", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("evidence_kind", sa.Text(), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=True),
        sa.Column("normalized", postgresql.JSONB(), nullable=True),
        sa.Column("parse_status", sa.Text(), nullable=False),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column("pending_reason", sa.Text(), nullable=True),
        sa.Column("parse_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "record_version >= 0", name="ck_import_raw_revision_version"
        ),
        sa.CheckConstraint(
            "evidence_kind IN ('superseded','version_conflict')",
            name="ck_import_raw_revision_kind",
        ),
        sa.ForeignKeyConstraint(
            ["import_raw_id"], ["health.import_raw.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "import_raw_id",
            "record_version",
            "payload_hash",
            name="ux_import_raw_revision_evidence",
        ),
        schema="health",
    )
    op.create_index(
        "ix_import_raw_revision_parent",
        "import_raw_revisions",
        ["import_raw_id", "received_at"],
        schema="health",
    )

    # Existing unsupported rows become explainable without changing their raw
    # evidence. Existing heart-rate rows are intentionally left pending so the
    # explicit replay endpoint can normalize them under the new parser.
    op.execute(
        """
        UPDATE health.import_raw
        SET pending_reason = CASE
          WHEN parse_error LIKE '同一 client_record_id/version%%' THEN 'version_conflict'
          WHEN record_type = 'unknown' THEN 'unsupported_record_type'
          WHEN record_type = 'heart_rate' THEN 'parser_upgrade_available'
          WHEN parse_status = 'failed' THEN 'normalization_failed'
          ELSE 'awaiting_normalization'
        END
        WHERE source = 'health_connect'
          AND parse_status IN ('pending', 'failed')
          AND pending_reason IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_import_raw_revision_parent",
        table_name="import_raw_revisions",
        schema="health",
    )
    op.drop_table("import_raw_revisions", schema="health")
    op.drop_index(
        "ix_import_raw_pending_reason", table_name="import_raw", schema="health"
    )
    op.drop_constraint(
        "ck_import_raw_normalization_attempts",
        "import_raw",
        schema="health",
        type_="check",
    )
    op.drop_column("import_raw", "last_normalization_at", schema="health")
    op.drop_column("import_raw", "normalization_attempts", schema="health")
    op.drop_column("import_raw", "pending_reason", schema="health")
    op.drop_column("daily_activity", "field_sources", schema="health")
