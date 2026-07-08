"""共享依赖：模板环境、登录守卫。"""
from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth
from app.config import BASE_DIR
from app.timeutil import today_local

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["today_local"] = today_local


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
