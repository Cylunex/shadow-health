"""表单数值/日期解析的防御性行为（NaN 500 与未来日期是审查修复项）。"""
from datetime import date, timedelta

import pytest

from app.routers.diet import _parse_decimal
from app.routers.metrics import _parse_date as metrics_parse_date
from app.routers.workout import _parse_date as workout_parse_date
from app.timeutil import today_local


@pytest.mark.parametrize("bad", ["nan", "NaN", "sNaN", "inf", "-Infinity"])
def test_diet_decimal_rejects_non_finite(bad):
    with pytest.raises(ValueError):
        _parse_decimal(bad, "热量", 20000)


def test_diet_decimal_normal_and_empty():
    assert _parse_decimal("", "热量", 20000) is None
    # quantize(0.1) 默认银行家舍入：123.45 → 123.4（营养值显示口径，可接受）
    assert float(_parse_decimal("123.45", "热量", 20000)) == 123.4


def test_future_dates_clamped_to_today():
    future = (today_local() + timedelta(days=30)).isoformat()
    assert metrics_parse_date(future) == today_local()
    assert workout_parse_date(future) == today_local()


def test_past_date_passthrough():
    assert metrics_parse_date("2026-01-15") == date(2026, 1, 15)
