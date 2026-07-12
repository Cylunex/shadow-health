"""Agent 通道核查页：agent（Hermes/OpenClaw）说「记好了」之后，来这页眼见为实。

- GET  /agent-log                    页面（通道健康度 + 最近 30 条写入流水）
- GET  /fragments/agent-log/status   状态片段（页面 hx-trigger="every 5s" 轮询，
                                     90s 内新到的记录带「新」徽标）
- POST /agent-log/revoke             行内撤销：删掉该条的归一化行（复用
                                     agent.delete_record，与 /api/agent/delete 同一实现）

数据全部来自现成落库：import_raw(source='agent') 留档流水 + sync_state('agent')。

撤销与「已撤销」判定：P2 留档 raw 里没存归一化行 id，按现状反查——
workout 按 external_id='agent-{client_id}' 唯一索引精确定位；diet 没有
external_id 列，按解析后的全部字段（同日同餐同文本同数值）内容匹配，
多条命中取 id 最小的一条（内容完全相同的行删哪条等价，逐次撤销逐条消化）。
撤销成功把 {revoked_row_id, revoked_at} 写进留档行的 blob（raw 原样不动，
不污染审计留档）；反查落空（行已被 MCP delete 或页面删掉）也如实显示「已撤销」。
habit/metric 不支持撤销：habit 是先到先得的声明式打卡、metric 是覆盖写，
删行都还原不了原状，到对应页面改。
"""
from __future__ import annotations

from datetime import date as date_type
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import DietLog, Habit, ImportRaw, SyncState, WorkoutLog
from app.routers.agent import SOURCE, delete_record
from app.routers.offline import _METRIC_BOUNDS, parse_diet_payload
from app.routers.scale import _ago
from app.timeutil import now_local

router = APIRouter(dependencies=[Depends(require_login)])

LIST_LIMIT = 30
FRESH_SECONDS = 90  # last_seen_at 在此窗口内的记录标「新」

TYPE_LABELS = {"diet": "饮食", "workout": "训练", "metric": "指标", "habit": "打卡"}
REVOCABLE_TYPES = ("diet", "workout")


def _summary(rtype: str, payload: dict, habit_names: dict[int, str]) -> str:
    """流水行内容摘要：diet 提 free_text、workout 提 session_type、metric 列字段名。"""
    if rtype == "diet":
        parts = [str(payload.get("meal") or "").strip(),
                 str(payload.get("free_text") or "").strip() or "—"]
        kcal = str(payload.get("kcal") or "").strip()
        if kcal:
            parts.append(f"{kcal} kcal")
        return " · ".join(p for p in parts if p)
    if rtype == "workout":
        parts = [str(payload.get("session_type") or "").strip() or "—"]
        dur = str(payload.get("duration_min") or "").strip()
        if dur:
            parts.append(f"{dur} 分钟")
        dist = str(payload.get("distance_km") or "").strip()
        if dist:
            parts.append(f"{dist} km")
        return " · ".join(parts)
    if rtype == "metric":
        parts = []
        for field, value in payload.items():
            bounds = _METRIC_BOUNDS.get(field)
            if bounds is not None and str(value or "").strip():
                parts.append(f"{bounds[0]} {value}")
        return " · ".join(parts) or "—"
    # habit
    try:
        habit_id = int(str(payload.get("habit_id")).strip())
    except (TypeError, ValueError):
        habit_id = None
    name = habit_names.get(habit_id, f"习惯 #{habit_id}" if habit_id else "—")
    count = str(payload.get("done_count") or "").strip()
    return f"{name} ×{count}" if count else name


