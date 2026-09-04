"""Deterministic, bounded domain workflows. Private measurements never enter logs."""
from __future__ import annotations

import uuid
import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.machine_auth import MachineAPIError
from app.models import HealthEvidence, HealthGoal, HealthMonitor, HealthPreference, HealthTask, ImportRaw, SyncCursor
from app.services.agent_drafts import digest
from app.timeutil import LOCAL_TZ, today_local

ALGORITHM = "weekly-evidence-v1"
METRICS = {"weight_kg": ("体重", "kg"), "sleep_hours": ("睡眠", "小时"),
           "steps": ("步数", "步"), "diet_kcal": ("已记录饮食", "kcal")}
PREFERENCES = {"notification_style": {"quiet", "weekly"}, "training_focus": {"everyday", "strength"},
               "review_day": {"monday", "sunday"}}
MONITORS = {"sync-late", "weekly-review"}


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def owner_id(request):
    identity = request.scope.get("shadow_identity")
    name = getattr(identity, "shadow_user_id", None) or getattr(identity, "subject", None) or getattr(identity, "username", None)
    if not name:
        raise MachineAPIError(403, "identity_required", "缺少用户身份。")
    return "browser:" + digest(name)[:40]


def weekly_facts(db, end: date, days=7, progress=None):
    from app.routers.machine_agent import MachineHealthService
    service = MachineHealthService(db)
    start = end - timedelta(days=days * 2 - 1)
    source_states = [{"source": r.source, "record_type": r.record_type,
        "permission": r.permission_state, "fingerprint": digest(r.source_fingerprint),
        "changed_at": r.source_changed_at.isoformat() if r.source_changed_at else None,
        "needs_resync": r.needs_resync} for r in db.scalars(select(SyncCursor).where(
            SyncCursor.record_type.in_(("weight", "sleep", "steps"))).order_by(SyncCursor.source, SyncCursor.record_type))]
    daily = []
    for index in range(days * 2):
        if progress and index % 5 == 0:
            progress()
        day = start + timedelta(days=index)
        summary = service.summary("primary", day)
        # Explicit allowlist: no mood, private habits, labs, photos, notes or heart-rate.
        daily.append({"date": str(day), "values": {k: summary["indicators"][k] for k in METRICS},
                      "quality": {k: summary["data_quality"]["indicators"][k] for k in METRICS},
                      "resource_uri": summary["resource_uri"]})
    for row in daily:
        for state in source_states:
            if state["permission"] in {"denied", "revoked"}:
                key = {"weight":"weight_kg", "sleep":"sleep_hours", "steps":"steps"}[state["record_type"]]
                row["values"][key] = None
                row["quality"][key] = {**row["quality"][key], "present": False, "reason": "source_permission_revoked"}
    return {"algorithm": ALGORITHM, "timezone": str(LOCAL_TZ), "end": str(end), "days": days,
            "daily": daily, "source_states": source_states}


def analyze_facts(facts):
    days = facts["days"]
    baseline, current = facts["daily"][:days], facts["daily"][days:]
    cards = []
    for key, (label, unit) in METRICS.items():
        groups = []
        sources = []
        for window in (baseline, current):
            valid = [row for row in window if row["quality"][key]["present"] and row["values"][key] is not None]
            groups.append([float(row["values"][key]) for row in valid])
            sources.append(sorted({s for row in valid for s in row["quality"][key]["sources"]}))
        before, now = groups
        # Same-source comparison only. Missingness never produces a zero average.
        comparable = len(before) >= 3 and len(now) >= 3 and sources[0] == sources[1] and len(sources[1]) == 1
        record_type = {"weight_kg":"weight", "sleep_hours":"sleep", "steps":"steps"}.get(key)
        source_uncertain = any(s["record_type"] == record_type and (s["needs_resync"] or
            (s["changed_at"] and facts["daily"][0]["date"] <= datetime.fromisoformat(s["changed_at"]).astimezone(LOCAL_TZ).date().isoformat() <= facts["end"]))
            for s in facts.get("source_states", []))
        comparable = comparable and not source_uncertain
        mean = round(sum(now) / len(now), 2) if len(now) >= 3 else None
        delta = round(mean - sum(before) / len(before), 2) if comparable else None
        reason = "有效记录不足，不判断趋势" if len(now) < 3 or len(before) < 3 else "来源变化或混合来源，不作跨期比较"
        text = f"{label}有效记录 {len(now)}/{days} 天"
        if mean is not None:
            text += f"，有记录日均值 {mean:g} {unit}"
        if delta is not None:
            text += f"，较前一独立窗口 {delta:+g} {unit}"
        else:
            text += f"；{reason}"
        if key == "diet_kcal":
            text += "。记录覆盖不代表饮食完整，不据此调整摄入目标"
        cards.append({"metric": key, "label": label, "unit": unit, "coverage": len(now),
                      "baseline_coverage": len(before), "average": mean, "delta": delta,
                      "sources": sources[1], "text": text})
    return cards


