"""共享依赖：模板环境（含全局函数）、登录守卫。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from app import auth
from app.config import BASE_DIR
from app.services.pr import strength_lines
from app.timeutil import LOCAL_TZ, today_local


def prefixed(request: Request, path: str) -> str:
    """子路径部署（/shealth）URL 生成：部署前缀 + path。

    前缀由 main.forwarded_prefix 中间件从 X-Forwarded-Prefix 写入 scope，
    直连（开发/测试）时为空。Python 侧重定向/HX-Redirect 用这个，模板用全局 u()。
    """
    return request.scope.get("x_forwarded_prefix", "") + path


def redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    """带前缀的站内重定向；path 传应用内绝对路径（如 '/login'）。"""
    return RedirectResponse(prefixed(request, path), status_code=status_code)


@pass_context
def _u(context: Any, path: str) -> str:
    """模板全局 u('/xxx')：为裸绝对路径补部署前缀（从上下文里的 request 取）。"""
    request = context.get("request")
    if request is None:
        return path
    return request.scope.get("x_forwarded_prefix", "") + path


# 外部导入源（三星 zip / 直读 / Health Connect / Keep）写入的英文 session_type →
# 中文显示名。仅展示层翻译，库里保持原值——跑步图关键词（metrics._RUN_KEYWORDS）、
# 跑步 PR（pr._is_run）、Keep 去重等逻辑都依赖原文。
SESSION_TYPE_LABELS: dict[str, str] = {
    "walking": "健走",
    "running": "跑步",
    "treadmill": "跑步机",
    "circuit_training": "循环训练",
    "cycling": "骑行",
    "mountain_biking": "山地骑行",
    "exercise_bike": "动感单车",
    "spinning": "动感单车",
    "hiking": "徒步登山",
    "backpacking": "负重徒步",
    "swimming": "游泳",
    "squats": "深蹲",
    "lunges": "弓步",
    "elliptical": "椭圆机",
    "elliptical_trainer": "椭圆机",
    "rowing": "划船",
    "rowing_machine": "划船机",
    "stair_climbing": "爬楼梯",
    "stair_climbing_machine": "登山机",
    "step_machine": "踏步机",
    "yoga": "瑜伽",
    "pilates": "普拉提",
    "stretching": "拉伸",
    "plank": "平板支撑",
    "push_ups": "俯卧撑",
    "pull_ups": "引体向上",
    "sit_ups": "仰卧起坐",
    "crunches": "卷腹",
    "burpees": "波比跳",
    "jumping_jacks": "开合跳",
    "jump_rope": "跳绳",
    "weight_machine": "器械力量",
    "strength_training": "力量训练",
    "weight_training": "力量训练",
    "deadlifts": "硬拉",
    "bench_press": "卧推",
    "hiit": "HIIT",
    "high_intensity_interval_training": "HIIT",
    "interval_training": "间歇训练",
    "aerobics": "有氧操",
    "dancing": "舞蹈",
    "boxing": "拳击",
    "martial_arts": "武术",
    "tai_chi": "太极",
    "basketball": "篮球",
    "soccer": "足球",
    "football": "足球",
    "badminton": "羽毛球",
    "table_tennis": "乒乓球",
    "tennis": "网球",
    "volleyball": "排球",
    "golf": "高尔夫",
    "baseball": "棒球",
    "skating": "滑冰",
    "inline_skating": "轮滑",
    "skiing": "滑雪",
    "snowboarding": "单板滑雪",
    "other": "其他运动",
    "other_workout": "其他运动",
    "custom": "自定义训练",
}


def session_label(session_type: Any) -> str:
    """运动类型显示名：外源英文枚举翻中文；手动录入的中文原样返回；
    未知英文只把下划线换空格；空值回退「训练」。"""
    s = str(session_type or "").strip()
    if not s:
        return "训练"
    label = SESSION_TYPE_LABELS.get(s.lower())
    if label:
        return label
    return s.replace("_", " ") if s.isascii() else s


def local_hm(dt: Any) -> str:
    """tz-aware datetime → 本地 HH:MM；None/无效返回 ''（训练详情展开的开始时间）。"""
    try:
        return dt.astimezone(LOCAL_TZ).strftime("%H:%M")
    except (AttributeError, ValueError, OSError):
        return ""


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
templates.env.globals["session_label"] = session_label
templates.env.globals["local_hm"] = local_hm
templates.env.globals["strength_lines"] = strength_lines
templates.env.globals["u"] = _u


class LoginRequired(Exception):
    """未登录访问页面/片段时抛出，由全局 handler 转跳登录页。"""


def require_login(request: Request) -> None:
    token = request.cookies.get(auth.SESSION_COOKIE)
    if not auth.session_valid(token):
        raise LoginRequired()


def login_redirect(request: Request) -> RedirectResponse:
    # HTMX 片段请求返回 HX-Redirect，整页请求 302
    login_url = prefixed(request, "/login")
    if request.headers.get("HX-Request"):
        resp = RedirectResponse(login_url, status_code=303)
        resp.headers["HX-Redirect"] = login_url
        return resp
    return RedirectResponse(login_url, status_code=303)
