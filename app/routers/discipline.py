"""自律 / 释放记录：三星“起飞”专用事件的独立查看页。"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import local_hm, require_login, templates
from app.models import ReleaseLog
from app.services.discipline import discipline_summary
from app.timeutil import today_local

router = APIRouter(dependencies=[Depends(require_login)])


def _calendar(db: Session, today: date, days: int = 91) -> list[dict]:
    start = today - timedelta(days=days - 1)
    start -= timedelta(days=start.isoweekday() - 1)
    counts = dict(db.execute(
        select(ReleaseLog.log_date, func.count())
        .where(ReleaseLog.log_date.between(start, today))
        .group_by(ReleaseLog.log_date)
    ).all())
    weeks = []
    cursor = start
    while cursor <= today:
        week_days = []
        for offset in range(7):
            d = cursor + timedelta(days=offset)
            week_days.append({
                "date": d,
                "future": d > today,
                "count": int(counts.get(d, 0)),
            })
        weeks.append({"days": week_days, "month": f"{cursor.month}月"})
        cursor += timedelta(days=7)
    return weeks


def _page_ctx(db: Session, day: date | None = None) -> dict:
    day = day or today_local()
    recent = db.execute(
        select(ReleaseLog)
        .order_by(ReleaseLog.log_date.desc(), ReleaseLog.started_at.desc().nullslast(), ReleaseLog.id.desc())
        .limit(30)
    ).scalars().all()
    return {
        "summary": discipline_summary(db, day),
        "weeks": _calendar(db, day),
        "recent": recent,
        "local_hm": local_hm,
    }


@router.get("/discipline")
def discipline_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "discipline.html", _page_ctx(db))
