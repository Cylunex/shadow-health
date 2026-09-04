"""Outcome-oriented Agent evals; PG cases only run against the dedicated test DB."""
from datetime import UTC, date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.models import AgentRecordDraft, BodyMetrics, DietLog, HealthEvidence, HealthGoal, HealthMonitor, HealthPreference, HealthTask
from app.machine_auth import MachineAPIError, MachinePrincipal
from app.routers.machine_agent import MachineHealthService, _validate_draft_payload
from app.services import health_companion as hc
from app.services.agent_drafts import approve_match, digest, propose_tool
from app.timeutil import today_local


def facts(before=7, current=7, changed=False):
    rows = []
    for i in range(14):
        present = i < before if i < 7 else i - 7 < current
        source = "new-device" if changed and i >= 7 else "manual"
        rows.append({"date": str(date(2026, 8, 1) + timedelta(days=i)),
            "values": {k: (70 if i < 7 else 71) if present else None for k in hc.METRICS},
            "quality": {k: {"present": present, "sources": [source] if present else []} for k in hc.METRICS}})
    return {"days": 7, "daily": rows}


@pytest.mark.parametrize("before,current", [(0,0),(1,7),(7,1),(2,2),(2,7),(7,2),(0,7),(7,0)])
def test_insufficient_data_never_invents_delta(before, current):
    for card in hc.analyze_facts(facts(before, current)):
        assert card["delta"] is None
        if current < 3:
            assert card["average"] is None


def test_disjoint_windows_and_source_change():
    assert hc.analyze_facts(facts())[0]["delta"] == 1
    assert hc.analyze_facts(facts(changed=True))[0]["delta"] is None


@pytest.mark.parametrize("field,value", [("kcal",-1),("amount_g",6000),("protein_g",2000),("fat_g",float('nan')),("carb_g",float('inf'))])
def test_draft_numeric_bounds(field, value):
    with pytest.raises(ValueError):
        _validate_draft_payload({"record_type":"meal","effective_date":str(today_local()),
            "fields":{"meal":"午餐","name":"面",field:value}})


@pytest.mark.parametrize("field", ["bp_systolic","spo2_pct","raw_sql","shell","actor_sub"])
def test_sensitive_fields_are_not_draft_tools(field):
    with pytest.raises(ValueError):
        _validate_draft_payload({"record_type":"metric","effective_date":str(today_local()),"fields":{field:80}})


@pytest.mark.parametrize("text", ['<script>alert(1)</script>', '[x](javascript:alert(1))', '<img src=x onerror=alert(1)>'])
def test_untrusted_model_html_not_executable(text):
    from app.routers.ai import _render_md
    rendered = _render_md(text)
    assert '<script' not in rendered and '<img' not in rendered and '<a ' not in rendered


@pytest.mark.parametrize("path,method", [("/api/ingest/agent","post"),("/api/agent/delete","post"),
    ("/api/agent/update","post"),("/api/agent/context","get"),("/api/agent/analysis","post")])
def test_legacy_api_is_closed_even_with_device_token(path, method):
    from app.main import app
    client = TestClient(app)
    response = getattr(client, method)(path, headers={"Authorization": "Bearer device-test-token"})
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "legacy_agent_retired"


@pytest.fixture
def db():
    from app.db import engine, SessionLocal
    if engine.url.database != "shadow_health_agent_test":
        pytest.skip("requires isolated shadow_health_agent_test database")
    with engine.connect() as conn:
        transaction = conn.begin()
        from sqlalchemy.orm import Session
        session = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        yield session
        session.close()
        transaction.rollback()


def create_draft(db, *, payload=None, key=None, agent="test-helper"):
    payload = payload or {"record_type":"meal","effective_date":str(today_local()),
        "fields":{"meal":"午餐","name":"测试面","items":[{"name":"测试面","kcal":400,"amount_g":200}]}}
    return MachineHealthService(db).create_draft(principal=MachinePrincipal(agent, frozenset(), frozenset(), {}),
        profile_id="primary", idempotency_key=key or uuid.uuid4().hex, payload=payload, payload_hash=digest(payload))[0]


