"""有效活动消耗统一口径与主要消费方回归。"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.activity_energy import _resolve

DAY = date(2020, 5, 20)


def test_effective_activity_energy_uses_max_without_double_counting_sources() -> None:
    workouts = [141, 148, 323]
    daily_wins = _resolve(800, workouts)
    assert daily_wins.kcal == 800
    assert daily_wins.source == "daily_activity"
    assert daily_wins.kcal != 800 + sum(workouts)

    workouts_win = _resolve(400, workouts)
    assert workouts_win.kcal == 612
    assert workouts_win.source == "workouts_higher"
    assert workouts_win.used_fallback is False


@pytest.mark.parametrize("daily", [None, 0, -50])
def test_workouts_are_fallback_for_missing_zero_or_negative_daily_activity(daily) -> None:
    result = _resolve(daily, [141, 148, 323])
    assert result.kcal == 612
    assert result.source == "workouts_fallback"
    assert result.used_fallback is True


def test_invalid_workout_values_are_ignored_and_abnormal_day_is_not_exposed() -> None:
    result = _resolve(None, [-20, float("nan"), 100, 30_000])
    assert result.kcal == 100
    assert result.workout_kcal == 100
    abnormal_total = _resolve(None, [11_000, 10_000])
    assert abnormal_total.kcal is None
    assert abnormal_total.source == "none"


def _db_ready() -> bool:
    try:
        from sqlalchemy import text

        from app.db import engine
        with engine.connect() as connection:
            connection.execute(text("SELECT 1 FROM health.workout_logs LIMIT 0"))
        return True
    except (OSError, SQLAlchemyError):
        return False


@pytest.fixture()
def db():
    if not _db_ready():
        pytest.skip("临时 PG 不可达")
    from sqlalchemy import delete

    from app.db import SessionLocal
    from app.models import BodyMetrics, DailyActivity, DietLog, WorkoutLog

    session = SessionLocal()
    for model in (WorkoutLog, DietLog, DailyActivity, BodyMetrics):
        session.execute(delete(model).where(model.log_date == DAY))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        for model in (WorkoutLog, DietLog, DailyActivity, BodyMetrics):
            session.execute(delete(model).where(model.log_date == DAY))
        session.commit()
        session.close()


def test_workout_fallback_reaches_today_diet_agent_report_and_ledger(db) -> None:
    from decimal import Decimal

    from app.models import BodyMetrics, DietLog, WorkoutLog
    from app.routers.agent import summary_data
    from app.routers.diet import _summary_ctx
    from app.routers.report import _daily_ctx
    from app.routers.today import _overview_ctx
    from app.services.energy import energy_ledger

    db.add_all([
        BodyMetrics(log_date=DAY, bmr_kcal=Decimal(1600), autofilled={}),
        DietLog(log_date=DAY, meal="午餐", free_text="测试餐", kcal=Decimal(1800)),
        WorkoutLog(log_date=DAY, session_type="循环训练", calories=141, source="manual"),
        WorkoutLog(log_date=DAY, session_type="步行", calories=148, source="manual"),
        WorkoutLog(log_date=DAY, session_type="步行", calories=323, source="manual"),
    ])
    db.commit()

    today = _overview_ctx(db, DAY)
    diet = _summary_ctx(db, DAY)
    agent = summary_data(db, DAY)
    report = _daily_ctx(db, DAY, DAY)
    ledger = energy_ledger(db, DAY, DAY)

    assert today["active_kcal"] == 612 and today["active_kcal_fallback"] is True
    assert diet["burn"] == 2212 and diet["energy_gap"] == -412
    assert agent["active_energy"] == {
        "kcal": 612, "source": "workouts_fallback", "used_fallback": True,
    }
    assert report["active_energy"].kcal == 612
    assert ledger is not None and ledger["gap_sum"] == -412
