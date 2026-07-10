"""周报有氧词表 / 习惯 streak·月完成率 / auto_rule 解析 / 月报冻结时机的口径锁（审查 S2）。

全部纯函数，不依赖 DB。快照惰性落库后不可再生，口径改动必须先过这里。
"""
from dataclasses import dataclass
from datetime import date

from app.routers.habits import _OPS, _RULE_RE, _month_rate, _streak, _week_sums
from app.routers.report import _month_frozen_after
from app.routers.review import _is_cardio


@dataclass
class H:
    period: str = "daily"
    target_per_period: int = 1


TODAY = date(2026, 7, 10)  # 周五；本周一 = 7/6


# ---------- 有氧词表 ----------
def test_is_cardio_hits_common_types():
    for s in ("walking", "跑步", "Indoor Cycling", "HIIT 循环", "快走", "swimming", "有氧"):
        assert _is_cardio(s), s


def test_is_cardio_misses_strength_and_empty():
    for s in ("力量", "推撑", "壶铃摇摆", "", None):
        assert not _is_cardio(s), s


# ---------- 习惯 streak（daily/weekly，当期未达标不破连击） ----------
def test_daily_streak_counts_consecutive_days():
    logs = {date(2026, 7, d): 1 for d in (8, 9, 10)}
    assert _streak(H(), logs, TODAY)[0] == 3


def test_daily_streak_today_not_done_starts_yesterday():
    logs = {date(2026, 7, 8): 1, date(2026, 7, 9): 1}
    assert _streak(H(), logs, TODAY)[0] == 2


def test_daily_streak_gap_breaks():
    logs = {date(2026, 7, 7): 1, date(2026, 7, 9): 1, date(2026, 7, 10): 1}
    assert _streak(H(), logs, TODAY)[0] == 2


def test_veto_row_zero_does_not_count():
    # done_count=0 = auto_rule 否决行（迁移 09）：不计入达标，也不破更早的连击
    logs = {date(2026, 7, 9): 1, date(2026, 7, 10): 0}
    assert _streak(H(), logs, TODAY)[0] == 1


def test_weekly_streak_sums_within_week():
    habit = H(period="weekly", target_per_period=2)
    logs = {
        date(2026, 6, 29): 1, date(2026, 7, 1): 1,   # 上周合计 2 → 达标
        date(2026, 7, 6): 1, date(2026, 7, 8): 1,    # 本周合计 2 → 达标
    }
    assert _streak(habit, logs, TODAY)[0] == 2


# ---------- 本月完成率 ----------
def test_month_rate_daily():
    logs = {date(2026, 7, d): 1 for d in (1, 2, 3, 4, 5)}  # 10 天里达标 5 天
    assert _month_rate(H(), logs, TODAY) == 50


def test_month_rate_weekly_counts_started_weeks():
    habit = H(period="weekly", target_per_period=2)
    # 与 7 月相交的已开始周：6/29、7/6 共 2 周；只有 6/29 那周达标
    logs = {date(2026, 6, 30): 2}
    assert _month_rate(habit, logs, TODAY) == 50


def test_week_sums_groups_by_monday():
    logs = {date(2026, 7, 6): 1, date(2026, 7, 8): 2, date(2026, 7, 5): 1}
    sums = _week_sums(logs)
    assert sums[date(2026, 7, 6)] == 3      # 本周一
    assert sums[date(2026, 6, 29)] == 1     # 7/5 属上一周


# ---------- auto_rule 解析 ----------
def test_auto_rule_regex_accepts_threshold_forms():
    for raw, field, op, num in (
        ("steps>=8000", "steps", ">=", "8000"),
        ("sleep_hours >= 7.5", "sleep_hours", ">=", "7.5"),
        ("resting_hr<60", "resting_hr", "<", "60"),
    ):
        m = _RULE_RE.match(raw)
        assert m and m.group(1) == field and m.group(2) == op and m.group(3) == num, raw


def test_auto_rule_regex_rejects_garbage():
    for raw in ("", "steps", "steps>=", ">=8000", "steps>=8000; DROP TABLE", "步数>=8000"):
        assert _RULE_RE.match(raw) is None, raw


def test_auto_rule_ops_evaluate():
    assert _OPS[">="](8000.0, 8000.0)
    assert not _OPS[">"](8000.0, 8000.0)
    assert _OPS["<"](59.0, 60.0)


# ---------- 月报冻结时机（H1：末周跨月须等整周结束） ----------
def test_month_frozen_waits_for_cross_month_week():
    # 2026-06 的最后一个周一是 6/29，该周到 7/5 结束 → 7/6 才可冻结
    assert _month_frozen_after(date(2026, 6, 1)) == date(2026, 7, 6)


def test_month_frozen_no_delay_when_month_ends_on_sunday():
    # 2026-05-31 是周日：最后一周恰在月末结束，6/1 即可冻结
    assert _month_frozen_after(date(2026, 5, 1)) == date(2026, 6, 1)
