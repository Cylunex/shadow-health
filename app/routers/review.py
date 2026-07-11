"""周报：列表（惰性生成）+ 快照展示 + 手写复盘（设计文档 §3.5 weekly_reviews、§四 /review 行）。

端点：
- GET /review               重定向到报告中心周报 tab（列表已并入 /report?t=weekly，report.py）
- GET /review/{week_start}  快照展示（只读）+ summary 手写复盘表单；缺失的历史完整周按需生成
- PUT /review/{week_start}  保存 summary，返回复盘表单片段

metrics_snapshot 聚合口径：
- 体重变化 = 周内首末两次非空体重差（不足 2 次记录则为 null）
- 日均热量/蛋白 = 周内合计 / 有饮食记录的天数
- 训练次数与总时长 = 全 source 合并（manual + keep + samsung_zip + health_connect）
- 习惯打卡率 = active daily 习惯的达标习惯日 / (习惯数 × 7)
- 周均步数 = daily_activity 有数据天的均值
- 周有氧分钟 = session_type 命中有氧关键词的 duration_min 合计，vs target_weekly_cardio_min
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import redirect, require_login, templates
from app.models import (
    AppSetting,
    BodyMetrics,
    DailyActivity,
    DietLog,
    Habit,
    HabitLog,
    WeeklyReview,
    WorkoutLog,
)
from app.timeutil import today_local

router = APIRouter(prefix="/review", dependencies=[Depends(require_login)])

# 有氧 session_type 关键词（手动中文 + 外部源英文小写），统计周有氧分钟用
_CARDIO_KEYWORDS = (
    "有氧", "hiit", "liss", "cardio",
    "walk", "run", "hik", "cycl", "bik", "swim", "row", "elliptical", "climb",
    "步行", "快走", "慢跑", "跑步", "徒步", "骑行", "游泳", "爬楼", "跳绳",
)


def _week_start_of(d: date) -> date:
    """所在周的周一（isoweekday，与 weekly_reviews 的 CHECK 一致）。"""
    return d - timedelta(days=d.isoweekday() - 1)


def _earliest_data_date(db: Session) -> date | None:
    """全库最早的数据日期（周/月报回填与详情翻页导航的下界）。"""
    mins = [
        db.execute(select(func.min(col))).scalar_one()
        for col in (
            BodyMetrics.log_date, DailyActivity.log_date, DietLog.log_date,
            WorkoutLog.log_date, HabitLog.log_date,
        )
    ]
    vals = [m for m in mins if m is not None]
    return min(vals) if vals else None


def _is_cardio(session_type: str | None) -> bool:
    if not session_type:
        return False
    s = session_type.lower()
    return any(k in s for k in _CARDIO_KEYWORDS)


def _fmt_f(v: Any) -> str:
    """浮点显示：去尾零；None → '—'。"""
    if v is None:
        return "—"
    s = f"{float(v):.2f}".rstrip("0").rstrip(".")
    return s or "0"


# ---------- 快照聚合 ----------
def _aggregate_week(db: Session, week_start: date) -> dict[str, Any]:
    week_end = week_start + timedelta(days=6)

    # 体重变化：周内首末两次非空体重差
    weights = db.execute(
        select(BodyMetrics.log_date, BodyMetrics.weight_kg)
        .where(
            BodyMetrics.log_date.between(week_start, week_end),
            BodyMetrics.weight_kg.is_not(None),
        )
        .order_by(BodyMetrics.log_date)
    ).all()
    weight_start = float(weights[0][1]) if weights else None
    weight_end = float(weights[-1][1]) if weights else None
    weight_change = round(weight_end - weight_start, 2) if len(weights) >= 2 else None

    # 饮食：日均热量/蛋白（按有记录的天数平均）
    diet_days, total_kcal, total_protein = db.execute(
        select(
            func.count(func.distinct(DietLog.log_date)),
            func.sum(DietLog.kcal),
            func.sum(DietLog.protein_g),
        ).where(DietLog.log_date.between(week_start, week_end))
    ).one()
    avg_kcal = round(float(total_kcal) / diet_days) if diet_days and total_kcal is not None else None
    avg_protein = (
        round(float(total_protein) / diet_days) if diet_days and total_protein is not None else None
    )

    # 训练：次数与总时长（全 source 合并）+ 有氧分钟 + sRPE 负荷
    wlogs = db.execute(
        select(WorkoutLog.session_type, WorkoutLog.duration_min, WorkoutLog.rpe).where(
            WorkoutLog.log_date.between(week_start, week_end)
        )
    ).all()
    workout_count = len(wlogs)
    workout_min = sum(r[1] or 0 for r in wlogs)
    cardio_min = sum(r[1] or 0 for r in wlogs if _is_cardio(r[0]))
    training_load = sum((r[2] or 0) * (r[1] or 0) for r in wlogs)
    unrated_min = sum(r[1] or 0 for r in wlogs if not r[2])

    # 围度变化：周内首末非空差值（有数据的部位才进快照）
    girth_changes: dict[str, float | None] = {}
    for field in ("waist_cm", "chest_cm", "arm_cm", "thigh_cm", "hip_cm"):
        col = getattr(BodyMetrics, field)
        pts = db.execute(
            select(col)
            .where(BodyMetrics.log_date.between(week_start, week_end), col.is_not(None))
            .order_by(BodyMetrics.log_date)
        ).all()
        girth_changes[field.replace("_cm", "_change")] = (
            round(float(pts[-1][0]) - float(pts[0][0]), 1) if len(pts) >= 2 else None
        )

    # 习惯打卡率：active daily 习惯的达标习惯日 / (习惯数×7)
    habits = db.execute(
        select(Habit.id, Habit.target_per_period).where(
            Habit.active.is_(True), Habit.period == "daily"
        )
    ).all()
    habit_rate = None
    if habits:
        targets = {hid: t or 1 for hid, t in habits}
        logs = db.execute(
            select(HabitLog.habit_id, HabitLog.done_count).where(
                HabitLog.habit_id.in_(list(targets)),
                HabitLog.log_date.between(week_start, week_end),
            )
        ).all()
        done_days = sum(1 for hid, c in logs if c >= targets[hid])
        habit_rate = round(done_days * 100 / (len(targets) * 7))

    # 步数：周均（按有数据的天数平均）
    step_days, steps_sum = db.execute(
        select(func.count(), func.sum(DailyActivity.steps)).where(
            DailyActivity.log_date.between(week_start, week_end),
            DailyActivity.steps.is_not(None),
        )
    ).one()
    avg_steps = round(float(steps_sum) / step_days) if step_days and steps_sum is not None else None

    # 周有氧目标（app_settings，值可能为 JSON null）
    setting = db.get(AppSetting, "target_weekly_cardio_min")
    try:
        target_cardio = int(setting.value) if setting is not None and setting.value is not None else None
    except (TypeError, ValueError):
        target_cardio = None

    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "weight_start": weight_start,
        "weight_end": weight_end,
        "weight_change": weight_change,
        "weight_days": len(weights),
        "avg_kcal": avg_kcal,
        "avg_protein_g": avg_protein,
        "diet_days": diet_days,
        "workout_count": workout_count,
        "workout_min": workout_min,
        "cardio_min": cardio_min,
        "training_load": training_load,
        "unrated_min": unrated_min,
        **girth_changes,
        "target_weekly_cardio_min": target_cardio,
        "habit_rate": habit_rate,
        "habit_count": len(habits),
        "avg_steps": avg_steps,
        "step_days": step_days,
    }


def _ensure_last_week(db: Session) -> None:
    """惰性生成：上一完整周（周一~周日）若无记录则现场聚合插入。"""
    last_week = _week_start_of(today_local()) - timedelta(days=7)
    exists = db.execute(
        select(WeeklyReview.id).where(WeeklyReview.week_start == last_week)
    ).scalar_one_or_none()
    if exists is None:
        db.execute(
            pg_insert(WeeklyReview)
            .values(week_start=last_week, metrics_snapshot=_aggregate_week(db, last_week))
            .on_conflict_do_nothing(index_elements=["week_start"])
        )


def _parse_week_start(week_start: str) -> date:
    try:
        d = date.fromisoformat(week_start.strip())
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="周起始日期格式不正确")
    if d.isoweekday() != 1:
        raise HTTPException(status_code=404, detail="week_start 必须是周一")
    return d


def _get_or_create_review(db: Session, ws: date) -> WeeklyReview:
    """已有则取；缺失但该周已完整（周日已过）则现场聚合生成；未完整的周 404。"""
    row = db.execute(
        select(WeeklyReview).where(WeeklyReview.week_start == ws)
    ).scalar_one_or_none()
    if row is None:
        if ws + timedelta(days=7) > today_local():
            raise HTTPException(status_code=404, detail="该周尚未结束，暂无周报")
        row = WeeklyReview(week_start=ws, metrics_snapshot=_aggregate_week(db, ws))
        db.add(row)
        db.flush()
    return row


# ---------- 展示上下文 ----------
def _range_label(ws: date) -> str:
    we = ws + timedelta(days=6)
    return f"{ws:%Y-%m-%d} ~ {we:%m-%d}"


def _list_row(r: WeeklyReview) -> dict[str, Any]:
    snap = r.metrics_snapshot or {}
    chips: list[str] = []
    if snap.get("weight_change") is not None:
        chips.append(f"体重 {snap['weight_change']:+.1f} kg")
    if snap.get("avg_kcal") is not None:
        chips.append(f"日均 {snap['avg_kcal']} kcal")
    if snap.get("workout_count"):
        chips.append(f"训练 {snap['workout_count']} 次")
    if snap.get("habit_rate") is not None:
        chips.append(f"打卡 {snap['habit_rate']}%")
    if snap.get("avg_steps") is not None:
        chips.append(f"步数 {snap['avg_steps']:,}")
    return {
        "week_start": r.week_start.isoformat(),
        "label": _range_label(r.week_start),
        "has_summary": bool((r.summary or "").strip()),
        "chips": chips,
    }


def _snapshot_cards(snap: dict[str, Any]) -> list[dict[str, Any]]:
    g = snap.get
    cards: list[dict[str, Any]] = []

    wc = g("weight_change")
    if wc is not None:
        cards.append({
            "label": "体重变化",
            "value": f"{wc:+.1f} kg",
            "sub": f"{_fmt_f(g('weight_start'))} → {_fmt_f(g('weight_end'))} kg",
        })
    elif g("weight_end") is not None:
        cards.append({"label": "体重变化", "value": f"{_fmt_f(g('weight_end'))} kg", "sub": "本周仅 1 次记录"})
    else:
        cards.append({"label": "体重变化", "value": "—", "sub": "本周无体重记录"})

    diet_days = g("diet_days") or 0
    cards.append({
        "label": "日均热量",
        "value": f"{g('avg_kcal')} kcal" if g("avg_kcal") is not None else "—",
        "sub": f"记录 {diet_days} 天",
    })
    cards.append({
        "label": "日均蛋白质",
        "value": f"{g('avg_protein_g')} g" if g("avg_protein_g") is not None else "—",
        "sub": f"记录 {diet_days} 天",
    })
    cards.append({
        "label": "训练",
        "value": f"{g('workout_count') or 0} 次",
        "sub": f"合计 {g('workout_min') or 0} 分钟（全部来源）",
    })
    if g("training_load"):
        unrated = g("unrated_min") or 0
        cards.append({
            "label": "训练负荷 (sRPE)",
            "value": f"{g('training_load')}",
            "sub": f"另有 {unrated} 分钟未评级" if unrated else "RPE×分钟",
        })
    girth_labels = (
        ("waist_change", "腰围"), ("chest_change", "胸围"), ("hip_change", "臀围"),
        ("thigh_change", "大腿围"), ("arm_change", "臂围"),
    )
    girth_parts = [
        f"{label} {g(key):+.1f}" for key, label in girth_labels if g(key) is not None
    ]
    if girth_parts:
        cards.append({
            "label": "围度变化 (cm)",
            "value": " · ".join(girth_parts[:2]),
            "sub": " · ".join(girth_parts[2:]) or "周内首末差",
        })

    cardio = g("cardio_min") or 0
    target = g("target_weekly_cardio_min")
    cards.append({
        "label": "周有氧",
        "value": f"{cardio} / {target} 分钟" if target else f"{cardio} 分钟",
        "sub": "已达标 ✓" if target and cardio >= target else ("目标 %s 分钟" % target if target else "未设周有氧目标"),
        "tone": "text-emerald-400" if target and cardio >= target else None,
    })
    cards.append({
        "label": "习惯打卡率",
        "value": f"{g('habit_rate')}%" if g("habit_rate") is not None else "—",
        "sub": f"{g('habit_count') or 0} 项 daily 习惯 × 7 天",
    })
    cards.append({
        "label": "周均步数",
        "value": f"{g('avg_steps'):,}" if g("avg_steps") is not None else "—",
        "sub": f"{g('step_days') or 0} 天有数据",
    })
    return cards


# ---------- 路由 ----------
@router.get("")
def review_list(request: Request):
    """列表已并入报告中心（/report 周报 tab），旧入口重定向保关系。"""
    return redirect(request, "/report?t=weekly")


@router.get("/{week_start}")
def review_detail(week_start: str, request: Request, db: Session = Depends(get_db)):
    ws = _parse_week_start(week_start)
    row = _get_or_create_review(db, ws)
    # 上一周/下一周导航：下界 = 最早数据所在周（更早的周按需生成），上界 = 上一完整周
    earliest = _earliest_data_date(db)
    floor = _week_start_of(earliest) if earliest is not None else ws
    prev_ws = ws - timedelta(days=7)
    next_ws = ws + timedelta(days=7)
    return templates.TemplateResponse(
        request,
        "review_detail.html",
        {
            "week_start": ws.isoformat(),
            "range_label": _range_label(ws),
            "prev_week": prev_ws.isoformat() if prev_ws >= floor else None,
            "next_week": next_ws.isoformat()
            if next_ws + timedelta(days=7) <= today_local() else None,
            "cards": _snapshot_cards(row.metrics_snapshot or {}),
            "summary": row.summary or "",
            "saved": False,
        },
    )


@router.put("/{week_start}")
async def review_save(week_start: str, request: Request, db: Session = Depends(get_db)):
    ws = _parse_week_start(week_start)
    row = _get_or_create_review(db, ws)
    form = await request.form()
    summary = str(form.get("summary") or "").strip()
    row.summary = summary or None
    db.flush()
    return templates.TemplateResponse(
        request,
        "fragments/review_summary_form.html",
        {"week_start": ws.isoformat(), "summary": row.summary or "", "saved": True},
    )
