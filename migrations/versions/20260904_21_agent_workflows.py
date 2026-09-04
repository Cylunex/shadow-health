"""Domain tasks, evidence and explicitly managed personal routines.

Revision ID: 20260904u21
Revises: 20260901u20
"""
from alembic import op
import sqlalchemy as sa

revision = "20260904u21"
down_revision = "20260901u20"
branch_labels = depends_on = None


def upgrade():
    # Definitions are deliberately fixed here, independent of future ORM changes.
    def common():
        return [sa.Column("id", sa.Text, primary_key=True), sa.Column("owner", sa.Text, nullable=False)]

    def stamp(name="created_at", nullable=False):
        return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)

    op.create_table("health_tasks", *common(),
        sa.Column("task_key", sa.Text, nullable=False), sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False), sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("result", sa.JSON, nullable=False), sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("lease_token", sa.Text), stamp("lease_until", True), stamp(), stamp("finished_at", True),
        sa.UniqueConstraint("owner", "task_key"), schema="health")
    op.create_index("ix_health_tasks_status_lease", "health_tasks", ["status", "lease_until"], schema="health")
    op.create_table("health_evidence", *common(), sa.Column("profile_id", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False), sa.Column("fingerprint", sa.Text, nullable=False),
        stamp(), stamp("expires_at"), schema="health")
    op.create_table("health_preferences", *common(), sa.Column("name", sa.Text, nullable=False),
        sa.Column("value", sa.Text), sa.Column("status", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False), stamp("updated_at"), stamp("expires_at", True),
        sa.UniqueConstraint("owner", "name"), schema="health")
    op.create_table("health_goals", *common(), sa.Column("status", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False), sa.Column("plan", sa.JSON, nullable=False),
        sa.Column("history", sa.JSON, nullable=False), sa.Column("checkins", sa.JSON, nullable=False),
        sa.Column("outcome", sa.JSON, nullable=False), sa.Column("due_date", sa.Date, nullable=False),
        stamp(), schema="health")
    op.create_table("health_monitors", *common(), sa.Column("kind", sa.Text, nullable=False),
        sa.Column("mode", sa.Text, nullable=False), sa.Column("config", sa.JSON, nullable=False),
        sa.Column("state", sa.JSON, nullable=False), stamp("snoozed_until", True), stamp(),
        sa.UniqueConstraint("owner", "kind"), schema="health")


def downgrade():
    for table in ("health_monitors", "health_goals", "health_preferences", "health_evidence", "health_tasks"):
        op.drop_table(table, schema="health")
