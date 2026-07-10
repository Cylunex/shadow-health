"""养生打卡（设计文档 §3.5、§四 /habits 行与「习惯打卡交互」段）。

端点契约（其他模块按这些 URL 调用，不可变动）：
- GET  /habits                      管理页：全部习惯 + 启停 + streak + 本月完成率
- POST /habits/{id}/toggle          target=1 打卡：当日存在则删、不存在则插（再点即撤销）
- POST /habits/{id}/increment       target>1 计数 +1（INSERT ON CONFLICT done_count+1）
- POST /habits/{id}/decrement       target>1 计数 -1（减到 0 删行）
- POST /habits/{id}/active          启停习惯
- GET  /fragments/habits/today      今日打卡列表片段（今日面板 hx-get 加载）
- GET  /fragments/habits/summary    streak/今日完成率汇总条
- GET  /fragments/habits/heatmap    打卡日历热力图片段（months=3/6/12，服务端渲染零 JS）

注：/fragments/* 不在 /habits 前缀下，故本路由不设 prefix，路径写全。
所有打卡写操作响应头带 HX-Trigger: habit-changed，summary 片段被动刷新。
weekly 口径：周一为一周起点（isoweekday），done = 本周 habit_logs done_count 求和。
"""
from __future__ import annotations

import operator
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import BodyMetrics, DailyActivity, DietLog, Habit, HabitLog, WorkoutLog
from app.routers.workout import HEATMAP_LEVEL_CLASSES, HEATMAP_MONTHS_OPTIONS
from app.services.sleep import sessions_by_date as _sleep_sessions_by_date
from app.timeutil import LOCAL_TZ, today_local

router = APIRouter(dependencies=[Depends(require_login)])

HX_TRIGGER = {"HX-Trigger": "habit-changed"}

# ---------- auto_rule：阈值型自动判定（如 'steps>=8000'、'sleep_hours>=7'） ----------
_RULE_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(>=|<=|==|>|<)\s*(\d+(?:\.\d+)?)\s*$")
_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    ">": operator.gt,
    "<": operator.lt,
}
# 字段白名单：先查 daily_activity，再查 body_metrics
_ACTIVITY_FIELDS = {"steps", "distance_m", "active_kcal", "hr_min", "hr_avg", "hr_max"}
_METRIC_FIELDS = {
    "weight_kg", "body_fat_pct", "sleep_hours", "resting_hr", "spo2_pct",
    "waist_cm", "bp_systolic", "bp_diastolic", "energy_level", "sleep_quality",
}
# 派生字段（当日聚合，不对应单列）：
# - workout_min        当日训练总分钟（0 = 真实未训练，可判未达标）
# - diet_count         当日饮食记录笔数
# - sleep_start_clock  当夜（wake_date=当日）最早入睡钟点：23:00→23、00:30→24.5
#                      （凌晨读作 24+，"sleep_start_clock<23" 即「23 点前睡」）
_DERIVED_FIELDS = {"workout_min", "diet_count", "sleep_start_clock"}


def _rule_value(db: Session, field: str, day: date):
    """auto_rule 左值取数：当日 daily_activity / body_metrics / 派生聚合；
    无行或字段 NULL 返回 None（退回手动）。"""
    if field in _ACTIVITY_FIELDS:
        row = db.get(DailyActivity, day)
        return getattr(row, field) if row is not None else None
    if field in _METRIC_FIELDS:
        row = db.execute(
            select(BodyMetrics).where(BodyMetrics.log_date == day)
        ).scalar_one_or_none()
        return getattr(row, field) if row is not None else None
    if field == "workout_min":
        return float(db.execute(
            select(func.coalesce(func.sum(WorkoutLog.duration_min), 0)).where(
                WorkoutLog.log_date == day
            )
        ).scalar_one())
    if field == "diet_count":
        return float(db.execute(
            select(func.count()).select_from(DietLog).where(DietLog.log_date == day)
        ).scalar_one())
    if field == "sleep_start_clock":
        sessions = _sleep_sessions_by_date(db, day, day).get(day, [])
        if not sessions:
            return None
        start = min(s.start_at for s in sessions).astimezone(LOCAL_TZ)
        clock = start.hour + start.minute / 60
        return clock + 24 if clock < 12 else clock
    return None


