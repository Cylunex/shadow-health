"""身体指标：upsert 录入 + 趋势图 + 最近 30 天历史（设计文档 §3.1、§3.2 展示侧、§四 /metrics）。

端点：
- GET  /metrics                    页面（表单 + 图表区 + 历史表格）
- POST /metrics                    按 log_date upsert，只更新非空字段并 mark_manual，返回表单片段
- GET  /fragments/metrics/form     表单片段（换日期时局部刷新）
- GET  /fragments/metrics/chart    Chart.js 图表片段（metric × days）
- GET  /fragments/metrics/history  最近 30 天历史表格片段（保存后被动刷新）
- GET  /fragments/metrics/quick    今日面板迷你表单（体重/睡眠/晨勃）

注：图表/quick 片段路径在 /fragments/ 下，无法共用 /metrics prefix，故本
router 不设 prefix，各路由写全路径。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import AppSetting, BodyMetrics, DailyActivity, SleepSession
from app.services.autofill import get_or_create_day, mark_manual
from app.timeutil import today_local

router = APIRouter(dependencies=[Depends(require_login)])

# 来源 → 徽标短名（历史表格灰色小徽标）
SOURCE_LABELS = {
    "samsung_zip": "三星",
    "health_connect": "HC",
    "samsung_direct": "手表",
    "keep_api": "Keep",
    "keep_file": "Keep",
    "miscale": "体脂秤",
}

# 表单字段定义：(字段名, 中文名, 类型, 下限, 上限)。上下限做轻量合法性校验，
# 同时防止超出 Numeric 精度导致 commit 阶段报错。
_FIELD_DEFS: list[tuple[str, str, str, float, float]] = [
    ("weight_kg", "体重", "decimal", 20, 500),
    ("body_fat_pct", "体脂率", "decimal", 1, 75),
    ("waist_cm", "腰围", "decimal", 30, 300),
    ("bp_systolic", "收缩压", "int", 50, 300),
    ("bp_diastolic", "舒张压", "int", 30, 200),
    ("resting_hr", "静息心率", "int", 20, 250),
    ("spo2_pct", "血氧", "decimal", 50, 100),
    ("sleep_hours", "睡眠时长", "decimal", 0, 24),
    ("sleep_quality", "睡眠质量", "int", 1, 5),
    ("energy_level", "精力", "int", 1, 5),
    ("muscle_mass_kg", "肌肉量", "decimal", 1, 300),
    ("skeletal_muscle_kg", "骨骼肌", "decimal", 1, 300),
    ("bmr_kcal", "基础代谢", "int", 300, 10000),
    ("body_water_kg", "体水分", "decimal", 1, 300),
    ("visceral_fat_level", "内脏脂肪等级", "int", 1, 60),
]
_NUM_FIELDS = [f[0] for f in _FIELD_DEFS]
# 无当日记录时预填「最近一次值」的慢变化字段
_PREFILL_FIELDS = ("weight_kg", "body_fat_pct", "waist_cm")

_CHART_DAYS = (7, 30, 90)
_CHART_METRICS: list[tuple[str, str]] = [
    ("weight", "体重"),
    ("body_fat", "体脂"),
    ("bp", "血压"),
    ("sleep", "睡眠"),
    ("sleep_stages", "睡眠分期"),
    ("steps", "步数"),
    ("waist", "腰围"),
]
_METRIC_KEYS = {m for m, _ in _CHART_METRICS}
# 各指标主色（emerald 主调，血压第二条线用 sky）
_COLORS = {
    "weight": "#34d399",
    "body_fat": "#fbbf24",
    "bp_systolic": "#34d399",
    "bp_diastolic": "#38bdf8",
    "sleep": "#a78bfa",
    "steps": "#34d399",
    "waist": "#38bdf8",
}
# 睡眠分期堆叠色：(字段, 图例, 颜色)
_STAGE_DEFS = [
    ("deep_min", "深睡", "#6366f1"),
    ("light_min", "浅睡", "#a78bfa"),
    ("rem_min", "REM", "#38bdf8"),
    ("awake_min", "清醒", "#64748b"),
]
# 有目标值可画参考线的指标：metric -> (app_settings key, 单位)
_TARGET_KEYS = {"weight": ("target_weight_kg", "kg"), "steps": ("target_steps", "步")}


# ---------- 通用小工具 ----------
def _fmt(value: Any) -> str:
    """Decimal/数值 → 去尾零的显示字符串；None → ''。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    s = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _parse_date(raw: Any) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return today_local()


