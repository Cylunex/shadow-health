"""Runtime-neutral Shadow Agent machine API for the dedicated Health Profile."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, time, timedelta
from datetime import date as date_type
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.diet_notes import DIET_NOTES_MAX_LENGTH, parse_diet_notes
from app.machine_auth import (
    MachineAPIError,
    MachinePrincipal,
    authenticate_machine_request,
    authorize_machine_principal,
    machine_request_id,
)
from app.models import (
    SCHEMA,
    AgentMachineAudit,
    AgentRecordDraft,
    BodyMetrics,
    DietLog,
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
    # Daily minimum is an explicit resting-heart-rate proxy, not a diagnosis.
    "heart_rate": ("daily_activity", "hr_min", "日最低心率（静息代理）", "bpm"),
}
SUMMARY_INDICATORS = (
    "diet_kcal",
    "protein_g",
    "steps",
    "workout_sessions",
    "workout_min",
    "weight_kg",
    "sleep_hours",
    "mood_score",
)
MIN_TREND_POINTS = 3


def _source_name(provenance: Any, default: str = "manual") -> str:
    provenance = _json_object(provenance)
    if isinstance(provenance, dict):
        value = provenance.get("source") or provenance.get("channel")
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return default


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _freshness_status(*, present: bool, effective_day: date_type) -> str:
    if not present:
        return "unknown"
    age_days = max((today_local() - effective_day).days, 0)
    return "fresh" if age_days <= 2 else "stale"


def _as_date(value: Any) -> date_type:
    if isinstance(value, date_type):
        return value
    return date_type.fromisoformat(str(value))


def _metric_evidence(
    *,
    present: bool,
    effective_day: date_type,
    sources: set[str] | None = None,
    last_updated_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "present": present,
        "effective_date": effective_day.isoformat() if present else None,
        "last_updated_at": _iso(last_updated_at),
        "freshness": _freshness_status(present=present, effective_day=effective_day),
        "sources": sorted(sources or set()),
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
                f"SELECT COUNT(*) AS records, COALESCE(SUM(kcal), 0) AS kcal, "
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
            text(
                f"SELECT steps, source, field_sources, updated_at FROM {SCHEMA}.daily_activity "
                f"WHERE log_date = :day"
            ),
            {"day": day},
        ).one_or_none()
        metrics = self.db.execute(
            text(
                f"SELECT weight_kg, sleep_hours, mood_score, autofilled, updated_at "
                f"FROM {SCHEMA}.body_metrics WHERE log_date = :day"
            ),
            {"day": day},
        ).one_or_none()
        diet_meta = self.db.execute(
            select(DietLog.provenance, DietLog.updated_at).where(DietLog.log_date == day)
        ).all()
        workout_meta = self.db.execute(
            select(WorkoutLog.source, WorkoutLog.updated_at).where(
                WorkoutLog.log_date == day
            )
        ).all()

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
        diet_present = int(diet.records) > 0
        workout_present = int(workout.sessions) > 0
        metric_sources = _json_object(metrics.autofilled) if metrics else {}
        diet_sources = {_source_name(row.provenance) for row in diet_meta}
        workout_sources = {str(row.source or "manual") for row in workout_meta}
        activity_sources = _json_object(activity.field_sources) if activity else {}
        diet_updated = max((row.updated_at for row in diet_meta), default=None)
        workout_updated = max((row.updated_at for row in workout_meta), default=None)
        evidence = {
            "diet_kcal": _metric_evidence(
                present=diet_present,
                effective_day=day,
                sources=diet_sources,
                last_updated_at=diet_updated,
            ),
            "protein_g": _metric_evidence(
                present=diet_present,
                effective_day=day,
                sources=diet_sources,
                last_updated_at=diet_updated,
            ),
            "steps": _metric_evidence(
                present=values["steps"] is not None,
                effective_day=day,
                sources=(
                    {str(activity_sources.get("steps") or activity.source)}
                    if activity
                    else set()
                ),
                last_updated_at=activity.updated_at if activity else None,
            ),
            "workout_sessions": _metric_evidence(
                present=workout_present,
                effective_day=day,
                sources=workout_sources,
                last_updated_at=workout_updated,
            ),
            "workout_min": _metric_evidence(
                present=workout_present,
                effective_day=day,
                sources=workout_sources,
                last_updated_at=workout_updated,
            ),
        }
        for field in ("weight_kg", "sleep_hours", "mood_score"):
            source = metric_sources.get(field) or "manual"
            evidence[field] = _metric_evidence(
                present=values[field] is not None,
                effective_day=day,
                sources={source} if values[field] is not None else set(),
                last_updated_at=metrics.updated_at if metrics else None,
            )
        observed_count = sum(1 for item in evidence.values() if item["present"])
        all_sources = sorted({source for item in evidence.values() for source in item["sources"]})
        parts = [
            f"{day.isoformat()} 健康摘要",
            (
                f"饮食 {values['diet_kcal']:g} kcal / 蛋白质 {values['protein_g']:g} g"
                if diet_present else "饮食未记录"
            ),
            f"步数 {values['steps']}" if values["steps"] is not None else "步数未记录",
            (
                f"训练 {values['workout_sessions']} 次、{values['workout_min']} 分钟"
                if workout_present else "训练未记录"
            ),
        ]
        if values["sleep_hours"] is not None:
            parts.append(f"睡眠 {values['sleep_hours']:g} 小时")
        if values["weight_kg"] is not None:
            parts.append(f"体重 {values['weight_kg']:g} kg")
        resource_uri = f"shadow://health/profiles/{profile_id}/summary/{day.isoformat()}"
        return {
            "summary": (
                "；".join(parts)
                + f"。字段覆盖 {observed_count}/{len(SUMMARY_INDICATORS)}"
                + (f"，来源 {', '.join(all_sources)}" if all_sources else "，暂无来源证据")
                + "。仅用于记录与趋势参考，不构成诊断或治疗建议。"
            ),
            "resource_uri": resource_uri,
            "date": day.isoformat(),
            "indicators": values,
            "data_quality": {
                "observed_indicators": observed_count,
                "expected_indicators": len(SUMMARY_INDICATORS),
                "coverage_ratio": round(observed_count / len(SUMMARY_INDICATORS), 3),
                "sources": all_sources,
                "indicators": evidence,
            },
        }

    def trend(self, profile_id: str, metric: str, days: int) -> dict[str, Any]:
        table, column, label, unit = TREND_FIELDS[metric]
        end_day = today_local()
        start_day = end_day - timedelta(days=days - 1)
        extra = (
            "autofilled, updated_at"
            if table == "body_metrics"
            else "source, field_sources, updated_at"
        )
        rows = self.db.execute(
            text(
                f"SELECT log_date, {column} AS value, {extra} FROM {SCHEMA}.{table} "
                f"WHERE log_date BETWEEN :start_day AND :end_day "
                f"AND {column} IS NOT NULL ORDER BY log_date"
            ),
            {"start_day": start_day, "end_day": end_day},
        ).all()
        values = [_number(row.value) for row in rows]
        first = values[0] if values else None
        last = values[-1] if values else None
        sufficient = len(values) >= MIN_TREND_POINTS
        average = round(sum(values) / len(values), 2) if sufficient else None
        change = round(last - first, 2) if sufficient and first is not None and last is not None else None
        sources: set[str] = set()
        last_updated_at: datetime | None = None
        for row in rows:
            if table == "body_metrics":
                markers = _json_object(row.autofilled)
                sources.add(str(markers.get(column) or "manual"))
            else:
                markers = _json_object(row.field_sources)
                sources.add(str(markers.get(column) or row.source or "unknown"))
            if row.updated_at is not None and (
                last_updated_at is None or row.updated_at > last_updated_at
            ):
                last_updated_at = row.updated_at
        coverage_ratio = round(len(values) / days, 3)
        latest_day = _as_date(rows[-1].log_date) if rows else None
        if not values:
            summary = f"近 {days} 天没有可用的{label}记录。"
        elif not sufficient:
            summary = (
                f"近 {days} 天{label}只有 {len(values)} 个记录点（覆盖 {len(values)}/{days} 天），"
                "样本不足，暂不计算均值或变化。"
            )
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
            "data_quality": {
                "coverage_ratio": coverage_ratio,
                "covered_days": len(values),
                "expected_days": days,
                "sufficient_for_trend": sufficient,
                "sources": sorted(sources),
                "latest_effective_date": latest_day.isoformat() if latest_day else None,
                "last_updated_at": _iso(last_updated_at),
                "freshness": (
                    _freshness_status(present=True, effective_day=latest_day)
                    if latest_day else "unknown"
                ),
            },
        }

    def weekly_suggestions(self, profile_id: str, now: datetime | None = None) -> dict[str, Any]:
        """Build one bounded, explainable suggestion from confirmed aggregate facts only."""
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        end_day = today_local()
        start_day = end_day - timedelta(days=6)
        daily = [self.summary(profile_id, start_day + timedelta(days=index)) for index in range(7)]
        present = [item for item in daily if item["data_quality"]["observed_indicators"]]
        recorded_days = len(present)
        missing_ratio = round((7 - recorded_days) / 7, 3)
        steps = [item["indicators"]["steps"] for item in daily if item["indicators"]["steps"] is not None]
        sleep = [item["indicators"]["sleep_hours"] for item in daily if item["indicators"]["sleep_hours"] is not None]
        workout_min = sum(item["indicators"]["workout_min"] for item in daily)
        diet_days = sum(
            1
            for item in daily
            if item["data_quality"]["indicators"]["diet_kcal"]["present"]
        )
        coverage = {
            "any": recorded_days,
            "steps": len(steps),
            "sleep": len(sleep),
            "workout": sum(
                1 for item in daily
                if item["data_quality"]["indicators"]["workout_sessions"]["present"]
            ),
            "diet": diet_days,
        }
        all_sources = sorted({
            source
            for item in daily
            for source in item["data_quality"]["sources"]
        })
        facts = [
            f"近 7 天任意健康字段覆盖 {recorded_days}/7 天",
            f"训练记录覆盖 {coverage['workout']}/7 天、合计 {workout_min} 分钟",
        ]
        if len(steps) >= MIN_TREND_POINTS:
            facts.append(f"有记录日平均 {round(sum(steps) / len(steps))} 步")
        elif steps:
            facts.append(f"步数仅覆盖 {len(steps)}/7 天，不计算周均")
        if len(sleep) >= MIN_TREND_POINTS:
            facts.append(f"有记录日平均睡眠 {sum(sleep) / len(sleep):.1f} 小时")
        elif sleep:
            facts.append(f"睡眠仅覆盖 {len(sleep)}/7 天，不计算周均")
        facts.append(f"饮食记录覆盖 {diet_days}/7 天")
        week_start = end_day - timedelta(days=end_day.weekday())
        week_key = f"{week_start.isocalendar().year}-W{week_start.isocalendar().week:02d}"
        evidence = f"shadow://health/profiles/{profile_id}/weekly-reviews/{week_key}"
        valid_until = datetime.combine(week_start + timedelta(days=7), time.min, tzinfo=UTC)
        digest = hashlib.sha256(f"{profile_id}:{week_key}".encode()).hexdigest()[:20]
        sufficient_domains = sum(coverage[key] >= MIN_TREND_POINTS for key in ("steps", "sleep", "workout", "diet"))
        if recorded_days < 3 or sufficient_domains < 2:
            summary = "本周数据覆盖不足，当前只展示记录事实，不生成精确趋势判断。"
            importance = "low"
            allowed_actions = ["view_evidence", "snooze", "ignore", "mute"]
        else:
            summary = "本周健康记录已形成可回顾的聚合脉络，可以据此检查节奏并创建调整草稿。"
            importance = "normal"
            allowed_actions = ["view_evidence", "create_draft", "snooze", "ignore", "mute"]
        quality_score = sum(coverage[key] / 7 for key in ("steps", "sleep", "workout", "diet")) / 4
        return {
            "protocol": "shadow.suggestion-list.v1",
            "items": [{
                "protocol": "shadow.suggestion.v1",
                "suggestion_id": f"sug_health_{digest}",
                "domain": "health",
                "rule_id": "health.weekly-review",
                "dedupe_key": f"health:{profile_id}:weekly-review:{week_key}",
                "title": "本周健康回顾已就绪",
                "summary": summary,
                "reason": "；".join(facts) + "。数据只用于个人记录与趋势参考，不构成诊断或治疗建议。",
                "evidence_refs": [evidence],
                "importance": importance,
                "confidence": round(max(0.1, quality_score), 3),
                "allowed_actions": allowed_actions,
                "created_at": datetime.combine(week_start, time.min, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
                "valid_until": valid_until.isoformat().replace("+00:00", "Z"),
                "data_freshness": {
                    "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                    "missing_ratio": missing_ratio,
                    "coverage_days": coverage,
                    "sources": all_sources,
                    "quality_score": round(quality_score, 3),
                    "latest_effective_date": max(
                        (item["date"] for item in present), default=None
                    ),
                },
            }],
            "generated_at": observed_at.isoformat().replace("+00:00", "Z"),
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
            items = fields.get("items")
            meal_items = items if isinstance(items, list) else [fields]
            records = [
                DietLog(
                    log_date=draft.effective_date,
                    meal=fields["meal"],
                    free_text=item["name"],
                    amount_g=item.get("amount_g"),
                    kcal=item.get("kcal"),
                    protein_g=item.get("protein_g"),
                    fat_g=item.get("fat_g"),
                    carb_g=item.get("carb_g"),
                    notes=item.get("notes"),
                    provenance={
                        **provenance,
                        "meal_name": fields["name"],
                        "item_index": index,
                        "item_count": len(meal_items),
                    },
                )
                for index, item in enumerate(meal_items, start=1)
            ]
            self.db.add_all(records)
            self.db.flush()
            result_ids = [record.id for record in records]
            resource_uri = (
                f"shadow://health/diet/{result_ids[0]}"
                if len(result_ids) == 1
                else f"shadow://health/diet/batches/{draft.draft_id}"
            )
            draft.payload = {**draft.payload, "_result_ids": result_ids}
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

    def pending_drafts(
        self, principal: MachinePrincipal, profile_id: str, limit: int
    ) -> list[AgentRecordDraft]:
        return list(
            self.db.scalars(
                select(AgentRecordDraft)
                .where(
                    AgentRecordDraft.agent_id == principal.agent_id,
                    AgentRecordDraft.profile_id == profile_id,
                    AgentRecordDraft.status == "pending",
                )
                .order_by(AgentRecordDraft.created_at, AgentRecordDraft.draft_id)
                .limit(limit)
            )
        )

    def reject_draft(self, draft: AgentRecordDraft) -> bool:
        if draft.status == "rejected":
            return True
        if draft.status != "pending":
            raise MachineAPIError(409, "draft_state_invalid", "The health draft is not pending review.")
        draft.status = "rejected"
        self.db.flush()
        return False


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


@router.get("/profiles/{profile_id}/suggestions", operation_id="list_health_suggestions")
def list_health_suggestions(
    request: Request,
    profile_id: str,
    service: MachineHealthService = Depends(get_machine_health_service),
) -> JSONResponse:
    capability = "health.suggestions.read"
    principal, request_id = _authorize(
        request, service, profile_id, capability, "suggestions:read"
    )
    result = service.weekly_suggestions(profile_id)
    evidence = result["items"][0]["evidence_refs"][0]
    service.audit(
        request_id=request_id,
        principal=principal,
        capability=capability,
        profile_id=profile_id,
        outcome="success",
        status_code=200,
        resource_uri=evidence,
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


@router.get("/profiles/{profile_id}/drafts")
def list_pending_health_record_drafts(
    request: Request,
    profile_id: str,
    limit: int = Query(default=200, ge=1, le=200),
    service: MachineHealthService = Depends(get_machine_health_service),
) -> JSONResponse:
    capability = "health.records.write"
    principal, request_id = _authorize(
        request, service, profile_id, capability, "records:write"
    )
    drafts = service.pending_drafts(principal, profile_id, limit)
    service.audit(
        request_id=request_id,
        principal=principal,
        capability=capability,
        profile_id=profile_id,
        outcome="success",
        status_code=200,
    )
    return JSONResponse(
        {
            "items": [
                {
                    "resource_uri": f"shadow://health/drafts/{draft.draft_id}",
                    "draft_id": draft.draft_id,
                    "profile_id": draft.profile_id,
                    "record_type": draft.record_type,
                    "effective_date": draft.effective_date.isoformat(),
                    "fields": draft.payload["fields"],
                    "note": draft.payload.get("note", ""),
                    "created_at": draft.created_at.isoformat(),
                    "status": "pending",
                }
                for draft in drafts
            ],
            "truncated": len(drafts) == limit,
        }
    )


@router.post("/profiles/{profile_id}/drafts/{draft_id}/reject")
def reject_health_record_draft(
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
    replayed = service.reject_draft(draft)
    resource_uri = f"shadow://health/drafts/{draft.draft_id}"
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
            "status": "rejected",
            "replayed": replayed,
        }
    )


@router.post("/nexus/reviews", status_code=201, operation_id="create_nexus_health_review")
async def create_nexus_health_review(
    request: Request,
    profile_id: str = Query(min_length=1, max_length=64),
    service: MachineHealthService = Depends(get_machine_health_service),
) -> JSONResponse:
    """Create the domain-owned draft behind one Nexus Proposal."""
    capability = "health.records.draft"
    principal, request_id = _authorize(
        request, service, profile_id, capability, "drafts:create"
    )
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise MachineAPIError(
            400, "invalid_idempotency_key", "A stable idempotency key is required."
        )
    try:
        raw = json.loads(await _read_bounded_body(request, MAX_DRAFT_BODY_BYTES))
        payload = _nexus_health_payload(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise MachineAPIError(400, "invalid_nexus_review", "Nexus review is invalid.") from exc
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    draft, replayed = service.create_draft(
        principal=principal,
        profile_id=profile_id,
        idempotency_key=idempotency_key,
        payload=payload,
        payload_hash=payload_hash,
    )
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
        _health_review_envelope(draft, trace_id=request_id, replayed=replayed),
        status_code=201,
    )


@router.get("/nexus/reviews", operation_id="list_nexus_health_reviews")
def list_nexus_health_reviews(
    request: Request,
    profile_id: str = Query(min_length=1, max_length=64),
    limit: int = Query(default=200, ge=1, le=200),
    service: MachineHealthService = Depends(get_machine_health_service),
) -> JSONResponse:
    capability = "health.records.write"
    principal, request_id = _authorize(
        request, service, profile_id, capability, "records:write"
    )
    drafts = service.pending_drafts(principal, profile_id, limit)
    service.audit(
        request_id=request_id,
        principal=principal,
        capability=capability,
        profile_id=profile_id,
        outcome="success",
        status_code=200,
    )
    return JSONResponse(
        {
            "protocol": "shadow.review.v1",
            "items": [
                _health_review_envelope(draft, trace_id=request_id) for draft in drafts
            ],
            "truncated": len(drafts) == limit,
            "trace_id": request_id,
        }
    )


@router.post(
    "/nexus/reviews/{review_id}/commit",
    operation_id="commit_nexus_health_review",
)
def commit_nexus_health_review(
    review_id: str,
    request: Request,
    profile_id: str = Query(min_length=1, max_length=64),
    service: MachineHealthService = Depends(get_machine_health_service),
) -> JSONResponse:
    capability = "health.records.write"
    principal, request_id = _authorize(
        request, service, profile_id, capability, "records:write"
    )
    draft = service.db.get(AgentRecordDraft, review_id)
    if draft is None or draft.profile_id != profile_id or draft.agent_id != principal.agent_id:
        raise MachineAPIError(404, "draft_not_found", "The health draft was not found.")
    receipt, replayed = service.commit_draft(draft)
    service.audit(
        request_id=request_id,
        principal=principal,
        capability=capability,
        profile_id=profile_id,
        outcome="replayed" if replayed else "success",
        status_code=200,
        resource_uri=receipt,
    )
    return JSONResponse(
        _health_review_envelope(
            draft, trace_id=request_id, replayed=replayed, receipt=receipt
        )
    )


@router.post(
    "/nexus/reviews/{review_id}/reject",
    operation_id="reject_nexus_health_review",
)
def reject_nexus_health_review(
    review_id: str,
    request: Request,
    profile_id: str = Query(min_length=1, max_length=64),
    service: MachineHealthService = Depends(get_machine_health_service),
) -> JSONResponse:
    capability = "health.records.write"
    principal, request_id = _authorize(
        request, service, profile_id, capability, "records:write"
    )
    draft = service.db.get(AgentRecordDraft, review_id)
    if draft is None or draft.profile_id != profile_id or draft.agent_id != principal.agent_id:
        raise MachineAPIError(404, "draft_not_found", "The health draft was not found.")
    replayed = service.reject_draft(draft)
    service.audit(
        request_id=request_id,
        principal=principal,
        capability=capability,
        profile_id=profile_id,
        outcome="replayed" if replayed else "success",
        status_code=200,
        resource_uri=f"shadow://health/drafts/{draft.draft_id}",
    )
    return JSONResponse(
        _health_review_envelope(draft, trace_id=request_id, replayed=replayed)
    )


def _nexus_health_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {
        "intent",
        "summary",
        "fields",
        "source_text",
        "source_refs",
    }:
        raise ValueError
    intent = raw.get("intent")
    fields = raw.get("fields")
    if not isinstance(intent, str) or not intent.startswith("health."):
        raise ValueError
    if not isinstance(fields, dict):
        raise ValueError
    record_type = fields.get("recordType")
    effective_date = str(fields.get("effectiveDate") or today_local().isoformat())
    native: dict[str, Any]
    if record_type == "metric":
        aliases = {
            "weightKg": "weight_kg",
            "sleepHours": "sleep_hours",
            "moodScore": "mood_score",
        }
        native = {
            target: fields[source]
            for source, target in aliases.items()
            if source in fields
        }
    elif record_type == "meal":
        native = {
            "meal": fields.get("meal"),
            "name": fields.get("mealName") or raw.get("summary"),
        }
        for source, target in {
            "amountG": "amount_g",
            "kcal": "kcal",
            "proteinG": "protein_g",
            "fatG": "fat_g",
            "carbG": "carb_g",
        }.items():
            if source in fields:
                native[target] = fields[source]
        if "notes" in fields:
            native["notes"] = fields["notes"]
        items_json = fields.get("mealItemsJson")
        if items_json is not None:
            items = json.loads(str(items_json))
            if not isinstance(items, list):
                raise ValueError
            native["items"] = [
                {
                    ({
                        "amountG": "amount_g",
                        "proteinG": "protein_g",
                        "fatG": "fat_g",
                        "carbG": "carb_g",
                    }.get(key, key)): value
                    for key, value in item.items()
                }
                for item in items
                if isinstance(item, dict)
            ]
    elif record_type == "workout":
        native = {
            "session_type": fields.get("sessionType") or "运动",
            "duration_min": fields.get("durationMin"),
        }
        if "distanceKm" in fields:
            native["distance_km"] = fields["distanceKm"]
        if "rpe" in fields:
            native["rpe"] = fields["rpe"]
    else:
        raise ValueError
    return _validate_draft_payload(
        {
            "record_type": record_type,
            "effective_date": effective_date,
            "fields": native,
            "note": str(raw.get("source_text") or raw.get("summary") or "")[:500],
        }
    )


def _health_review_envelope(
    draft: AgentRecordDraft,
    *,
    trace_id: str,
    replayed: bool = False,
    receipt: str | None = None,
) -> dict[str, Any]:
    fields = dict(draft.payload["fields"])
    aliases = {
        "weight_kg": "weightKg",
        "sleep_hours": "sleepHours",
        "mood_score": "moodScore",
        "name": "mealName",
        "amount_g": "amountG",
        "protein_g": "proteinG",
        "fat_g": "fatG",
        "carb_g": "carbG",
        "session_type": "sessionType",
        "duration_min": "durationMin",
        "distance_km": "distanceKm",
    }
    projected = {aliases.get(key, key): value for key, value in fields.items() if key != "items"}
    projected.update(
        {
            "recordType": draft.record_type,
            "effectiveDate": draft.effective_date.isoformat(),
        }
    )
    if isinstance(fields.get("items"), list):
        projected["mealItemsJson"] = json.dumps(fields["items"], ensure_ascii=False)
    state = {"pending": "pending", "applied": "committed", "rejected": "rejected"}.get(
        draft.status, draft.status
    )
    return {
        "protocol": "shadow.review.v1",
        "review_id": draft.draft_id,
        "reference": f"shadow://health/drafts/{draft.draft_id}",
        "revision": 1,
        "domain": "health",
        "intent": f"health.{draft.record_type}",
        "summary": draft.payload.get("note") or f"Health {draft.record_type} 草稿",
        "fields": projected,
        "risk_level": "L2",
        "state": state,
        "created_at": draft.created_at.isoformat(),
        "source_refs": [],
        "trace_id": trace_id,
        "receipt": receipt,
        "replayed": replayed,
    }


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
    allowed = {
        "meal", "name", "amount_g", "kcal", "protein_g", "fat_g", "carb_g", "notes", "items",
    }
    if set(fields) - allowed or fields.get("meal") not in {"早餐", "午餐", "晚餐", "加餐"}:
        raise ValueError
    name = fields.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ValueError
    result: dict[str, Any] = {"meal": fields["meal"], "name": name.strip()}
    for key, high in {
        "amount_g": 5000,
        "kcal": 20000,
        "protein_g": 1000,
        "fat_g": 1000,
        "carb_g": 5000,
    }.items():
        if key in fields:
            result[key] = _bounded_number(fields[key], 0, high)
    if "notes" in fields:
        notes = fields["notes"]
        if not isinstance(notes, str) or len(notes.strip()) > DIET_NOTES_MAX_LENGTH:
            raise ValueError
        result["notes"] = parse_diet_notes(notes)
    if "items" in fields:
        items = fields["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            raise ValueError
        result["items"] = [_validate_meal_item(item) for item in items]
    return result


def _validate_meal_item(raw: Any) -> dict[str, Any]:
    allowed = {"name", "amount_g", "kcal", "protein_g", "fat_g", "carb_g", "notes"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError
    name = raw.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ValueError
    result: dict[str, Any] = {"name": name.strip()}
    for key, high in {
        "amount_g": 5000,
        "kcal": 20000,
        "protein_g": 1000,
        "fat_g": 1000,
        "carb_g": 5000,
    }.items():
        if key in raw:
            result[key] = _bounded_number(raw[key], 0, high)
    if "notes" in raw:
        notes = raw["notes"]
        if not isinstance(notes, str) or len(notes.strip()) > DIET_NOTES_MAX_LENGTH:
            raise ValueError
        result["notes"] = parse_diet_notes(notes)
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