def create_evidence(db, owner, end, days=7, progress=None):
    facts = weekly_facts(db, end, days, progress)
    row = HealthEvidence(id="he_" + uuid.uuid4().hex, owner=owner, profile_id="primary",
                         payload={**facts, "cards": analyze_facts(facts)}, fingerprint=digest(facts),
                         expires_at=datetime.now(UTC) + timedelta(days=30))
    db.add(row)
    db.flush()
    return row


def evidence_state(db, row):
    if utc(row.expires_at) <= datetime.now(UTC):
        return "expired"
    current = weekly_facts(db, date.fromisoformat(row.payload["end"]), row.payload["days"])
    if any(s["permission"] in {"denied", "revoked"} for s in current["source_states"]):
        return "revoked"
    return "current" if digest(current) == row.fingerprint else "stale"


def enqueue(db, owner, kind, payload, key):
    if kind not in {"weekly-review", "analysis"}:
        raise ValueError("unknown task kind")
    existing = db.scalars(select(HealthTask).where(HealthTask.owner == owner, HealthTask.task_key == key)).first()
    if existing:
        if existing.kind != kind or existing.payload != payload:
            raise MachineAPIError(409, "idempotency_conflict", "任务标识已用于其他内容。")
        return existing
    row = HealthTask(id="ht_" + uuid.uuid4().hex, owner=owner, kind=kind, payload=payload,
                     task_key=key, status="pending", result={}, attempts=0)
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return enqueue(db, owner, kind, payload, key)
    return row


def claim(db):
    now = datetime.now(UTC)
    row = db.scalars(select(HealthTask).where(or_(HealthTask.status == "pending",
        (HealthTask.status == "running") & (HealthTask.lease_until < now)))
        .order_by(HealthTask.created_at).with_for_update(skip_locked=True).limit(1)).first()
    if not row:
        return None
    if row.attempts >= 3:
        row.status, row.result, row.finished_at = "failed", {"error": "任务重试已达上限"}, now
        db.commit()
        return None
    row.status, row.attempts = "running", row.attempts + 1
    row.lease_token, row.lease_until = uuid.uuid4().hex, now + timedelta(minutes=5)
    db.commit()
    return row.id, row.lease_token