def _parse_form(form: Any) -> tuple[dict[str, Any], list[str]]:
    """提取提交了非空值的字段；返回 (parsed, 格式错误的字段中文名)。"""
    values: dict[str, Any] = {}
    errors: list[str] = []
    for name, label, kind, lo, hi in _FIELD_DEFS:
        raw = form.get(name)
        if raw is None or str(raw).strip() == "":
            continue
        text = str(raw).strip()
        try:
            parsed: Any = int(text) if kind == "int" else Decimal(text)
        except (ValueError, InvalidOperation):
            errors.append(label)
            continue
        if not (Decimal(str(lo)) <= Decimal(str(parsed)) <= Decimal(str(hi))):
            errors.append(f"{label}（{lo}~{hi}）")
            continue
        values[name] = parsed
    raw_me = form.get("morning_erection")
    if raw_me is not None and str(raw_me).strip() != "":
        values["morning_erection"] = str(raw_me).strip() == "1"
    raw_notes = form.get("notes")
    if raw_notes is not None and str(raw_notes).strip() != "":
        values["notes"] = str(raw_notes).strip()
    return values, errors


def _same(current: Any, new: Any) -> bool:
    if current is None:
        return False
    if isinstance(new, Decimal) and not isinstance(current, Decimal):
        return Decimal(str(current)) == new
    return current == new