def test_draft_only_then_atomic_receipt(db):
    before = db.scalar(select(func.count()).select_from(DietLog))
    row = create_draft(db)
    assert db.scalar(select(func.count()).select_from(DietLog)) == before
    receipt, replayed = MachineHealthService(db).commit_draft(row)
    assert receipt.startswith("shadow://health/diet/") and not replayed
    assert MachineHealthService(db).commit_draft(row) == (receipt, True)
    assert db.scalar(select(func.count()).select_from(DietLog)) == before + 1


def test_rejected_and_expired_cannot_apply(db):
    row = create_draft(db)
    MachineHealthService(db).reject_draft(row)
    with pytest.raises(MachineAPIError):
        MachineHealthService(db).commit_draft(row)
    expired = create_draft(db)
    expired.payload = {**expired.payload, "_expires_at": (datetime.now(UTC)-timedelta(seconds=1)).isoformat()}
    db.flush()
    with pytest.raises(MachineAPIError, match="过期"):
        MachineHealthService(db).commit_draft(expired)


def test_same_key_different_payload_conflicts(db):
    key = uuid.uuid4().hex
    row = create_draft(db, key=key)
    with pytest.raises(MachineAPIError):
        create_draft(db, key=key, payload={"record_type":"metric","effective_date":str(today_local()),"fields":{"weight_kg":80}})
    with pytest.raises(MachineAPIError):
        approve_match(row, "wrong-content")


def test_update_keeps_before_and_refuses_changed_target(db):
    record = DietLog(log_date=today_local(), meal="午餐", free_text="原食物", kcal=300)
    db.add(record); db.flush()
    payload = {"record_type":"meal","effective_date":str(today_local()),"operation":"update", "target_id":record.id,
               "fields":{"meal":"午餐","name":"修正食物","kcal":350}}
    row = create_draft(db, payload=payload)
    record.kcal = 320
    db.flush()
    with pytest.raises(MachineAPIError, match="原记录已改变"):
        MachineHealthService(db).commit_draft(row)
    second = create_draft(db, payload=payload)
    MachineHealthService(db).commit_draft(second)
    assert record.provenance["revisions"][-1]["before"]["kcal"] == "320"
    assert record.free_text == "修正食物"


def test_tool_without_identity_cannot_write(db):
    from app.services.ai_tools import run_tool
    result = run_tool(db,"record_diet",{"date":str(today_local()),"meal":"午餐","items":[{"name":"面"}]})
    assert "error" in result


def test_preferences_forget_and_owner_isolation(db):
    hc.set_preference(db,"one","training_focus","everyday")
    db.flush()
    assert hc.preferences(db,"one") == {"training_focus":"everyday"}
    assert hc.preferences(db,"two") == {}
    hc.set_preference(db,"one","training_focus",forget=True); db.flush()
    assert hc.preferences(db,"one") == {}
    assert db.scalar(select(HealthPreference).where(HealthPreference.owner=="one")).value is None


@pytest.mark.parametrize("name,value",[("diagnosis","x"),("training_focus","run_sql"),("notification_style","always")])
def test_memory_allowlist(db,name,value):
    with pytest.raises(ValueError): hc.set_preference(db,"one",name,value)


def test_evidence_late_data_and_expiry(db):
    end = today_local()-timedelta(days=40)
    row = hc.create_evidence(db,"one",end)
    assert hc.evidence_state(db,row)=="current"
    db.add(BodyMetrics(log_date=end, weight_kg=72)); db.flush()
    assert hc.evidence_state(db,row)=="stale"
    row.expires_at = datetime.now(UTC)-timedelta(seconds=1)
    assert hc.evidence_state(db,row)=="expired"


