"""AI 智能分析（LLM 模块）：数据分析报告 + 带上下文问答。

端点：
- GET  /ai            页面：最近一次分析 + 生成按钮 + 提问框
- POST /ai/analyze    生成分析（days=7|30|90），结果缓存 app_settings['ai_analysis']
- POST /ai/ask        自由问答（不缓存）

调用为同步长请求（数十秒），路由用普通 def 走线程池，前端 hx-indicator 转圈。
"""
from __future__ import annotations

import markdown as md_lib
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import AppSetting
from app.services import llm
from app.timeutil import now_local

router = APIRouter(prefix="/ai", dependencies=[Depends(require_login)])

_CACHE_KEY = "ai_analysis"
_DAYS_OPTIONS = (7, 30, 90)


def _render_md(content: str) -> str:
    return md_lib.markdown(content, extensions=["tables", "sane_lists"])


def _cached(db: Session) -> dict | None:
    row = db.get(AppSetting, _CACHE_KEY)
    if row is None or not isinstance(row.value, dict):
        return None
    value = row.value
    return {
        "html": _render_md(value.get("content", "")),
        "generated_at": value.get("generated_at", ""),
        "days": value.get("days"),
    }


@router.get("")
def ai_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "ai.html",
        {
            "configured": llm.is_configured(),
            "model": llm.model_name(),
            "analysis": _cached(db),
            "days_options": _DAYS_OPTIONS,
        },
    )


@router.post("/analyze")
async def ai_analyze(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        days = int(str(form.get("days", "30")))
    except ValueError:
        days = 30
    if days not in _DAYS_OPTIONS:
        days = 30

    try:
        # 线程池外交给 anyio：FastAPI async 路由里跑同步长调用会卡事件循环，
        # 用 run_in_threadpool 保持其他请求可响应
        from starlette.concurrency import run_in_threadpool

        content = await run_in_threadpool(llm.analyze, db, days)
    except llm.LLMError as e:
        return templates.TemplateResponse(
            request, "fragments/ai_analysis.html", {"error": str(e)}
        )

    generated_at = now_local().strftime("%Y-%m-%d %H:%M")
    stmt = pg_insert(AppSetting).values(
        key=_CACHE_KEY,
        value={"content": content, "generated_at": generated_at, "days": days},
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"value": stmt.excluded.value, "updated_at": text("now()")},
    )
    db.execute(stmt)

    return templates.TemplateResponse(
        request,
        "fragments/ai_analysis.html",
        {"analysis": {"html": _render_md(content), "generated_at": generated_at, "days": days}},
    )


@router.post("/ask")
async def ai_ask(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    question = str(form.get("question", "")).strip()
    if not question:
        return templates.TemplateResponse(
            request, "fragments/ai_answer.html", {"error": "先输入问题再提交。"}
        )
    if len(question) > 2000:
        return templates.TemplateResponse(
            request, "fragments/ai_answer.html", {"error": "问题太长了，精简到 2000 字以内。"}
        )
    try:
        from starlette.concurrency import run_in_threadpool

        answer = await run_in_threadpool(llm.ask, db, question)
    except llm.LLMError as e:
        return templates.TemplateResponse(
            request, "fragments/ai_answer.html", {"error": str(e)}
        )
    return templates.TemplateResponse(
        request,
        "fragments/ai_answer.html",
        {"question": question, "html": _render_md(answer)},
    )
