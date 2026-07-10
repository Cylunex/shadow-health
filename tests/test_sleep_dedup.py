"""睡眠跨源去重（services/sleep.pick_preferred）：审查发现的翻倍 bug 的回归锁。"""
from dataclasses import dataclass
from datetime import date

from app.services.sleep import pick_preferred


@dataclass
class S:
    wake_date: date
    source: str
    total_sleep_min: int


D = date(2026, 7, 5)


def test_prefers_samsung_direct_over_zip():
    rows = [S(D, "samsung_zip", 400), S(D, "samsung_direct", 410)]
    picked = pick_preferred(rows)
    assert [s.total_sleep_min for s in picked[D]] == [410]


def test_prefers_hc_over_zip():
    rows = [S(D, "samsung_zip", 400), S(D, "health_connect", 405)]
    assert [s.total_sleep_min for s in pick_preferred(rows)[D]] == [405]


def test_same_source_segments_are_summable():
    # 同 source 多条 = 分段睡眠，全部保留
    rows = [S(D, "samsung_direct", 300), S(D, "samsung_direct", 90)]
    assert sorted(s.total_sleep_min for s in pick_preferred(rows)[D]) == [90, 300]


def test_unknown_source_lowest_priority():
    rows = [S(D, "manual_import", 999), S(D, "samsung_zip", 400)]
    assert [s.total_sleep_min for s in pick_preferred(rows)[D]] == [400]


def test_multiple_days_independent():
    d2 = date(2026, 7, 6)
    rows = [S(D, "samsung_zip", 400), S(d2, "samsung_direct", 410)]
    picked = pick_preferred(rows)
    assert picked[D][0].source == "samsung_zip"
    assert picked[d2][0].source == "samsung_direct"
