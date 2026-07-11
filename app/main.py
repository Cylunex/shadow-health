"""shadow-health 主应用：路由注册、认证/CSRF 中间件、健康检查。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import auth
from app.config import BASE_DIR, get_settings
from app.db import engine, wait_for_db
from app.deps import LoginRequired, login_redirect, templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_db()
    yield


app = FastAPI(title="shadow-health", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def csrf_same_origin(request: Request, call_next):
    """最小 CSRF：非 GET 请求校验同源（§7.2）；/api/ingest/* 走 Bearer 豁免。"""
    if request.method not in ("GET", "HEAD", "OPTIONS") and not request.url.path.startswith(
        "/api/ingest/"
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
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return PlainTextResponse("ok")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """从根路径下发 Service Worker，使其拿到 '/' scope（/static/ 下默认 scope 罩不住页面）。"""
    return FileResponse(
        str(BASE_DIR / "static" / "sw.js"), media_type="application/javascript"
    )


@app.get("/login")
def login_page(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if auth.session_valid(token):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(request: Request):
    ip = request.client.host if request.client else "?"
    if auth.is_locked(ip):
        return templates.TemplateResponse(
            request, "login.html", {"error": "尝试次数过多，请 1 分钟后再试"}, status_code=429
        )
    form = await request.form()
    password = str(form.get("password", ""))
    stored = get_settings().auth_password_hash
    if not stored or not auth.verify_password(password, stored):
        auth.record_failure(ip)
        return templates.TemplateResponse(
            request, "login.html", {"error": "密码不正确"}, status_code=401
        )
    auth.clear_failures(ip)
    token = auth.create_session()
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


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
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


def _register_routers() -> None:
    from app.routers import (
        ai, diet, habits, ingest, metrics, offline, reminders, report, review, scale,
        settings, today, workout,
    )

    app.include_router(today.router)
    app.include_router(ai.router)
    app.include_router(metrics.router)
    app.include_router(diet.router)
    app.include_router(workout.router)
    app.include_router(habits.router)
    app.include_router(review.router)
    app.include_router(report.router)
    app.include_router(settings.router)
    app.include_router(ingest.router)
    app.include_router(offline.router)
    app.include_router(scale.router)
    app.include_router(reminders.router)


_register_routers()
