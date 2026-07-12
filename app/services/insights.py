"""跨域洞察引擎（V6 P6）：预设固定配对的分桶对比——单用户长周期数据的独有玩法。

设计取舍（防伪相关）：
- **只做预设配对**，不做开放式相关矩阵：几十个指标两两配对必然捞出一堆巧合，
  预设的 6 组都有明确的生理/行为假设，结论才可信可行动。
- 分桶对比而非相关系数：「睡 <6.5h 的次日精力均值 2.8 vs 睡够的 3.6」比
  r=-0.34 可读得多，也不假装精确。
- 双桶各 ≥8 个样本才输出；差异太小（< 结果量纲的显著阈值）不输出——
  宁可少说，不说废话。

输出给三处用：月报「洞察」卡、llm.build_context（AI 复盘引用）、后续周简报。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BodyMetrics, DailyActivity, DietLog, WorkoutLog
from app.services import readiness as readiness_service
from app.services import sleep as sleep_service

WINDOW_DAYS = 90
MIN_BUCKET_N = 8
SLEEP_SHORT_MIN = 6.5 * 60  # 「没睡够」阈值
BEDTIME_LATE_MIN = 660  # 距中午 660 分钟 = 23:00

# 每条洞察：差异绝对值达到该阈值才值得说
_THRESHOLDS = {
    "energy": 0.4,      # 精力 1~5
    "mood": 0.6,        # 心情 1~10
    "kcal": 120.0,      # 日摄入 kcal
    "steps": 800.0,     # 日步数
    "erection_pct": 12.0,  # 晨勃率百分点
}


def _bucket_stat(
    days_a: list[date], days_b: list[date], values: dict[date, float]
) -> tuple[float, int, float, int] | None:
    """两桶日期在 values 上的均值与样本数；任一桶样本不足返回 None。"""
    va = [values[d] for d in days_a if d in values]
    vb = [values[d] for d in days_b if d in values]
    if len(va) < MIN_BUCKET_N or len(vb) < MIN_BUCKET_N:
        return None
    return (sum(va) / len(va), len(va), sum(vb) / len(vb), len(vb))


def build_insights(db: Session, today: date) -> list[dict[str, Any]]:
    """近 90 天固定配对洞察。每条：{title, a_label, a_value, b_label, b_value,
    diff_text, n}——只输出样本充足且差异过阈值的。"""
    start = today - timedelta(days=WINDOW_DAYS - 1)
    out: list[dict[str, Any]] = []

    # ---- 取数（一次取全，各配对复用） ----
    nights = {r["date"]: r for r in sleep_service.night_rows(db, start, today)}
    bm_rows = db.execute(
        select(
            BodyMetrics.log_date, BodyMetrics.energy_level, BodyMetrics.mood_score,
            BodyMetrics.morning_erection,
        ).where(BodyMetrics.log_date.between(start, today))
    ).all()
    energy = {d: float(e) for d, e, _m, _me in bm_rows if e is not None}
    mood = {d: float(m) for d, _e, m, _me in bm_rows if m is not None}
    erection = {d: (1.0 if me else 0.0) for d, _e, _m, me in bm_rows if me is not None}
    kcal = {
        d: float(v)
        for d, v in db.execute(
            select(DietLog.log_date, func.sum(DietLog.kcal))
            .where(DietLog.log_date.between(start, today), DietLog.kcal.is_not(None))
            .group_by(DietLog.log_date)
        )
    }
    steps = {
        d: float(v)
        for d, v in db.execute(
            select(DailyActivity.log_date, DailyActivity.steps).where(
                DailyActivity.log_date.between(start, today), DailyActivity.steps.is_not(None)
            )
        )
    }
    loads = readiness_service.daily_loads(db, start, today)
    trained_days = {
        d for (d,) in db.execute(
            select(func.distinct(WorkoutLog.log_date)).where(
                WorkoutLog.log_date.between(start, today)
            )
        )
    }

    # 睡眠分桶（按「夜」，对应影响是**次日**）
    short_nights = [d for d, r in nights.items() if r["total_min"] < SLEEP_SHORT_MIN]
    good_nights = [d for d, r in nights.items() if r["total_min"] >= 7 * 60]
    next_short = [d + timedelta(days=1) for d in short_nights]
    next_good = [d + timedelta(days=1) for d in good_nights]

    def _emit(
        title: str, a_label: str, b_label: str, stat: tuple | None,
        threshold_key: str, fmt: Callable[[float], str],
    ) -> None:
        if stat is None:
            return
        a_val, a_n, b_val, b_n = stat
        if abs(a_val - b_val) < _THRESHOLDS[threshold_key]:
            return
        out.append({
            "title": title,
            "a_label": a_label, "a_value": fmt(a_val),
            "b_label": b_label, "b_value": fmt(b_val),
            "n": a_n + b_n,
        })

    fmt1 = lambda v: f"{v:.1f}"  # noqa: E731
    fmt0 = lambda v: f"{v:,.0f}"  # noqa: E731

    # 1. 没睡够 → 次日精力
    _emit("睡眠与次日精力", "睡 <6.5h 的次日精力", "睡够 7h 的次日精力",
          _bucket_stat(next_short, next_good, energy), "energy", fmt1)
    # 2. 没睡够 → 次日摄入（睡眠剥夺升食欲是强共识假设）
    _emit("睡眠与次日食欲", "睡 <6.5h 的次日摄入", "睡够 7h 的次日摄入",
          _bucket_stat(next_short, next_good, kcal), "kcal", lambda v: f"{v:,.0f} kcal")
    # 3. 没睡够 → 次日步数
    _emit("睡眠与次日活动量", "睡 <6.5h 的次日步数", "睡够 7h 的次日步数",
          _bucket_stat(next_short, next_good, steps), "steps", fmt0)
    # 4. 高负荷 → 次日心情（训练日均负荷的 1.3 倍算「高负荷日」，对照=其余全部天）
    if loads:
        avg_load = sum(loads.values()) / len(loads)
        window_days = [start + timedelta(days=i) for i in range((today - start).days)]
        heavy = [d + timedelta(days=1) for d in window_days if loads.get(d, 0) > avg_load * 1.3]
        light = [d + timedelta(days=1) for d in window_days if loads.get(d, 0) <= avg_load * 1.3]
        _emit("训练负荷与次日心情", "高负荷日的次日心情", "普通日的次日心情",
              _bucket_stat(heavy, light, mood), "mood", fmt1)
    # 5. 晚睡 → 次日晨勃率（23:00 后入睡）
    late = [d + timedelta(days=1) for d, r in nights.items() if r["bedtime_min"] >= BEDTIME_LATE_MIN]
    early = [d + timedelta(days=1) for d, r in nights.items() if r["bedtime_min"] < BEDTIME_LATE_MIN]
    _emit("就寝时间与晨勃", "23 点后入睡的次日晨勃率", "23 点前入睡的次日晨勃率",
          _bucket_stat(late, early, erection), "erection_pct",
          lambda v: f"{v * 100:.0f}%")
    # 6. 训练日 vs 休息日 → 当日心情
    rest_days = [d for d in mood if d not in trained_days]
    _emit("训练与当日心情", "训练日心情", "休息日心情",
          _bucket_stat(sorted(trained_days), rest_days, mood), "mood", fmt1)

    return out


def insights_lines(db: Session, today: date) -> list[str]:
    """给 llm.build_context / 月报用的一行式结论。"""
    return [
        f"- {i['title']}：{i['a_label']} {i['a_value']} vs {i['b_label']} {i['b_value']}"
        f"（{i['n']} 天样本）"
        for i in build_insights(db, today)
    ]
