"""Authenticated review, evidence, routines and task UI. Single Health profile."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import redirect, require_login, templates
from app.machine_auth import MachineAPIError
from app.models import AgentRecordDraft, BodyMetrics, DietLog, HealthEvidence, HealthGoal, HealthMonitor, HealthTask, WorkoutLog
from app.routers.machine_agent import MachineHealthService
from app.services import health_companion as hc
from app.services.agent_drafts import approve_match, locked_review, validate_review
from app.timeutil import today_local

router = APIRouter(prefix="/companion", dependencies=[Depends(require_login)])


def owned(db, model, row_id, owner, *, lock=False):
    query = select(model).where(model.id == row_id, model.owner == owner)
    row = db.scalars(query.with_for_update().execution_options(populate_existing=True) if lock else query).first()
    if row is None:
        raise MachineAPIError(404, "not_found", "内容不存在或不属于当前用户。")
    return row


def visible_drafts_query(owner):
    return select(AgentRecordDraft).where(AgentRecordDraft.profile_id == "primary", or_(
            ~AgentRecordDraft.agent_id.startswith("browser:"), AgentRecordDraft.agent_id == owner))


def pending_drafts_query(owner):
    return visible_drafts_query(owner).where(AgentRecordDraft.status == "pending")


def draft_for(db, draft_id, owner):
    row = db.get(AgentRecordDraft, draft_id)
    if not row or row.profile_id != "primary" or (row.agent_id.startswith("browser:") and row.agent_id != owner):
        raise MachineAPIError(404, "not_found", "草案不存在。")
    return row


@router.get("")
def dashboard(request: Request, db: Session = Depends(get_db)):
    owner = hc.owner_id(request)
    drafts = list(db.scalars(pending_drafts_query(owner)
        .order_by(AgentRecordDraft.created_at.desc()).limit(200)))
    receipts = list(db.scalars(visible_drafts_query(owner).where(AgentRecordDraft.status != "pending")
        .order_by(AgentRecordDraft.created_at.desc()).limit(20)))
    tasks = list(db.scalars(select(HealthTask).where(HealthTask.owner == owner)
        .order_by(HealthTask.created_at.desc()).limit(20)))
    goals = list(db.scalars(select(HealthGoal).where(HealthGoal.owner == owner)
        .order_by(HealthGoal.created_at.desc()).limit(30)))
    monitors = list(db.scalars(select(HealthMonitor).where(HealthMonitor.owner == owner)))
    hidden_outcomes = {g.id for g in goals if g.outcome and hc.revoked_cards(db,
        g.outcome.get("baseline", []) + g.outcome.get("current", []))}
    return templates.TemplateResponse(request, "companion.html", {
        "drafts": drafts, "receipts": receipts, "tasks": tasks, "goals": goals, "monitors": monitors,
        "prefs": hc.preferences(db, owner), "request_key": uuid.uuid4().hex,
        "monitor_modes": {m.kind: m.mode for m in monitors}, "hidden_outcomes": hidden_outcomes,
        "error": request.query_params.get("error", ""), "today": today_local()})


@router.get("/badge")
def badge(request: Request, db: Session = Depends(get_db)):
    owner = hc.owner_id(request)
    count = db.scalar(select(func.count()).select_from(pending_drafts_query(owner).subquery()))
    monitors = db.scalars(select(HealthMonitor).where(HealthMonitor.owner == owner)).all()
    notices = [r.state.get("message") for r in monitors if hc.monitor_visible(db, r)]
    return templates.TemplateResponse(request, "fragments/companion_badge.html", {"count": count, "notices": notices[:1]})


@router.get("/drafts/{draft_id}")
def draft_page(draft_id: str, request: Request, db: Session = Depends(get_db)):
    row = draft_for(db, draft_id, hc.owner_id(request))
    records = list(db.scalars(select(DietLog).where(DietLog.id.in_(row.payload.get("_result_ids", []))))) if row.record_type == "meal" else []
    totals, readback = None, {}
    review_error = None
    if row.status == "pending":
        try:
            validate_review(db, row, lock=False)
        except MachineAPIError as exc:
            review_error = exc.message
    if row.status == "applied":
        if row.record_type == "meal":
            totals = db.execute(select(func.sum(DietLog.kcal), func.sum(DietLog.protein_g)).where(
                DietLog.log_date == row.effective_date)).one()
        else:
            record = (db.scalar(select(BodyMetrics).where(BodyMetrics.log_date == row.effective_date))
                      if row.record_type == "metric" else db.scalar(select(WorkoutLog).where(
                          WorkoutLog.external_id == f"agent-{row.draft_id}")))
            if record:
                readback = {k: getattr(record, k) for k in row.payload["fields"] if hasattr(record, k)}
    return templates.TemplateResponse(request, "companion_draft.html", {
        "draft": row, "records": records, "totals": totals, "readback": readback, "review_error": review_error,
        "error": request.query_params.get("error", "")})


@router.post("/drafts/{draft_id}/{action}")
async def draft_action(draft_id: str, action: str, request: Request, db: Session = Depends(get_db)):
    row = locked_review(db, draft_for(db, draft_id, hc.owner_id(request)))
    form = await request.form()
    approve_match(row, str(form.get("revision", "")))
    service = MachineHealthService(db)
    if action in {"renew", "revise"} and row.payload.get("_superseded_by"):
        return redirect(request, f"/companion/drafts/{row.payload['_superseded_by']}")
    if action == "renew" and row.status == "pending":
        from app.services.agent_drafts import digest
        from app.machine_auth import MachinePrincipal
        from app.models import DietPhoto
        from app.routers.machine_agent import _validate_draft_payload
        payload = {k: v for k, v in row.payload.items() if not k.startswith("_")}
        # Keep food-catalog authority on re-review, recalculating against today's catalog.
        if row.record_type == "meal":
            fields = dict(payload["fields"])
            items = [dict(i) for i in fields.get("items", [fields])]
            for item, food_id in zip(items, row.payload.get("_food_ids", [])):
                if food_id:
                    item["food_id"] = food_id
            payload["fields"] = {**fields, "items": items} if "items" in fields else items[0]
        try:
            payload = _validate_draft_payload(payload)
        except (ValueError, TypeError):
            raise MachineAPIError(409, "review_upgrade_required", "旧草案内容不符合当前规范，请修改内容或拒绝后重新生成。")
        new, _ = service.create_draft(principal=MachinePrincipal(row.agent_id, frozenset(), frozenset(), {}),
            profile_id=row.profile_id, idempotency_key=digest(["renew", row.draft_id]),
            payload=payload, payload_hash=digest(payload))
        new.payload = {**new.payload, "_supersedes": row.draft_id}
        if row.payload.get("_photo_id"):
            photo = db.scalar(select(DietPhoto).where(DietPhoto.id == row.payload["_photo_id"]).with_for_update())
            if photo is None:
                raise MachineAPIError(409, "photo_removed", "原照片已移除，请拒绝旧草案后重新上传。")
            new.payload = {**new.payload, "_photo_id": photo.id, "_vision_trace": row.payload.get("_vision_trace")}
            photo.analysis = {**(photo.analysis or {}), "draft_id": new.draft_id}
        service.reject_draft(row)
        row.payload = {**row.payload, "_superseded_by": new.draft_id}
        service.audit(request_id=uuid.uuid4().hex,
            principal=MachinePrincipal(hc.owner_id(request), frozenset(), frozenset(), {}),
            capability="health.records.write", profile_id="primary", outcome="renew", status_code=200,
            resource_uri=f"shadow://health/drafts/{new.draft_id}")
        return redirect(request, f"/companion/drafts/{new.draft_id}")
    if action == "revise" and row.record_type == "meal" and row.status == "pending":
        from app.routers.machine_agent import _validate_draft_payload
        from app.services.agent_drafts import digest
        from app.machine_auth import MachinePrincipal
        items = row.payload["fields"].get("items", [row.payload["fields"]])
        edited = []
        try:
            for index, item in enumerate(items):
                value = {"name": str(form.get(f"name_{index}", item["name"])),
                         "notes": str(form.get(f"notes_{index}", item.get("notes") or ""))}
                for field in ("amount_g", "kcal", "protein_g", "fat_g", "carb_g"):
                    supplied = str(form.get(f"{field}_{index}", "")).strip()
                    if supplied:
                        value[field] = float(supplied)
                catalog_ids = row.payload.get("_food_ids", [])
                if form.get(f"use_catalog_{index}") and index < len(catalog_ids) and catalog_ids[index]:
                    value["food_id"] = catalog_ids[index]
                edited.append(value)
            payload = {k: v for k, v in row.payload.items() if not k.startswith("_")}
            payload["fields"] = {"name": "、".join(i["name"] for i in edited)[:120],
                                  "meal": str(form.get("meal", row.payload["fields"]["meal"])), "items": edited}
            payload["effective_date"] = str(form.get("date", row.effective_date))
            payload = _validate_draft_payload(payload)
        except (ValueError, TypeError):
            raise MachineAPIError(400, "invalid_draft", "请核对日期、食物和数值范围。")
        new, _ = service.create_draft(principal=MachinePrincipal(row.agent_id, frozenset(), frozenset(), {}),
            profile_id=row.profile_id, idempotency_key=hc.digest([row.draft_id, payload]),
            payload=payload, payload_hash=digest(payload))
        new.payload = {**new.payload, "_supersedes": row.draft_id}
        if row.payload.get("_photo_id"):
            from app.models import DietPhoto
            photo = db.scalar(select(DietPhoto).where(DietPhoto.id == row.payload["_photo_id"]).with_for_update())
            if photo is None:
                raise MachineAPIError(409, "photo_removed", "原照片已移除，请重新生成草案。")
            new.payload = {**new.payload, "_photo_id": photo.id, "_vision_trace": row.payload.get("_vision_trace")}
            photo.analysis = {**(photo.analysis or {}), "draft_id": new.draft_id}
        service.reject_draft(row)
        row.payload = {**row.payload, "_superseded_by": new.draft_id}
        service.audit(request_id=uuid.uuid4().hex,
            principal=MachinePrincipal(hc.owner_id(request), frozenset(), frozenset(), {}),
            capability="health.records.write", profile_id="primary", outcome="revise", status_code=200,
            resource_uri=f"shadow://health/drafts/{new.draft_id}")
        return redirect(request, f"/companion/drafts/{new.draft_id}")
    if action == "approve":
        service.commit_draft(row)
    elif action == "reject":
        service.reject_draft(row)
    else:
        raise MachineAPIError(404, "not_found", "操作不存在。")
    from app.machine_auth import MachinePrincipal
    service.audit(request_id=uuid.uuid4().hex,
        principal=MachinePrincipal(hc.owner_id(request), frozenset(), frozenset(), {}),
        capability="health.records.write", profile_id="primary", outcome=action, status_code=200,
        resource_uri=f"shadow://health/drafts/{row.draft_id}")
    return redirect(request, f"/companion/drafts/{row.draft_id}")


@router.post("/reviews")
async def create_review(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    key = str(form.get("request_key", ""))
    if len(key) != 32:
        raise MachineAPIError(400, "request_key_required", "请刷新后重试。")
    # Last completed calendar week; do not mix partial current week with full baseline.
    end = today_local() - timedelta(days=today_local().weekday() + 1)
    task = hc.enqueue(db, hc.owner_id(request), "weekly-review", {"end": str(end), "days": 7}, key)
    return redirect(request, f"/companion/tasks/{task.id}")


@router.get("/tasks/{task_id}")
def task_page(task_id: str, request: Request, db: Session = Depends(get_db)):
    row = owned(db, HealthTask, task_id, hc.owner_id(request))
    return templates.TemplateResponse(request, "companion_task.html", {"task": row})


@router.get("/tasks/{task_id}/status")
def task_status(task_id: str, request: Request, db: Session = Depends(get_db)):
    row = owned(db, HealthTask, task_id, hc.owner_id(request))
    return templates.TemplateResponse(request, "fragments/companion_task_status.html", {"task": row})


@router.post("/tasks/{task_id}/{action}")
def task_action(task_id: str, action: str, request: Request, db: Session = Depends(get_db)):
    row = owned(db, HealthTask, task_id, hc.owner_id(request), lock=True)
    if action == "cancel" and row.status in {"pending", "running"}:
        row.status, row.lease_token = "cancelled", None
        row.lease_until, row.finished_at = None, datetime.now(UTC)
    elif action == "retry" and row.status == "failed" and row.attempts < 3:
        row.status, row.lease_token = "pending", None
        row.lease_until, row.finished_at, row.result = None, None, {}
    else:
        raise MachineAPIError(409, "task_state_invalid", "当前状态不能执行此操作。")
    return redirect(request, f"/companion/tasks/{task_id}")


@router.get("/evidence/{evidence_id}")
def evidence_page(evidence_id: str, request: Request, db: Session = Depends(get_db)):
    row = owned(db, HealthEvidence, evidence_id, hc.owner_id(request))
    state = hc.evidence_state(db, row)
    return templates.TemplateResponse(request, "companion_evidence.html", {
        "evidence": row, "state": state, "today": today_local(), "due": today_local() + timedelta(days=7),
        "request_key": uuid.uuid4().hex})


@router.post("/evidence/{evidence_id}/refresh")
async def evidence_refresh(evidence_id: str, request: Request, db: Session = Depends(get_db)):
    owner = hc.owner_id(request)
    row = owned(db, HealthEvidence, evidence_id, owner)
    form = await request.form()
    key = str(form.get("request_key", ""))
    if len(key) != 32:
        raise MachineAPIError(400, "request_key_required", "请刷新后重试。")
    payload = row.payload
    if "end" not in payload:  # Expired evidence is stripped; the job retains only window metadata.
        task = db.scalar(select(HealthTask).where(HealthTask.owner == owner,
            HealthTask.result["evidence_id"].as_string() == evidence_id).limit(1))
        payload = task.payload if task else {}
    if not payload.get("end") or payload.get("days") not in (7, 30, 90):
        raise MachineAPIError(409, "window_unavailable", "原复盘窗口已不可用，请从健康助手发起新复盘。")
    task = hc.enqueue(db, owner, "analysis", {"end": payload["end"], "days": payload["days"]}, key)
    return redirect(request, f"/companion/tasks/{task.id}")


@router.post("/preferences")
async def preference_action(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        hc.set_preference(db, hc.owner_id(request), str(form.get("name", "")),
            str(form.get("value", "")), form.get("action") == "forget")
    except ValueError as exc:
        raise MachineAPIError(400, "invalid_preference", str(exc)) from exc
    return redirect(request, "/companion#preferences")


@router.post("/goals")
async def goal_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        hc.new_goal(db, hc.owner_id(request), str(form.get("title", "")),
                    date.fromisoformat(str(form.get("due_date", ""))), str(form.get("evidence_id", "")))
    except ValueError as exc:
        raise MachineAPIError(400, "invalid_goal", str(exc)) from exc
    return redirect(request, "/companion#goals")


@router.post("/goals/{goal_id}")
async def goal_action(goal_id: str, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    row = owned(db, HealthGoal, goal_id, hc.owner_id(request), lock=True)
    try:
        hc.mutate_goal(db, row, str(form.get("action", "")), int(str(form.get("version", "0"))),
                       str(form.get("note", "")))
    except ValueError as exc:
        raise MachineAPIError(400, "invalid_goal", str(exc)) from exc
    return redirect(request, "/companion#goals")


@router.post("/monitors")
async def monitor_action(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        hc.configure_monitor(db, hc.owner_id(request), str(form.get("kind", "")), str(form.get("mode", "")))
    except ValueError as exc:
        raise MachineAPIError(400, "invalid_monitor", str(exc)) from exc
    return redirect(request, "/companion#monitors")


@router.post("/monitors/{monitor_id}/{action}")
def monitor_dismiss(monitor_id: str, action: str, request: Request, db: Session = Depends(get_db)):
    row = owned(db, HealthMonitor, monitor_id, hc.owner_id(request), lock=True)
    if action == "snooze":
        row.snoozed_until = datetime.now(UTC) + timedelta(days=1)
    elif action == "dismiss":
        row.state = {**row.state, "dismissed_key": row.state.get("key")}
    else:
        raise MachineAPIError(404, "not_found", "操作不存在。")
    row.state = {**row.state, "visible": False}
    return redirect(request, "/companion#monitors")
