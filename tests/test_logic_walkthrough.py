"""Real PostgreSQL, synthetic records only: navigation and failure-path walkthroughs."""
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import (AgentRecordDraft, BodyMetrics, DietLog, DietPhoto, FitnessTest,
                        HealthEvidence, HealthGoal, HealthTask, ImportRaw, LabResult, SyncCursor)
from app.services import health_companion as hc
from app.services.agent_drafts import digest
from test_health_companion import db, create_draft


@pytest.fixture
def client(db, sso_headers):
    from app.main import app
    from app.db import get_db
    def override_db():
        yield db
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, headers=sso_headers, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        yield client
    app.dependency_overrides.pop(get_db, None)


OWNER = "browser:" + digest("test-user")[:40]
PAGES = ["/", "/metrics", "/diet", "/diet/foods", "/diet/recipes", "/discipline", "/labs",
         "/fitness", "/workout", "/workout/timer", "/workout/exercises", "/workout/plans",
         "/report", "/report/daily", "/report/annual", "/review", "/habits", "/scale",
         "/settings", "/settings/imports", "/settings/imports/new", "/agent-log", "/ai", "/companion",
         "/companion/badge", "/fragments/today/overview", "/fragments/today/scale-status",
         "/fragments/today/readiness", "/fragments/today/rings", "/fragments/diet/day",
         "/fragments/metrics/chart", "/fragments/metrics/quick", "/fragments/workout/plan-cards",
         "/fragments/workout/load", "/fragments/habits/today"]


@pytest.mark.parametrize("path", PAGES)
def test_all_feature_pages_and_fragments_render(client, path):
    response = client.get(path)
    assert response.status_code == 200, (path, response.text[:500])


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = set()
    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if (name == "hx-get" or (tag == "a" and name == "href")) and value and value.startswith("/"):
                self.urls.add(value)


def test_navigation_links_and_lazy_fragments_resolve(client):
    parser = Links()
    for path in PAGES:
        parser.feed(client.get(path).text)
    # External auth/exports/device actions are deliberately not exercised by navigation.
    for url in sorted(parser.urls):
        if url.startswith(("/auth", "/logout", "/settings/backup", "/settings/export", "/static", "/diet/photos")):
            continue
        response = client.get(url, follow_redirects=False)
        assert response.status_code in (200, 303, 307), (url, response.status_code, response.text[:300])


