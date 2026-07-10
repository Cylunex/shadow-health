"""每日提醒摘要（Android 壳的 ReminderWorker 调用，Bearer 鉴权）。

GET /api/reminders/digest：今日打卡缺口 + 热量/蛋白/步数进度 + 本周有氧缺口，
message 字段服务端拼好中文文案，客户端只负责展示成通知。

局域网 http 下 Web Push 不可用（Push API 要求 HTTPS），提醒走壳内本地通知，
与体脂秤监听/三星同步共用 INGEST_TOKEN。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppSetting, DailyActivity, DietLog, Habit, HabitLog, WorkoutLog
from app.routers.review import _is_cardio
from app.timeutil import today_local

router = APIRouter(prefix="/api/reminders")


def _setting_num(db: Session, key: str) -> float | None:
    row = db.get(AppSetting, key)
    if row is None:
        return None
    try:
        n = float(row.value)  # type: ignore[arg-type]
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


@router.get("/digest")
def reminders_digest(request: Request, db: Session = Depends(get_db)) -> Response:
    from app.routers.ingest import _bearer_reject

    reject = _bearer_reject(request)
    if reject is not None:
        return reject

    today = today_local()
    week_start = today - timedelta(days=today.isoweekday() - 1)

    # 今日未达标的 daily 习惯（与打卡页口径一致：done_count >= target）
    habits = db.execute(
        select(Habit).where(Habit.active.is_(True), Habit.period == "daily")
        .order_by(Habit.sort, Habit.id)
    ).scalars().all()
    done_by_habit = {
        hid: cnt
        for hid, cnt in db.execute(
            select(HabitLog.habit_id, HabitLog.done_count).where(HabitLog.log_date == today)
        )
    }
    pending = [
        h.name for h in habits if done_by_habit.get(h.id, 0) < (h.target_per_period or 1)
    ]

    kcal, protein = db.execute(
        select(
            func.coalesce(func.sum(DietLog.kcal), 0),
            func.coalesce(func.sum(DietLog.protein_g), 0),
        ).where(DietLog.log_date == today)
    ).one()
    steps = db.execute(
        select(DailyActivity.steps).where(DailyActivity.log_date == today)
    ).scalar_one_or_none() or 0
    cardio_rows = db.execute(
        select(WorkoutLog.session_type, WorkoutLog.duration_min).where(
            WorkoutLog.log_date >= week_start,
            WorkoutLog.log_date <= today,
            WorkoutLog.duration_min.is_not(None),
        )
    ).all()
    cardio_min = sum(dur for st, dur in cardio_rows if _is_cardio(st))

    t_kcal = _setting_num(db, "target_kcal")
    t_protein = _setting_num(db, "target_protein_g")
    t_steps = _setting_num(db, "target_steps")
    t_cardio = _setting_num(db, "target_weekly_cardio_min")

    parts: list[str] = []
    if pending:
        head = "、".join(pending[:3]) + ("…" if len(pending) > 3 else "")
        parts.append(f"还有 {len(pending)} 项打卡：{head}")
    if t_protein and float(protein) < t_protein:
        parts.append(f"蛋白质还差 {round(t_protein - float(protein))}g")
    if t_steps and steps < t_steps:
        parts.append(f"步数 {steps}/{round(t_steps)}")
    if t_cardio and cardio_min < t_cardio:
        parts.append(f"本周有氧还差 {round(t_cardio - cardio_min)} 分钟")

    all_done = not parts
    payload: dict[str, Any] = {
        "date": today.isoformat(),
        "habits_pending": len(pending),
        "habits_total": len(habits),
        "kcal": float(kcal),
        "protein_g": float(protein),
        "steps": steps,
        "weekly_cardio_min": cardio_min,
        "all_done": all_done,
        "message": "今日目标全部达成 🎉" if all_done else " · ".join(parts),
    }
    return JSONResponse(payload)
