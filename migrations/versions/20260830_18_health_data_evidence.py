"""Add stable source-record evidence and per-type sync cursors.

Revision ID: 20260830u18
Revises: 20260822u17
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830u18"
down_revision: Union[str, None] = "20260822u17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "import_raw",
        sa.Column(
            "client_record_id",
            sa.Text(),
            sa.Computed("external_id", persisted=True),
            nullable=False,
        ),
        schema="health",
    )
    op.add_column(
        "import_raw",
        sa.Column("record_version", sa.Integer(), server_default="0", nullable=False),
        schema="health",
    )
    op.add_column(
        "import_raw", sa.Column("payload_hash", sa.Text(), nullable=True), schema="health"
    )
    op.add_column(
        "import_raw",
        sa.Column("provenance", postgresql.JSONB(), nullable=True),
        schema="health",
    )
    op.add_column(
        "import_raw",
        sa.Column("normalized", postgresql.JSONB(), nullable=True),
        schema="health",
    )
    op.create_check_constraint(
        "ck_import_raw_record_version",
        "import_raw",
        "record_version >= 0",
        schema="health",
    )
    op.create_index(
        "ix_import_raw_identity_version",
        "import_raw",
        ["source", "record_type", "client_record_id", "record_version"],
        schema="health",
    )

    op.create_table(
        "sync_cursors",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("cursor_token", sa.Text(), nullable=True),
        sa.Column("watermark", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "permission_state", sa.Text(), server_default="unknown", nullable=False
        ),
        sa.Column("source_fingerprint", sa.Text(), nullable=True),
        sa.Column(
            "needs_resync", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("source_changed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "permission_state IN ('unknown','granted','denied','revoked')",
            name="ck_sync_cursor_permission",
        ),
        sa.PrimaryKeyConstraint("source", "record_type"),
        schema="health",
    )
    # Preserve the old zip/live overlap boundary while allowing each type to
    # advance independently from this point forward.
    op.execute(
        """
        INSERT INTO health.sync_cursors (source, record_type, watermark)
        SELECT 'health_connect', record_type, watermark
        FROM health.sync_state
        CROSS JOIN (VALUES ('steps'), ('weight'), ('sleep'), ('exercise'), ('heart_rate'))
          AS supported(record_type)
        WHERE source = 'health_connect'
        ON CONFLICT (source, record_type) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("sync_cursors", schema="health")
    op.drop_index(
        "ix_import_raw_identity_version", table_name="import_raw", schema="health"
    )
    op.drop_column("import_raw", "normalized", schema="health")
    op.drop_column("import_raw", "provenance", schema="health")
    op.drop_column("import_raw", "payload_hash", schema="health")
    op.drop_constraint(
        "ck_import_raw_record_version",
        "import_raw",
        schema="health",
        type_="check",
    )
    op.drop_column("import_raw", "record_version", schema="health")
    op.drop_column("import_raw", "client_record_id", schema="health")
