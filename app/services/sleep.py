"""睡眠会话跨源去重（审查修复）。

同一夜可能同时存在 samsung_zip / health_connect / samsung_direct 三种 source 的
会话记录（zip 历史与直读回溯窗口重叠、双通道并存等），直接按 wake_date 求和会翻倍。
所有"按夜汇总/展示"的读取都必须经本模块：每夜只取优先级最高的单一 source
（同 source 多条 = 分段睡眠，保留求和）。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SleepSession
from app.timeutil import LOCAL_TZ

# 新鲜度优先：直读（上游可修订）> HC 实时 > zip 历史 > 其他
SOURCE_PRIORITY = ("samsung_direct", "health_connect", "samsung_zip")

SLEEP_OK_MIN = 7 * 60  # 「睡够」判定线（≥7h，与 AI 建议口径一致）


def _rank(source: str | None) -> int:
    try:
        return SOURCE_PRIORITY.index(source or "")
    except ValueError:
        return len(SOURCE_PRIORITY)


def pick_preferred(rows: list) -> dict[date, list]:
    """纯函数：按 wake_date 分组并只保留优先 source 的会话（同 source 多条保留）。

    入参对象只需有 .wake_date 与 .source 属性（便于单测）。
    """
    by_day: dict[date, list] = {}
    for s in rows:
        cur = by_day.get(s.wake_date)
        if not cur:
            by_day[s.wake_date] = [s]
            continue
        old_rank, new_rank = _rank(cur[0].source), _rank(s.source)
        if new_rank < old_rank:
            by_day[s.wake_date] = [s]
        elif new_rank == old_rank:
            cur.append(s)
    return by_day


def sessions_by_date(db: Session, start: date, end: date) -> dict[date, list[SleepSession]]:
    """wake_date -> 该夜优先 source 的全部会话。"""
    rows = db.execute(
        select(SleepSession).where(SleepSession.wake_date.between(start, end))
    ).scalars().all()
    return pick_preferred(list(rows))


def total_sleep_min(db: Session, d: date) -> int:
    """该夜总睡眠分钟（优先 source 内求和；无数据返回 0）。"""
    sessions = sessions_by_date(db, d, d).get(d, [])
    return sum(s.total_sleep_min or 0 for s in sessions)


# ---------- 睡眠质量洞察（V6 P1）----------

def _bedtime_min_from_noon(dt) -> int:
    """入睡时刻 → 距中午 12:00 的分钟数（0~1439）。以中午为原点是为了让
    23:30 与 00:30 落在同一段连续数轴上（690 与 750），跨午夜可直接算均值/标准差。"""
    local = dt.astimezone(LOCAL_TZ)
    return (local.hour * 60 + local.minute - 720) % 1440


def night_rows(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    """每夜（跨源去重后）质量行：总睡眠/在床/效率/分期/入睡时刻。

    效率 = 总睡眠 ÷ 在床时长（分段会话求和），钳到 100%；
    bedtime_min = 最早一段的开始时刻距中午分钟数（主段入睡）。
    """
    rows: list[dict[str, Any]] = []
    for d, sessions in sorted(sessions_by_date(db, start, end).items()):
        total = sum(s.total_sleep_min or 0 for s in sessions)
        if total <= 0:
            continue
        in_bed = sum(
            max(int((s.end_at - s.start_at).total_seconds() // 60), 0) for s in sessions
        )
        stages: dict[str, int | None] = {}
        for field in ("deep_min", "light_min", "rem_min", "awake_min"):
            vals = [getattr(s, field) for s in sessions if getattr(s, field) is not None]
            stages[field] = sum(vals) if vals else None
        rows.append({
            "date": d,
            "total_min": total,
            "in_bed_min": in_bed,
            "efficiency_pct": min(round(total * 100 / in_bed), 100) if in_bed > 0 else None,
            **stages,
            "bedtime_min": _bedtime_min_from_noon(min(s.start_at for s in sessions)),
        })
    return rows


def fmt_bedtime(mins_from_noon: float) -> str:
    """距中午分钟数 → HH:MM 时钟显示。"""
    clock = (int(round(mins_from_noon)) + 720) % 1440
    return f"{clock // 60:02d}:{clock % 60:02d}"


def stage_stats(db: Session, start: date, end: date) -> dict[str, Any] | None:
    """区间睡眠质量汇总：均时长/效率/深睡 REM 占比/达标夜数/就寝规律性。
    无任何有效夜返回 None。"""
    rows = night_rows(db, start, end)
    if not rows:
        return None
    nights = len(rows)
    total_sum = sum(r["total_min"] for r in rows)
    eff = [r["efficiency_pct"] for r in rows if r["efficiency_pct"] is not None]
    deep = [(r["deep_min"], r["total_min"]) for r in rows if r["deep_min"] is not None]
    rem = [(r["rem_min"], r["total_min"]) for r in rows if r["rem_min"] is not None]
    bedtimes = [r["bedtime_min"] for r in rows]
    mean_bt = sum(bedtimes) / nights
    std_bt = (sum((b - mean_bt) ** 2 for b in bedtimes) / nights) ** 0.5
    return {
        "nights": nights,
        "avg_sleep_h": round(total_sum / nights / 60, 1),
        "sleep_ok_days": sum(1 for r in rows if r["total_min"] >= SLEEP_OK_MIN),
        "avg_efficiency_pct": round(sum(eff) / len(eff)) if eff else None,
        "deep_pct": round(sum(d for d, _ in deep) * 100 / sum(t for _, t in deep)) if deep else None,
        "rem_pct": round(sum(r_ for r_, _ in rem) * 100 / sum(t for _, t in rem)) if rem else None,
        "avg_bedtime": fmt_bedtime(mean_bt),
        "bedtime_std_min": round(std_bt),
    }