def test_goal_version_checkin_and_cancel(db):
    evidence=hc.create_evidence(db,"one",today_local()-timedelta(days=30))
    goal=hc.new_goal(db,"one","规律作息",today_local()+timedelta(days=7),evidence.id); db.flush()
    hc.mutate_goal(db,goal,"checkin",1,"今天有做到")
    with pytest.raises(MachineAPIError): hc.mutate_goal(db,goal,"pause",1)
    hc.mutate_goal(db,goal,"pause",2)
    hc.mutate_goal(db,goal,"resume",3)
    hc.mutate_goal(db,goal,"revise",4,"尽量准时休息")
    assert goal.history[0]["plan"]["title"]=="规律作息"
    hc.mutate_goal(db,goal,"cancel",5)
    assert goal.status=="cancelled"


def test_monitor_shadow_gate_quiet_dismiss_and_off(db):
    row=hc.configure_monitor(db,"one","sync-late","shadow"); db.flush()
    with pytest.raises(ValueError): hc.configure_monitor(db,"one","sync-late","inbox")
    hc.evaluate_monitor(db,row)
    assert row.state["visible"] is False
    row.created_at=datetime.now(UTC)-timedelta(days=8)
    row.state={**row.state,"observed_days":[str(today_local()-timedelta(days=d)) for d in range(7)]}
    hc.configure_monitor(db,"one","sync-late","inbox")
    now=datetime(2026,9,4,4,tzinfo=UTC)
    hc.evaluate_monitor(db,row,now)
    assert row.state["visible"] is True
    row.state={**row.state,"dismissed_key":row.state["key"]}
    hc.evaluate_monitor(db,row,now)
    assert row.state["visible"] is False
    hc.configure_monitor(db,"one","sync-late","off")
    assert row.state=={}


def test_expired_lease_reclaims_and_retry_cap(db):
    task=hc.enqueue(db,"one","weekly-review",{"end":str(today_local())},uuid.uuid4().hex)
    db.commit()
    id1, token1=hc.claim(db)
    task.lease_until=datetime.now(UTC)-timedelta(seconds=1); db.commit()
    id2, token2=hc.claim(db)
    assert id1==id2 and token1!=token2 and task.attempts==2
    task.attempts=3; task.lease_until=datetime.now(UTC)-timedelta(seconds=1); db.commit()
    assert hc.claim(db) is None
    assert task.status=="failed"


def test_templates_compile():
    from app.deps import templates
    for name in templates.env.list_templates():
        templates.env.get_template(name)


def test_two_concurrent_approvals_create_one_record(db):
    from app.db import SessionLocal
    # Separate connections are necessary to exercise PostgreSQL row locks.
    with SessionLocal() as session:
        row = create_draft(session, agent="concurrency-" + uuid.uuid4().hex)
        draft_id = row.draft_id
        session.commit()
    def approve():
        with SessionLocal() as session:
            row = session.get(AgentRecordDraft, draft_id)
            receipt = MachineHealthService(session).commit_draft(row)
            session.commit()
            return receipt
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _: approve(), range(2)))
        assert receipts[0][0] == receipts[1][0]
        assert sorted(r[1] for r in receipts) == [False, True]
    finally:
        with SessionLocal() as session:
            row = session.get(AgentRecordDraft, draft_id)
            session.execute(delete(DietLog).where(DietLog.id.in_(row.payload.get("_result_ids", []))))
            session.delete(row); session.commit()


def test_worker_commits_result_once_and_recovers(db):
    from app.db import SessionLocal
    owner = "worker-" + uuid.uuid4().hex
    with SessionLocal() as session:
        task = hc.enqueue(session,owner,"weekly-review",{"end":str(today_local()),"days":7},uuid.uuid4().hex)
        session.commit(); task_id = task.id
    try:
        assert hc.run_once(SessionLocal)
        with SessionLocal() as session:
            row = session.get(HealthTask,task_id)
            assert row.status == "done" and row.attempts == 1
            evidence_id = row.result["evidence_id"]
            assert session.get(HealthEvidence,evidence_id).owner == owner
        assert not hc.run_once(SessionLocal)
    finally:
        with SessionLocal() as session:
            session.execute(delete(HealthTask).where(HealthTask.owner==owner))
            session.execute(delete(HealthEvidence).where(HealthEvidence.owner==owner))
            session.commit()


