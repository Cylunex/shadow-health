"""Shadow Agent plugin drafts and metadata-only audit.

Revision ID: 20260822u17
Revises: 20260803u16
Create Date: 2026-08-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260822u17"
down_revision: Union[str, None] = "20260803u16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_record_drafts",
        sa.Column("draft_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("draft_id"),
        sa.UniqueConstraint(
            "agent_id", "idempotency_key", name="ux_agent_draft_idempotency"
        ),
        schema="health",
    )
    op.create_index(
        "ix_agent_record_drafts_profile_created",
        "agent_record_drafts",
        ["profile_id", "created_at"],
        schema="health",
    )
    op.create_table(
        "agent_machine_audit",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("capability", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("resource_uri", sa.Text(), nullable=True),
        sa.Column("detail_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="health",
    )
    op.create_index(
        "ix_agent_machine_audit_created",
        "agent_machine_audit",
        ["created_at"],
        schema="health",
    )
    op.create_index(
        "ix_agent_machine_audit_agent",
        "agent_machine_audit",
        ["agent_id", "created_at"],
        schema="health",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_machine_audit_agent",
        table_name="agent_machine_audit",
        schema="health",
    )
    op.drop_index(
        "ix_agent_machine_audit_created",
        table_name="agent_machine_audit",
        schema="health",
    )
    op.drop_table("agent_machine_audit", schema="health")
    op.drop_index(
        "ix_agent_record_drafts_profile_created",
        table_name="agent_record_drafts",
        schema="health",
    )
    op.drop_table("agent_record_drafts", schema="health")
