"""Runtime-neutral Shadow Agent machine API for the dedicated Health Profile."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date as date_type, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.machine_auth import (
    MachineAPIError,
    MachinePrincipal,
    authenticate_machine_request,
    authorize_machine_principal,
    machine_request_id,
)
from app.models import (
    AgentMachineAudit,
    AgentRecordDraft,
    BodyMetrics,
    DietLog,
    SCHEMA,
    WorkoutLog,
)
from app.timeutil import today_local

router = APIRouter(prefix="/api/machine/v1/agent")

AUDIENCE = "health"
PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
MAX_DRAFT_BODY_BYTES = 16 * 1024
TREND_FIELDS = {
    "weight_kg": ("body_metrics", "weight_kg", "体重", "kg"),
    "sleep_hours": ("body_metrics", "sleep_hours", "睡眠", "小时"),
    "steps": ("daily_activity", "steps", "步数", "步"),
}


class MachineHealthService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def audit(
        self,
        *,
        request_id: str,
        principal: MachinePrincipal,
        capability: str,
        profile_id: str,
        outcome: str,
        status_code: int,
        resource_uri: str | None = None,
        detail_code: str | None = None,
    ) -> None:
        self.db.add(
            AgentMachineAudit(
                request_id=request_id,
                agent_id=principal.agent_id,
                capability=capability,
                profile_id=profile_id,
                outcome=outcome,
                status_code=status_code,
                resource_uri=resource_uri,
                detail_code=detail_code,
            )
        )
        self.db.flush()

    def commit_audit(self) -> None:
        """Persist a denial before the request dependency rolls its transaction back."""
        self.db.commit()

    def summary(self, profile_id: str, day: date_type) -> dict[str, Any]:
        diet = self.db.execute(
            text(
                f"SELECT COALESCE(SUM(kcal), 0) AS kcal, "
                f"COALESCE(SUM(protein_g), 0) AS protein_g "
                f"FROM {SCHEMA}.diet_logs WHERE log_date = :day"
            ),
            {"day": day},
        ).one()
        workout = self.db.execute(
            text(
                f"SELECT COUNT(*) AS sessions, COALESCE(SUM(duration_min), 0) AS minutes "
                f"FROM {SCHEMA}.workout_logs WHERE log_date = :day"
            ),
            {"day": day},
        ).one()
        activity = self.db.execute(
            text(f"SELECT steps FROM {SCHEMA}.daily_activity WHERE log_date = :day"),
            {"day": day},
        ).one_or_none()
        metrics = self.db.execute(
            text(
                f"SELECT weight_kg, sleep_hours, mood_score "
                f"FROM {SCHEMA}.body_metrics WHERE log_date = :day"
            ),
            {"day": day},
        ).one_or_none()

        values = {
            "diet_kcal": _number(diet.kcal),
            "protein_g": _number(diet.protein_g),
            "steps": int(activity.steps) if activity and activity.steps is not None else None,
            "workout_sessions": int(workout.sessions),
            "workout_min": int(workout.minutes),
            "weight_kg": _number(metrics.weight_kg) if metrics else None,
            "sleep_hours": _number(metrics.sleep_hours) if metrics else None,
            "mood_score": int(metrics.mood_score) if metrics and metrics.mood_score else None,
        }
        parts = [
            f"{day.isoformat()} 健康摘要",
            f"饮食 {values['diet_kcal']:g} kcal / 蛋白质 {values['protein_g']:g} g",
            f"步数 {values['steps']}" if values["steps"] is not None else "步数未记录",
            f"训练 {values['workout_sessions']} 次、{values['workout_min']} 分钟",
        ]
        if values["sleep_hours"] is not None:
            parts.append(f"睡眠 {values['sleep_hours']:g} 小时")
        if values["weight_kg"] is not None:
            parts.append(f"体重 {values['weight_kg']:g} kg")
        resource_uri = f"shadow://health/profiles/{profile_id}/summary/{day.isoformat()}"
        return {
            "summary": "；".join(parts) + "。仅用于记录与趋势参考，不构成诊断或治疗建议。",
            "resource_uri": resource_uri,
            "date": day.isoformat(),
            "indicators": values,
        }

    def trend(self, profile_id: str, metric: str, days: int) -> dict[str, Any]:
        table, column, label, unit = TREND_FIELDS[metric]
        end_day = today_local()
        start_day = end_day - timedelta(days=days - 1)
        rows = self.db.execute(
            text(
                f"SELECT log_date, {column} AS value FROM {SCHEMA}.{table} "
                f"WHERE log_date BETWEEN :start_day AND :end_day "
                f"AND {column} IS NOT NULL ORDER BY log_date"
            ),
            {"start_day": start_day, "end_day": end_day},
        ).all()
        values = [_number(row.value) for row in rows]
        first = values[0] if values else None
        last = values[-1] if values else None
        average = round(sum(values) / len(values), 2) if values else None
        change = round(last - first, 2) if first is not None and last is not None else None
        if not values:
            summary = f"近 {days} 天没有可用的{label}记录。"
        else:
            summary = (
                f"近 {days} 天{label}有 {len(values)} 个记录点，"
                f"均值 {average:g} {unit}，期初 {first:g}、期末 {last:g} {unit}，"
                f"变化 {change:+g} {unit}。"
            )
        summary += " 趋势仅供个人记录参考，不构成诊断或治疗建议。"
        return {
            "summary": summary,
            "resource_uri": f"shadow://health/profiles/{profile_id}/trends/{metric}/{days}d",
            "metric": metric,
            "days": days,
            "data_points": len(values),
            "statistics": {
                "first": first,
                "last": last,
                "average": average,
                "change": change,
                "unit": unit,
            },
        }

    def create_draft(
        self,
        *,
        principal: MachinePrincipal,
        profile_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        payload_hash: str,
    ) -> tuple[AgentRecordDraft, bool]:
        existing = self.db.execute(
            select(AgentRecordDraft).where(
                AgentRecordDraft.agent_id == principal.agent_id,
                AgentRecordDraft.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise MachineAPIError(
                    409,
                    "idempotency_conflict",
                    "The idempotency key was already used for different content.",
                )
            return existing, True

        draft = AgentRecordDraft(
            draft_id=f"hd_{uuid.uuid4().hex}",
            agent_id=principal.agent_id,
            profile_id=profile_id,
            record_type=payload["record_type"],
            effective_date=date_type.fromisoformat(payload["effective_date"]),
            payload=payload,
            payload_hash=payload_hash,
            idempotency_key=idempotency_key,
            status="pending",
        )
        try:
            with self.db.begin_nested():
                self.db.add(draft)
                self.db.flush()
        except IntegrityError:
            existing = self.db.execute(
                select(AgentRecordDraft).where(
                    AgentRecordDraft.agent_id == principal.agent_id,
                    AgentRecordDraft.idempotency_key == idempotency_key,
                )
            ).scalar_one()
            if existing.payload_hash != payload_hash:
                raise MachineAPIError(
                    409,
                    "idempotency_conflict",
                    "The idempotency key was already used for different content.",
                )
            return existing, True
        return draft, False

    def commit_draft(self, draft: AgentRecordDraft) -> tuple[str, bool]:
        """Materialize one explicitly reviewed Nexus draft into canonical Health data."""
        if draft.status == "applied":
            resource_uri = draft.payload.get("_result_uri")
            if isinstance(resource_uri, str) and resource_uri:
                return resource_uri, True
            raise MachineAPIError(409, "draft_state_invalid", "The applied draft has no result reference.")
        if draft.status != "pending":
            raise MachineAPIError(409, "draft_state_invalid", "The draft is not pending review.")
        fields = draft.payload["fields"]
        provenance = {
            "source": "shadow-nexus",
            "agent_draft_id": draft.draft_id,
            "agent_id": draft.agent_id,
        }
        if draft.record_type == "meal":
            record = DietLog(
                log_date=draft.effective_date,
                meal=fields["meal"],
                free_text=fields["name"],
                amount_g=fields.get("amount_g"),
                kcal=fields.get("kcal"),
                protein_g=fields.get("protein_g"),
                provenance=provenance,
            )
            self.db.add(record)
            self.db.flush()
            resource_uri = f"shadow://health/diet/{record.id}"
        elif draft.record_type == "workout":
            record = WorkoutLog(
                log_date=draft.effective_date,
                session_type=fields["session_type"],
                duration_min=fields["duration_min"],
                distance_km=fields.get("distance_km"),
                rpe=fields.get("rpe"),
                notes=fields.get("notes") or draft.payload.get("note"),
                source="manual",
                external_id=f"agent-{draft.draft_id}",
                detail={"provenance": provenance},
            )
            self.db.add(record)
            self.db.flush()
            resource_uri = f"shadow://health/workouts/{record.id}"
        else:
            record = self.db.execute(
                select(BodyMetrics).where(BodyMetrics.log_date == draft.effective_date)
            ).scalar_one_or_none()
            if record is None:
                record = BodyMetrics(log_date=draft.effective_date, autofilled={})
                self.db.add(record)
            markers = dict(record.autofilled or {})
            for field_name, value in fields.items():
                setattr(record, field_name, value)
                markers[field_name] = "shadow-nexus"
            record.autofilled = markers
            self.db.flush()
            resource_uri = f"shadow://health/metrics/{draft.effective_date.isoformat()}"
        draft.payload = {**draft.payload, "_result_uri": resource_uri}
        draft.status = "applied"
        self.db.flush()
        return resource_uri, False


def get_machine_health_service(db: Session = Depends(get_db)) -> MachineHealthService:
    return MachineHealthService(db)


@router.get("/profiles/{profile_id}/summary")
def get_health_profile_summary(
    request: Request,
    profile_id: str,
    date_value: str = Query(default="", alias="date"),
    service: MachineHealthService = Depends(get_machine_health_service),
) -> JSONResponse:
    capability = "health.summary.read"
    principal, request_id = _authorize(
        request, service, profile_id, capability, "summary:read"
    )
    try:
        day = date_type.fromisoformat(date_value) if date_value else today_local()
        if day > today_local():
            raise ValueError
    except ValueError as exc:
        service.audit(
            request_id=request_id,
            principal=principal,
            capability=capability,
            profile_id=profile_id,
            outcome="rejected",
            status_code=400,
            detail_code="invalid_date",
        )
        service.commit_audit()
        raise MachineAPIError(
            400, "invalid_date", "Date must be today or an earlier ISO date."
        ) from exc
    result = service.summary(profile_id, day)
    service.audit(
        request_id=request_id,
        principal=principal,
        capability=capability,
        profile_id=profile_id,
        outcome="success",
        status_code=200,
        resource_uri=result["resource_uri"],
    )
    return JSONResponse(result)


@router.get("/profiles/{profile_id}/trends")
def get_health_profile_trend(
    request: Request,
    profile_id: str,
    metric: str,
    days: int = 30,
    service: MachineHealthService = Depends(get_machine_health_service),
) -> JSONResponse:
    capability = "health.trends.read"
    principal, request_id = _authorize(
        request, service, profile_id, capability, "trends:read"
    )
    if metric not in TREND_FIELDS or not 7 <= days <= 90:
        service.audit(
            request_id=request_id,
            principal=principal,
            capability=capability,
            profile_id=profile_id,
            outcome="rejected",
            status_code=400,
            detail_code="invalid_trend_query",
        )
        service.commit_audit()
        raise MachineAPIError(
            400,
            "invalid_trend_query",
            "Metric or trend window is outside the supported boundary.",
        )
    result = service.trend(profile_id, metric, days)
    service.audit(
        request_id=request_id,
        principal=principal,
        capability=capability,
        profile_id=profile_id,
        outcome="success",
        status_code=200,
        resource_uri=result["resource_uri"],
    )
    return JSONResponse(result)


@router.post("/profiles/{profile_id}/drafts", status_code=201)
async def create_health_record_draft(
    request: Request,
    profile_id: str,
    service: MachineHealthService = Depends(get_machine_health_service),
) -> JSONResponse:
    capability = "health.records.draft"
    principal, request_id = _authorize(
        request, service, profile_id, capability, "drafts:create"
    )
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not IDEMPOTENCY_RE.fullmatch(idempotency_key):
        service.audit(
            request_id=request_id,
            principal=principal,
            capability=capability,
            profile_id=profile_id,
            outcome="rejected",
            status_code=400,
            detail_code="invalid_idempotency_key",
        )
        service.commit_audit()
        raise MachineAPIError(
            400,
            "invalid_idempotency_key",
            "A stable idempotency key is required for draft creation.",
        )
    try:
        body = await _read_bounded_body(request, MAX_DRAFT_BODY_BYTES)
    except MachineAPIError:
        service.audit(
            request_id=request_id,
            principal=principal,
            capability=capability,
            profile_id=profile_id,
            outcome="rejected",
            status_code=413,
            detail_code="payload_too_large",
        )
        service.commit_audit()
        raise
    try:
        payload = _validate_draft_payload(json.loads(body))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        service.audit(
            request_id=request_id,
            principal=principal,
            capability=capability,
            profile_id=profile_id,
            outcome="rejected",
            status_code=400,
            detail_code="invalid_draft",
        )
        service.commit_audit()
        raise MachineAPIError(400, "invalid_draft", "Draft content is invalid.") from exc
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    try:
        draft, replayed = service.create_draft(
            principal=principal,
            profile_id=profile_id,
            idempotency_key=idempotency_key,
            payload=payload,
            payload_hash=payload_hash,
        )
    except MachineAPIError as exc:
        service.audit(
            request_id=request_id,
            principal=principal,
            capability=capability,
            profile_id=profile_id,
            outcome="rejected",
            status_code=exc.status_code,
            detail_code=exc.code,
        )
        service.commit_audit()
        raise
    resource_uri = f"shadow://health/drafts/{draft.draft_id}"
    service.audit(
        request_id=request_id,
        principal=principal,
        capability=capability,
        profile_id=profile_id,
        outcome="replayed" if replayed else "success",
        status_code=201,
        resource_uri=resource_uri,
    )
    return JSONResponse(
        {
            "resource_uri": resource_uri,
            "summary": f"已创建 {draft.record_type} 健康记录草案，等待用户审核。",
            "draft_id": draft.draft_id,
            "profile_id": draft.profile_id,
            "status": "pending",
            "direct_domain_write": False,
            "replayed": replayed,
        },
        status_code=201,
    )


@router.post("/profiles/{profile_id}/drafts/{draft_id}/commit")
def commit_health_record_draft(
    request: Request,
    profile_id: str,
    draft_id: str,
    service: MachineHealthService = Depends(get_machine_health_service),
) -> JSONResponse:
    capability = "health.records.write"
    principal, request_id = _authorize(
        request, service, profile_id, capability, "records:write"
    )
    draft = service.db.get(AgentRecordDraft, draft_id)
    if draft is None or draft.profile_id != profile_id or draft.agent_id != principal.agent_id:
        service.audit(
            request_id=request_id,
            principal=principal,
            capability=capability,
            profile_id=profile_id,
            outcome="rejected",
            status_code=404,
            detail_code="draft_not_found",
        )
        raise MachineAPIError(404, "draft_not_found", "The health draft was not found.")
    try:
        resource_uri, replayed = service.commit_draft(draft)
    except MachineAPIError as exc:
        service.audit(
            request_id=request_id,
            principal=principal,
            capability=capability,
            profile_id=profile_id,
            outcome="rejected",
            status_code=exc.status_code,
            detail_code=exc.code,
        )
        raise
    service.audit(
        request_id=request_id,
        principal=principal,
        capability=capability,
        profile_id=profile_id,
        outcome="replayed" if replayed else "success",
        status_code=200,
        resource_uri=resource_uri,
    )
    return JSONResponse(
        {
            "resource_uri": resource_uri,
            "draft_id": draft.draft_id,
            "profile_id": draft.profile_id,
            "record_type": draft.record_type,
            "status": "applied",
            "replayed": replayed,
        }
    )


def _authorize(
    request: Request,
    service: MachineHealthService,
    profile_id: str,
    capability: str,
    grant: str,
) -> tuple[MachinePrincipal, str]:
    if not PROFILE_ID_RE.fullmatch(profile_id):
        raise MachineAPIError(404, "resource_not_found", "Health profile was not found.")
    principal = authenticate_machine_request(request)
    request_id = machine_request_id(request)
    try:
        authorize_machine_principal(
            principal,
            audience=AUDIENCE,
            scope=capability,
            profile_id=profile_id,
            grant=grant,
        )
    except MachineAPIError as exc:
        service.audit(
            request_id=request_id,
            principal=principal,
            capability=capability,
            profile_id=profile_id,
            outcome="denied",
            status_code=exc.status_code,
            detail_code=exc.code,
        )
        service.commit_audit()
        raise
    return principal, request_id


def _validate_draft_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"record_type", "effective_date", "fields", "note"}:
        raise ValueError
    record_type = raw.get("record_type")
    if record_type not in {"metric", "meal", "workout"}:
        raise ValueError
    effective_date = date_type.fromisoformat(str(raw.get("effective_date", "")))
    if effective_date > today_local() or effective_date < today_local() - timedelta(days=366):
        raise ValueError
    fields = raw.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError
    note = raw.get("note")
    if note is not None and (not isinstance(note, str) or len(note.strip()) > 500):
        raise ValueError

    validators = {
        "metric": _validate_metric_fields,
        "meal": _validate_meal_fields,
        "workout": _validate_workout_fields,
    }
    normalized_fields = validators[record_type](fields)
    result: dict[str, Any] = {
        "record_type": record_type,
        "effective_date": effective_date.isoformat(),
        "fields": normalized_fields,
    }
    if note and note.strip():
        result["note"] = note.strip()
    return result


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise MachineAPIError(413, "payload_too_large", "Draft payload exceeds the limit.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise MachineAPIError(413, "payload_too_large", "Draft payload exceeds the limit.")
    return bytes(body)


def _validate_metric_fields(fields: dict[str, Any]) -> dict[str, Any]:
    bounds = {"weight_kg": (20, 400), "sleep_hours": (0, 24), "mood_score": (1, 10)}
    if set(fields) - set(bounds):
        raise ValueError
    result = {key: _bounded_number(value, *bounds[key]) for key, value in fields.items()}
    if "mood_score" in result and not float(result["mood_score"]).is_integer():
        raise ValueError
    return result


def _validate_meal_fields(fields: dict[str, Any]) -> dict[str, Any]:
    allowed = {"meal", "name", "amount_g", "kcal", "protein_g"}
    if set(fields) - allowed or fields.get("meal") not in {"早餐", "午餐", "晚餐", "加餐"}:
        raise ValueError
    name = fields.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ValueError
    result: dict[str, Any] = {"meal": fields["meal"], "name": name.strip()}
    for key, high in {"amount_g": 5000, "kcal": 20000, "protein_g": 1000}.items():
        if key in fields:
            result[key] = _bounded_number(fields[key], 0, high)
    return result


def _validate_workout_fields(fields: dict[str, Any]) -> dict[str, Any]:
    allowed = {"session_type", "duration_min", "distance_km", "rpe", "notes"}
    if set(fields) - allowed:
        raise ValueError
    session_type = fields.get("session_type")
    if not isinstance(session_type, str) or not 1 <= len(session_type.strip()) <= 120:
        raise ValueError
    duration = _bounded_number(fields.get("duration_min"), 1, 1440)
    if not float(duration).is_integer():
        raise ValueError
    result: dict[str, Any] = {
        "session_type": session_type.strip(),
        "duration_min": int(duration),
    }
    if "distance_km" in fields:
        result["distance_km"] = _bounded_number(fields["distance_km"], 0, 1000)
    if "rpe" in fields:
        rpe = _bounded_number(fields["rpe"], 1, 10)
        if not float(rpe).is_integer():
            raise ValueError
        result["rpe"] = int(rpe)
    if "notes" in fields:
        notes = fields["notes"]
        if not isinstance(notes, str) or len(notes.strip()) > 500:
            raise ValueError
        result["notes"] = notes.strip()
    return result


def _bounded_number(value: Any, low: float, high: float) -> int | float:
    if isinstance(value, bool):
        raise ValueError
    number = float(value)
    if not low <= number <= high:
        raise ValueError
    return int(number) if number.is_integer() else number


def _number(value: Any) -> float:
    return float(value or 0)
