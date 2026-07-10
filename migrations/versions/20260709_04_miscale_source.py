"""import_raw 来源词表加 'miscale'（小米体脂秤 BLE 网关通道）

Revision ID: 20260709u4
Revises: 20260709u3
Create Date: 2026-07-09

"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260709u4"
down_revision: Union[str, None] = "20260709u3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "source IN ('samsung_zip','health_connect','keep_api','keep_file')"
_NEW = "source IN ('samsung_zip','health_connect','keep_api','keep_file','miscale')"


def upgrade() -> None:
    op.drop_constraint("ck_import_source", "import_raw", schema="health", type_="check")
    op.create_check_constraint("ck_import_source", "import_raw", _NEW, schema="health")


def downgrade() -> None:
    # miscale 存量行在旧词表下非法，先清掉——否则重建 CHECK 校验失败、降级中止
    # （单用户自用可接受；原始测量可由秤/手机端重放）
    op.execute("DELETE FROM health.import_raw WHERE source = 'miscale'")
    op.drop_constraint("ck_import_source", "import_raw", schema="health", type_="check")
    op.create_check_constraint("ck_import_source", "import_raw", _OLD, schema="health")