def _eval_auto_rule(db: Session, rule: str | None, day: date) -> bool | None:
    """True=达标 / False=未达标 / None=无数据或规则不可解析（退回手动）。"""
    m = _RULE_RE.match(rule or "")
    if not m:
        return None
    value = _rule_value(db, m.group(1), day)
    if value is None:
        return None
    return bool(_OPS[m.group(2)](float(value), float(m.group(3))))


def _apply_auto_rules(
    db: Session, habits: list[Habit], day: date
) -> tuple[dict[int, bool | None], int]:
    """带 auto_rule 的习惯先自动判定：达标且当日无记录则写 habit_logs（幂等）。

    返回 (habit_id -> 判定结果, 实际新插入行数)。只插不删：用户手动记录永不被自动清掉；
    手动撤销留下的 done_count=0 否决行同样占住唯一键，ON CONFLICT 不会回填。
    """
    status: dict[int, bool | None] = {}
    inserted = 0
    for h in habits:
        if not h.auto_rule:
            continue
        ok = _eval_auto_rule(db, h.auto_rule, day)
        status[h.id] = ok
        if ok:
            # daily 一次判定即当日满额；weekly 每天最多 +1（多天各一行，
            # _week_sums 周内求和达标）——否则单天判定直接插满周目标
            per_day = (h.target_per_period or 1) if h.period == "daily" else 1
            # RETURNING 判断是否真插入（冲突时无返回行；驱动 rowcount 对 ON CONFLICT 可能报 -1）
            new_id = db.execute(
                pg_insert(HabitLog)
                .values(habit_id=h.id, log_date=day, done_count=per_day)
                .on_conflict_do_nothing(index_elements=["habit_id", "log_date"])
                .returning(HabitLog.id)
            ).scalar_one_or_none()
            if new_id is not None:
                inserted += 1
    return status, inserted


# ---------- 统计：streak / 本月完成率 ----------
def _week_start(d: date) -> date:
    """周一为一周起点（isoweekday）。"""
    return d - timedelta(days=d.isoweekday() - 1)


def _logs_map(db: Session, habit_ids: list[int]) -> dict[int, dict[date, int]]:
    """habit_id -> {log_date: done_count}（单用户数据量小，直接全取）。"""
    out: dict[int, dict[date, int]] = defaultdict(dict)
    if not habit_ids:
        return out
    for hid, d, c in db.execute(
        select(HabitLog.habit_id, HabitLog.log_date, HabitLog.done_count).where(
            HabitLog.habit_id.in_(habit_ids)
        )
    ):
        out[hid][d] = c
    return out


def _week_sums(logs: dict[date, int]) -> dict[date, int]:
    weeks: dict[date, int] = defaultdict(int)
    for d, c in logs.items():
        weeks[_week_start(d)] += c
    return weeks


def _streak(habit: Habit, logs: dict[date, int], today: date) -> tuple[int, str]:
    """连续达标：daily 按天、weekly 按周；当期未达标不破连击（从上一期起算）。"""
    target = habit.target_per_period or 1
    if habit.period == "weekly":
        weeks = _week_sums(logs)
        w = _week_start(today)
        if weeks.get(w, 0) < target:
            w -= timedelta(days=7)
        n = 0
        while weeks.get(w, 0) >= target:
            n += 1
            w -= timedelta(days=7)
        return n, f"连续 {n} 周"
    d = today
    if logs.get(d, 0) < target:
        d -= timedelta(days=1)
    n = 0
    while logs.get(d, 0) >= target:
        n += 1
        d -= timedelta(days=1)
    return n, f"连续 {n} 天"