def test_browser_review_requires_matching_revision_and_owner(db, sso_headers):
    from app.main import app
    from app.db import get_db
    from app.auth import ForwardIdentity
    from app.deps import require_login
    owner = "browser:" + digest("test-user")[:40]
    row=create_draft(db,agent=owner)
    def override_db(): yield db
    app.dependency_overrides[get_db]=override_db
    try:
        client=TestClient(app,headers=sso_headers,base_url="http://localhost",client=("127.0.0.1",50000))
        page=client.get(f"/companion/drafts/{row.draft_id}")
        assert page.status_code==200 and "尚未写入" in page.text
        assert page.headers["cache-control"] == "private, no-store"
        denied=client.post(f"/companion/drafts/{row.draft_id}/approve",data={"revision":"wrong"})
        assert denied.status_code==409
        result=client.post(f"/companion/drafts/{row.draft_id}/approve",data={"revision":row.payload_hash},follow_redirects=False)
        assert result.status_code==303
        assert db.get(AgentRecordDraft,row.draft_id).status=="applied"
        other=create_draft(db,agent="browser:other")
        assert client.get(f"/companion/drafts/{other.draft_id}").status_code==404
        assert client.get("/companion",headers={**sso_headers,"X-Forwarded-Prefix":"/health"}).status_code==200
    finally:
        app.dependency_overrides.pop(get_db,None)


def test_invalid_catalog_food_is_clear_client_error(db):
    with pytest.raises(MachineAPIError) as error:
        create_draft(db, payload={"record_type":"meal", "effective_date":str(today_local()),
            "fields":{"meal":"午餐","name":"不存在的食物","food_id":2147483647,"amount_g":100}})
    assert error.value.status_code == 400


def test_quiet_preference_suppresses_inbox(db):
    hc.set_preference(db,"quiet-user","notification_style","quiet"); db.flush()
    row=hc.configure_monitor(db,"quiet-user","sync-late","shadow")
    row.mode="inbox"
    hc.evaluate_monitor(db,row,datetime(2026,9,4,4,tzinfo=UTC))
    assert row.state["active"] and not row.state["visible"]


def test_removed_photo_invalidates_draft(db):
    row=create_draft(db)
    row.payload={**row.payload,"_photo_id":2147483647}; db.flush()
    with pytest.raises(MachineAPIError) as error:
        MachineHealthService(db).commit_draft(row)
    assert error.value.code == "photo_removed"


@pytest.mark.parametrize("items", ["food", [None], ["food"], [{"name":None}]])
def test_malformed_meal_tools_fail_without_write(db, items):
    result=propose_tool(db,"record_diet",{"date":str(today_local()),"meal":"午餐","items":items},"one","key")
    assert "error" in result


def test_device_metric_cannot_be_overwritten_by_agent(db):
    day=today_local()-timedelta(days=100)
    db.add(BodyMetrics(log_date=day,weight_kg=72,autofilled={"weight_kg":"miscale"})); db.flush()
    with pytest.raises(MachineAPIError) as error:
        create_draft(db,payload={"record_type":"metric","effective_date":str(day),"fields":{"weight_kg":80}})
    assert error.value.code == "source_read_only"


def test_paused_review_cannot_materialize(db, monkeypatch):
    from app.config import get_settings
    row=create_draft(db)
    monkeypatch.setattr(get_settings(),"agent_review_enabled",False)
    with pytest.raises(MachineAPIError) as error: MachineHealthService(db).commit_draft(row)
    assert error.value.code == "review_paused" and row.status == "pending"


def test_model_output_budget_and_context_bound():
    from app.services.llm import _CallBudget, LLMError
    budget=_CallBudget()
    assert [budget.limit(4000,[]) for _ in range(3)] == [4000]*3
    with pytest.raises(LLMError): budget.limit(1,[])
    with pytest.raises(LLMError): _CallBudget().limit(4000,["x"*64001])


