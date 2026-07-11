"""AI 智能分析（LLM 模块）：数据分析报告 + 带上下文问答。

端点：
- GET  /ai                  页面：最近一次分析 + 生成按钮 + 提问框
- POST /ai/analyze          发起分析任务（days=7|30|90）：后台线程执行，立即返回轮询片段
- GET  /ai/analyze/status   轮询任务状态：running=继续轮询 / done=渲染结果 / failed=错误
- POST /ai/ask              自由问答（同步，不缓存——交互式短等待可接受）

分析耗时 1-2 分钟：同步长请求在手机 WebView 锁屏/切应用即作废，改为后台任务 +
app_settings 状态轮询（照导入中心 job 模式），点完就走、回来看结果。
状态存 app_settings['ai_analysis_job']；结果缓存 app_settings['ai_analysis']。
"""
from __future__ import annotations

import threading
from datetime import datetime

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
_JOB_KEY = "ai_analysis_job"
_JOB_TIMEOUT_S = 600  # 超过视为中断（服务重启丢线程），提示重新生成
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


def _set_setting(db: Session, key: str, value: dict) -> None:
    stmt = pg_insert(AppSetting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"value": stmt.excluded.value, "updated_at": text("now()")},
    )
    db.execute(stmt)


def _job_state(db: Session) -> dict:
    """任务状态；running 超时（服务重启丢线程）折算成 failed。"""
    row = db.get(AppSetting, _JOB_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    if value.get("status") == "running":
        try:
            started = datetime.fromisoformat(str(value.get("started_at", "")))
            if (now_local() - started).total_seconds() > _JOB_TIMEOUT_S:
                return {"status": "failed", "error": "分析中断（超时或服务重启），请重新生成。"}
        except ValueError:
            return {"status": "failed", "error": "任务状态异常，请重新生成。"}
    return value


def _job_fragment(request: Request, db: Session, state: dict):
    """轮询片段：running 自带 every 3s 轮询；终态渲染结果/错误（无 trigger 即停）。"""
    return templates.TemplateResponse(
        request,
        "fragments/ai_job_status.html",
        {
            "running": state.get("status") == "running",
            "days": state.get("days"),
            "error": state.get("error") if state.get("status") == "failed" else None,
            "analysis": _cached(db),
        },
    )


def _run_analysis(days: int) -> None:
    """后台线程：独立会话跑 LLM 分析，结果与终态写 app_settings。"""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        content = llm.analyze(db, days)
        generated_at = now_local().strftime("%Y-%m-%d %H:%M")
        _set_setting(db, _CACHE_KEY, {
            "content": content, "generated_at": generated_at, "days": days,
        })
        _set_setting(db, _JOB_KEY, {"status": "done"})
        db.commit()
    except Exception as e:  # LLMError 或意外异常都要落终态，否则前端轮询到超时
        db.rollback()
        msg = str(e) if isinstance(e, llm.LLMError) else f"分析失败：{str(e)[:200]}"
        try:
            _set_setting(db, _JOB_KEY, {"status": "failed", "error": msg})
            db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()


@router.get("")
def ai_page(request: Request, db: Session = Depends(get_db)):
    state = _job_state(db)
    cfg = llm.get_config(db)
    return templates.TemplateResponse(
        request,
        "ai.html",
        {
            "configured": cfg["configured"],
            "model": cfg["model"],
            "provider_label": llm.PROVIDER_LABELS[cfg["provider"]],
            "analysis": _cached(db),
            "job_running": state.get("status") == "running",
            "job_days": state.get("days"),
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

    state = _job_state(db)
    if state.get("status") == "running":
        return _job_fragment(request, db, state)  # 已在跑：直接接上轮询
    if not llm.is_configured(db):
        return _job_fragment(request, db, {
            "status": "failed",
            "error": "未配置 AI 模型 API Key——到 设置→AI 模型 填入即可使用。",
        })
    state = {"status": "running", "days": days, "started_at": now_local().isoformat()}
    _set_setting(db, _JOB_KEY, state)
    db.commit()  # 后台线程用独立会话读状态，必须先落库
    threading.Thread(target=_run_analysis, args=(days,), daemon=True).start()
    return _job_fragment(request, db, state)


@router.get("/analyze/status")
def ai_analyze_status(request: Request, db: Session = Depends(get_db)):
    return _job_fragment(request, db, _job_state(db))


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
