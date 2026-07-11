"""秤接收状态页：直观确认体脂秤测量有没有到服务器。

- GET /scale                    页面（通道健康度 + 今日体重 + 最近测量流水）
- GET /fragments/scale/status   状态片段（页面 hx-trigger="every 3s" 轮询，
                                上秤后几秒内就能看到新行带「新」徽标蹦出来）

数据全部来自现成落库：import_raw(source='miscale') 留档流水 +
sync_state('miscale') 通道状态 + body_metrics 今日行。壳内（ShellBridge
存在时）页面另显示「开秤监听 3 分钟」按钮，一页完成 开监听→上秤→看结果。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import BodyMetrics, ImportRaw, SyncState
from app.routers.metrics import SOURCE_LABELS
from app.timeutil import LOCAL_TZ, now_local, today_local

router = APIRouter(dependencies=[Depends(require_login)])

FRESH_SECONDS = 90  # last_seen_at 在此窗口内的测量标「新」


def _ago(now: datetime, ts: datetime | None) -> str:
    if ts is None:
        return "—"
    secs = int((now - ts).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs} 秒前"
    if secs < 3600:
        return f"{secs // 60} 分钟前"
    if secs < 86400:
        return f"{secs // 3600} 小时前"
    return f"{ts.astimezone(LOCAL_TZ):%m-%d %H:%M}"


def _scale_ts_label(raw_ts: object) -> str:
    """import_raw.raw.ts（秤 RTC 的 ISO 串）→ 'MM-dd HH:mm'；解析不了原样截断。"""
    try:
        return f"{datetime.fromisoformat(str(raw_ts)):%m-%d %H:%M}"
    except (TypeError, ValueError):
        return str(raw_ts)[:16]


def _status_ctx(db: Session) -> dict:
    now = now_local()
    state = db.get(SyncState, "miscale")
    rows = db.execute(
        select(ImportRaw)
        .where(ImportRaw.source == "miscale")
        .order_by(ImportRaw.id.desc())
        .limit(20)
    ).scalars().all()
    items = []
    for r in rows:
        raw = r.raw if isinstance(r.raw, dict) else {}
        seen = r.last_seen_at or r.imported_at
        items.append({
            "scale_ts": _scale_ts_label(raw.get("ts")),
            "weight": raw.get("weight_kg"),
            "impedance": raw.get("impedance"),
            "status": r.parse_status,
            "ago": _ago(now, seen),
            "fresh": seen is not None and (now - seen).total_seconds() < FRESH_SECONDS,
        })
    bm = db.execute(
        select(BodyMetrics).where(BodyMetrics.log_date == today_local())
    ).scalar_one_or_none()
    weight_source = None
    if bm is not None and bm.weight_kg is not None:
        src = (bm.autofilled or {}).get("weight_kg")
        weight_source = SOURCE_LABELS.get(src, src) if src else "手动"
    return {
        "state": state,
        "state_ago": _ago(now, state.last_success_at) if state is not None else None,
        "items": items,
        "bm": bm,
        "weight_source": weight_source,
    }


@router.get("/scale")
def scale_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "scale.html", _status_ctx(db))


@router.get("/fragments/scale/status")
def scale_status_fragment(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "fragments/scale_status.html", _status_ctx(db)
    )
