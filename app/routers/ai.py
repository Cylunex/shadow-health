"""Bounded interactive AI plus restart-safe deterministic analysis tasks."""
from __future__ import annotations

import html
import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import prefixed, require_login, templates
from app.models import HealthTask
from app.services import health_companion as hc, llm
from app.timeutil import today_local

router = APIRouter(prefix="/ai", dependencies=[Depends(require_login)])
_DAYS_OPTIONS = (7, 30, 90)


def _render_md(content: str) -> str:
    # Model output is untrusted. No executable HTML or unsafe links.
    return "<p>" + html.escape(content).replace("\n", "<br>") + "</p>"


@router.get("")
def ai_page(request: Request, db: Session = Depends(get_db)):
    cfg = llm.get_config(db)
    task = db.scalar(select(HealthTask).where(HealthTask.owner == hc.owner_id(request),
        HealthTask.kind == "analysis").order_by(HealthTask.created_at.desc()).limit(1))
    return templates.TemplateResponse(request, "ai.html", {
        "configured": cfg["configured"], "model": cfg["model"],
        "provider_label": llm.PROVIDER_LABELS[cfg["provider"]],
        "task": task, "days_options": _DAYS_OPTIONS,
        "request_key": uuid.uuid4().hex})


@router.post("/analyze")
async def ai_analyze(request: Request, db: Session = Depends(get_db)):
    from fastapi.responses import Response
    form = await request.form()
    try:
        days = int(str(form.get("days", "7")))
    except ValueError:
        days = 7
    if days not in _DAYS_OPTIONS:
        days = 7
    task = hc.enqueue(db, hc.owner_id(request), "analysis", {"days": days, "end": str(today_local())},
                      str(form.get("request_key") or uuid.uuid4().hex)[:128])
    return Response(headers={"HX-Redirect": prefixed(request, f"/companion/tasks/{task.id}")})


@router.get("/analyze/status")
def ai_status(request: Request, db: Session = Depends(get_db)):
    task = db.scalars(select(HealthTask).where(HealthTask.owner == hc.owner_id(request), HealthTask.kind == "analysis")
                      .order_by(HealthTask.created_at.desc()).limit(1)).first()
    return templates.TemplateResponse(request, "fragments/companion_task_status.html", {"task": task})


@router.post("/ask")
async def ai_ask(request: Request, db: Session = Depends(get_db)):
    from starlette.concurrency import run_in_threadpool
    form = await request.form()
    question = str(form.get("question", "")).strip()
    if not question or len(question) > 2000:
        return templates.TemplateResponse(request, "fragments/ai_answer.html", {"error": "请输入 1–2000 字问题。"})
    try:
        answer, actions = await run_in_threadpool(llm.ask, db, question, 7, hc.owner_id(request),
            str(form.get("request_key") or uuid.uuid4().hex)[:128])
    except llm.LLMError:
        return templates.TemplateResponse(request, "fragments/ai_answer.html", {
            "error": "模型暂不可用；若已生成草案，可到待处理查看，请勿重复提交。"})
    return templates.TemplateResponse(request, "fragments/ai_answer.html", {
        "question": question, "html": _render_md(answer), "actions": actions,
        "next_request_key": uuid.uuid4().hex}, headers={"HX-Trigger": "drafts-changed"})
