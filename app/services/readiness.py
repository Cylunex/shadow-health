"""训练负荷模型与每日准备度（V6 P3）——全部纯本地统计，零外网依赖。

三块能力（依据：Apple Training Load / Intervals.icu / Fitbit Readiness /
三星 Energy Score 的公开思路，输入全是库里已有数据）：
1. 负荷模型：sRPE 日负荷（RPE×分钟）之上算 ACWR（急慢性负荷比：7 天均 ÷ 28 天均，
   0.8~1.3 安全带、>1.5 受伤风险预警）、Foster 单调性（7 天均÷标准差，>2 提示
   训练太单调）、CTL/ATL/TSB 指数加权曲线（42/7 天时间常数，体能-疲劳-状态）。
2. 静息心率基线：daily_activity.hr_min 作 RHR 代理，7 天均 vs 28 天基线抬升
   ≥5 bpm 提示疲劳/前驱感冒/过度训练。
3. 准备度分数：昨夜睡眠 / 前日负荷 / RHR / 主观（精力·心情）各对自身 28 天
   基线做 z 分数 → 映射 0~100 → 加权合成；缺哪项就把权重摊给其余项，
   全缺则不出分（宁缺毋滥，不编数）。

纯计算函数不碰 DB（pytest 直测口径）；*_ctx 薄包装负责取数。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BodyMetrics, DailyActivity, WorkoutLog
from app.services import sleep as sleep_service
from app.timeutil import today_local

# ACWR 分档（急慢性负荷比）
ACWR_BANDS = (
    (0.8, "low", "负荷偏低，可以加量"),
    (1.3, "ok", "负荷适中，保持"),
    (1.5, "high", "负荷偏高，注意恢复"),
    (float("inf"), "very_high", "加量过快（>1.5），谨防受伤，建议减量"),
)
MONOTONY_WARN = 2.0  # Foster 单调性阈值
RHR_ELEVATED_BPM = 5  # 7 天均值较 28 天基线抬升告警线
CTL_TC, ATL_TC = 42, 7  # EWMA 时间常数（天）

# 准备度组件权重（缺项时按可用项重新归一）
READINESS_WEIGHTS = {"sleep": 0.35, "load": 0.25, "rhr": 0.25, "subjective": 0.15}


# ---------- 纯计算 ----------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _std(xs: list[float]) -> float:
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def acwr_stats(loads: dict[date, float], today: date) -> dict[str, Any] | None:
    """ACWR + 单调性。loads = 日负荷（无训练的天视为 0）。
    28 天内完全无负荷返回 None（没得比）。"""
    acute = [loads.get(today - timedelta(days=i), 0.0) for i in range(7)]
    chronic = [loads.get(today - timedelta(days=i), 0.0) for i in range(28)]
    if not any(chronic):
        return None
    acute_mean, chronic_mean = _mean(acute), _mean(chronic)
    ratio = round(acute_mean / chronic_mean, 2) if chronic_mean > 0 else None
    band = label = None
    if ratio is not None:
        for hi, b, text in ACWR_BANDS:
            if ratio < hi:
                band, label = b, text
                break
    std7 = _std(acute)
    if std7 > 0.01:
        monotony = min(round(acute_mean / std7, 1), 9.9)  # 封顶防天文数字
    else:
        # σ≈0：完全没练（均值 0）无单调性可言；天天同量 = 最单调，取封顶值
        monotony = None if acute_mean < 1 else 9.9
    return {
        "acute_daily": round(acute_mean),
        "chronic_daily": round(chronic_mean),
        "ratio": ratio,
        "band": band,
        "band_label": label,
        "monotony": monotony,
        "monotony_warn": monotony is not None and monotony > MONOTONY_WARN,
    }


def ewma_curves(loads: dict[date, float], end: date, days: int = 90) -> list[dict[str, Any]]:
    """CTL/ATL/TSB 曲线。窗口前预热 3×CTL_TC 天（EWMA 需 ~3τ 才收敛，预热
    不足首点会系统性偏低）；再往前的负荷影响 <5%，忽略。
    TSB（状态）= 昨日 CTL − 昨日 ATL 的今日投影，这里取当日 CTL−ATL 简化口径。"""
    k_ctl = 1 - math.exp(-1 / CTL_TC)
    k_atl = 1 - math.exp(-1 / ATL_TC)
    start = end - timedelta(days=days - 1)
    warm_start = start - timedelta(days=3 * CTL_TC)
    ctl = atl = 0.0
    out: list[dict[str, Any]] = []
    d = warm_start
    while d <= end:
        load = loads.get(d, 0.0)
        ctl += (load - ctl) * k_ctl
        atl += (load - atl) * k_atl
        if d >= start:
            out.append({
                "date": d,
                "ctl": round(ctl, 1),
                "atl": round(atl, 1),
                "tsb": round(ctl - atl, 1),
            })
        d += timedelta(days=1)
    return out


def component_score(value: float, baseline: list[float], higher_better: bool) -> int | None:
    """单组件 0~100：对 28 天基线做 z 分数，50 为基线均值，每 1σ 走 20 分。
    基线少于 7 个点或几乎无波动（σ≈0）时不打分。"""
    if len(baseline) < 7:
        return None
    m, s = _mean(baseline), _std(baseline)
    if s < 1e-6:
        return None
    z = (value - m) / s
    if not higher_better:
        z = -z
    return max(0, min(100, round(50 + 20 * z)))


def combine_readiness(components: dict[str, int | None]) -> int | None:
    """加权合成；缺项把权重摊给可用项，全缺返回 None。"""
    avail = {k: v for k, v in components.items() if v is not None}
    if not avail:
        return None
    total_w = sum(READINESS_WEIGHTS[k] for k in avail)
    return round(sum(v * READINESS_WEIGHTS[k] for k, v in avail.items()) / total_w)


# ---------- 取数包装 ----------

def daily_loads(db: Session, start: date, end: date) -> dict[date, float]:
    """sRPE 日负荷（只有带 RPE 的记录计入，与周负荷卡口径一致）。"""
    rows = db.execute(
        select(WorkoutLog.log_date, WorkoutLog.duration_min, WorkoutLog.rpe).where(
            WorkoutLog.log_date.between(start, end),
            WorkoutLog.rpe.is_not(None),
            WorkoutLog.duration_min.is_not(None),
        )
    ).all()
    out: dict[date, float] = {}
    for d, dur, rpe in rows:
        out[d] = out.get(d, 0.0) + rpe * dur
    return out


def rhr_status(db: Session, today: date) -> dict[str, Any] | None:
    """静息心率（hr_min 代理）7 天均 vs 28 天基线。数据不足返回 None。"""
    rows = db.execute(
        select(DailyActivity.log_date, DailyActivity.hr_min).where(
            DailyActivity.log_date >= today - timedelta(days=27),
            DailyActivity.hr_min.is_not(None),
        )
    ).all()
    base = [float(v) for _, v in rows]
    cur = [float(v) for d, v in rows if d >= today - timedelta(days=6)]
    if len(base) < 10 or len(cur) < 3:
        return None
    delta = round(_mean(cur) - _mean(base), 1)
    return {
        "cur7": round(_mean(cur)),
        "base28": round(_mean(base)),
        "delta": delta,
        "elevated": delta >= RHR_ELEVATED_BPM,
    }


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def vitals_alert(db: Session, today: date | None = None) -> dict[str, Any] | None:
    """夜间体征联合预警（V6 E1，Apple Vitals 思路）：每指标 28 天滚动
    中位数±3×MAD 定「个人典型区间」，只看坏方向越界（睡眠过短/静息心率
    偏高/血氧偏低），**≥2 项同时越界才报**——找系统性变化，不为单点噪声吵人。"""
    today = today or today_local()
    start = today - timedelta(days=27)
    hits: list[dict[str, Any]] = []

    def _check(label: str, series: dict[date, float], bad_high: bool, unit: str = "") -> None:
        latest_day = max((d for d in series if d >= today - timedelta(days=1)), default=None)
        if latest_day is None:
            return
        baseline = [v for d, v in series.items() if d != latest_day]
        if len(baseline) < 10:
            return
        med = _median(baseline)
        mad = _median([abs(v - med) for v in baseline])
        if mad < 1e-6:
            return
        v = series[latest_day]
        if bad_high and v > med + 3 * mad:
            hits.append({"label": label, "value": f"{v:g}{unit}", "typical": f"≤{med + 3 * mad:.1f}{unit}"})
        elif not bad_high and v < med - 3 * mad:
            hits.append({"label": label, "value": f"{v:g}{unit}", "typical": f"≥{med - 3 * mad:.1f}{unit}"})

    nights = sleep_service.night_rows(db, start, today)
    _check("睡眠时长", {r["date"]: round(r["total_min"] / 60, 1) for r in nights},
           bad_high=False, unit="h")
    hr_rows = db.execute(
        select(DailyActivity.log_date, DailyActivity.hr_min).where(
            DailyActivity.log_date >= start, DailyActivity.hr_min.is_not(None)
        )
    ).all()
    _check("静息心率", {d: float(v) for d, v in hr_rows}, bad_high=True, unit="bpm")
    spo2_rows = db.execute(
        select(BodyMetrics.log_date, BodyMetrics.spo2_pct).where(
            BodyMetrics.log_date >= start, BodyMetrics.spo2_pct.is_not(None)
        )
    ).all()
    _check("血氧", {d: float(v) for d, v in spo2_rows}, bad_high=False, unit="%")

    if len(hits) < 2:
        return None
    return {
        "items": hits,
        "text": "、".join(f"{h['label']} {h['value']}（典型 {h['typical']}）" for h in hits),
    }


def _suggestion(score: int, rhr_elevated: bool, acwr: dict[str, Any] | None) -> str:
    """规则化建议（离线模板；措辞中性，不指责）。"""
    if rhr_elevated:
        return "静息心率高于基线，身体可能在扛压（疲劳/前驱感冒），今天以拉伸、慢走等恢复性活动为主"
    if acwr and acwr.get("band") == "very_high":
        return "近一周加量偏快，今天控制强度，优先技术与拉伸"
    if score >= 70:
        return "状态在线，适合安排爆发循环或加量的力量训练"
    if score >= 40:
        return "状态平稳，按计划正常训练即可"
    return "恢复优先：今天适合拉伸、散步或完全休息，睡个好觉比多练一次更值"


def readiness_ctx(db: Session, today: date | None = None) -> dict[str, Any] | None:
    """每日准备度：分数 + 组件明细 + 建议 + RHR 告警。数据全缺返回 None。"""
    today = today or today_local()
    base_start = today - timedelta(days=28)

    # 1. 昨夜睡眠时长 vs 28 天基线（多睡=好）
    nights = sleep_service.night_rows(db, base_start, today)
    sleep_score = None
    last_night = next((r for r in reversed(nights) if r["date"] in (today, today - timedelta(days=1))), None)
    if last_night is not None:
        baseline = [float(r["total_min"]) for r in nights if r["date"] != last_night["date"]]
        sleep_score = component_score(float(last_night["total_min"]), baseline, higher_better=True)

    # 2. 前日负荷 vs 28 天基线（练得狠=今天该缓，低分；无训练天计 0 参与基线）
    loads = daily_loads(db, base_start, today)
    load_score = None
    if loads:
        y = today - timedelta(days=1)
        baseline = [loads.get(base_start + timedelta(days=i), 0.0) for i in range((y - base_start).days)]
        load_score = component_score(loads.get(y, 0.0), baseline, higher_better=False)

    # 3. RHR（低=恢复好）
    rhr = rhr_status(db, today)
    rhr_score = None
    if rhr is not None:
        rows = db.execute(
            select(DailyActivity.log_date, DailyActivity.hr_min).where(
                DailyActivity.log_date >= base_start, DailyActivity.hr_min.is_not(None)
            ).order_by(DailyActivity.log_date)
        ).all()
        if rows:
            latest = float(rows[-1][1])
            baseline = [float(v) for _, v in rows[:-1]]
            rhr_score = component_score(latest, baseline, higher_better=False)

    # 4. 主观（昨天的精力/心情均值 vs 基线）
    subj_score = None
    rows = db.execute(
        select(BodyMetrics.log_date, BodyMetrics.energy_level, BodyMetrics.mood_score).where(
            BodyMetrics.log_date >= base_start,
        ).order_by(BodyMetrics.log_date)
    ).all()
    subj = {
        d: _mean([float(v) for v in (e, m) if v is not None])
        for d, e, m in rows if e is not None or m is not None
    }
    y = today - timedelta(days=1)
    latest_subj = subj.get(today, subj.get(y))
    if latest_subj is not None:
        baseline = [v for d, v in subj.items() if d not in (today, y)]
        subj_score = component_score(latest_subj, baseline, higher_better=True)

    components = {
        "sleep": sleep_score, "load": load_score, "rhr": rhr_score, "subjective": subj_score,
    }
    score = combine_readiness(components)
    if score is None:
        return None
    acwr = acwr_stats(loads, today)
    band = "high" if score >= 70 else ("mid" if score >= 40 else "low")
    labels = {"sleep": "昨夜睡眠", "load": "前日负荷", "rhr": "静息心率", "subjective": "主观状态"}
    return {
        "score": score,
        "band": band,
        "band_label": {"high": "状态好", "mid": "平稳", "low": "先恢复"}[band],
        "components": [
            {"key": k, "label": labels[k], "score": v}
            for k, v in components.items() if v is not None
        ],
        "rhr": rhr,
        "suggestion": _suggestion(score, bool(rhr and rhr["elevated"]), acwr),
    }