def test_expired_draft_recovery_is_idempotent_and_requires_new_approval(db, client):
    row = create_draft(db, agent=OWNER)
    row.payload = {**row.payload, "_expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
    db.flush()
    page = client.get(f"/companion/drafts/{row.draft_id}")
    assert "重新核对最新依据" in page.text and "确认以上全部内容" not in page.text
    url = f"/companion/drafts/{row.draft_id}/renew"
    data = {"revision": row.payload_hash}
    first = client.post(url, data=data, follow_redirects=False)
    second = client.post(url, data=data, follow_redirects=False)
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    new = db.get(AgentRecordDraft, row.payload["_superseded_by"])
    assert row.status == "rejected" and new.status == "pending"
    assert "确认以上全部内容" in client.get(first.headers["location"]).text
    assert "查看最新修订版" in client.get(f"/companion/drafts/{row.draft_id}").text


def test_photo_revision_moves_photo_only_after_approval(db, client):
    from app.routers.machine_agent import MachineHealthService
    day = hc.today_local()
    photo = DietPhoto(log_date=day, meal="午餐", filename="synthetic-test.jpg")
    db.add(photo); db.flush()
    row = create_draft(db, agent=OWNER)
    row.payload = {**row.payload, "_photo_id": photo.id}
    photo.analysis = {"draft_id": row.draft_id, "status": "pending_review"}
    db.flush()
    data = {"revision": row.payload_hash, "date": str(day - timedelta(days=1)), "meal": "晚餐",
            "name_0": "核对后的面", "kcal_0": "320", "amount_g_0": "160"}
    response = client.post(f"/companion/drafts/{row.draft_id}/revise", data=data, follow_redirects=False)
    assert response.status_code == 303
    repeat = client.post(f"/companion/drafts/{row.draft_id}/revise", data=data, follow_redirects=False)
    assert repeat.headers["location"] == response.headers["location"]
    assert photo.log_date == day and photo.meal == "午餐"
    revised = db.get(AgentRecordDraft, row.payload["_superseded_by"])
    MachineHealthService(db).commit_draft(revised)
    assert photo.log_date == day - timedelta(days=1) and photo.meal == "晚餐"
    record = db.get(DietLog, revised.payload["_result_ids"][0])
    assert record.log_date == photo.log_date and record.meal == photo.meal


def test_draft_replay_summary_describes_applied_state(db):
    from app.services.agent_drafts import propose_tool
    from app.routers.machine_agent import MachineHealthService
    args = {"date": str(hc.today_local()), "meal": "午餐", "items": [{"name": "测试食物"}]}
    result = propose_tool(db, "record_diet", args, OWNER, "replay-summary")
    MachineHealthService(db).commit_draft(db.get(AgentRecordDraft, result["draft_id"]))
    repeated = propose_tool(db, "record_diet", args, OWNER, "replay-summary")
    assert repeated["status"] == "applied" and "没有重复写入" in repeated["summary"]


def test_ai_page_rejoins_analysis_and_rotates_question_key(db, client, monkeypatch):
    from app.services import llm
    task = hc.enqueue(db, OWNER, "analysis", {"end": str(hc.today_local()), "days": 30}, uuid.uuid4().hex)
    hc.enqueue(db, OWNER, "weekly-review", {"end": str(hc.today_local()), "days": 7}, uuid.uuid4().hex)
    db.flush()
    assert task.id in client.get("/ai").text and task.id in client.get("/ai/analyze/status").text
    monkeypatch.setattr(llm, "ask", lambda *args: ("已生成草案", []))
    response = client.post("/ai/ask", data={"question": "记录午餐", "request_key": "initial-key"})
    assert 'id="ai-ask-key"' in response.text and 'hx-swap-oob="outerHTML"' in response.text
    assert 'value="initial-key"' not in response.text


def test_weekly_snooze_survives_next_day_and_dismiss_stops_work(db):
    row = hc.configure_monitor(db, "snooze-user", "weekly-review", "shadow")
    row.created_at = datetime(2026, 8, 1, tzinfo=UTC)
    row.mode = "inbox"
    monday = datetime(2026, 9, 7, 4, tzinfo=UTC)
    row.snoozed_until = monday + timedelta(days=1)
    hc.evaluate_monitor(db, row, monday)
    key = row.state["key"]
    assert not row.state["visible"]
    assert db.scalar(select(func.count()).select_from(HealthTask).where(HealthTask.owner == row.owner)) == 0
    hc.evaluate_monitor(db, row, monday + timedelta(days=1, minutes=1))
    assert row.state["key"] == key and row.state["visible"]
    task = db.scalar(select(HealthTask).where(HealthTask.owner == row.owner))
    assert task.payload["end"] == "2026-09-06"
    db.delete(task); db.flush()
    row.state = {**row.state, "dismissed_key": key}
    hc.evaluate_monitor(db, row, monday + timedelta(days=2))
    assert not row.state["visible"]
    assert db.scalar(select(func.count()).select_from(HealthTask).where(HealthTask.owner == row.owner)) == 0


def test_cached_monitor_hidden_at_night_or_after_recovery(db):
    row = hc.configure_monitor(db, "visibility-user", "sync-late", "shadow")
    row.mode = "inbox"
    now = datetime.now(UTC).replace(hour=4, minute=0)
    hc.evaluate_monitor(db, row, now)
    assert hc.monitor_visible(db, row, now)
    assert not hc.monitor_visible(db, row, now.replace(hour=16))  # midnight Shanghai
    db.add(ImportRaw(source="health_connect", external_id=uuid.uuid4().hex, record_type="steps", raw={}, imported_at=now))
    db.flush()
    assert not hc.monitor_visible(db, row, now)


def test_exhausted_task_does_not_starve_next_and_cancel_clears_lease(db, client):
    exhausted = hc.enqueue(db, OWNER, "analysis", {"end": str(hc.today_local())}, uuid.uuid4().hex)
    exhausted.attempts = 3
    next_task = hc.enqueue(db, OWNER, "analysis", {"end": str(hc.today_local())}, uuid.uuid4().hex)
    db.flush()
    task_id, _ = hc.claim(db)
    assert task_id == next_task.id and exhausted.status == "failed"
    assert "/retry" not in client.get(f"/companion/tasks/{exhausted.id}").text
    response = client.post(f"/companion/tasks/{next_task.id}/cancel", follow_redirects=False)
    assert response.status_code == 303
    assert next_task.lease_until is None and next_task.finished_at is not None


def test_goal_outcome_respects_revocation_and_empty_checkin(db, client):
    end = hc.today_local() - timedelta(days=120)
    db.add(BodyMetrics(log_date=end, weight_kg=73, autofilled={"weight_kg": "health_connect"})); db.flush()
    evidence = hc.create_evidence(db, OWNER, end)
    goal = hc.new_goal(db, OWNER, "规律记录", hc.today_local() + timedelta(days=7), evidence.id); db.flush()
    with pytest.raises(ValueError, match="感受"):
        hc.mutate_goal(db, goal, "checkin", 1, "  ")
    hc.mutate_goal(db, goal, "complete", 1)
    db.merge(SyncCursor(source="health_connect", record_type="weight", permission_state="revoked")); db.flush()
    assert "行动回看中的数据快照已隐藏" in client.get("/companion").text


def test_fitness_bad_second_value_does_not_save_first(db, client):
    day = date(2020, 8, 3)
    response = client.post("/fitness", data={"test_date": str(day), "pushup_max": "25", "plank_sec": "NaN"})
    assert response.status_code == 200 and "超出合理范围" in response.text
    assert not db.scalar(select(FitnessTest).where(FitnessTest.test_date == day))


def test_lab_units_and_ranges_never_mix(db, client):
    day = date(2020, 8, 3)
    response = client.post("/labs", data={"report_date": str(day), "item": "total_chol", "value": "180", "unit": "mg/dL"})
    assert response.status_code == 200
    row = db.scalar(select(LabResult).where(LabResult.report_date == day, LabResult.item_key == "total_chol"))
    assert row.ref_high is None and row.unit == "mg/dL"
    response = client.post("/labs", data={"report_date": str(day), "item": "total_chol", "value": "180", "ref_low": "200", "ref_high": "100"})
    assert "下限不能大于上限" in response.text
    db.add(LabResult(report_date=day-timedelta(days=1), item_key="total_chol", item_label="总胆固醇", value=5, unit="mmol/L")); db.flush()
    from app.routers.labs import _page_ctx
    groups = [g for g in _page_ctx(db)["groups"].values() if g["label"] == "总胆固醇"]
    assert len(groups) == 2 and {g["unit"] for g in groups} == {"mg/dL", "mmol/L"}


def test_bulk_lab_error_preserves_preview_and_saves_nothing(db, client):
    day = date(2020, 8, 5)
    response = client.post("/labs/bulk", data={"report_date": str(day), "label_0": "测试化验甲", "value_0": "12",
        "label_1": "测试化验乙", "value_1": "NaN"})
    assert "未保存任何行" in response.text and 'value="12"' in response.text
    assert not db.scalar(select(LabResult).where(LabResult.report_date == day))


def test_maintenance_failure_does_not_block_retention(db, monkeypatch):
    row = hc.configure_monitor(db, "bad-rule", "sync-late", "shadow")
    evidence = hc.create_evidence(db, "bad-rule", hc.today_local())
    evidence.expires_at = datetime.now(UTC) - timedelta(days=1); db.flush()
    def fail(*args):
        raise ValueError("synthetic rule failure")
    monkeypatch.setattr(hc, "evaluate_monitor", fail)
    @contextmanager
    def session_factory():
        yield db
    hc.maintenance(session_factory)
    db.refresh(evidence)
    assert evidence.payload == {"expired": True}


def test_expired_evidence_refresh_keeps_original_window(db, client):
    end = hc.today_local() - timedelta(days=50)
    evidence = hc.create_evidence(db, OWNER, end, 30)
    original = hc.enqueue(db, OWNER, "analysis", {"end": str(end), "days": 30}, uuid.uuid4().hex)
    original.status, original.result = "done", {"evidence_id": evidence.id}
    evidence.expires_at = datetime.now(UTC) - timedelta(days=1)
    evidence.payload = {"expired": True}
    db.flush()
    response = client.post(f"/companion/evidence/{evidence.id}/refresh",
        data={"request_key": uuid.uuid4().hex}, follow_redirects=False)
    assert response.status_code == 303
    new = db.get(HealthTask, response.headers["location"].rsplit("/", 1)[-1])
    assert new.payload == original.payload and new.status == "pending"


def test_goal_retry_reuses_existing_even_when_evidence_is_stale(db):
    end = hc.today_local() - timedelta(days=130)
    evidence = hc.create_evidence(db, OWNER, end)
    due = hc.today_local() + timedelta(days=7)
    original = hc.new_goal(db, OWNER, "记录习惯", due, evidence.id)
    db.add(BodyMetrics(log_date=end, weight_kg=78)); db.flush()
    assert hc.evidence_state(db, evidence) == "stale"
    assert hc.new_goal(db, OWNER, "记录习惯", due, evidence.id).id == original.id


def test_monitor_select_reflects_saved_mode(db, client):
    hc.configure_monitor(db, OWNER, "sync-late", "shadow"); db.flush()
    page = client.get("/companion")
    assert '<option value="shadow" selected>' in page.text
    assert '<option value="off" selected>' in page.text


def test_noisy_other_owner_drafts_cannot_hide_my_inbox(db, client):
    mine = create_draft(db, agent=OWNER)
    others = [AgentRecordDraft(draft_id="hd_"+uuid.uuid4().hex, agent_id="browser:someone-else",
        profile_id="primary", record_type="meal", effective_date=hc.today_local(),
        payload=mine.payload, payload_hash=mine.payload_hash, idempotency_key=uuid.uuid4().hex,
        status="pending") for _ in range(205)]
    db.add_all(others); db.flush()
    page = client.get("/companion")
    assert mine.draft_id in page.text and others[0].draft_id not in page.text


def test_stale_identity_map_cannot_approve_changed_record(db):
    from app.db import SessionLocal
    from app.machine_auth import MachineAPIError
    from app.routers.machine_agent import MachineHealthService
    record_id = draft_id = None
    try:
        with SessionLocal() as writer:
            record = DietLog(log_date=hc.today_local(), meal="午餐", free_text="并发测试", kcal=250)
            writer.add(record); writer.flush(); record_id = record.id
            draft = create_draft(writer, payload={"record_type":"meal", "effective_date":str(hc.today_local()),
                "operation":"update", "target_id":record.id, "fields":{"meal":"午餐", "name":"修订", "kcal":300}})
            draft_id = draft.draft_id; writer.commit()
        with SessionLocal() as reviewer:
            cached = reviewer.get(DietLog, record_id)
            pending = reviewer.get(AgentRecordDraft, draft_id)
            with SessionLocal() as editor:
                editor.get(DietLog, record_id).kcal = 280
                editor.commit()
            assert float(cached.kcal) == 250
            with pytest.raises(MachineAPIError, match="原记录已改变"):
                MachineHealthService(reviewer).commit_draft(pending)
            reviewer.rollback()
    finally:
        from sqlalchemy import delete
        with SessionLocal() as cleanup:
            if draft_id:
                cleanup.execute(delete(AgentRecordDraft).where(AgentRecordDraft.draft_id == draft_id))
            if record_id:
                cleanup.execute(delete(DietLog).where(DietLog.id == record_id))
            cleanup.commit()


def test_applied_draft_updates_homepage_sync_status(db):
    from app.models import SyncState
    from app.routers.machine_agent import MachineHealthService
    old = db.get(SyncState, "agent")
    if old:
        old.consecutive_failures = 2
        old.last_error = "historical failure"
        db.flush()
    MachineHealthService(db).commit_draft(create_draft(db))
    db.expire_all()
    state = db.get(SyncState, "agent")
    assert state.last_success_at is not None and state.consecutive_failures == 0 and state.last_error is None


def test_review_receipt_is_findable_after_leaving_confirmation(db, client):
    from app.routers.machine_agent import MachineHealthService
    row = create_draft(db, agent=OWNER)
    MachineHealthService(db).commit_draft(row)
    page = client.get("/companion")
    assert "最近审核回执" in page.text and f'/companion/drafts/{row.draft_id}' in page.text


def test_nonfinite_historical_value_is_missing_not_evidence(db):
    from decimal import Decimal
    end = hc.today_local() - timedelta(days=140)
    db.add(BodyMetrics(log_date=end, weight_kg=Decimal("NaN"))); db.flush()
    evidence = hc.create_evidence(db, OWNER, end)
    day = evidence.payload["daily"][-1]
    assert day["values"]["weight_kg"] is None
    assert not day["quality"]["weight_kg"]["present"]
