"""把三星自定义运动“起飞”解释为释放事件，并计算自律统计。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ReleaseLog
from app.timeutil import today_local

RELEASE_SESSION_NAMES = frozenset({"起飞"})


def is_release_session(session_type: Any, title: Any = None) -> bool:
    """精确匹配专用名称；不做模糊包含，避免误伤普通训练。"""
    values = {str(v or "").strip().casefold() for v in (session_type, title)}
    return bool(values & {name.casefold() for name in RELEASE_SESSION_NAMES})


def discipline_summary(
    db: Session,
    day: date | None = None,
) -> dict[str, Any]:
    """当前/最长无释放天数及近月计数；同日多次按事件数统计。"""
    day = day or today_local()
    latest = db.execute(
        select(ReleaseLog)
        .where(ReleaseLog.log_date <= day)
        .order_by(ReleaseLog.started_at.desc().nullslast(), ReleaseLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    release_days = sorted({
        d for (d,) in db.execute(
            select(ReleaseLog.log_date).where(ReleaseLog.log_date <= day)
        )
    })

    current_days = (day - latest.log_date).days if latest is not None else None
    longest_days = current_days or 0
    for previous, current in zip(release_days, release_days[1:]):
        longest_days = max(longest_days, max(0, (current - previous).days - 1))

    month_start = day.replace(day=1)
    month_count = db.execute(
        select(func.count()).select_from(ReleaseLog).where(
            ReleaseLog.log_date.between(month_start, day)
        )
    ).scalar_one()
    recent_count = db.execute(
        select(func.count()).select_from(ReleaseLog).where(
            ReleaseLog.log_date.between(day - timedelta(days=29), day)
        )
    ).scalar_one()
    total_count = db.execute(
        select(func.count()).select_from(ReleaseLog).where(ReleaseLog.log_date <= day)
    ).scalar_one()
    return {
        "latest": latest,
        "current_days": current_days,
        "longest_days": longest_days,
        "month_count": int(month_count),
        "recent_count": int(recent_count),
        "total_count": int(total_count),
    }