# ---------- 片段上下文 ----------
def _form_context(
    db: Session,
    log_date: date,
    saved: bool = False,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    row = db.execute(
        select(BodyMetrics).where(BodyMetrics.log_date == log_date)
    ).scalar_one_or_none()
    v: dict[str, str] = {}
    for name in _NUM_FIELDS:
        v[name] = _fmt(getattr(row, name)) if row is not None else ""
    # 当日无记录（或该字段为空）时，慢变化字段预填最近一次值
    for name in _PREFILL_FIELDS:
        if not v[name]:
            col = getattr(BodyMetrics, name)
            last = db.execute(
                select(col)
                .where(col.is_not(None), BodyMetrics.log_date != log_date)
                .order_by(BodyMetrics.log_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last is not None:
                v[name] = _fmt(last)
    return {
        "log_date": log_date.isoformat(),
        "v": v,
        "notes": (row.notes or "") if row else "",
        "chips": {
            "sleep_quality": row.sleep_quality if row else None,
            "energy_level": row.energy_level if row else None,
            "morning_erection": row.morning_erection if row else None,
        },
        "saved": saved,
        "errors": errors or [],
        "fmt": _fmt,
    }


def _quick_context(db: Session, saved: bool = False, errors: list[str] | None = None) -> dict[str, Any]:
    today = today_local()
    row = db.execute(
        select(BodyMetrics).where(BodyMetrics.log_date == today)
    ).scalar_one_or_none()
    return {
        "quick_log_date": today.isoformat(),
        "quick_weight": _fmt(row.weight_kg) if row else "",
        "quick_sleep": _fmt(row.sleep_hours) if row else "",
        "quick_me": row.morning_erection if row else None,
        "quick_saved": saved,
        "quick_errors": errors or [],
    }


def _history_context(db: Session) -> dict[str, Any]:
    today = today_local()
    rows = (
        db.execute(
            select(BodyMetrics)
            .where(BodyMetrics.log_date >= today - timedelta(days=29))
            .order_by(BodyMetrics.log_date.desc())
        )
        .scalars()
        .all()
    )
    return {"history_rows": rows, "source_labels": SOURCE_LABELS, "fmt": _fmt}


# ---------- 图表数据 ----------
def _line_dataset(
    label: str,
    by_day: dict[date, tuple[float, bool]],
    day_list: list[date],
    color: str,
) -> dict[str, Any]:
    """手动点实心圆、自动点空心三角；缺日 null（spanGaps:false 断线）。"""
    data: list[float | None] = []
    styles: list[str] = []
    bg: list[str] = []
    for d in day_list:
        if d in by_day:
            val, manual = by_day[d]
            data.append(val)
            styles.append("circle" if manual else "triangle")
            bg.append(color if manual else "rgba(0,0,0,0)")
        else:
            data.append(None)
            styles.append("circle")
            bg.append(color)
    return {"label": label, "data": data, "pointStyle": styles, "pointBg": bg, "color": color}


def _bm_field_map(db: Session, field: str, start: date, end: date) -> dict[date, tuple[float, bool]]:
    col = getattr(BodyMetrics, field)
    rows = db.execute(
        select(BodyMetrics.log_date, col, BodyMetrics.autofilled).where(
            BodyMetrics.log_date.between(start, end), col.is_not(None)
        )
    ).all()
    return {
        r[0]: (float(r[1]), field not in (r[2] or {}))
        for r in rows
    }


def _target_line(db: Session, metric: str) -> dict[str, Any] | None:
    """目标参考线：读 app_settings 目标值，未设定（缺行/JSON null）返回 None。"""
    entry = _TARGET_KEYS.get(metric)
    if entry is None:
        return None
    key, unit = entry
    value = db.execute(select(AppSetting.value).where(AppSetting.key == key)).scalar_one_or_none()
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return {"value": float(value), "label": f"目标 {_fmt(value)} {unit}"}


def _chart_context(db: Session, metric: str, days: int) -> dict[str, Any]:
    today = today_local()
    start = today - timedelta(days=days - 1)
    day_list = [start + timedelta(days=i) for i in range(days)]
    chart_type = "bar" if metric in ("steps", "sleep_stages") else "line"
    datasets: list[dict[str, Any]] = []

    if metric == "bp":
        datasets.append(
            _line_dataset("收缩压", _bm_field_map(db, "bp_systolic", start, today), day_list, _COLORS["bp_systolic"])
        )
        datasets.append(
            _line_dataset("舒张压", _bm_field_map(db, "bp_diastolic", start, today), day_list, _COLORS["bp_diastolic"])
        )
    elif metric == "sleep":
        # body_metrics.sleep_hours 优先；NULL 的日子回退当日 sleep_sessions 合计
        by_day = _bm_field_map(db, "sleep_hours", start, today)
        sess = db.execute(
            select(SleepSession.wake_date, func.sum(SleepSession.total_sleep_min))
            .where(
                SleepSession.wake_date.between(start, today),
                SleepSession.total_sleep_min.is_not(None),
            )
            .group_by(SleepSession.wake_date)
        ).all()
        for wake_date, total_min in sess:
            if wake_date not in by_day and total_min:
                by_day[wake_date] = (round(total_min / 60.0, 1), False)
        datasets.append(_line_dataset("睡眠时长 (h)", by_day, day_list, _COLORS["sleep"]))
    elif metric == "sleep_stages":
        # 各分期按 wake_date 求和（一日多段合并），单位小时；整段 NULL 的分期不画
        rows = db.execute(
            select(
                SleepSession.wake_date,
                *(func.sum(getattr(SleepSession, field)) for field, *_ in _STAGE_DEFS),
            )
            .where(SleepSession.wake_date.between(start, today))
            .group_by(SleepSession.wake_date)
        ).all()
        for idx, (_, label, color) in enumerate(_STAGE_DEFS):
            by_day = {
                r[0]: (round(float(r[idx + 1]) / 60.0, 1), False)
                for r in rows
                if r[idx + 1] is not None
            }
            datasets.append(_line_dataset(f"{label} (h)", by_day, day_list, color))
    elif metric == "steps":
        rows = db.execute(
            select(DailyActivity.log_date, DailyActivity.steps).where(
                DailyActivity.log_date.between(start, today), DailyActivity.steps.is_not(None)
            )
        ).all()
        by_day = {r[0]: (float(r[1]), False) for r in rows}
        datasets.append(_line_dataset("步数", by_day, day_list, _COLORS["steps"]))
    else:
        field, label = {
            "weight": ("weight_kg", "体重 (kg)"),
            "body_fat": ("body_fat_pct", "体脂率 (%)"),
            "waist": ("waist_cm", "腰围 (cm)"),
        }[metric]
        datasets.append(_line_dataset(label, _bm_field_map(db, field, start, today), day_list, _COLORS[metric]))

    target = _target_line(db, metric)
    payload = {
        "type": chart_type,
        "labels": [d.isoformat() for d in day_list],
        "datasets": datasets,
        "unit": "day" if days <= 30 else "week",
        "beginAtZero": metric in ("steps", "sleep", "sleep_stages"),
        "stacked": metric == "sleep_stages",
        "target": target,
    }
    has_data = any(v is not None for ds in datasets for v in ds["data"])
    return {
        "chart": {
            "metric": metric,
            "days": days,
            "payload_json": json.dumps(payload, ensure_ascii=False),
            "has_data": has_data,
            "target_label": target["label"] if target else None,
            "metric_options": _CHART_METRICS,
            "days_options": _CHART_DAYS,
        }
    }


# ---------- 路由 ----------
@router.get("/metrics")
def metrics_page(request: Request, db: Session = Depends(get_db)):
    ctx: dict[str, Any] = {}
    ctx.update(_form_context(db, today_local()))
    ctx.update(_chart_context(db, "weight", 30))
    ctx.update(_history_context(db))
    return templates.TemplateResponse(request, "metrics.html", ctx)


@router.post("/metrics")
async def metrics_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    log_date = _parse_date(form.get("log_date"))
    fragment = str(form.get("fragment") or "form")
    values, errors = _parse_form(form)

    saved = False
    if values and not errors:
        row = get_or_create_day(db, log_date)
        manual_fields: list[str] = []
        for field, value in values.items():
            # 自动回填字段原样提交（未改动）时不视为手动保存，保留来源徽标
            if field in (row.autofilled or {}) and _same(getattr(row, field), value):
                continue
            setattr(row, field, value)
            manual_fields.append(field)
        if manual_fields:
            mark_manual(row, manual_fields)
        db.flush()
        saved = True
    elif not values and not errors:
        errors = ["没有可保存的内容"]

    if fragment == "quick":
        resp = templates.TemplateResponse(
            request, "fragments/metrics_quick.html", _quick_context(db, saved=saved, errors=errors)
        )
    else:
        resp = templates.TemplateResponse(
            request, "fragments/metrics_form.html", _form_context(db, log_date, saved=saved, errors=errors)
        )
    if saved:
        resp.headers["HX-Trigger"] = "metrics-changed"
    return resp


@router.get("/fragments/metrics/form")
def metrics_form_fragment(request: Request, log_date: str = "", db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "fragments/metrics_form.html", _form_context(db, _parse_date(log_date))
    )


@router.get("/fragments/metrics/chart")
def metrics_chart_fragment(
    request: Request, metric: str = "weight", days: int = 30, db: Session = Depends(get_db)
):
    if metric not in _METRIC_KEYS:
        metric = "weight"
    if days not in _CHART_DAYS:
        days = 30
    return templates.TemplateResponse(
        request, "fragments/metrics_chart.html", _chart_context(db, metric, days)
    )


@router.get("/fragments/metrics/history")
def metrics_history_fragment(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "fragments/metrics_history.html", _history_context(db)
    )


@router.get("/fragments/metrics/quick")
def metrics_quick_fragment(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "fragments/metrics_quick.html", _quick_context(db)
    )
