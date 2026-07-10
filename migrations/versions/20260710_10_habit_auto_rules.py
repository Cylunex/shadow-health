"""存量习惯补 auto_rule（seed 幂等不回填已有行，用数据迁移送达生产库）

- 23点前睡  → sleep_start_clock<23（手表睡眠自动判定，凌晨钟点读 24+）
- 称重×2    → weight_kg>=20（当日有体重记录即 +1，weekly 周内求和）
- 量腰围×1  → waist_cm>=30
只更新 auto_rule 为空的行——用户手动配置过的不覆盖。

Revision ID: 20260710u10
Revises: 20260710u9
Create Date: 2026-07-10

"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260710u10"
down_revision: Union[str, None] = "20260710u9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RULES = {
    "23点前睡": "sleep_start_clock<23",
    "称重×2": "weight_kg>=20",
    "量腰围×1": "waist_cm>=30",
}


def upgrade() -> None:
    for name, rule in _RULES.items():
        op.execute(
            "UPDATE health.habits SET auto_rule = "
            f"'{rule}' WHERE name = '{name}' AND auto_rule IS NULL"
        )


def downgrade() -> None:
    for name, rule in _RULES.items():
        op.execute(
            "UPDATE health.habits SET auto_rule = NULL "
            f"WHERE name = '{name}' AND auto_rule = '{rule}'"
        )
