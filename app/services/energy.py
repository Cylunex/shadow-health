"""自适应能量引擎（V6 P4，MacroFactor 核心思路的本地化）——纯本地数学，零外网。

三件事：
1. 体重趋势线：对逐日体重做时间感知 EMA（日衰减 α=0.10，约 10 天时间常数），
   滤掉水分/宿便噪声——单日波动 ±1kg 很正常，趋势线才是真实变化。
2. TDEE 反推：能量守恒——窗口内 真实TDEE = 日均摄入 − 趋势体重变化×7700÷天数。
   静态公式（BMR+活动系数）永远是估计，这个是用你自己的身体实测出来的。
   数据门槛不达（饮食记录天数/体重点数不够）就不出数，宁缺毋滥。
3. 能量账本对账：区间累计缺口 ÷7700 = 理论体重变化，与实际趋势变化对账——
   差得远通常意味着饮食漏记或 BMR 失真，这是减脂期最诚实的一张卡。

去羞辱化原则（H5）：所有输出都是「趋势与调整」语言，不输出「超标/失败」。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BodyMetrics, DailyActivity, DietLog

KCAL_PER_KG = 7700  # 1kg 体脂 ≈ 7700 kcal
EMA_ALPHA_DAILY = 0.10  # 日衰减（≈10 天时间常数）

# TDEE 反推窗口与数据门槛
TDEE_WINDOW_DAYS = 28
TDEE_MIN_DIET_DAYS = 14
TDEE_MIN_WEIGHTS = 8
TDEE_MIN_SPAN_DAYS = 14


# ---------- 纯计算 ----------

def weight_trend(points: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """时间感知 EMA：测量断档 n 天时衰减按 (1-α)^n 补偿，趋势不因缺测跳变。
    入参需按日期升序；返回 (日期, 趋势值) 序列。"""
    if not points:
        return []
    out: list[tuple[date, float]] = [(points[0][0], points[0][1])]
    ema = points[0][1]
    prev_d = points[0][0]
    for d, v in points[1:]:
        gap = max((d - prev_d).days, 1)
        k = 1 - (1 - EMA_ALPHA_DAILY) ** gap
        ema += (v - ema) * k
        out.append((d, round(ema, 2)))
        prev_d = d
    return out


def estimate_tdee(
    trend: list[tuple[date, float]],
    intake_by_day: dict[date, float],
    start: date,
    end: date,
) -> dict[str, Any] | None:
    """窗口内 TDEE 反推。门槛：饮食 ≥14 天、体重 ≥8 点且首末跨度 ≥14 天。
    返回 {tdee, avg_intake, trend_change_kg, diet_days, span_days}；不达标 None。"""
    window_trend = [(d, v) for d, v in trend if start <= d <= end]
    intakes = [v for d, v in intake_by_day.items() if start <= d <= end]
    if len(window_trend) < TDEE_MIN_WEIGHTS or len(intakes) < TDEE_MIN_DIET_DAYS:
        return None
    span = (window_trend[-1][0] - window_trend[0][0]).days
    if span < TDEE_MIN_SPAN_DAYS:
        return None
    change_kg = window_trend[-1][1] - window_trend[0][1]
    avg_intake = sum(intakes) / len(intakes)
    tdee = avg_intake - change_kg * KCAL_PER_KG / span
    return {
        "tdee": round(tdee),
        "avg_intake": round(avg_intake),
        "trend_change_kg": round(change_kg, 2),
        "diet_days": len(intakes),
        "span_days": span,
    }


def suggest_target(tdee: float, rate_kgpw: float) -> int:
    """按目标速率给下周热量：TDEE + 速率×7700÷7（减重速率为负）。
    钳制在 TDEE±1000 且不低于 1200——极端目标直接砍到安全带。"""
    raw = tdee + rate_kgpw * KCAL_PER_KG / 7
    return round(max(1200, max(tdee - 1000, min(tdee + 1000, raw))))


# ---------- 取数 ----------

def weight_points(db: Session, start: date, end: date) -> list[tuple[date, float]]:
    rows = db.execute(
        select(BodyMetrics.log_date, BodyMetrics.weight_kg)
        .where(BodyMetrics.log_date.between(start, end), BodyMetrics.weight_kg.is_not(None))
        .order_by(BodyMetrics.log_date)
    ).all()
    return [(d, float(v)) for d, v in rows]


def intake_map(db: Session, start: date, end: date) -> dict[date, float]:
    rows = db.execute(
        select(DietLog.log_date, func.sum(DietLog.kcal))
        .where(DietLog.log_date.between(start, end), DietLog.kcal.is_not(None))
        .group_by(DietLog.log_date)
    ).all()
    return {d: float(v) for d, v in rows if v is not None}


def energy_ledger(db: Session, start: date, end: date) -> dict[str, Any] | None:
    """区间能量账本：累计缺口（摄入−(BMR+活动)，只累计「有饮食记录且 BMR 可知」
    的天）→ 理论体重变化，与实际趋势变化对账。没有可对账的天返回 None。"""
    intakes = intake_map(db, start, end)
    if not intakes:
        return None
    # BMR：截至各日最近一次体脂秤回填值——取全historia再逐日走指针
    bmr_rows = db.execute(
        select(BodyMetrics.log_date, BodyMetrics.bmr_kcal)
        .where(BodyMetrics.log_date <= end, BodyMetrics.bmr_kcal.is_not(None))
        .order_by(BodyMetrics.log_date)
    ).all()
    active_rows = {
        d: float(v)
        for d, v in db.execute(
            select(DailyActivity.log_date, DailyActivity.active_kcal).where(
                DailyActivity.log_date.between(start, end),
                DailyActivity.active_kcal.is_not(None),
            )
        )
    }
    gap_sum = 0.0
    gap_days = 0
    bi = 0
    cur_bmr: float | None = None
    for d in sorted(intakes):
        while bi < len(bmr_rows) and bmr_rows[bi][0] <= d:
            cur_bmr = float(bmr_rows[bi][1])
            bi += 1
        if cur_bmr is None:
            continue
        gap_sum += intakes[d] - (cur_bmr + active_rows.get(d, 0.0))
        gap_days += 1
    if gap_days == 0:
        return None
    # 实际变化：趋势线（带 30 天预热）在区间内的首末差；点太少退回原始首末差
    warm = weight_points(db, start - timedelta(days=30), end)
    trend = [(d, v) for d, v in weight_trend(warm) if d >= start]
    actual_kg = round(trend[-1][1] - trend[0][1], 2) if len(trend) >= 2 else None
    return {
        "gap_sum": round(gap_sum),
        "gap_days": gap_days,
        "predicted_kg": round(gap_sum / KCAL_PER_KG, 2),
        "actual_kg": actual_kg,
    }


def tdee_ctx(db: Session, today: date) -> dict[str, Any] | None:
    """周 Check-in 数据：TDEE 估算 + 按速率设置给出的下周建议热量。"""
    from app.models import AppSetting

    start = today - timedelta(days=TDEE_WINDOW_DAYS - 1)
    warm = weight_points(db, start - timedelta(days=30), today)
    trend = weight_trend(warm)
    est = estimate_tdee(trend, intake_map(db, start, today), start, today)
    if est is None:
        return None

    def _num_setting(key: str) -> float | None:
        row = db.get(AppSetting, key)
        v = row.value if row is not None else None
        if isinstance(v, dict):
            v = v.get("value")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    rate = _num_setting("energy_rate_kgpw")
    if rate is None:
        rate = -0.35  # 缺省温和减重（≈-385 kcal/日）
    current_target = _num_setting("target_kcal")
    suggested = suggest_target(est["tdee"], rate)
    return {
        **est,
        "rate_kgpw": rate,
        "current_target": round(current_target) if current_target else None,
        "suggested_target": suggested,
        # 建议与现目标差 <75 kcal 就不折腾（去羞辱化：不制造无意义的调整焦虑）
        "adjust_needed": current_target is None or abs(suggested - current_target) >= 75,
    }