def _resolve_row_id(db: Session, r: ImportRaw) -> int | None:
    """反查该留档行对应的归一化行 id；行不在（已删）返回 None。"""
    raw = r.raw if isinstance(r.raw, dict) else {}
    client_id = str(raw.get("client_id") or "").strip()
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    if r.record_type == "workout":
        return db.execute(
            select(WorkoutLog.id).where(
                WorkoutLog.source == "manual",
                WorkoutLog.external_id == f"{SOURCE}-{client_id}",
            )
        ).scalar_one_or_none()
    # diet：按解析后的全部字段内容匹配（与归一化落库值同口径），多条命中取 id 最小
    try:
        d = date_type.fromisoformat(str(raw.get("date")).strip())
        cols = parse_diet_payload(payload)
    except (TypeError, ValueError):
        return None
    stmt = select(DietLog.id).where(DietLog.log_date == d, DietLog.food_id.is_(None))
    for field, value in cols.items():
        col = getattr(DietLog, field)
        stmt = stmt.where(col.is_(None) if value is None else col == value)
    return db.execute(stmt.order_by(DietLog.id).limit(1)).scalar_one_or_none()


def _status_ctx(db: Session, revoke_error: str | None = None) -> dict:
    now = now_local()
    state = db.get(SyncState, SOURCE)
    rows = db.execute(
        select(ImportRaw)
        .where(ImportRaw.source == SOURCE)
        .order_by(ImportRaw.id.desc())
        .limit(LIST_LIMIT)
    ).scalars().all()
    habit_names = {h.id: h.name for h in db.execute(select(Habit)).scalars()}
    items = []
    for r in rows:
        raw = r.raw if isinstance(r.raw, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        seen = r.last_seen_at or r.imported_at
        revoked = isinstance(r.blob, dict) and r.blob.get("revoked_row_id") is not None
        revocable = False
        if not revoked and r.record_type in REVOCABLE_TYPES and r.parse_status == "parsed":
            if _resolve_row_id(db, r) is not None:
                revocable = True
            else:
                revoked = True  # 行已不在（MCP 端删的 / 页面上删的）
        items.append({
            "raw_id": r.id,
            "type_label": TYPE_LABELS.get(r.record_type, r.record_type),
            "summary": _summary(r.record_type, payload, habit_names),
            "rec_date": str(raw.get("date") or "—"),
            "status": r.parse_status,
            "error": (r.parse_error or "")[:200] if r.parse_status == "failed" else "",
            "ago": _ago(now, seen),
            "fresh": seen is not None and (now - seen).total_seconds() < FRESH_SECONDS,
            "revoked": revoked,
            "revocable": revocable,
        })
    return {
        "state": state,
        "state_ago": _ago(now, state.last_success_at) if state is not None else None,
        "items": items,
        "revoke_error": revoke_error,
    }


@router.get("/agent-log")
def agent_log_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "agent_log.html", _status_ctx(db))


@router.get("/fragments/agent-log/status")
def agent_log_status_fragment(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "fragments/agent_log_status.html", _status_ctx(db)
    )


@router.post("/agent-log/revoke")
async def agent_log_revoke(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        raw_id = int(str(form.get("raw_id")).strip())
    except (TypeError, ValueError):
        raw_id = 0
    r = db.get(ImportRaw, raw_id)
    error: str | None = None
    if (
        r is None or r.source != SOURCE
        or r.record_type not in REVOCABLE_TYPES or r.parse_status != "parsed"
    ):
        error = "这条记录不能撤销（仅已入库的饮食/训练记录可撤销）"
    elif not (isinstance(r.blob, dict) and r.blob.get("revoked_row_id") is not None):
        row_id = _resolve_row_id(db, r)
        if row_id is not None:  # 反查落空 = 行已删，幂等按已撤销显示
            status, body = delete_record(db, r.record_type, row_id)
            if status == 200:
                r.blob = {"revoked_row_id": row_id, "revoked_at": now_local().isoformat()}
                db.flush()
            else:
                error = str(body.get("error") or "撤销失败")
    return templates.TemplateResponse(
        request, "fragments/agent_log_status.html", _status_ctx(db, revoke_error=error)
    )
