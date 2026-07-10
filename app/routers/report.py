"""报告中心：日报 + 周报入口 + 月报（交接文档 2026-07-10「日报 + 月报」任务）。

端点契约：
- GET /report?t=daily|weekly|monthly   报告中心：日/周/月三 tab（HTMX 片段切换）
- GET /fragments/report/tabs?t=        tab 片段（chips + 当前 tab 列表，整体 outerHTML 替换）
- GET /report/daily?d=YYYY-MM-DD       当日日报（默认今天；饮食页同款翻天导航，不允许未来）
- GET /report/monthly/{month_start}    月报详情：快照展示（只读）+ 手写复盘（照周报模式）
- PUT /report/monthly/{month_start}    保存月报复盘，返回复盘表单片段

周报详情沿用 /review/{week_start}（review.py），本模块只提供列表入口。
日报无表——纯聚合：三环/饮食四项/训练明细+负荷/打卡清单/体重体成分/睡眠
（睡眠按夜跨源去重，必须走 services/sleep）。

月报 metrics_snapshot 聚合口径：
- 体重/体脂/围度变化 = 月内首末两次非空差（不足 2 次为 null）
- 训练次数/分钟/sRPE 负荷/有氧分钟 = 全 source 合并
- 有氧达标周数 = 周一落在本月内的 ISO 周中，整周（允许跨月）有氧分钟 >= 周目标的周数
- 打卡率 = active daily 习惯的达标习惯日 / (习惯数 × 当月天数)
- 饮食 = 记录天数 + 日均四项（合计 / 有记录天数）
- 步数 = 有数据天的日均 + 达标天数（target_steps，缺省 8000）
- 饮食连击最高 = 月内最长连续记录天数
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import (
    AppSetting,
    BodyMetrics,
    DailyActivity,
    DietLog,
    Habit,
    HabitLog,
    MonthlyReview,
    WeeklyReview,
    WorkoutLog,
)
from app.routers.diet import _summary_ctx as _diet_summary_ctx
from app.routers.review import _ensure_last_week, _fmt_f, _is_cardio, _list_row
from app.routers.today import WEEKDAY_CN, _rings, _target_steps
from app.routers.workout import SOURCE_LABELS
from app.services import sleep
from app.timeutil import today_local

router = APIRouter(dependencies=[Depends(require_login)])

TABS = (("daily", "日报"), ("weekly", "周报"), ("monthly", "月报"))
DAILY_LIST_DAYS = 14  # 日报 tab 列出最近两周

# 日报「身体」卡字段：(字段, 标签, 单位)
_BODY_FIELDS = (
    ("weight_kg", "体重", "kg"),
    ("body_fat_pct", "体脂率", "%"),
    ("skeletal_muscle_kg", "骨骼肌", "kg"),
    ("muscle_mass_kg", "肌肉量", "kg"),
    ("visceral_fat_level", "内脏脂肪", "级"),
    ("waist_cm", "腰围", "cm"),
    ("resting_hr", "静息心率", "bpm"),
    ("spo2_pct", "血氧", "%"),
    ("energy_level", "精力", "/5"),
)


# ---------- 月份纯函数（单测锁口径） ----------
def _month_start_of(d: date) -> date:
    return d.replace(day=1)


def _next_month_start(ms: date) -> date:
    return (ms + timedelta(days=32)).replace(day=1)


def _month_end(ms: date) -> date:
    return _next_month_start(ms) - timedelta(days=1)


def _prev_month_start(ms: date) -> date:
    return _month_start_of(ms - timedelta(days=1))


def _month_mondays(ms: date) -> list[date]:
    """周一落在本月内的 ISO 周（每周只归属其周一所在月，跨月不重复计）。"""
    first = ms + timedelta(days=(8 - ms.isoweekday()) % 7)
    end = _next_month_start(ms)
    return [first + timedelta(days=7 * i) for i in range((end - first).days // 7 + 1)
            if first + timedelta(days=7 * i) < end]


def _max_run(days: set[date], start: date, end: date) -> int:
    """[start, end] 区间内出现在 days 中的最长连续天数。"""
    best = cur = 0
    d = start
    while d <= end:
        cur = cur + 1 if d in days else 0
        best = max(best, cur)
        d += timedelta(days=1)
    return best


def _fmt_sleep_min(m: int) -> str:
    return f"{m // 60} 小时 {m % 60} 分" if m >= 60 else f"{m} 分"


def _month_label(ms: date) -> str:
    return f"{ms.year} 年 {ms.month} 月"


# ---------- 日报聚合 ----------
def _habit_checklist(db: Session, d: date) -> tuple[list[dict[str, Any]], int]:
    """active daily 习惯当日达标清单（与打卡页口径一致：done_count >= target）。"""
    habits = db.execute(
        select(Habit).where(Habit.active.is_(True), Habit.period == "daily")
        .order_by(Habit.sort, Habit.id)
    ).scalars().all()
    counts = {
        hid: c
        for hid, c in db.execute(
            select(HabitLog.habit_id, HabitLog.done_count).where(HabitLog.log_date == d)
        )
    }
    items = [
        {
            "name": h.name,
            "target": h.target_per_period or 1,
            "count": counts.get(h.id, 0),
            "done": counts.get(h.id, 0) >= (h.target_per_period or 1),
        }
        for h in habits
    ]
    return items, sum(1 for i in items if i["done"])


def _daily_ctx(db: Session, d: date, today: date) -> dict[str, Any]:
    activity = db.get(DailyActivity, d)
    steps = activity.steps if activity is not None else None
    target_steps = _target_steps(db)

    # 训练明细 + sRPE 负荷（全 source）
    wlogs = db.execute(
        select(WorkoutLog).where(WorkoutLog.log_date == d)
        .order_by(WorkoutLog.started_at, WorkoutLog.id)
    ).scalars().all()
    workout_min = sum(w.duration_min or 0 for w in wlogs)
    training_load = sum((w.rpe or 0) * (w.duration_min or 0) for w in wlogs)

    habit_items, habit_done = _habit_checklist(db, d)

    # 身体指标：当日非空字段 + 血压成对特判
    bm = db.execute(
        select(BodyMetrics).where(BodyMetrics.log_date == d)
    ).scalar_one_or_none()
    body_items: list[dict[str, str]] = []
    if bm is not None:
        for field, label, unit in _BODY_FIELDS:
            v = getattr(bm, field)
            if v is not None:
                body_items.append({"label": label, "value": f"{_fmt_f(v)} {unit}"})
        if bm.bp_systolic is not None and bm.bp_diastolic is not None:
            body_items.append({"label": "血压", "value": f"{bm.bp_systolic}/{bm.bp_diastolic} mmHg"})

    # 睡眠：按夜跨源去重（services/sleep）；无会话回退 body_metrics.sleep_hours（手记/回填）
    sessions = sleep.sessions_by_date(db, d, d).get(d, [])
    sleep_min = sum(s.total_sleep_min or 0 for s in sessions)
    sleep_stages = ""
    if sessions:
        parts = []
        for attr, label in (("deep_min", "深睡"), ("light_min", "浅睡"), ("rem_min", "REM")):
            v = sum(getattr(s, attr) or 0 for s in sessions)
            if v:
                parts.append(f"{label} {_fmt_sleep_min(v)}")
        sleep_stages = " · ".join(parts)
    sleep_hours_fallback = bm.sleep_hours if bm is not None and not sessions else None

    ctx: dict[str, Any] = {
        "d": d,
        "prev_d": d - timedelta(days=1),
        "next_d": d + timedelta(days=1),
        "is_today": d == today,
        "weekday_cn": WEEKDAY_CN[d.isoweekday() - 1],
        "rings": _rings(db, d, steps, target_steps),
        "activity": activity,
        "wlogs": wlogs,
        "workout_min": workout_min,
        "training_load": training_load,
        "source_labels": SOURCE_LABELS,
        "habit_items": habit_items,
        "habit_done": habit_done,
        "body_items": body_items,
        "sleep_min": sleep_min,
        "sleep_label": _fmt_sleep_min(sleep_min) if sleep_min else None,
        "sleep_stages": sleep_stages,
        "sleep_hours_fallback": sleep_hours_fallback,
    }
    ctx.update(_diet_summary_ctx(db, d))  # 四项 vs 目标 + 连击 + fmt
    return ctx


def _daily_rows(db: Session, n: int = DAILY_LIST_DAYS) -> list[dict[str, Any]]:
    """日报 tab：最近 n 天，每天一行 + 摘要 chips（批量查询，不逐日扫）。"""
    today = today_local()
    start = today - timedelta(days=n - 1)
    kcal_by_day = {
        d: k
        for d, k in db.execute(
            select(DietLog.log_date, func.sum(DietLog.kcal))
            .where(DietLog.log_date.between(start, today))
            .group_by(DietLog.log_date)
        )
    }
    workout_by_day = {
        d: (c, int(m))
        for d, c, m in db.execute(
            select(
                WorkoutLog.log_date,
                func.count(),
                func.coalesce(func.sum(WorkoutLog.duration_min), 0),
            )
            .where(WorkoutLog.log_date.between(start, today))
            .group_by(WorkoutLog.log_date)
        )
    }
    steps_by_day = {
        d: s
        for d, s in db.execute(
            select(DailyActivity.log_date, DailyActivity.steps).where(
                DailyActivity.log_date.between(start, today),
                DailyActivity.steps.is_not(None),
            )
        )
    }
    targets = {
        hid: t or 1
        for hid, t in db.execute(
            select(Habit.id, Habit.target_per_period).where(
                Habit.active.is_(True), Habit.period == "daily"
            )
        )
    }
    done_by_day: dict[date, int] = defaultdict(int)
    if targets:
        for hid, d, c in db.execute(
            select(HabitLog.habit_id, HabitLog.log_date, HabitLog.done_count).where(
                HabitLog.log_date.between(start, today),
                HabitLog.habit_id.in_(list(targets)),
            )
        ):
            if c >= targets[hid]:
                done_by_day[d] += 1

    rows = []
    for i in range(n):
        d = today - timedelta(days=i)
        chips: list[str] = []
        if kcal_by_day.get(d) is not None:
            chips.append(f"{round(float(kcal_by_day[d]))} kcal")
        if d in workout_by_day:
            chips.append(f"训练 {workout_by_day[d][0]} 次 · {workout_by_day[d][1]} 分")
        if steps_by_day.get(d) is not None:
            chips.append(f"步数 {steps_by_day[d]:,}")
        if targets:
            chips.append(f"打卡 {done_by_day.get(d, 0)}/{len(targets)}")
        rows.append({
            "d": d.isoformat(),
            "label": f"{d:%m-%d}",
            "weekday": WEEKDAY_CN[d.isoweekday() - 1],
            "is_today": d == today,
            "chips": chips,
        })
    return rows


# ---------- 月报快照聚合 ----------
def _first_last_change(
    db: Session, col, ms: date, me: date, nd: int = 2
) -> tuple[float | None, float | None, float | None, int]:
    """月内首末非空值：(首值, 末值, 差, 记录数)；不足 2 次差为 None。"""
    pts = db.execute(
        select(col)
        .where(BodyMetrics.log_date.between(ms, me), col.is_not(None))
        .order_by(BodyMetrics.log_date)
    ).all()
    if not pts:
        return None, None, None, 0
    first, last = float(pts[0][0]), float(pts[-1][0])
    change = round(last - first, nd) if len(pts) >= 2 else None
    return first, last, change, len(pts)


def _aggregate_month(db: Session, ms: date) -> dict[str, Any]:
    me = _month_end(ms)
    days_in_month = (me - ms).days + 1

    weight_start, weight_end, weight_change, weight_days = _first_last_change(
        db, BodyMetrics.weight_kg, ms, me
    )
    fat_start, fat_end, fat_change, _ = _first_last_change(
        db, BodyMetrics.body_fat_pct, ms, me, nd=1
    )
    girth_changes: dict[str, float | None] = {}
    for field in ("waist_cm", "chest_cm", "arm_cm", "thigh_cm", "hip_cm"):
        _, _, change, _ = _first_last_change(db, getattr(BodyMetrics, field), ms, me, nd=1)
        girth_changes[field.replace("_cm", "_change")] = change

    # 训练：月内合计 + 有氧达标周（整周口径，取数窗口向后多罩 6 天）
    mondays = _month_mondays(ms)
    fetch_end = max(me, mondays[-1] + timedelta(days=6)) if mondays else me
    wl = db.execute(
        select(
            WorkoutLog.log_date, WorkoutLog.session_type,
            WorkoutLog.duration_min, WorkoutLog.rpe,
        ).where(WorkoutLog.log_date.between(ms, fetch_end))
    ).all()
    in_month = [r for r in wl if r[0] <= me]
    workout_count = len(in_month)
    workout_min = sum(r[2] or 0 for r in in_month)
    cardio_min = sum(r[2] or 0 for r in in_month if _is_cardio(r[1]))
    training_load = sum((r[3] or 0) * (r[2] or 0) for r in in_month)
    unrated_min = sum(r[2] or 0 for r in in_month if not r[3])

    setting = db.get(AppSetting, "target_weekly_cardio_min")
    try:
        target_cardio = int(setting.value) if setting is not None and setting.value is not None else None
    except (TypeError, ValueError):
        target_cardio = None
    cardio_weeks_ok = None
    if target_cardio:
        cardio_weeks_ok = sum(
            1
            for w in mondays
            if sum(
                r[2] or 0
                for r in wl
                if w <= r[0] <= w + timedelta(days=6) and _is_cardio(r[1])
            )
            >= target_cardio
        )

    # 打卡率：达标习惯日 / (习惯数 × 当月天数)
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
                HabitLog.log_date.between(ms, me),
            )
        ).all()
        done_days = sum(1 for hid, c in logs if c >= targets[hid])
        habit_rate = round(done_days * 100 / (len(targets) * days_in_month))

    # 饮食：记录天数 + 日均四项 + 月内最长连击
    diet_days, s_kcal, s_protein, s_fat, s_carb = db.execute(
        select(
            func.count(func.distinct(DietLog.log_date)),
            func.sum(DietLog.kcal),
            func.sum(DietLog.protein_g),
            func.sum(DietLog.fat_g),
            func.sum(DietLog.carb_g),
        ).where(DietLog.log_date.between(ms, me))
    ).one()

    def _avg(total: Any) -> int | None:
        return round(float(total) / diet_days) if diet_days and total is not None else None

    diet_day_set = {
        r[0]
        for r in db.execute(
            select(func.distinct(DietLog.log_date)).where(DietLog.log_date.between(ms, me))
        )
    }
    diet_streak_max = _max_run(diet_day_set, ms, me)

    # 步数：日均 + 达标天数
    target_steps = _target_steps(db)
    step_days, steps_sum = db.execute(
        select(func.count(), func.sum(DailyActivity.steps)).where(
            DailyActivity.log_date.between(ms, me), DailyActivity.steps.is_not(None)
        )
    ).one()
    avg_steps = round(float(steps_sum) / step_days) if step_days and steps_sum is not None else None
    steps_ok_days = db.execute(
        select(func.count()).where(
            DailyActivity.log_date.between(ms, me), DailyActivity.steps >= target_steps
        )
    ).scalar_one()

    return {
        "month_start": ms.isoformat(),
        "month_end": me.isoformat(),
        "days_in_month": days_in_month,
        "weight_start": weight_start,
        "weight_end": weight_end,
        "weight_change": weight_change,
        "weight_days": weight_days,
        "body_fat_start": fat_start,
        "body_fat_end": fat_end,
        "body_fat_change": fat_change,
        **girth_changes,
        "workout_count": workout_count,
        "workout_min": workout_min,
        "cardio_min": cardio_min,
        "training_load": training_load,
        "unrated_min": unrated_min,
        "target_weekly_cardio_min": target_cardio,
        "cardio_weeks_ok": cardio_weeks_ok,
        "cardio_weeks_total": len(mondays),
        "habit_rate": habit_rate,
        "habit_count": len(habits),
        "diet_days": diet_days,
        "avg_kcal": _avg(s_kcal),
        "avg_protein_g": _avg(s_protein),
        "avg_fat_g": _avg(s_fat),
        "avg_carb_g": _avg(s_carb),
        "diet_streak_max": diet_streak_max,
        "avg_steps": avg_steps,
        "step_days": step_days,
        "steps_ok_days": steps_ok_days,
        "target_steps": target_steps,
    }


# ---------- 月报惰性生成（照 review.py 模式） ----------
def _ensure_last_month(db: Session) -> None:
    """上一完整月若无记录则现场聚合插入。"""
    last = _prev_month_start(_month_start_of(today_local()))
    exists = db.execute(
        select(MonthlyReview.id).where(MonthlyReview.month_start == last)
    ).scalar_one_or_none()
    if exists is None:
        db.execute(
            pg_insert(MonthlyReview)
            .values(month_start=last, metrics_snapshot=_aggregate_month(db, last))
            .on_conflict_do_nothing(index_elements=["month_start"])
        )


def _parse_month_start(month_start: str) -> date:
    try:
        d = date.fromisoformat(month_start.strip())
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="月份格式不正确")
    if d.day != 1:
        raise HTTPException(status_code=404, detail="month_start 必须是每月 1 号")
    return d


def _get_or_create_month(db: Session, ms: date) -> MonthlyReview:
    """已有则取；缺失但该月已完整则现场聚合生成；未结束的月 404。"""
    row = db.execute(
        select(MonthlyReview).where(MonthlyReview.month_start == ms)
    ).scalar_one_or_none()
    if row is None:
        if _next_month_start(ms) > today_local():
            raise HTTPException(status_code=404, detail="该月尚未结束，暂无月报")
        row = MonthlyReview(month_start=ms, metrics_snapshot=_aggregate_month(db, ms))
        db.add(row)
        db.flush()
    return row


# ---------- 月报展示上下文 ----------
def _month_list_row(r: MonthlyReview) -> dict[str, Any]:
    snap = r.metrics_snapshot or {}
    chips: list[str] = []
    if snap.get("weight_change") is not None:
        chips.append(f"体重 {snap['weight_change']:+.1f} kg")
    if snap.get("workout_count"):
        chips.append(f"训练 {snap['workout_count']} 次")
    if snap.get("habit_rate") is not None:
        chips.append(f"打卡 {snap['habit_rate']}%")
    if snap.get("avg_steps") is not None:
        chips.append(f"步数 {snap['avg_steps']:,}")
    if snap.get("diet_days"):
        chips.append(f"饮食 {snap['diet_days']} 天")
    return {
        "month_start": r.month_start.isoformat(),
        "label": _month_label(r.month_start),
        "has_summary": bool((r.summary or "").strip()),
        "chips": chips,
    }


def _month_cards(snap: dict[str, Any]) -> list[dict[str, Any]]:
    g = snap.get
    cards: list[dict[str, Any]] = []

    wc = g("weight_change")
    if wc is not None:
        cards.append({
            "label": "体重变化",
            "value": f"{wc:+.1f} kg",
            "sub": f"{_fmt_f(g('weight_start'))} → {_fmt_f(g('weight_end'))} kg · {g('weight_days')} 次记录",
        })
    elif g("weight_end") is not None:
        cards.append({"label": "体重变化", "value": f"{_fmt_f(g('weight_end'))} kg", "sub": "本月仅 1 次记录"})
    else:
        cards.append({"label": "体重变化", "value": "—", "sub": "本月无体重记录"})

    if g("body_fat_change") is not None:
        cards.append({
            "label": "体脂率变化",
            "value": f"{g('body_fat_change'):+.1f}%",
            "sub": f"{_fmt_f(g('body_fat_start'))} → {_fmt_f(g('body_fat_end'))} %",
        })

    girth_labels = (
        ("waist_change", "腰围"), ("chest_change", "胸围"), ("hip_change", "臀围"),
        ("thigh_change", "大腿围"), ("arm_change", "臂围"),
    )
    girth_parts = [f"{label} {g(key):+.1f}" for key, label in girth_labels if g(key) is not None]
    if girth_parts:
        cards.append({
            "label": "围度变化 (cm)",
            "value": " · ".join(girth_parts[:2]),
            "sub": " · ".join(girth_parts[2:]) or "月内首末差",
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

    if g("cardio_weeks_ok") is not None:
        ok, total = g("cardio_weeks_ok"), g("cardio_weeks_total") or 0
        cards.append({
            "label": "有氧达标周",
            "value": f"{ok} / {total} 周",
            "sub": f"周目标 {g('target_weekly_cardio_min')} 分钟 · 月有氧 {g('cardio_min') or 0} 分钟",
            "tone": "text-emerald-400" if total and ok == total else None,
        })
    else:
        cards.append({
            "label": "月有氧",
            "value": f"{g('cardio_min') or 0} 分钟",
            "sub": "未设周有氧目标",
        })

    cards.append({
        "label": "习惯打卡率",
        "value": f"{g('habit_rate')}%" if g("habit_rate") is not None else "—",
        "sub": f"{g('habit_count') or 0} 项 daily 习惯 × {g('days_in_month')} 天",
    })

    diet_days = g("diet_days") or 0
    macro_parts = [
        f"{label} {g(key)} g"
        for key, label in (("avg_protein_g", "蛋白"), ("avg_fat_g", "脂肪"), ("avg_carb_g", "碳水"))
        if g(key) is not None
    ]
    cards.append({
        "label": "日均热量",
        "value": f"{g('avg_kcal')} kcal" if g("avg_kcal") is not None else "—",
        "sub": " · ".join(macro_parts) or f"记录 {diet_days} 天",
    })
    cards.append({
        "label": "饮食记录",
        "value": f"{diet_days} / {g('days_in_month')} 天",
        "sub": f"最长连击 {g('diet_streak_max') or 0} 天",
    })
    cards.append({
        "label": "日均步数",
        "value": f"{g('avg_steps'):,}" if g("avg_steps") is not None else "—",
        "sub": f"达标(≥{g('target_steps'):,}) {g('steps_ok_days') or 0} 天 · {g('step_days') or 0} 天有数据",
    })
    return cards


# ---------- tab 上下文 ----------
def _tabs_ctx(db: Session, t: str) -> dict[str, Any]:
    t = t if t in ("daily", "weekly", "monthly") else "daily"
    ctx: dict[str, Any] = {"t": t, "tabs": TABS}
    if t == "daily":
        ctx["rows"] = _daily_rows(db)
    elif t == "weekly":
        _ensure_last_week(db)
        reviews = (
            db.execute(select(WeeklyReview).order_by(WeeklyReview.week_start.desc()))
            .scalars().all()
        )
        ctx["rows"] = [_list_row(r) for r in reviews]
    else:
        _ensure_last_month(db)
        reviews = (
            db.execute(select(MonthlyReview).order_by(MonthlyReview.month_start.desc()))
            .scalars().all()
        )
        ctx["rows"] = [_month_list_row(r) for r in reviews]
    return ctx


# ---------- 路由 ----------
@router.get("/report")
def report_page(request: Request, t: str = "daily", db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "report.html", _tabs_ctx(db, t))


@router.get("/fragments/report/tabs")
def report_tabs_fragment(request: Request, t: str = "daily", db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "fragments/report_tabs.html", _tabs_ctx(db, t))


@router.get("/report/daily")
def report_daily(request: Request, d: str | None = None, db: Session = Depends(get_db)):
    """当日日报：默认今天，可前后翻天（不允许未来）。"""
    today = today_local()
    try:
        day = date.fromisoformat(str(d).strip()) if d else today
    except ValueError:
        day = today
    day = min(day, today)
    return templates.TemplateResponse(request, "report_daily.html", _daily_ctx(db, day, today))


@router.get("/report/monthly/{month_start}")
def report_month_detail(month_start: str, request: Request, db: Session = Depends(get_db)):
    ms = _parse_month_start(month_start)
    row = _get_or_create_month(db, ms)
    return templates.TemplateResponse(
        request,
        "report_month_detail.html",
        {
            "month_start": ms.isoformat(),
            "range_label": _month_label(ms),
            "cards": _month_cards(row.metrics_snapshot or {}),
            "summary": row.summary or "",
            "saved": False,
        },
    )


@router.put("/report/monthly/{month_start}")
async def report_month_save(month_start: str, request: Request, db: Session = Depends(get_db)):
    ms = _parse_month_start(month_start)
    row = _get_or_create_month(db, ms)
    form = await request.form()
    summary = str(form.get("summary") or "").strip()
    row.summary = summary or None
    db.flush()
    return templates.TemplateResponse(
        request,
        "fragments/report_month_summary_form.html",
        {"month_start": ms.isoformat(), "summary": row.summary or "", "saved": True},
    )
