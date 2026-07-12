"""力量组次明细（workout_logs.detail["strength"]）：入库校验、行摘要、动作 PR（Hevy 式）。

明细结构（手动表单「动作明细」折叠区，Alpine 动态行序列化成 JSON）：
  detail["strength"] = [
    {"exercise": "标准俯卧撑", "sets": [{"reps": 15}, {"reps": 12}]},
    {"exercise": "壶铃摇摆", "sets": [{"reps": 15, "weight_kg": 16.0}, ...]},
  ]
weight_kg 可空（自重动作）。

PR 口径（exercise_prs）：
- max_reps    单组最大次数
- max_weight  单组最大重量（有重量的动作）
- max_volume  单组最大容量 reps × weight
- ready       某天该动作 ≥3 组且每组 ≥15 次 → 自重进阶链常用「3×15 升级」标准，
              动作库页据此提示可试同链下一 level
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WorkoutLog

ADVANCE_SETS = 3
ADVANCE_REPS = 15
MAX_ITEMS = 20
MAX_SETS = 12

# 跑步判定（与 metrics 页跑步图同口径）
RUN_KEYWORDS = ("跑", "run")
BEST_PACE_MIN_KM = 3.0  # 最快均配速只认 ≥3km 的跑（太短的冲刺没有可比性）


def _is_run(session_type: str | None) -> bool:
    s = (session_type or "").lower()
    return any(k in s for k in RUN_KEYWORDS)


def cardio_prs(db: Session) -> dict[str, Any] | None:
    """跑步 PR（V7 B4）：最长单次距离 / 最快均配速（≥3km）/ 单月最大跑量 / 累计。
    没有任何带距离的跑步记录返回 None。纯 sRPE 之外的「客观进步」证据。"""
    rows = db.execute(
        select(
            WorkoutLog.log_date, WorkoutLog.session_type,
            WorkoutLog.duration_min, WorkoutLog.distance_km,
        ).where(WorkoutLog.distance_km.is_not(None))
    ).all()
    runs = [
        (d, float(km), dur)
        for d, stype, dur, km in rows
        if _is_run(stype) and km and float(km) > 0
    ]
    if not runs:
        return None
    longest = max(runs, key=lambda r: r[1])
    paced = [
        (d, km, dur / km) for d, km, dur in runs
        if dur and km >= BEST_PACE_MIN_KM
    ]
    best_pace = min(paced, key=lambda r: r[2]) if paced else None
    by_month: dict[str, float] = {}
    for d, km, _dur in runs:
        key = f"{d:%Y-%m}"
        by_month[key] = by_month.get(key, 0.0) + km
    top_month = max(by_month.items(), key=lambda kv: kv[1])
    return {
        "total_km": round(sum(km for _, km, _d in runs), 1),
        "runs": len(runs),
        "longest_km": round(longest[1], 2),
        "longest_date": longest[0],
        "best_pace_min_per_km": round(best_pace[2], 2) if best_pace else None,
        "best_pace_date": best_pace[0] if best_pace else None,
        "best_pace_km": round(best_pace[1], 2) if best_pace else None,
        "top_month": top_month[0],
        "top_month_km": round(top_month[1], 1),
    }


def normalize_strength(raw: Any) -> list[dict[str, Any]] | None:
    """表单 JSON → 校验后的明细；无有效内容返回 None（不入库）。"""
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for item in raw[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("exercise") or "").strip()[:30]
        sets_in = item.get("sets")
        if not name or not isinstance(sets_in, list):
            continue
        sets: list[dict[str, Any]] = []
        for s in sets_in[:MAX_SETS]:
            if not isinstance(s, dict):
                continue
            try:
                reps = int(s.get("reps"))
            except (TypeError, ValueError):
                continue
            if not (1 <= reps <= 999):
                continue
            one: dict[str, Any] = {"reps": reps}
            w = s.get("weight_kg")
            if w not in (None, ""):
                try:
                    wf = round(float(w), 1)
                except (TypeError, ValueError):
                    wf = None
                if wf is not None and 0 < wf <= 500:
                    one["weight_kg"] = wf
            sets.append(one)
        if sets:
            out.append({"exercise": name, "sets": sets})
    return out or None


def strength_lines(detail: Any) -> list[str]:
    """行内摘要：['标准俯卧撑 15/12/10', '壶铃摇摆 16kg×15×3']（模板全局函数）。"""
    if not isinstance(detail, dict):
        return []
    lines: list[str] = []
    for item in detail.get("strength") or []:
        if not isinstance(item, dict):
            continue
        sets = [s for s in (item.get("sets") or []) if isinstance(s, dict) and s.get("reps")]
        if not sets:
            continue
        reps = [str(s["reps"]) for s in sets]
        weights = {s.get("weight_kg") for s in sets}
        w = weights.pop() if len(weights) == 1 else None
        if w and len(set(reps)) == 1:
            seg = f"{w:g}kg×{reps[0]}×{len(reps)}"
        elif w:
            seg = f"{w:g}kg {'/'.join(reps)}"
        else:
            seg = "/".join(reps)
        lines.append(f"{item.get('exercise')} {seg}")
    return lines


def exercise_prs(db: Session) -> dict[str, dict[str, Any]]:
    """全库扫描 detail.strength → 每动作 PR（单用户数据量小，直接全取）。"""
    prs: dict[str, dict[str, Any]] = {}
    rows = db.execute(
        select(WorkoutLog.log_date, WorkoutLog.detail).where(WorkoutLog.detail.is_not(None))
    ).all()
    for log_date, detail in rows:
        if not isinstance(detail, dict):
            continue
        for item in detail.get("strength") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("exercise") or "").strip()
            sets = [s for s in (item.get("sets") or []) if isinstance(s, dict)]
            if not name or not sets:
                continue
            pr = prs.setdefault(name, {
                "max_reps": 0, "max_weight": None, "max_volume": None,
                "sessions": 0, "ready": False, "last_date": None,
            })
            pr["sessions"] += 1
            if pr["last_date"] is None or log_date > pr["last_date"]:
                pr["last_date"] = log_date
            good_sets = 0
            for s in sets:
                reps = s.get("reps") or 0
                if not isinstance(reps, int):
                    continue
                w = s.get("weight_kg")
                pr["max_reps"] = max(pr["max_reps"], reps)
                if isinstance(w, (int, float)) and w > 0:
                    pr["max_weight"] = max(pr["max_weight"] or 0, w)
                    pr["max_volume"] = max(pr["max_volume"] or 0, round(reps * w, 1))
                if reps >= ADVANCE_REPS:
                    good_sets += 1
            if good_sets >= ADVANCE_SETS:
                pr["ready"] = True
    return prs