def _month_rate(habit: Habit, logs: dict[date, int], today: date) -> int:
    """本月完成率（%）：daily=达标天/本月已过天数；weekly=达标周/与本月相交的已开始周。"""
    target = habit.target_per_period or 1
    month_start = today.replace(day=1)
    if habit.period == "weekly":
        weeks = _week_sums(logs)
        w = _week_start(month_start)
        total = ok = 0
        while w <= today:
            total += 1
            if weeks.get(w, 0) >= target:
                ok += 1
            w += timedelta(days=7)
        return round(ok * 100 / total) if total else 0
    days = (today - month_start).days + 1
    ok = sum(
        1 for i in range(days) if logs.get(month_start + timedelta(days=i), 0) >= target
    )
    return round(ok * 100 / days)


# ---------- 片段上下文 ----------
def _item_state(
    habit: Habit, logs: dict[date, int], today: date, auto_status: bool | None
) -> dict:
    """今日打卡条目的展示状态。daily 看今日，weekly 看本周求和。"""
    target = habit.target_per_period or 1
    today_count = logs.get(today, 0)
    if habit.period == "weekly":
        ws = _week_start(today)
        period_count = sum(c for d, c in logs.items() if ws <= d <= today)
    else:
        period_count = today_count
    return {
        "target": target,
        "today_count": today_count,
        "period_count": period_count,
        "done": period_count >= target,
        "auto": bool(habit.auto_rule),
        "auto_status": auto_status,
    }


def _get_habit(db: Session, habit_id: int) -> Habit:
    habit = db.get(Habit, habit_id)
    if habit is None:
        raise HTTPException(status_code=404, detail="习惯不存在")
    return habit


def _parse_habit_date(raw: Any) -> date:
    """打卡补记日期：默认今天；只允许最近 30 天内、不允许未来（日报页补卡入口用）。"""
    today = today_local()
    try:
        d = date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return today
    if d > today or d < today - timedelta(days=30):
        return today
    return d


def _render_item(request: Request, db: Session, habit: Habit, day: date | None = None):
    """打卡写操作后回传该习惯条目片段（不重跑 auto 写入，避免撤销被立即覆盖）。"""
    day = day or today_local()
    logs = _logs_map(db, [habit.id])[habit.id]
    auto_status = _eval_auto_rule(db, habit.auto_rule, day) if habit.auto_rule else None
    return templates.TemplateResponse(
        request,
        "fragments/habits_today_item.html",
        {"habit": habit, "st": _item_state(habit, logs, day, auto_status)},
        headers=dict(HX_TRIGGER),
    )


def _manage_row_ctx(habit: Habit, logs: dict[date, int], today: date) -> dict:
    _, streak_label = _streak(habit, logs, today)
    return {
        "habit": habit,
        "streak_label": streak_label,
        "month_rate": _month_rate(habit, logs, today),
    }


# ---------- 打卡日历热力图 ----------
def _heatmap_ctx(db: Session, months: int) -> dict:
    """打卡日历（复用训练热力图的周列布局与配色）：按天统计每日习惯达标数。

    只统计当前启用中的 daily 习惯（weekly 习惯按周达标，无法落到单日格子）。
    档位按当日达标比例分 4 档：<50% / <75% / <100% / 全勤。
    """
    if months not in HEATMAP_MONTHS_OPTIONS:
        months = 3
    today = today_local()
    start = today - timedelta(days=round(months * 30.44))
    start -= timedelta(days=start.isoweekday() - 1)  # 对齐周一

    habits = db.execute(
        select(Habit).where(Habit.active.is_(True), Habit.period == "daily")
    ).scalars().all()
    targets = {h.id: h.target_per_period or 1 for h in habits}
    total = len(habits)

    done_by_day: dict[date, int] = defaultdict(int)
    if targets:
        for hid, d, c in db.execute(
            select(HabitLog.habit_id, HabitLog.log_date, HabitLog.done_count).where(
                HabitLog.habit_id.in_(list(targets)),
                HabitLog.log_date.between(start, today),
            )
        ):
            if c >= targets[hid]:
                done_by_day[d] += 1

    weeks: list[dict] = []
    col_start = start
    prev_month: int | None = None
    while col_start <= today:
        days: list[dict] = []
        for i in range(7):
            d = col_start + timedelta(days=i)
            if d > today:
                days.append({"future": True})
                continue
            done = done_by_day.get(d, 0)
            if total == 0 or done == 0:
                level = 0
            else:
                ratio = done / total
                level = 4 if ratio >= 1 else (3 if ratio >= 0.75 else (2 if ratio >= 0.5 else 1))
            days.append({"future": False, "date": d, "done": done, "level": level})
        month_label = ""
        if col_start.month != prev_month:
            month_label = f"{col_start.month}月"
            prev_month = col_start.month
        weeks.append({"days": days, "month_label": month_label})
        col_start += timedelta(days=7)

    return {
        "hm": {
            "months": months,
            "options": HEATMAP_MONTHS_OPTIONS,
            "weeks": weeks,
            "level_classes": HEATMAP_LEVEL_CLASSES,
            "total": total,
            "days_any": sum(1 for v in done_by_day.values() if v > 0),
            "days_full": sum(1 for v in done_by_day.values() if total and v >= total),
        }
    }


