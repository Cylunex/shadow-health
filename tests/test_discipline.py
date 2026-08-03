"""三星“起飞”分流、自律统计与页面回归。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.models import ReleaseLog, WorkoutLog
from app.routers.ingest import _upsert_samsung_exercise
from app.services.discipline import discipline_summary, is_release_session


def test_release_name_is_exact():
    assert is_release_session("起飞")
    assert is_release_session(" 起飞 ")
    assert is_release_session("other", "起飞")
    assert not is_release_session("起飞训练")
    assert not is_release_session("running")


def _db_ready() -> bool:
    try:
        from app.db import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM health.release_logs LIMIT 1"))
        return True
    except Exception:
        return False


@pytest.fixture()
def db():
    if not _db_ready():
        pytest.skip("release_logs 测试库不可达或未迁移")
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _release(day: date, suffix: str) -> ReleaseLog:
    return ReleaseLog(
        log_date=day,
        started_at=datetime(day.year, day.month, day.day, 14, tzinfo=timezone.utc),
        duration_min=10,
        source="samsung_direct",
        external_id=f"test-release-{suffix}-{uuid4().hex}",
    )


def test_discipline_summary_streaks_and_counts(db):
    for day, suffix in (
        (date(2020, 1, 1), "a"),
        (date(2020, 1, 5), "b"),
        (date(2020, 1, 6), "c"),
    ):
        db.add(_release(day, suffix))
    db.flush()

    summary = discipline_summary(db, date(2020, 1, 10))
    assert summary["current_days"] == 4
    assert summary["longest_days"] == 4
    assert summary["month_count"] == 3
    assert summary["recent_count"] == 3
    assert summary["total_count"] == 3


def test_samsung_takeoff_moves_between_release_and_workout(db):
    sid = f"sd-test-discipline-{uuid4().hex}"
    data = {
        "sid": sid,
        "start": "2020-02-03T14:00:00+00:00",
        "type": "起飞",
        "duration_min": 13,
        "distance_km": None,
        "calories": 100,
        "avg_hr": 90,
        "max_hr": 110,
    }
    _upsert_samsung_exercise(db, data)
    db.flush()
    release = db.execute(
        select(ReleaseLog).where(ReleaseLog.external_id == sid)
    ).scalar_one()
    assert release.duration_min == 13
    assert db.execute(
        select(WorkoutLog).where(WorkoutLog.external_id == sid)
    ).scalar_one_or_none() is None

    data["type"] = "running"
    data["distance_km"] = 2.5
    _upsert_samsung_exercise(db, data)
    db.flush()
    assert db.execute(
        select(ReleaseLog).where(ReleaseLog.external_id == sid)
    ).scalar_one_or_none() is None
    workout = db.execute(
        select(WorkoutLog).where(WorkoutLog.external_id == sid)
    ).scalar_one()
    assert workout.session_type == "running"


def test_discipline_page_renders(db):
    from fastapi.testclient import TestClient

    from app import auth
    from app.main import app

    token = auth.create_session()
    with TestClient(app) as client:
        client.cookies.set(auth.SESSION_COOKIE, token)
        response = client.get("/discipline")
    assert response.status_code == 200
    assert "当前连续自律" in response.text or "还没有释放记录" in response.text
    assert "最近 90 天" in response.text
    assert "不计入训练次数" in response.text
