"""habit_logs.done_count 允许 0：auto_rule 习惯手动撤销的「当日否决」标记

之前撤销 = 删行，下次今日面板加载 _apply_auto_rules 发现规则仍满足会幂等补插，
撤销被静默还原。改为保留 done_count=0 的行占住 (habit_id, log_date) 唯一键，
ON CONFLICT DO NOTHING 自然不再补插；所有达标判断均为 done_count >= target，
0 值行不计入任何统计。

Revision ID: 20260710u9
Revises: 20260710u8
Create Date: 2026-07-10

"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260710u9"
down_revision: Union[str, None] = "20260710u8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_done_count", "habit_logs", schema="health", type_="check")
    op.create_check_constraint(
        "ck_done_count", "habit_logs", "done_count >= 0", schema="health"
    )


def downgrade() -> None:
    # 否决行在旧词表下非法，先清掉（单用户自用可接受）
    op.execute("DELETE FROM health.habit_logs WHERE done_count = 0")
    op.drop_constraint("ck_done_count", "habit_logs", schema="health", type_="check")
    op.create_check_constraint(
        "ck_done_count", "habit_logs", "done_count > 0", schema="health"
    )