def test_wrong_nexus_revision_cannot_apply(db):
    from app.routers.machine_agent import _check_approval
    row=create_draft(db)
    with pytest.raises(MachineAPIError): _check_approval(row,{"revision":2})
    with pytest.raises(MachineAPIError): _check_approval(row,{"revision":1,"payload_hash":"wrong"})
    _check_approval(row,{"revision":1,"payload_hash":row.payload_hash})


def test_source_revocation_hides_previous_evidence(db):
    from app.models import SyncCursor
    evidence=hc.create_evidence(db,"one",today_local()-timedelta(days=60))
    cursor=SyncCursor(source="health_connect",record_type="weight",permission_state="revoked")
    db.merge(cursor); db.flush()
    assert hc.evidence_state(db,evidence)=="revoked"


def test_worker_cancellation_fences_uncommitted_result(db,monkeypatch):
    from app.db import SessionLocal
    from sqlalchemy import update
    owner="cancel-"+uuid.uuid4().hex
    with SessionLocal() as session:
        task=hc.enqueue(session,owner,"weekly-review",{"end":str(today_local()),"days":7},uuid.uuid4().hex)
        session.commit(); task_id=task.id
    original=hc.create_evidence
    def cancel_after_computing(*args,**kwargs):
        evidence=original(*args,**kwargs)
        with SessionLocal() as other:
            other.execute(update(HealthTask).where(HealthTask.id==task_id).values(status="cancelled",lease_token=None))
            other.commit()
        return evidence
    monkeypatch.setattr(hc,"create_evidence",cancel_after_computing)
    try:
        assert hc.run_once(SessionLocal)
        with SessionLocal() as session:
            assert session.get(HealthTask,task_id).status=="cancelled"
            assert session.scalar(select(func.count()).select_from(HealthEvidence).where(HealthEvidence.owner==owner))==0
    finally:
        with SessionLocal() as session:
            session.execute(delete(HealthTask).where(HealthTask.owner==owner)); session.commit()


def test_scale_attempt_dedup_and_gateway_does_not_erase_identity(db,sso_headers):
    from app.config import get_settings
    from app.db import get_db
    from app.main import app
    from app.models import ImportRaw
    from app.timeutil import now_local
    def override_db(): yield db
    app.dependency_overrides[get_db]=override_db
    client=TestClient(app,headers=sso_headers,base_url="http://localhost",client=("127.0.0.1",50000))
    attempt=str(uuid.uuid4())
    headers={"Authorization":"Bearer " + get_settings().ingest_token}
    payload={"ts":now_local().isoformat(),"weight_kg":73.85,"attempt_id":attempt}
    try:
        first=client.post("/api/ingest/miscale",json=payload,headers=headers)
        assert first.status_code==200 and first.json()["new"]==1
        retry=client.post("/api/ingest/miscale",json=payload,headers=headers)
        assert retry.json()["new"]==0
        gateway=client.post("/api/ingest/miscale",json={k:v for k,v in payload.items() if k!="attempt_id"},headers=headers)
        assert gateway.json()["new"]==0
        row=db.scalar(select(ImportRaw).where(ImportRaw.provenance["last_attempt_id"].astext==attempt))
        assert row.provenance["first_attempt_id"]==attempt and row.last_normalization_at
        assert "称重已接收并入库" in client.get("/fragments/today/scale-status",params={"start":1,"attempt_id":attempt}).text
        second=str(uuid.uuid4())
        client.post("/api/ingest/miscale",json={**payload,"attempt_id":second},headers=headers)
        assert "重复消息" in client.get("/fragments/today/scale-status",params={"start":1,"attempt_id":second}).text
        assert "秤监听中" in client.get("/fragments/today/scale-status",params={"start":1,"attempt_id":attempt}).text
    finally:
        app.dependency_overrides.pop(get_db,None)
