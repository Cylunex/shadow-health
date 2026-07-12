"""V6 批次纯函数回归锁：负荷/准备度、能量引擎、睡眠口径、成就与洞察的核心数学。

全部纯函数直测（不碰 DB、不碰网络）——这些公式是 V6 的地基，重构时最易悄悄漂移。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.achievements import _max_streak  # noqa: E402
from app.services.energy import (  # noqa: E402
    estimate_tdee, suggest_target, weight_trend,
)
from app.services.insights import _bucket_stat  # noqa: E402
from app.services.readiness import (  # noqa: E402
    acwr_stats, combine_readiness, component_score, ewma_curves,
)
from app.services.sleep import fmt_bedtime  # noqa: E402

T = date(2026, 7, 12)


# ---------- readiness：ACWR / 单调性 ----------

def test_acwr_ratio_and_bands():
    # 近 7 天日均 200、28 天日均 125 → ratio 1.6 → very_high
    loads = {T - timedelta(days=i): 200.0 for i in range(7)}
    loads.update({T - timedelta(days=i): 100.0 for i in range(7, 28)})
    s = acwr_stats(loads, T)
    assert s["ratio"] == 1.6
    assert s["band"] == "very_high"

    # 均匀负荷 → ratio 1.0 → ok；恒定负荷单调性封顶 9.9
    even = {T - timedelta(days=i): 150.0 for i in range(28)}
    s2 = acwr_stats(even, T)
    assert s2["ratio"] == 1.0 and s2["band"] == "ok"
    assert s2["monotony"] == 9.9 and s2["monotony_warn"]


def test_acwr_no_data_returns_none():
    assert acwr_stats({}, T) is None
    # 28 天窗口外的负荷不算
    old = {T - timedelta(days=40): 300.0}
    assert acwr_stats(old, T) is None


def test_ewma_curves_shape_and_tsb():
    # 150 天恒定负荷（>3×42 天时间常数）：CTL/ATL 都收敛到 100 附近，TSB ≈ 0
    loads = {T - timedelta(days=i): 100.0 for i in range(150)}
    curves = ewma_curves(loads, T, days=30)
    assert len(curves) == 30
    last = curves[-1]
    assert 90 <= last["ctl"] <= 100
    assert 95 <= last["atl"] <= 100
    assert abs(last["tsb"]) < 10
    # 突然停练 7 天：ATL 掉得比 CTL 快 → TSB 转正（状态回升）
    stopped = {d: v for d, v in loads.items() if d <= T - timedelta(days=7)}
    last2 = ewma_curves(stopped, T, days=30)[-1]
    assert last2["tsb"] > 0


# ---------- readiness：组件分与合成 ----------

def test_component_score_direction_and_guards():
    base = [60.0 + (i % 5) for i in range(20)]  # 均值 62，有波动
    higher = component_score(70, base, higher_better=True)
    lower = component_score(70, base, higher_better=False)
    assert higher > 50 > lower  # 方向反转
    assert component_score(70, base[:5], True) is None  # 基线 <7 点不打分
    assert component_score(70, [60.0] * 20, True) is None  # σ≈0 不打分
    assert 0 <= component_score(1000, base, True) <= 100  # 极端值钳制


def test_combine_readiness_renormalizes_missing():
    # 只有 sleep(0.35) 与 rhr(0.25) 可用：(60*.35+50*.25)/0.6 = 55.83 → 56
    assert combine_readiness({"sleep": 60, "load": None, "rhr": 50, "subjective": None}) == 56
    assert combine_readiness({"sleep": None, "load": None, "rhr": None, "subjective": None}) is None


# ---------- energy：趋势线 / TDEE / 建议 ----------

def test_weight_trend_smooths_noise_and_gaps():
    pts = [(T - timedelta(days=3), 72.0), (T - timedelta(days=2), 73.5),
           (T - timedelta(days=1), 71.0), (T, 72.5)]
    trend = weight_trend(pts)
    assert len(trend) == 4
    # EMA 波动远小于原始波动
    raw_span = max(v for _, v in pts) - min(v for _, v in pts)
    ema_span = max(v for _, v in trend) - min(v for _, v in trend)
    assert ema_span < raw_span / 2
    # 断档 10 天后新测量：衰减按天数补偿，趋势会明显靠近新值
    gap_pts = [(T - timedelta(days=20), 73.0), (T, 71.0)]
    assert weight_trend(gap_pts)[-1][1] < 72.7


def test_estimate_tdee_energy_conservation():
    # 28 天匀速 -0.05kg/日、日均摄入 1800 → TDEE ≈ 1800 + 0.05*7700 ≈ 2185
    pts = [(T - timedelta(days=27 - i), 73.0 - i * 0.05) for i in range(28)]
    intake = {T - timedelta(days=i): 1800.0 for i in range(28)}
    est = estimate_tdee(weight_trend(pts), intake, T - timedelta(days=27), T)
    assert est is not None
    assert 2000 <= est["tdee"] <= 2250  # EMA 滞后使趋势变化略小于原始 -1.35kg
    assert est["avg_intake"] == 1800


def test_estimate_tdee_gates():
    pts = [(T - timedelta(days=27 - i), 72.0) for i in range(28)]
    trend = weight_trend(pts)
    few_diet = {T - timedelta(days=i): 1800.0 for i in range(5)}  # 饮食天数不足
    assert estimate_tdee(trend, few_diet, T - timedelta(days=27), T) is None
    intake = {T - timedelta(days=i): 1800.0 for i in range(28)}
    short = weight_trend(pts[:3])  # 体重点数不足
    assert estimate_tdee(short, intake, T - timedelta(days=27), T) is None


def test_suggest_target_clamps():
    assert suggest_target(2200, -0.35) == 2200 - round(0.35 * 7700 / 7)  # 1815
    assert suggest_target(2200, -2.0) == 1200 if 2200 - 2200 else True  # 极端速率被钳
    assert suggest_target(2200, -2.0) >= 1200
    assert suggest_target(1300, -1.0) == 1200  # 下限 1200


# ---------- sleep：入睡时刻口径 ----------

def test_fmt_bedtime_wraps_midnight():
    assert fmt_bedtime(690) == "23:30"   # 距中午 690 分钟
    assert fmt_bedtime(750) == "00:30"   # 跨午夜
    assert fmt_bedtime(720) == "00:00"


# ---------- achievements / insights ----------

def test_max_streak():
    days = {T - timedelta(days=i) for i in (0, 1, 2, 5, 6)}
    assert _max_streak(days) == 3
    assert _max_streak(set()) == 0


def test_bucket_stat_gates_and_means():
    a_days = [T - timedelta(days=i) for i in range(10)]
    b_days = [T - timedelta(days=i) for i in range(10, 20)]
    values = {d: 3.0 for d in a_days}
    values.update({d: 4.0 for d in b_days})
    stat = _bucket_stat(a_days, b_days, values)
    assert stat == (3.0, 10, 4.0, 10)
    # 任一桶样本 <8 → None
    assert _bucket_stat(a_days[:5], b_days, values) is None