# ---------- 页面 ----------
def _manage_sections_ctx(db: Session) -> dict:
    """启用/停用双分区上下文（管理页与 habit-changed 刷新片段共用）。"""
    today = today_local()
    habits = db.execute(
        select(Habit).order_by(Habit.sort, Habit.id)
    ).scalars().all()
    logs = _logs_map(db, [h.id for h in habits])
    rows = [_manage_row_ctx(h, logs[h.id], today) for h in habits]
    return {
        "active_rows": [r for r in rows if r["habit"].active],
        "inactive_rows": [r for r in rows if not r["habit"].active],
    }


@router.get("/habits")
def habits_page(request: Request, db: Session = Depends(get_db)):
    """管理页：全部习惯（含 inactive）+ 启停开关 + streak + 本月完成率 + 打卡日历。"""
    ctx: dict = _manage_sections_ctx(db)
    ctx.update(_heatmap_ctx(db, 3))
    return templates.TemplateResponse(request, "habits.html", ctx)


@router.get("/fragments/habits/manage")
def habits_manage_fragment(request: Request, db: Session = Depends(get_db)):
    """启用/停用双分区片段（habit-changed 被动刷新：行迁移分区 + 计数同步）。"""
    return templates.TemplateResponse(
        request, "fragments/habits_manage_sections.html", _manage_sections_ctx(db)
    )


# ---------- 打卡写操作 ----------
@router.post("/habits/{habit_id}/toggle")
async def habit_toggle(habit_id: int, request: Request, db: Session = Depends(get_db)):
    """target=1 打卡：当日存在则撤销、不存在则插——再点一次即撤销。

    可选表单参数 d = 补记日期（≤今天、最近 30 天内；日报页翻到昨天补卡用）。
    auto_rule 习惯的撤销不删行，而是留 done_count=0 的「否决」行占住唯一键：
    否则下次今日面板加载 _apply_auto_rules 会幂等补插，撤销被静默还原。
    """
    habit = _get_habit(db, habit_id)
    form = await request.form()
    day = _parse_habit_date(form.get("d"))
    row = db.execute(
        select(HabitLog).where(HabitLog.habit_id == habit_id, HabitLog.log_date == day)
    ).scalar_one_or_none()
    if row is not None:
        if row.done_count == 0:
            row.done_count = habit.target_per_period or 1  # 从否决态重新打卡
        elif habit.auto_rule:
            row.done_count = 0  # 否决：规则仍满足也不再回填
        else:
            db.delete(row)
        db.flush()
    else:
        db.execute(
            pg_insert(HabitLog)
            .values(habit_id=habit_id, log_date=day, done_count=1)
            .on_conflict_do_nothing(index_elements=["habit_id", "log_date"])
        )
    return _render_item(request, db, habit, day)


@router.post("/habits/{habit_id}/increment")
async def habit_increment(habit_id: int, request: Request, db: Session = Depends(get_db)):
    """target>1 计数打卡 +1：INSERT ON CONFLICT done_count+1（d = 可选补记日期）。"""
    habit = _get_habit(db, habit_id)
    form = await request.form()
    day = _parse_habit_date(form.get("d"))
    stmt = pg_insert(HabitLog).values(habit_id=habit_id, log_date=day, done_count=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=["habit_id", "log_date"],
        set_={
            "done_count": HabitLog.__table__.c.done_count + 1,
            "updated_at": text("now()"),
        },
    )
    db.execute(stmt)
    return _render_item(request, db, habit, day)


