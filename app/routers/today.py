"""今日面板（设计文档 §四 "/" 行）。

自己只查 daily_activity + app_settings 两张表；
计划卡/习惯/指标/饮食各区块通过 HTMX 片段由对应模块提供。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import AppSetting, DailyActivity
from app.timeutil import now_local, today_local

router = APIRouter(dependencies=[Depends(require_login)])

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
DEFAULT_TARGET_STEPS = 8000  # 设计文档 §5.6：target_steps 默认 8000（源 06）


def _greeting(hour: int) -> str:
    if 5 <= hour < 9:
        return "早上好"
    if 9 <= hour < 12:
        return "上午好"
    if 12 <= hour < 14:
        return "中午好"
    if 14 <= hour < 18:
        return "下午好"
    if 18 <= hour < 23:
        return "晚上好"
    return "夜深了，注意休息"


def _target_steps(db: Session) -> int:
    """读 app_settings.target_steps；缺失/非法回退默认 8000。"""
    row = db.get(AppSetting, "target_steps")
    if row is not None:
        value = row.value
        if isinstance(value, dict):  # 容错：{"value": 8000} 形式
            value = value.get("value")
        try:
            n = int(value)  # type: ignore[arg-type]
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    return DEFAULT_TARGET_STEPS


@router.get("/")
def today_page(request: Request, db: Session = Depends(get_db)):
    today = today_local()
    activity = db.get(DailyActivity, today)
    steps = activity.steps if activity is not None else None
    target_steps = _target_steps(db)
    steps_pct = 0
    if steps is not None and target_steps > 0:
        steps_pct = min(100, round(steps * 100 / target_steps))
    ctx = {
        "greeting": _greeting(now_local().hour),
        "today": today,
        "weekday_cn": WEEKDAY_CN[today.isoweekday() - 1],
        "steps": steps,
        "target_steps": target_steps,
        "steps_pct": steps_pct,
    }
    return templates.TemplateResponse(request, "today.html", ctx)
