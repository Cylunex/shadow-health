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
    op.drop_constraint("ck_import_source", "import_raw", schema="health", type_="check")
    op.create_check_constraint("ck_import_source", "import_raw", _RAW_OLD, schema="health")
    op.drop_constraint("ck_workout_source", "workout_logs", schema="health", type_="check")
    op.create_check_constraint("ck_workout_source", "workout_logs", _WK_OLD, schema="health")