@router.post("/habits/{habit_id}/decrement")
async def habit_decrement(habit_id: int, request: Request, db: Session = Depends(get_db)):
    """target>1 计数打卡 -1：减到 0 删行（auto 习惯留否决行）；当日无行则 no-op。"""
    habit = _get_habit(db, habit_id)
    form = await request.form()
    day = _parse_habit_date(form.get("d"))
    row = db.execute(
        select(HabitLog).where(HabitLog.habit_id == habit_id, HabitLog.log_date == day)
    ).scalar_one_or_none()
    if row is not None:
        if row.done_count <= 1:
            if habit.auto_rule:
                row.done_count = 0  # 否决行：防 _apply_auto_rules 回填
            else:
                db.delete(row)
        else:
            row.done_count = row.done_count - 1
        db.flush()
    return _render_item(request, db, habit, day)


@router.post("/habits/{habit_id}/active")
def habit_active(habit_id: int, request: Request, db: Session = Depends(get_db)):
    """启停习惯，回传管理页该行片段。"""
    habit = _get_habit(db, habit_id)
    habit.active = not habit.active
    db.flush()
    today = today_local()
    logs = _logs_map(db, [habit.id])[habit.id]
    return templates.TemplateResponse(
        request,
        "fragments/habits_manage_row.html",
        _manage_row_ctx(habit, logs, today),
        headers=dict(HX_TRIGGER),
    )


# ---------- 片段 ----------
@router.get("/fragments/habits/today")
def habits_today_fragment(request: Request, db: Session = Depends(get_db)):
    """今日打卡列表：active 习惯按 sort 排列；auto_rule 习惯先自动判定写 habit_logs。"""
    today = today_local()
    habits = db.execute(
        select(Habit).where(Habit.active.is_(True)).order_by(Habit.sort, Habit.id)
    ).scalars().all()
    auto_status, inserted = _apply_auto_rules(db, habits, today)
    logs = _logs_map(db, [h.id for h in habits])
    items = [
        {"habit": h, "st": _item_state(h, logs[h.id], today, auto_status.get(h.id))}
        for h in habits
    ]
    # 自动判定真的写入了新行时通知 summary 刷新
    headers = dict(HX_TRIGGER) if inserted else None
    return templates.TemplateResponse(
        request, "fragments/habits_today.html", {"items": items}, headers=headers
    )


@router.get("/fragments/habits/heatmap")
def habits_heatmap_fragment(request: Request, months: int = 3, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "fragments/habits_heatmap.html", _heatmap_ctx(db, months)
    )


@router.get("/fragments/habits/summary")
def habits_summary_fragment(request: Request, db: Session = Depends(get_db)):
    """streak/今日完成率汇总条（页面上以 hx-trigger="habit-changed from:body" 被动刷新）。"""
    today = today_local()
    habits = db.execute(
        select(Habit).where(Habit.active.is_(True)).order_by(Habit.sort, Habit.id)
    ).scalars().all()
    logs = _logs_map(db, [h.id for h in habits])
    ws = _week_start(today)
    daily = [h for h in habits if h.period != "weekly"]
    weekly = [h for h in habits if h.period == "weekly"]
    daily_done = sum(
        1 for h in daily if logs[h.id].get(today, 0) >= (h.target_per_period or 1)
    )
    weekly_done = sum(
        1
        for h in weekly
        if sum(c for d, c in logs[h.id].items() if ws <= d <= today)
        >= (h.target_per_period or 1)
    )
    best_streak = max((_streak(h, logs[h.id], today)[0] for h in daily), default=0)
    pct = round(daily_done * 100 / len(daily)) if daily else 0
    return templates.TemplateResponse(
        request,
        "fragments/habits_summary.html",
        {
            "daily_done": daily_done,
            "daily_total": len(daily),
            "weekly_done": weekly_done,
            "weekly_total": len(weekly),
            "pct": pct,
            "best_streak": best_streak,
        },
    )
