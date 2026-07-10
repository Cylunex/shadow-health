"""月报纯函数口径锁（routers/report）：月份边界、ISO 周归属、连击最大值、month_start 校验。"""
from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app.routers.report import (
    _max_run,
    _month_end,
    _month_mondays,
    _month_start_of,
    _next_month_start,
    _parse_month_start,
    _prev_month_start,
)


# ---------- 月份边界 ----------
def test_month_bounds_regular():
    assert _month_start_of(date(2026, 7, 10)) == date(2026, 7, 1)
    assert _next_month_start(date(2026, 7, 1)) == date(2026, 8, 1)
    assert _month_end(date(2026, 7, 1)) == date(2026, 7, 31)


def test_month_bounds_year_wrap():
    assert _next_month_start(date(2026, 12, 1)) == date(2027, 1, 1)
    assert _prev_month_start(date(2027, 1, 1)) == date(2026, 12, 1)


def test_month_end_february_leap():
    assert _month_end(date(2028, 2, 1)) == date(2028, 2, 29)  # 2028 闰年
    assert _month_end(date(2027, 2, 1)) == date(2027, 2, 28)


# ---------- ISO 周归属（周一落在本月才算本月的周） ----------
def test_month_mondays_june_2026_starts_on_monday():
    # 2026-06-01 是周一：6 月有 5 个周一
    mondays = _month_mondays(date(2026, 6, 1))
    assert mondays == [date(2026, 6, d) for d in (1, 8, 15, 22, 29)]


def test_month_mondays_july_2026():
    # 2026-07-01 是周三：第一个周一是 7-06，共 4 个
    mondays = _month_mondays(date(2026, 7, 1))
    assert mondays == [date(2026, 7, d) for d in (6, 13, 20, 27)]


def test_month_mondays_all_within_month_and_are_mondays():
    for ms in (date(2026, 1, 1), date(2026, 2, 1), date(2026, 12, 1), date(2028, 2, 1)):
        mondays = _month_mondays(ms)
        assert 4 <= len(mondays) <= 5
        for w in mondays:
            assert w.isoweekday() == 1
            assert _month_start_of(w) == ms
        # 每周只归属一次：相邻正好差 7 天
        assert all((b - a).days == 7 for a, b in zip(mondays, mondays[1:]))


# ---------- 月内最长连击 ----------
MS, ME = date(2026, 6, 1), date(2026, 6, 30)


def test_max_run_empty():
    assert _max_run(set(), MS, ME) == 0


def test_max_run_full_month():
    days = {MS + timedelta(days=i) for i in range(30)}
    assert _max_run(days, MS, ME) == 30


def test_max_run_picks_longest_of_multiple_runs():
    days = {date(2026, 6, d) for d in (1, 2, 5, 6, 7, 8, 20)}
    assert _max_run(days, MS, ME) == 4  # 5~8


def test_max_run_ignores_days_outside_range():
    # 5 月末的连续记录不影响 6 月口径（连击只算月内区间）
    days = {date(2026, 5, 30), date(2026, 5, 31), date(2026, 6, 1)}
    assert _max_run(days, MS, ME) == 1


def test_max_run_touching_month_end():
    days = {date(2026, 6, d) for d in (28, 29, 30)}
    assert _max_run(days, MS, ME) == 3


# ---------- month_start 校验 ----------
def test_parse_month_start_valid():
    assert _parse_month_start("2026-06-01") == date(2026, 6, 1)
    assert _parse_month_start(" 2026-06-01 ") == date(2026, 6, 1)


def test_parse_month_start_rejects_non_first_day():
    with pytest.raises(HTTPException) as exc:
        _parse_month_start("2026-06-15")
    assert exc.value.status_code == 404


def test_parse_month_start_rejects_garbage():
    with pytest.raises(HTTPException) as exc:
        _parse_month_start("not-a-date")
    assert exc.value.status_code == 404
