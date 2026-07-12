"""单人成就体系（V6 H1）：本地规则计算 + 首达日期落档，零社交零攀比。

设计：成就本体**永远按数据实时计算**（幂等、可回放），achievements 表只记
「第一次达成是哪天」——徽章墙要显示日期、digest 要庆祝「新达成」，只有
首达时刻需要持久化。撤销/删数据导致条件不再满足时徽章不收回（已达成的
事实发生过），这也是所有成就系统的通行语义。

规则挑「长期主义」向：连续性、累计量、个人纪录——不设「单日爆量」类
（与去羞辱化原则一致，不鼓励报复性冲量）。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Achievement, DietLog, Habit, HabitLog, WorkoutLog
from app.services import sleep as sleep_service
from app.services.pr import exercise_prs

# (key, 徽章名, 描述, 阈值)——同系列共用统计量
_DIET_STREAK = [("diet_streak_7", "连击初成", "连续记录饮食 7 天", 7),
                ("diet_streak_30", "月度全勤", "连续记录饮食 30 天", 30),
                ("diet_streak_100", "百日不断", "连续记录饮食 100 天", 100)]
_RUN_KM = [("run_100", "百公里", "累计跑量 100 km", 100),
           ("run_500", "五百公里", "累计跑量 500 km", 500),
           ("run_1000", "千里之行", "累计跑量 1000 km", 1000)]
_WORKOUT_MIN = [("train_1k", "千分钟", "累计训练 1,000 分钟", 1000),
                ("train_5k", "五千分钟", "累计训练 5,000 分钟", 5000),
                ("train_10k", "万分钟俱乐部", "累计训练 10,000 分钟", 10000)]
_HABIT_DAYS = [("habit_100", "百日打卡", "习惯达标累计 100 天次", 100),
               ("habit_500", "五百天次", "习惯达标累计 500 天次", 500)]
_SLEEP_STREAK = [("sleep_streak_7", "睡眠自律周", "连续 7 夜睡够 7 小时", 7)]
_PR_COUNT = [("pr_5", "力量档案", "5 个动作有组次纪录", 5)]

_RUN_KEYWORDS = ("跑", "run")


def _max_streak(days: set[date]) -> int:
    """日期集合中最长连续天数。"""
    best = cur = 0
    prev: date | None = None
    for d in sorted(days):
        cur = cur + 1 if prev is not None and (d - prev).days == 1 else 1
        best = max(best, cur)
        prev = d
    return best


def _series(defs: list[tuple], value: float, fmt: str = "{:.0f}") -> list[dict[str, Any]]:
    return [
        {
            "key": key, "name": name, "desc": desc,
            "earned": value >= threshold,
            "progress": f"{fmt.format(value)} / {fmt.format(threshold)}",
        }
        for key, name, desc, threshold in defs
    ]


def evaluate(db: Session) -> list[dict[str, Any]]:
    """全部成就的当前状态（earned + 进度文本）。单用户数据量小，全表扫无压力。"""
    from app.timeutil import today_local

    today = today_local()
    diet_days = {d for (d,) in db.execute(select(func.distinct(DietLog.log_date)))}

    run_km = 0.0
    total_min = 0
    for stype, dur, dist in db.execute(
        select(WorkoutLog.session_type, WorkoutLog.duration_min, WorkoutLog.distance_km)
    ):
        total_min += dur or 0
        s = (stype or "").lower()
        if dist and any(k in s for k in _RUN_KEYWORDS):
            run_km += float(dist)

    habit_targets = {
        h.id: h.target_per_period or 1 for h in db.execute(select(Habit)).scalars()
    }
    habit_days = sum(
        1 for hid, c in db.execute(select(HabitLog.habit_id, HabitLog.done_count))
        if c >= habit_targets.get(hid, 1)
    )

    # 连续睡够 7h：只需近一年窗口——更早的连击对「当下激励」意义有限
    ok_nights = {
        r["date"]
        for r in sleep_service.night_rows(db, today - timedelta(days=365), today)
        if r["total_min"] >= 7 * 60
    }
    sleep_streak = _max_streak(ok_nights)

    pr_count = sum(1 for _n, p in exercise_prs(db).items() if p["sessions"] >= 2)

    out: list[dict[str, Any]] = []
    out += _series(_DIET_STREAK, _max_streak(diet_days))
    out += _series(_RUN_KM, run_km, "{:.0f}")
    out += _series(_WORKOUT_MIN, total_min, "{:,.0f}")
    out += _series(_HABIT_DAYS, habit_days)
    out += _series(_SLEEP_STREAK, sleep_streak)
    out += _series(_PR_COUNT, pr_count)
    return out


def sync_and_list(db: Session, today: date) -> tuple[list[dict[str, Any]], list[str]]:
    """计算全部成就 + 把新达成的首达日期落档；返回 (带日期的清单, 本次新达成名)。"""
    items = evaluate(db)
    earned_map = {a.key: a.earned_on for a in db.execute(select(Achievement)).scalars()}
    newly: list[str] = []
    for it in items:
        if it["earned"] and it["key"] not in earned_map:
            db.add(Achievement(key=it["key"], label=it["name"], earned_on=today))
            earned_map[it["key"]] = today
            newly.append(it["name"])
        it["earned_on"] = earned_map.get(it["key"])
    if newly:
        db.flush()
    return items, newly
