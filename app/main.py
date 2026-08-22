"""shadow-health 主应用：路由注册、认证/CSRF 中间件、健康检查。"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import auth
from app.config import BASE_DIR, get_settings
from app.db import engine, wait_for_db
from app.deps import LoginRequired, login_redirect, redirect, templates
from app.machine_auth import MachineAPIError
from app.oidc import (
    SESSION_COOKIE,
    TRANSACTION_COOKIE,
    OIDCError,
    clear_session_cookie,
    clear_transaction_cookie,
    get_oidc_service,
    sanitize_return_to,
    set_session_cookie,
    set_transaction_cookie,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_db()
    yield


app = FastAPI(title="shadow-health", lifespan=lifespan)
logger = logging.getLogger("shadow_health.auth")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(MachineAPIError)
async def machine_api_error_handler(request: Request, exc: MachineAPIError):
    logger.warning(
        "machine_api_rejected path=%s status=%s code=%s",
        request.url.path,
        exc.status_code,
        exc.code,
    )
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.middleware("http")
async def forwarded_prefix(request: Request, call_next):
    """子路径部署（§V3 P1）：nginx 剥前缀转发并带 X-Forwarded-Prefix 头，
    这里存进 scope 供 URL 生成（模板 u() / deps.prefixed）。
    路由与 request.url.path 不受影响（path 已被 nginx 剥过）；直连无头时为空，行为零变化。
    不用 uvicorn --root-path：同一进程要同时服务有/无前缀两种形态。
    存私有键而非 root_path：Starlette 的 Mount(StaticFiles) 会把 root_path 组合进
    子 scope，而 path 又不含前缀（违反 ASGI 约定），文件解析会错位 404。"""
    prefix = request.headers.get("X-Forwarded-Prefix", "").strip()
    if prefix and prefix.startswith("/"):
        request.scope["x_forwarded_prefix"] = prefix.rstrip("/")
    return await call_next(request)


@app.middleware("http")
async def csrf_same_origin(request: Request, call_next):
    """最小 CSRF：非 GET 请求校验同源（§7.2）；/api/ingest/* 与 /api/agent/*
    走 Bearer 豁免（Authorization 头跨站伪造不了——此前 agent POST 靠非浏览器
    客户端不带 Origin 隐性放行，V5 显式化）。"""
    if request.method not in (
        "GET",
        "HEAD",
        "OPTIONS",
    ) and not request.url.path.startswith(
        ("/api/ingest/", "/api/agent/", "/api/machine/")
    ):
        sec_fetch_site = request.headers.get("Sec-Fetch-Site")
        if sec_fetch_site is not None:
            if sec_fetch_site not in ("same-origin", "none"):
                return PlainTextResponse("Forbidden", status_code=403)
        else:
            origin = request.headers.get("Origin")
            host = request.headers.get("Host", "")
            if origin is not None and origin.split("://", 1)[-1] != host:
                return PlainTextResponse("Forbidden", status_code=403)
    return await call_next(request)


@app.exception_handler(LoginRequired)
async def login_required_handler(request: Request, exc: LoginRequired):
    return login_redirect(request)


@app.get("/healthz")
def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/readyz")
def readyz() -> PlainTextResponse:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return PlainTextResponse("ready")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """从根路径下发 Service Worker，使其拿到 '/' scope（/static/ 下默认 scope 罩不住页面）。"""
    return FileResponse(
        str(BASE_DIR / "static" / "sw.js"), media_type="application/javascript"
    )


@app.get("/login")
def login_page(request: Request, return_to: str = "/"):
    settings = get_settings()
    if auth.browser_identity(request, settings) is not None:
        return redirect(request, "/")
    if settings.auth_mode == "legacy-forward":
        return RedirectResponse(settings.sso_entry_url, status_code=303)
    try:
        service = get_oidc_service()
        state, nonce, challenge = service.store.create_login_transaction(
            return_to=sanitize_return_to(return_to),
            ttl_seconds=service.config.transaction_ttl_seconds,
        )
        target = service.client.authorization_url(
            state=state, nonce=nonce, challenge=challenge
        )
        response = RedirectResponse(target, status_code=302)
        set_transaction_cookie(
            response, state, service.config.transaction_ttl_seconds
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    except OIDCError:
        return PlainTextResponse("browser authentication unavailable", status_code=503)


@app.get("/auth/callback")
def auth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    stage = "consume_state"
    try:
        cookie_state = request.cookies.get(TRANSACTION_COOKIE, "")
        if not state or not secrets.compare_digest(state, cookie_state):
            raise OIDCError("login transaction is not bound to this browser", reason="state")
        service = get_oidc_service()
        transaction = service.store.consume_login_transaction(state)
        stage = "provider_response"
        if error:
            raise OIDCError("identity provider rejected login", reason="provider")
        stage = "exchange_code"
        tokens = service.client.exchange_code(
            code=code, verifier=transaction["code_verifier"]
        )
        stage = "verify_id_token"
        claims = service.client.verify_id_token(
            tokens["id_token"], nonce=transaction["nonce"]
        )
        claims = service.client.complete_profile_claims(claims, tokens)
        if service.config.required_group not in claims.get("groups", ()):
            return PlainTextResponse("application access is not permitted", status_code=403)
        identity = service.store.upsert_identity(claims)
        session = service.store.create_session(
            identity, service.config.session_ttl_seconds
        )
        response = RedirectResponse(
            sanitize_return_to(transaction["return_to"]), status_code=303
        )
        set_session_cookie(response, session)
        clear_transaction_cookie(response)
        response.headers["Cache-Control"] = "no-store"
        return response
    except OIDCError as exc:
        logger.warning("oidc_callback_rejected stage=%s reason=%s", stage, exc.reason)
        response = PlainTextResponse("OIDC callback validation failed", status_code=400)
        clear_transaction_cookie(response)
        response.headers["Cache-Control"] = "no-store"
        return response


@app.get("/more")
def more_page(request: Request):
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.deps import require_login
    from app.models import BodyMetrics, DietLog, Habit, HabitLog, WorkoutLog
    from app.routers.habits import _logs_map, _streak
    from app.timeutil import today_local

    require_login(request)
    db = SessionLocal()
    try:
        w_count, w_min, w_km = db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(WorkoutLog.duration_min), 0),
                func.coalesce(func.sum(WorkoutLog.distance_km), 0),
            )
        ).one()
        habit_count = db.execute(
            select(func.coalesce(func.sum(HabitLog.done_count), 0))
        ).scalar_one()
        diet_count = db.execute(select(func.count()).select_from(DietLog)).scalar_one()
        record_days = db.execute(
            select(func.count()).select_from(BodyMetrics)
        ).scalar_one()
        today = today_local()
        habits = db.execute(select(Habit).where(Habit.active.is_(True))).scalars().all()
        logs = _logs_map(db, [h.id for h in habits])
        best_streak = max(
            (_streak(h, logs[h.id], today)[0] for h in habits if h.period == "daily"),
            default=0,
        )
        stats = {
            "workout_count": w_count,
            "workout_min": int(w_min),
            "workout_km": round(float(w_km), 1),
            "habit_count": int(habit_count),
            "diet_count": diet_count,
            "record_days": record_days,
            "best_streak": best_streak,
        }
    finally:
        db.close()
    return templates.TemplateResponse(request, "more.html", {"stats": stats})


@app.post("/logout")
def logout(request: Request):
    settings = get_settings()
    if settings.auth_mode == "legacy-forward":
        client_host = request.client.host if request.client else ""
        identity = auth.forward_identity(request.headers, client_host, settings)
        target = settings.sso_logout_url if identity is not None else settings.sso_entry_url
        return RedirectResponse(target, status_code=303)
    try:
        service = get_oidc_service()
        service.store.revoke_session(request.cookies.get(SESSION_COOKIE, ""))
        response = RedirectResponse(service.client.global_logout_url(), status_code=303)
    except OIDCError:
        response = RedirectResponse(settings.oidc_post_logout_redirect_uri, status_code=303)
    clear_session_cookie(response)
    return response


def _register_routers() -> None:
    from app.routers import (
        agent,
        agent_log,
        ai,
        awards,
        diet,
        discipline,
        fitness,
        habits,
        ingest,
        labs,
        machine_agent,
        metrics,
        offline,
        reminders,
        report,
        review,
        scale,
        settings,
        today,
        workout,
    )

    app.include_router(today.router)
    app.include_router(awards.router)
    app.include_router(fitness.router)
    app.include_router(labs.router)
    app.include_router(ai.router)
    app.include_router(machine_agent.router)
    app.include_router(metrics.router)
    app.include_router(diet.router)
    app.include_router(discipline.router)
    app.include_router(workout.router)
    app.include_router(habits.router)
    app.include_router(review.router)
    app.include_router(report.router)
    app.include_router(settings.router)
    app.include_router(ingest.router)
    app.include_router(offline.router)
    app.include_router(agent.router)
    app.include_router(agent_log.router)
    app.include_router(scale.router)
    app.include_router(reminders.router)


_register_routers()
