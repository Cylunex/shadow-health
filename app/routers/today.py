"""今日面板（设计文档 §四 "/" 行）。

自查 daily_activity / workout_logs / habits + app_settings（三环聚合）；
计划卡/习惯/指标/饮食各区块通过 HTMX 片段由对应模块提供。
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import AppSetting, DailyActivity, Habit, HabitLog, ImportRaw, WorkoutLog
from app.timeutil import now_local, today_local

router = APIRouter(dependencies=[Depends(require_login)])

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
DEFAULT_TARGET_STEPS = 8000  # 设计文档 §5.6：target_steps 默认 8000（源 06）
DAILY_WORKOUT_TARGET_MIN = 30  # 三环之「训练环」日目标（Apple Fitness 同款默认值）
AGENT_FRESH_SECONDS = 600  # 「Agent 最近写入」提示窗口（60s 轮询，窗口放宽到 10 分钟）


def _greeting(hour: int) -> str:
    if 5 <= hour < 9:
        return "早上好"
    if 9 <= hour < 12:
        return "上午好"
    if 12 <= hour < 14:
        return "中午好"
    if 14 <= hour < 18:
        return "下午好"
    if 18 <= hour < 23:
        return "晚上好"
    return "夜深了，注意休息"


def _target_steps(db: Session) -> int:
    """读 app_settings.target_steps；缺失/非法回退默认 8000。"""
    row = db.get(AppSetting, "target_steps")
    if row is not None:
        value = row.value
        if isinstance(value, dict):  # 容错：{"value": 8000} 形式
            value = value.get("value")
        try:
            n = int(value)  # type: ignore[arg-type]
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    return DEFAULT_TARGET_STEPS


def _rings(db: Session, today: date, steps: int | None, target_steps: int) -> list[dict]:
    """今日三环（Apple Fitness 式激励）：步数 / 训练分钟 / 打卡完成。"""
    workout_min = db.execute(
        select(func.coalesce(func.sum(WorkoutLog.duration_min), 0)).where(
            WorkoutLog.log_date == today
        )
    ).scalar_one()
    habits = db.execute(
        select(Habit.id, Habit.target_per_period).where(
            Habit.active.is_(True), Habit.period == "daily"
        )
    ).all()
    targets = {hid: t or 1 for hid, t in habits}
    done = 0
    if targets:
        logs = db.execute(
            select(HabitLog.habit_id, HabitLog.done_count).where(
                HabitLog.log_date == today, HabitLog.habit_id.in_(list(targets))
            )
        ).all()
        done = sum(1 for hid, c in logs if c >= targets[hid])

    def pct(v: float, target: float) -> int:
        return min(100, round(v * 100 / target)) if target else 0

    return [
        {
            "label": "步数", "color": "#34d399",
            "pct": pct(steps or 0, target_steps),
            "value": f"{steps or 0:,}", "sub": f"目标 {target_steps:,}",
        },
        {
            "label": "训练", "color": "#38bdf8",
            "pct": pct(int(workout_min), DAILY_WORKOUT_TARGET_MIN),
            "value": str(int(workout_min)), "sub": f"目标 {DAILY_WORKOUT_TARGET_MIN} 分钟",
        },
        {
            "label": "打卡", "color": "#a78bfa",
            "pct": pct(done, len(targets)),
            "value": str(done), "sub": f"共 {len(targets)} 项",
        },
    ]


@router.get("/")
def today_page(request: Request, db: Session = Depends(get_db)):
    today = today_local()
    ctx = {
        "greeting": _greeting(now_local().hour),
        "today": today,
        "weekday_cn": WEEKDAY_CN[today.isoweekday() - 1],
    }
    return templates.TemplateResponse(request, "today.html", ctx)


@router.get("/fragments/today/agent-fresh")
def agent_fresh_fragment(request: Request, db: Session = Depends(get_db)):
    """「Agent 最近写入」迷你提示：近 10 分钟经 agent 通道收到的留档条数，
    有才显示（无写入渲染空片段）。60s 轮询——agent 在外面记了东西，
    正开着今日页也能看见，点进 /agent-log 核对。"""
    since = now_local() - timedelta(seconds=AGENT_FRESH_SECONDS)
    count = db.execute(
        select(func.count()).select_from(ImportRaw).where(
            ImportRaw.source == "agent",
            func.coalesce(ImportRaw.last_seen_at, ImportRaw.imported_at) >= since,
        )
    ).scalar_one()
    return templates.TemplateResponse(
        request, "fragments/today_agent_fresh.html", {"count": count}
    )


@router.get("/fragments/today/rings")
def rings_fragment(request: Request, db: Session = Depends(get_db)):
    """三环片段：打卡/训练写操作后经 habit-changed / workout-changed 被动刷新，
    与同页汇总条保持一致（否则环数据停留在页面加载时刻）。"""
    today = today_local()
    activity = db.get(DailyActivity, today)
    steps = activity.steps if activity is not None else None
    return templates.TemplateResponse(
        request,
        "fragments/today_rings.html",
        {"rings": _rings(db, today, steps, _target_steps(db))},
    )
