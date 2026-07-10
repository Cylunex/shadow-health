"""来源词表加 'offline'（手机离线记录补发通道，docs/offline-plan.md 阶段一）

Revision ID: 20260711u11
Revises: 20260710u10
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260711u11"
down_revision: Union[str, None] = "20260710u10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RAW_OLD = (
    "source IN ('samsung_zip','health_connect','keep_api','keep_file','miscale','samsung_direct')"
)
_RAW_NEW = (
    "source IN ('samsung_zip','health_connect','keep_api','keep_file','miscale',"
    "'samsung_direct','offline')"
)


def upgrade() -> None:
    op.drop_constraint("ck_import_source", "import_raw", schema="health", type_="check")
    op.create_check_constraint("ck_import_source", "import_raw", _RAW_NEW, schema="health")


def downgrade() -> None:
    # offline 存量留档在旧词表下非法，先清掉——否则重建 CHECK 校验失败、降级中止。
    # 归一化行不清：habit/diet 无来源标记，workout 走 source='manual'+external_id
    # 前缀，旧词表下均合法（单用户自用可接受，与迁移 05 同策略）
    op.execute("DELETE FROM health.import_raw WHERE source = 'offline'")
    op.execute("DELETE FROM health.sync_state WHERE source = 'offline'")
    op.drop_constraint("ck_import_source", "import_raw", schema="health", type_="check")
    op.create_check_constraint("ck_import_source", "import_raw", _RAW_OLD, schema="health")
