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
from app.routers.ingest import _miscale_profile
from app.routers.metrics import SOURCE_LABELS
from app.services.miscale import compute_body_metrics
from app.timeutil import LOCAL_TZ, now_local, today_local

router = APIRouter(dependencies=[Depends(require_login)])

FRESH_SECONDS = 90  # last_seen_at 在此窗口内的测量标「新」

# 今日体成分网格：(字段, 中文名, 单位)
_BODY_FIELDS = [
    ("weight_kg", "体重", "kg"),
    ("body_fat_pct", "体脂率", "%"),
    ("muscle_mass_kg", "肌肉量", "kg"),
    ("body_water_kg", "体水分", "kg"),
    ("visceral_fat_level", "内脏脂肪", "级"),
    ("bmr_kcal", "基础代谢", "kcal"),
    ("skeletal_muscle_kg", "骨骼肌", "kg"),  # 手表 BIA 才有，秤算不出
]


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


def _fmt(v: object) -> str:
    s = f"{float(v):.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _status_ctx(db: Session) -> dict:
    now = now_local()
    state = db.get(SyncState, "miscale")
    sex, age, height_cm = _miscale_profile(db)
    rows = db.execute(
        select(ImportRaw)
        .where(ImportRaw.source == "miscale")
        .order_by(ImportRaw.id.desc())
        .limit(20)
    ).scalars().all()
    items = []
    profile_missing = False
    for r in rows:
        raw = r.raw if isinstance(r.raw, dict) else {}
        seen = r.last_seen_at or r.imported_at
        weight = raw.get("weight_kg")
        impedance = raw.get("impedance")
        # 该次测量的体成分：按当前档案现算展示（落库值在下方今日网格里）
        comp = None
        if isinstance(weight, (int, float)) and isinstance(impedance, (int, float)):
            values = compute_body_metrics(float(weight), float(impedance), sex, age, height_cm)
            if len(values) > 1:
                comp = (f"体脂 {values['body_fat_pct']}% · 肌肉 {values['muscle_mass_kg']} kg"
                        f" · 水分 {values['body_water_kg']} kg · 基代 {values['bmr_kcal']} kcal")
            else:
                profile_missing = True  # 有阻抗但档案不全，只记了体重
        items.append({
            "scale_ts": _scale_ts_label(raw.get("ts")),
            "weight": weight,
            "impedance": impedance,
            "comp": comp,
            "status": r.parse_status,
            "ago": _ago(now, seen),
            "fresh": seen is not None and (now - seen).total_seconds() < FRESH_SECONDS,
        })
    bm = db.execute(
        select(BodyMetrics).where(BodyMetrics.log_date == today_local())
    ).scalar_one_or_none()
    # 今日体成分网格：有值的字段 + 字段级来源徽标
    body_today = []
    if bm is not None:
        autofilled = bm.autofilled or {}
        for field, label, unit in _BODY_FIELDS:
            v = getattr(bm, field)
            if v is None:
                continue
            src = autofilled.get(field)
            body_today.append({
                "label": label,
                "value": _fmt(v),
                "unit": unit,
                "source": SOURCE_LABELS.get(src, src) if src else "手动",
            })
    return {
        "state": state,
        "state_ago": _ago(now, state.last_success_at) if state is not None else None,
        "items": items,
        "body_today": body_today,
        "profile_missing": profile_missing,
    }


@router.get("/scale")
def scale_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "scale.html", _status_ctx(db))


@router.get("/fragments/scale/status")
def scale_status_fragment(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "fragments/scale_status.html", _status_ctx(db)
    )
