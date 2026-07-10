"""睡眠会话跨源去重（审查修复）。

同一夜可能同时存在 samsung_zip / health_connect / samsung_direct 三种 source 的
会话记录（zip 历史与直读回溯窗口重叠、双通道并存等），直接按 wake_date 求和会翻倍。
所有"按夜汇总/展示"的读取都必须经本模块：每夜只取优先级最高的单一 source
（同 source 多条 = 分段睡眠，保留求和）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SleepSession

# 新鲜度优先：直读（上游可修订）> HC 实时 > zip 历史 > 其他
SOURCE_PRIORITY = ("samsung_direct", "health_connect", "samsung_zip")


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
