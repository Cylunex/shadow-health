"""来源词表加 'agent'/'legacy' + body_metrics.mood_score（V3 批次 P2，docs/subpath-agent-plan.md §2）

- 'agent'：多 Agent 写通道（/api/ingest/agent，Hermes/OpenClaw 经 MCP 统一写入）
- 'legacy'：P3 personal_data 旧库迁移的留档来源（脚本本批只交付不执行，词表先备好）
- mood_score：心情分 1~10（旧库 daily_summary.mood_score 的落点，用户拍板提前到本阶段）

Revision ID: 20260712u12
Revises: 20260711u11
Create Date: 2026-07-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260712u12"
down_revision: Union[str, None] = "20260711u11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RAW_OLD = (
    "source IN ('samsung_zip','health_connect','keep_api','keep_file','miscale',"
    "'samsung_direct','offline')"
)
_RAW_NEW = (
    "source IN ('samsung_zip','health_connect','keep_api','keep_file','miscale',"
    "'samsung_direct','offline','agent','legacy')"
)


def upgrade() -> None:
    op.drop_constraint("ck_import_source", "import_raw", schema="health", type_="check")
    op.create_check_constraint("ck_import_source", "import_raw", _RAW_NEW, schema="health")
    op.add_column(
        "body_metrics", sa.Column("mood_score", sa.SmallInteger(), nullable=True), schema="health"
    )
    op.create_check_constraint(
        "ck_mood_score", "body_metrics", "mood_score BETWEEN 1 AND 10", schema="health"
    )


def downgrade() -> None:
    # agent/legacy 存量留档在旧词表下非法，先清掉——否则重建 CHECK 校验失败、降级中止。
    # 归一化行不清：habit/diet/metric 无来源标记，workout 走 source='manual'+external_id
    # 前缀，旧词表下均合法（单用户自用可接受，与迁移 05/11 同策略）
    op.execute("DELETE FROM health.import_raw WHERE source IN ('agent','legacy')")
    op.execute("DELETE FROM health.sync_state WHERE source IN ('agent','legacy')")
    op.drop_constraint("ck_import_source", "import_raw", schema="health", type_="check")
    op.create_check_constraint("ck_import_source", "import_raw", _RAW_OLD, schema="health")
    op.drop_constraint("ck_mood_score", "body_metrics", schema="health", type_="check")
    op.drop_column("body_metrics", "mood_score", schema="health")
