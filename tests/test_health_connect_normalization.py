from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import delete, select, text

from app.models import DailyActivity, ImportRaw, ImportRawRevision, SyncState


def _db_ready() -> bool:
    try:
        from app.db import engine

        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture()
def api():
    if not _db_ready():
        pytest.skip("临时 PG 不可达")
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    token = get_settings().ingest_token
    if not token:
        pytest.skip("INGEST_TOKEN 未配置")
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


@pytest.fixture()
def hc_records():
    if not _db_ready():
        pytest.skip("临时 PG 不可达")
    from app.db import SessionLocal

    heart_id = f"test-heart-{uuid.uuid4().hex}"
    unknown_id = f"test-unknown-{uuid.uuid4().hex}"
    malformed_id = f"test-heart-bad-{uuid.uuid4().hex}"
    days = ("2098-01-01", "2098-01-02")
    with SessionLocal() as db:
        prior_state = db.get(SyncState, "health_connect")
        state_snapshot = None if prior_state is None else {
            "last_success_at": prior_state.last_success_at,
            "last_error": prior_state.last_error,
            "consecutive_failures": prior_state.consecutive_failures,
            "needs_reauth": prior_state.needs_reauth,
            "watermark": prior_state.watermark,
        }
        prior_days = {
            day: db.get(DailyActivity, datetime.fromisoformat(day).date())
            for day in days
        }
        day_snapshots = {
            day: None if row is None else {
                field: getattr(row, field)
                for field in (
                    "steps", "distance_m", "active_kcal", "hr_min", "hr_avg", "hr_max",
                    "source", "field_sources",
                )
            }
            for day, row in prior_days.items()
        }
    yield {
        "heart_id": heart_id,
        "unknown_id": unknown_id,
        "malformed_id": malformed_id,
        "days": days,
    }
    with SessionLocal() as db:
        db.execute(delete(ImportRaw).where(
            ImportRaw.source == "health_connect",
            ImportRaw.external_id.in_((heart_id, unknown_id, malformed_id)),
        ))
        for raw_day, snapshot in day_snapshots.items():
            day = datetime.fromisoformat(raw_day).date()
            row = db.get(DailyActivity, day)
            if snapshot is None:
                if row is not None:
                    db.delete(row)
            else:
                if row is None:
                    row = DailyActivity(log_date=day)
                    db.add(row)
                for field, value in snapshot.items():
                    setattr(row, field, value)
        state = db.get(SyncState, "health_connect")
        if state_snapshot is None:
            if state is not None:
                db.delete(state)
        else:
            assert state is not None
            for field, value in state_snapshot.items():
                setattr(state, field, value)
        db.commit()


def _heart_record(external_id: str, version: int, day: str, values: list[int]) -> dict:
    return {
        "recordType": "HeartRateRecord",
        "metadata": {"clientRecordId": external_id, "clientRecordVersion": version},
        "startTime": f"{day}T00:00:00Z",
        "endTime": f"{day}T00:10:00Z",
        "startZoneOffset": "+00:00",
        "samples": [
            {"time": f"{day}T00:0{index}:00Z", "beatsPerMinute": value}
            for index, value in enumerate(values)
        ],
    }


