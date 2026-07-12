"""mood_score 全链路口径锁（V4 A2）：指标图表注册/日报字段/周报月报快照均值 +
旧快照缺 avg_mood 字段的容错显示。

前半纯函数直测；后半带 DB 集成（Mac 临时 PG 55433），不可达自动跳过。
测试日期用 today-350 附近对齐周一（错开 offline 的 -300 与 agent 的 -320）。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.routers.metrics import _CHART_METRICS, _COLORS, _METRIC_KEYS
from app.routers.report import _BODY_FIELDS, _month_cards, _month_start_of
from app.routers.review import _snapshot_cards, _week_start_of


# ---------- 注册面：图表词表 / 日报字段 ----------

def test_mood_registered_in_chart_metrics():
    assert ("mood", "心情") in _CHART_METRICS
    assert "mood" in _METRIC_KEYS
    assert "mood" in _COLORS


def test_mood_in_daily_report_body_fields():
    assert ("mood_score", "心情", "/10") in _BODY_FIELDS


# ---------- 快照卡片：有值显示、旧快照缺字段容错不显示 ----------

def _labels(cards):
    return [c["label"] for c in cards]


def test_weekly_cards_show_mood_when_present():
    cards = _snapshot_cards({"avg_mood": 7.5, "mood_days": 3})
    assert "心情均分" in _labels(cards)
    card = next(c for c in cards if c["label"] == "心情均分")
    assert card["value"] == "7.5/10"
    assert "3 天" in card["sub"]


def test_weekly_cards_tolerate_legacy_snapshot_without_mood():
    # 已有快照惰性落库不可再生：缺 avg_mood 键必须整卡不显示，不能炸/不能显示占位
    assert "心情均分" not in _labels(_snapshot_cards({}))


# 月快照必有的基础键（_aggregate_month 恒写入；卡片渲染依赖）
_MONTH_BASE = {"days_in_month": 30, "target_steps": 8000}


def test_monthly_cards_show_mood_when_present():
    cards = _month_cards({**_MONTH_BASE, "avg_mood": 6.8, "mood_days": 10})
    card = next(c for c in cards if c["label"] == "心情均分")
    assert card["value"] == "6.8/10"


def test_monthly_cards_tolerate_legacy_snapshot_without_mood():
    assert "心情均分" not in _labels(_month_cards(_MONTH_BASE))


# ---------- DB 集成：周/月快照均值口径（不可达自动跳过） ----------

def _db_ready() -> bool:
    try:
        from sqlalchemy import text

        from app.db import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture()
def db():
    if not _db_ready():
        pytest.skip("临时 PG 不可达")
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def mood_week(db):
    """测试周内写两天 mood（6 和 9）；已有行只改 mood 字段并在收尾还原。"""
    from sqlalchemy import select

    from app.models import BodyMetrics
    from app.timeutil import today_local

    ws = _week_start_of(today_local() - timedelta(days=350))
    ms = _month_start_of(ws)
    # 该月已有别的 mood 数据会掺进均值，口径断言就不成立了——直接跳过（理论情形：
    # mood_score 列 2026-07 才加，一年前不会有值）
    exist = db.execute(
        select(BodyMetrics.id).where(
            BodyMetrics.log_date.between(ms, ms + timedelta(days=40)),
            BodyMetrics.mood_score.is_not(None),
        )
    ).first()
    if exist is not None:
        pytest.skip("测试月已有 mood 数据")

    created_ids, touched = [], []  # touched: (id, 原值=None)
    for d, score in ((ws, 6), (ws + timedelta(days=2), 9)):
        row = db.execute(
            select(BodyMetrics).where(BodyMetrics.log_date == d)
        ).scalar_one_or_none()
        if row is None:
            row = BodyMetrics(log_date=d, mood_score=score)
            db.add(row)
            db.flush()
            created_ids.append(row.id)
        else:
            touched.append(row.id)
            row.mood_score = score
    db.commit()
    yield ws
    for rid in created_ids:
        row = db.get(BodyMetrics, rid)
        if row is not None:
            db.delete(row)
    for rid in touched:
        row = db.get(BodyMetrics, rid)
        if row is not None:
            row.mood_score = None
    db.commit()


def test_aggregate_week_mood_average(db, mood_week):
    from app.routers.review import _aggregate_week

    snap = _aggregate_week(db, mood_week)
    assert snap["avg_mood"] == 7.5  # (6+9)/2
    assert snap["mood_days"] == 2


def test_aggregate_month_mood_average(db, mood_week):
    from app.routers.report import _aggregate_month

    # 两条测试行都在 ws 所在月（ws 为周一，ws+2 最多跨到次月 2 号——跨月时以
    # ws 所在月为准断言该月至少含 ws 那天的 6 分）
    ms = _month_start_of(mood_week)
    snap = _aggregate_month(db, ms)
    if _month_start_of(mood_week + timedelta(days=2)) == ms:
        assert snap["avg_mood"] == 7.5
        assert snap["mood_days"] == 2
    else:
        assert snap["avg_mood"] == 6.0
        assert snap["mood_days"] == 1


def test_chart_context_mood_renders(db):
    from app.routers.metrics import _chart_context

    ctx = _chart_context(db, "mood", 30)["chart"]
    assert ctx["metric"] == "mood"
    assert '心情分' in ctx["payload_json"]
