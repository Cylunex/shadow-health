"""来源词表加 'samsung_direct'（三星健康 Data SDK 直读通道）

Revision ID: 20260710u5
Revises: 20260709u4
Create Date: 2026-07-10

"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260710u5"
down_revision: Union[str, None] = "20260709u4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RAW_OLD = "source IN ('samsung_zip','health_connect','keep_api','keep_file','miscale')"
_RAW_NEW = (
    "source IN ('samsung_zip','health_connect','keep_api','keep_file','miscale','samsung_direct')"
)
_WK_OLD = "source IN ('manual','keep','samsung_zip','health_connect')"
_WK_NEW = "source IN ('manual','keep','samsung_zip','health_connect','samsung_direct')"


def upgrade() -> None:
    op.drop_constraint("ck_import_source", "import_raw", schema="health", type_="check")
    op.create_check_constraint("ck_import_source", "import_raw", _RAW_NEW, schema="health")
    op.drop_constraint("ck_workout_source", "workout_logs", schema="health", type_="check")
    op.create_check_constraint("ck_workout_source", "workout_logs", _WK_NEW, schema="health")


def downgrade() -> None:
    # samsung_direct 存量数据在旧词表下非法，先清掉——否则重建 CHECK 校验失败、
    # 降级中止（单用户自用可接受；直读通道会按水位线重新同步）
    op.execute("DELETE FROM health.import_raw WHERE source = 'samsung_direct'")
    op.execute("DELETE FROM health.workout_logs WHERE source = 'samsung_direct'")
    op.execute("DELETE FROM health.sleep_sessions WHERE source = 'samsung_direct'")
    op.execute("DELETE FROM health.sync_state WHERE source = 'samsung_direct'")
    op.drop_constraint("ck_import_source", "import_raw", schema="health", type_="check")
    op.create_check_constraint("ck_import_source", "import_raw", _RAW_OLD, schema="health")
    op.drop_constraint("ck_workout_source", "workout_logs", schema="health", type_="check")
    op.create_check_constraint("ck_workout_source", "workout_logs", _WK_OLD, schema="health")
