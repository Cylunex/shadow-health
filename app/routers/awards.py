"""成就徽章墙（V6 H1）：GET /achievements——实时计算 + 首达日期落档。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.services.achievements import sync_and_list
from app.timeutil import today_local

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/achievements")
def achievements_page(request: Request, db: Session = Depends(get_db)):
    items, _newly = sync_and_list(db, today_local())
    earned = [i for i in items if i["earned"]]
    locked = [i for i in items if not i["earned"]]
    return templates.TemplateResponse(
        request, "achievements.html", {"earned": earned, "locked": locked}
    )