def run_once(session_factory):
    with session_factory() as db:
        claimed = claim(db)
    if not claimed:
        return False
    task_id, token = claimed
    started = time.monotonic()

    def heartbeat():
        if time.monotonic() - started > 120:
            raise TimeoutError("task deadline exceeded")
        with session_factory() as lease_db:
            changed = lease_db.execute(update(HealthTask).where(HealthTask.id == task_id,
                HealthTask.status == "running", HealthTask.lease_token == token)
                .values(lease_until=datetime.now(UTC) + timedelta(minutes=5))).rowcount
            lease_db.commit()
            if not changed:
                raise RuntimeError("task cancelled or reclaimed")

    with session_factory() as db:
        row = db.get(HealthTask, task_id)
        try:
            end = date.fromisoformat(row.payload["end"])
            days = int(row.payload.get("days", 7))
            if days not in (7, 30, 90):
                raise ValueError("invalid window")
            # Deterministic, bounded analysis is deliberately not a background LLM loop.
            evidence = create_evidence(db, row.owner, end, days, heartbeat)
            result = {"evidence_id": evidence.id, "cards": evidence.payload["cards"], "days": days,
                      "duration_ms": round((time.monotonic() - started) * 1000), "model_calls": 0}
            changed = db.execute(update(HealthTask).where(HealthTask.id == task_id,
                HealthTask.status == "running", HealthTask.lease_token == token)
                .values(status="done", result=result, finished_at=datetime.now(UTC), lease_until=None)).rowcount
            if not changed:
                db.rollback()  # Cancelled or reclaimed: discard all computed output.
            else:
                db.commit()
        except Exception:
            db.rollback()
            db.execute(update(HealthTask).where(HealthTask.id == task_id,
                HealthTask.status == "running", HealthTask.lease_token == token)
                .values(status="failed", result={"error": "计算失败，请检查数据服务后重试"}, finished_at=datetime.now(UTC)))
            db.commit()
    return True


def set_preference(db, owner, name, value=None, forget=False):
    if name not in PREFERENCES or (not forget and value not in PREFERENCES[name]):
        raise ValueError("偏好值不支持")
    row = db.scalars(select(HealthPreference).where(HealthPreference.owner == owner,
        HealthPreference.name == name).with_for_update()).first()
    new = row is None
    if row is None:
        row = HealthPreference(id="hp_" + uuid.uuid4().hex, owner=owner, name=name)
    row.value = None if forget else value
    row.status = "forgotten" if forget else "confirmed"
    row.updated_at = datetime.now(UTC)
    row.expires_at = None if forget else datetime.now(UTC) + timedelta(days=365)
    if new:
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            return set_preference(db, owner, name, value, forget)
    # No transcript ingestion, derived memory, or persistent prompt cache exists.
    return row


def preferences(db, owner):
    now = datetime.now(UTC)
    return {r.name: r.value for r in db.scalars(select(HealthPreference).where(
        HealthPreference.owner == owner, HealthPreference.status == "confirmed"))
        if r.expires_at is None or utc(r.expires_at) > now}


def new_goal(db, owner, title, due, evidence_id):
    if not 1 <= len(title.strip()) <= 200 or not today_local() <= due <= today_local() + timedelta(days=90):
        raise ValueError("行动需 1–200 字，期限在未来 90 天以内")
    evidence = db.get(HealthEvidence, evidence_id)
    if not evidence or evidence.owner != owner or evidence_state(db, evidence) != "current":
        raise MachineAPIError(409, "evidence_stale", "请先生成最新复盘，再采纳行动。")
    goal_id = "hg_" + digest([owner, title.strip(), str(due), evidence_id])[:32]
    existing = db.get(HealthGoal, goal_id)
    if existing:
        return existing
    row = HealthGoal(id=goal_id, owner=owner, due_date=due,
                     plan={"title": title.strip(), "evidence_id": evidence_id,
                           "baseline": evidence.payload["cards"]})
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return db.get(HealthGoal, goal_id)
    return row


def mutate_goal(db, row, action, version, note=""):
    if row.version != version:
        raise MachineAPIError(409, "goal_changed", "行动已更新，请刷新。")
    if action not in {"pause", "resume", "cancel", "checkin", "complete", "revise"} or len(note) > 500:
        raise ValueError("无效行动")
    if row.status in {"completed", "cancelled"}:
        raise MachineAPIError(409, "goal_closed", "行动已结束。")
    if action == "checkin":
        if len(row.checkins) >= 100:
            raise ValueError("已达到回看记录上限")
        row.checkins = [*row.checkins, {"date": str(today_local()), "note": note}]
    elif action == "revise":
        if not note.strip() or len(note) > 200:
            raise ValueError("请输入 1–200 字行动")
        row.history = [*row.history, {"version": row.version, "plan": row.plan}]
        row.plan = {**row.plan, "title": note.strip()}
    elif action == "complete":
        facts = weekly_facts(db, today_local())
        row.outcome = {"baseline": row.plan["baseline"], "current": analyze_facts(facts),
                       "user_note": note, "caution": "仅描述同期变化，不能据此证明行动导致变化。"}
        row.status = "completed"
    else:
        row.status = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
    row.version += 1