def test_heart_rate_ingest_rebuilds_canonical_day_on_version_update(
    api, hc_records
) -> None:
    first_day, second_day = hc_records["days"]
    external_id = hc_records["heart_id"]
    first = api.post(
        "/api/ingest/health_connect",
        json={"records": [_heart_record(external_id, 1, first_day, [60, 70, 80])]},
    )
    assert first.status_code == 200

    from app.db import SessionLocal

    with SessionLocal() as db:
        raw = db.execute(select(ImportRaw).where(
            ImportRaw.source == "health_connect", ImportRaw.external_id == external_id
        )).scalar_one()
        daily = db.get(DailyActivity, datetime.fromisoformat(first_day).date())
        assert raw.parse_status == "parsed"
        assert raw.pending_reason is None
        assert raw.normalized == {"dates": [first_day], "sample_count": 3}
        assert raw.normalization_attempts == 1
        assert (daily.hr_min, daily.hr_avg, daily.hr_max) == (60, 70, 80)
        assert daily.field_sources["hr_avg"] == "health_connect"

    revised = api.post(
        "/api/ingest/health_connect",
        json={"records": [_heart_record(external_id, 2, second_day, [88, 92])]},
    )
    assert revised.status_code == 200
    with SessionLocal() as db:
        old_daily = db.get(DailyActivity, datetime.fromisoformat(first_day).date())
        new_daily = db.get(DailyActivity, datetime.fromisoformat(second_day).date())
        raw = db.execute(select(ImportRaw).where(
            ImportRaw.source == "health_connect", ImportRaw.external_id == external_id
        )).scalar_one()
        revision = db.execute(select(ImportRawRevision).where(
            ImportRawRevision.import_raw_id == raw.id,
            ImportRawRevision.evidence_kind == "superseded",
        )).scalar_one()
        assert (old_daily.hr_min, old_daily.hr_avg, old_daily.hr_max) == (None, None, None)
        assert (new_daily.hr_min, new_daily.hr_avg, new_daily.hr_max) == (88, 90, 92)
        assert revision.record_version == 1
        assert revision.raw["samples"][0]["beatsPerMinute"] == 60

    conflict = _heart_record(external_id, 2, second_day, [200])
    conflicted = api.post("/api/ingest/health_connect", json={"records": [conflict]})
    assert conflicted.status_code == 200
    assert conflicted.json()["conflicts"] == 1
    with SessionLocal() as db:
        raw = db.execute(select(ImportRaw).where(
            ImportRaw.source == "health_connect", ImportRaw.external_id == external_id
        )).scalar_one()
        conflict_revision = db.execute(select(ImportRawRevision).where(
            ImportRawRevision.import_raw_id == raw.id,
            ImportRawRevision.evidence_kind == "version_conflict",
        )).scalar_one()
        daily = db.get(DailyActivity, datetime.fromisoformat(second_day).date())
        assert raw.parse_status == "parsed"
        assert conflict_revision.pending_reason == "version_conflict"
        assert conflict_revision.raw["samples"][0]["beatsPerMinute"] == 200
        assert (daily.hr_min, daily.hr_avg, daily.hr_max) == (88, 90, 92)


def test_unknown_and_malformed_records_are_explainable_and_target_replayable(
    api, hc_records
) -> None:
    unknown_id = hc_records["unknown_id"]
    malformed_id = hc_records["malformed_id"]
    response = api.post("/api/ingest/health_connect", json={"records": [
        {
            "recordType": "NutritionRecord",
            "metadata": {"clientRecordId": unknown_id},
            "time": "2098-01-01T00:00:00Z",
            "protein": 12,
        },
        {
            "recordType": "HeartRateRecord",
            "metadata": {"clientRecordId": malformed_id},
            "samples": [{"time": "2098-01-01T00:00:00Z", "beatsPerMinute": 999}],
        },
    ]})
    assert response.status_code == 200

    from app.db import SessionLocal

    with SessionLocal() as db:
        rows = {
            row.external_id: row
            for row in db.execute(select(ImportRaw).where(
                ImportRaw.source == "health_connect",
                ImportRaw.external_id.in_((unknown_id, malformed_id)),
            )).scalars()
        }
        assert rows[unknown_id].parse_status == "pending"
        assert rows[unknown_id].pending_reason == "unsupported_record_type"
        assert rows[unknown_id].raw["protein"] == 12
        assert rows[malformed_id].parse_status == "failed"
        assert rows[malformed_id].pending_reason == "heart_rate_value_invalid"
        assert rows[malformed_id].parse_error.startswith("heart_rate_value_invalid:")

    unknown_replay = api.post("/api/ingest/health_connect/replay", json={
        "record_type": "unknown", "external_id": unknown_id,
    })
    assert unknown_replay.status_code == 200
    assert unknown_replay.json()["still_pending"] == 1
    failed_replay = api.post("/api/ingest/health_connect/replay", json={
        "record_type": "heart_rate", "external_id": malformed_id,
    })
    assert failed_replay.status_code == 200
    assert failed_replay.json()["failed"] == 1

    state = api.get("/api/ingest/health_connect/state")
    assert state.status_code == 200
    queue = state.json()["normalization_queue"]
    assert any(
        item["record_type"] == "unknown"
        and item["reason"] == "unsupported_record_type"
        and item["count"] >= 1
        for item in queue
    )
