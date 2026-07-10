"""共享依赖：模板环境（含全局函数）、登录守卫。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth
from app.config import BASE_DIR
from app.services.pr import strength_lines
from app.timeutil import today_local


def pace_str(duration_min: Any, distance_km: Any) -> str:
    """跑步配速 → 6'32" 形式；数据缺失或明显不是跑走（<2 或 >40 min/km）返回 ''。"""
    try:
        dur = float(duration_min or 0)
        km = float(distance_km or 0)
    except (TypeError, ValueError):
        return ""
    if dur <= 0 or km < 0.2:
        return ""
    pace = dur / km
    if not (2 <= pace <= 40):
        return ""
    m = int(pace)
    s = round((pace - m) * 60)
    if s == 60:
        m, s = m + 1, 0
    return f"{m}'{s:02d}\""


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["today_local"] = today_local
templates.env.globals["pace_str"] = pace_str
templates.env.globals["strength_lines"] = strength_lines


class LoginRequired(Exception):
    """未登录访问页面/片段时抛出，由全局 handler 转跳登录页。"""


def require_login(request: Request) -> None:
    token = request.cookies.get(auth.SESSION_COOKIE)
    if not auth.session_valid(token):
        raise LoginRequired()


def login_redirect(request: Request) -> RedirectResponse:
    # HTMX 片段请求返回 HX-Redirect，整页请求 302
    if request.headers.get("HX-Request"):
        resp = RedirectResponse("/login", status_code=303)
        resp.headers["HX-Redirect"] = "/login"
        return resp
    return RedirectResponse("/login", status_code=303)