def configure_monitor(db, owner, kind, mode):
    if kind not in MONITORS or mode not in {"off", "shadow", "inbox"}:
        raise ValueError("不支持的监测配置")
    row = db.scalars(select(HealthMonitor).where(HealthMonitor.owner == owner,
        HealthMonitor.kind == kind).with_for_update()).first()
    if row is None:
        row = HealthMonitor(id="hm_" + uuid.uuid4().hex, owner=owner, kind=kind, mode="shadow", state={})
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            return configure_monitor(db, owner, kind, mode)
    if mode == "inbox" and ((datetime.now(UTC) - utc(row.created_at)).days < 7
                           or len(row.state.get("observed_days", [])) < 7):
        raise ValueError("先完成至少 7 天影子观察，再启用站内提醒")
    row.mode = mode
    if mode == "off":
        row.state = {}
    return row


def evaluate_monitor(db, row, now=None):
    now = now or datetime.now(UTC)
    if row.mode == "off":
        return
    local = now.astimezone(LOCAL_TZ)
    pref = preferences(db, row.owner)
    if row.kind == "sync-late":
        latest = db.scalar(select(ImportRaw.imported_at).where(ImportRaw.source.in_(
            ("health_connect", "samsung_direct"))).order_by(ImportRaw.imported_at.desc()).limit(1))
        # Lack of ingestion is not a diagnosis of device failure.
        active = latest is None or (now - utc(latest)).total_seconds() > 48 * 3600
        key = digest({"kind": row.kind, "latest": str(latest), "rule": 1})
        message = "服务端近期未收到新的三星/健康连接记录；请查看手机权限和同步状态。"
    else:
        weekday = 0 if pref.get("review_day", "monday") == "monday" else 6
        active = local.weekday() == weekday
        key = f"weekly:{local.isocalendar().year}:{local.isocalendar().week}:v1"
        message = "本周可以复盘了；有空时查看即可。"
    old = dict(row.state or {})
    observed = sorted(set([*old.get("observed_days", []), str(local.date())]))[-30:]
    visible = bool(active and row.mode == "inbox" and 8 <= local.hour < 22
                   and pref.get("notification_style") != "quiet"
                   and not (row.snoozed_until and utc(row.snoozed_until) > now)
                   and old.get("dismissed_key") != key)
    row.state = {**old, "key": key, "active": active, "visible": visible,
                 "message": message, "evaluated_at": now.isoformat(),
                 "would_notify": active, "rule_version": 1, "observed_days": observed}
    if active and row.mode == "inbox" and row.kind == "weekly-review" and not (row.snoozed_until and utc(row.snoozed_until) > now):
        end = local.date() - timedelta(days=local.weekday() + 1)
        enqueue(db, row.owner, "weekly-review", {"end": str(end), "days": 7}, key)


def maintenance(session_factory):
    with session_factory() as db:
        for row in db.scalars(select(HealthMonitor).where(HealthMonitor.mode != "off").with_for_update(skip_locked=True)):
            evaluate_monitor(db, row)
        # Expired snapshots must not retain health content indefinitely.
        for row in db.scalars(select(HealthEvidence).where(HealthEvidence.expires_at < datetime.now(UTC))):
            row.payload = {"expired": True}
        for task in db.scalars(select(HealthTask).where(HealthTask.finished_at < datetime.now(UTC) - timedelta(days=30))):
            task.result = {k: v for k, v in task.result.items() if k != "cards"}
        db.commit()
