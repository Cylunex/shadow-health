"""运动训练（设计文档 §3.4、§四 /workout 三行与「记录编辑三件套」段）。

端点：
- GET    /workout                        今日训练页（计划卡 + 手动记录 + 最近 14 条 + 热力图）
- POST   /workout/logs                   记一笔（手动表单 / 计划卡「按模板完成」，source='manual'）
- GET    /workout/logs/{id}/edit         行内编辑表单片段（仅 source='manual'）
- PUT    /workout/logs/{id}              保存行内编辑（仅 manual）
- DELETE /workout/logs/{id}              删除（仅 manual，hx-confirm）
- GET    /workout/plans                  计划卡片列表（goal/duration/是否在执行）
- GET    /workout/plans/{id}             计划详情（python-markdown 渲染 + #week-N 锚点）
- POST   /workout/plans/{id}/enroll      启动计划（start_date 默认今天）
- POST   /workout/enrollments/{id}/finish   标记完成（落 end_date）
- POST   /workout/enrollments/{id}/abandon  放弃（落 end_date）
- GET    /fragments/workout/plan-cards   全部 active enrollment 的「今天该练什么」卡（今日面板 hx-get 用）
- GET    /fragments/workout/recent       最近 14 条记录片段（workout-changed 被动刷新）
- GET    /fragments/workout/heatmap      训练日历热力图片段（months=3/6/12，服务端 CSS grid，零 JS）
- GET    /fragments/workout/day          热力图格子点开的当日详情片段

注：/fragments/* 不在 /workout 前缀下，故本 router 不设 prefix，路径写全
（与 metrics/habits 同一处理方式）。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import markdown as md_lib
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import PlanEnrollment, WorkoutLog, WorkoutPlan
from app.timeutil import today_local

router = APIRouter(dependencies=[Depends(require_login)])

HX_TRIGGER = {"HX-Trigger": "workout-changed"}

SOURCE_LABELS = {
    "samsung_zip": "三星",
    "health_connect": "HC",
    "samsung_direct": "手表",
    "keep": "Keep",
}
STATUS_LABELS = {"active": "执行中", "done": "已完成", "abandoned": "已放弃"}
WEEKDAY_NAMES = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}

# 手动表单类型联想（datalist）
SESSION_TYPE_HINTS = ["力量", "有氧", "盆底", "早", "晚", "快走", "慢跑", "HIIT", "拉伸", "功法"]

# 热力图 5 档色（0=无训练 + 4 档 emerald 深浅，GitHub 风格；最高档带微光晕）
HEATMAP_LEVEL_CLASSES = [
    "bg-slate-800/60",
    "bg-emerald-900/80",
    "bg-emerald-700",
    "bg-emerald-500",
    "bg-emerald-300 shadow-[0_0_5px_rgba(110,231,183,0.45)]",
]
HEATMAP_MONTHS_OPTIONS = (3, 6, 12)


# ---------- 表单解析 ----------
def _parse_date(raw: Any) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return today_local()


def _form_int(form: Any, name: str, lo: int, hi: int, label: str, errors: list[str]) -> int | None:
    raw = form.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        errors.append(f"{label}格式不对")
        return None
    if not (lo <= value <= hi):
        errors.append(f"{label}需在 {lo}~{hi}")
        return None
    return value


def _form_decimal(
    form: Any, name: str, lo: float, hi: float, label: str, errors: list[str]
) -> Decimal | None:
    raw = form.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        errors.append(f"{label}格式不对")
        return None
    if not (Decimal(str(lo)) <= value <= Decimal(str(hi))):
        errors.append(f"{label}需在 {lo}~{hi}")
        return None
    return value


def _parse_log_form(form: Any) -> tuple[dict[str, Any], list[str]]:
    """公共字段：log_date / session_type / duration_min / distance_km / rpe / notes。"""
    errors: list[str] = []
    session_type = str(form.get("session_type") or "").strip()
    if not session_type:
        errors.append("训练类型必填")
    values: dict[str, Any] = {
        "log_date": _parse_date(form.get("log_date")),
        "session_type": session_type or None,
        "duration_min": _form_int(form, "duration_min", 0, 1440, "时长", errors),
        "distance_km": _form_decimal(form, "distance_km", 0, 1000, "距离", errors),
        "rpe": _form_int(form, "rpe", 1, 10, "RPE", errors),
        "notes": str(form.get("notes") or "").strip() or None,
    }
    return values, errors


def _get_manual_log(db: Session, log_id: int) -> WorkoutLog:
    log = db.get(WorkoutLog, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    if log.source != "manual":
        raise HTTPException(status_code=403, detail="外部导入记录只读")
    return log


# ---------- 片段上下文 ----------
def _plan_names(db: Session) -> dict[int, str]:
    return dict(db.execute(select(WorkoutPlan.id, WorkoutPlan.name)).all())


def _row_ctx(db: Session, log: WorkoutLog) -> dict[str, Any]:
    return {"log": log, "plan_names": _plan_names(db), "source_labels": SOURCE_LABELS}


def _recent_ctx(db: Session) -> dict[str, Any]:
    rows = (
        db.execute(
            select(WorkoutLog)
            .order_by(WorkoutLog.log_date.desc(), WorkoutLog.id.desc())
            .limit(14)
        )
        .scalars()
        .all()
    )
    return {"recent_rows": rows, "plan_names": _plan_names(db), "source_labels": SOURCE_LABELS}


def _manual_form_ctx(saved: bool = False, errors: list[str] | None = None) -> dict[str, Any]:
    return {
        "form_log_date": today_local().isoformat(),
        "form_saved": saved,
        "form_errors": errors or [],
        "session_type_hints": SESSION_TYPE_HINTS,
    }


def _phase_for(phases: list | None, week_no: int) -> dict | None:
    for ph in phases or []:
        weeks = ph.get("weeks") or []
        if len(weeks) == 2 and weeks[0] <= week_no <= weeks[1]:
            return ph
    return None


def _plan_cards_ctx(db: Session) -> dict[str, Any]:
    """今日面板计划卡（§3.4 末段算法）：多 active enrollment 逐个渲染。"""
    today = today_local()
    weekday = today.isoweekday()
    rows = db.execute(
        select(PlanEnrollment, WorkoutPlan)
        .join(WorkoutPlan, PlanEnrollment.plan_id == WorkoutPlan.id)
        .where(PlanEnrollment.status == "active")
        .order_by(PlanEnrollment.start_date, PlanEnrollment.id)
    ).all()

    # 今日已按该 enrollment 记录过的 session_type（用于「已完成 ✓」判定）
    done_map: dict[int, set[str]] = defaultdict(set)
    enr_ids = [enr.id for enr, _ in rows]
    if enr_ids:
        for eid, stype in db.execute(
            select(WorkoutLog.enrollment_id, WorkoutLog.session_type).where(
                WorkoutLog.log_date == today, WorkoutLog.enrollment_id.in_(enr_ids)
            )
        ):
            if stype:
                done_map[eid].add(stype)

    cards: list[dict[str, Any]] = []
    for enr, plan in rows:
        card: dict[str, Any] = {
            "enrollment": enr,
            "plan": plan,
            "weekday_name": WEEKDAY_NAMES[weekday],
            "not_started": False,
            "over": False,
            "phase": None,
            "sessions": [],
            "deep_link": None,
        }
        days = (today - enr.start_date).days
        if days < 0:
            card["not_started"] = True
            card["week_label"] = f"{enr.start_date.isoformat()} 开始"
            cards.append(card)
            continue

        week_no = days // 7 + 1
        day_no = days % 7 + 1
        cyclic = plan.duration_weeks is None
        over = (not cyclic) and week_no > plan.duration_weeks
        card.update(
            week_no=week_no,
            day_no=day_no,
            over=over,
            week_label=(
                f"第 {week_no} 周（循环）" if cyclic else f"第 {week_no}/{plan.duration_weeks} 周"
            ),
        )
        if over:
            cards.append(card)  # 超期：只提示完成/重开，不渲染当日内容
            continue

        card["phase"] = _phase_for(plan.phases, week_no)
        if plan.weekly_template:
            sessions: list[dict[str, Any]] = []
            for entry in plan.weekly_template:
                entry_wd = entry.get("weekday")
                if entry_wd != 0 and entry_wd != weekday:
                    continue  # weekday:0 每日项始终显示
                for s in entry.get("sessions") or []:
                    stype = str(s.get("type") or "").strip()
                    if not stype:
                        continue
                    item = {
                        "slot": s.get("slot"),
                        "type": stype,
                        "pelvic": s.get("pelvic"),
                        "daily": entry_wd == 0,
                        "done": stype in done_map[enr.id],
                    }
                    if not item["done"]:
                        payload: dict[str, Any] = {
                            "fragment": "plan-cards",
                            "from_template": "1",
                            "enrollment_id": enr.id,
                            "session_type": stype,
                            "week_no": week_no,
                        }
                        if item["slot"]:
                            payload["slot"] = item["slot"]
                        if item["pelvic"]:
                            payload["pelvic"] = item["pelvic"]
                        item["hx_vals"] = json.dumps(payload, ensure_ascii=False)
                    sessions.append(item)
            card["sessions"] = sessions
        else:
            # 无周表的计划（04 计划二/三/四）：降级深链详情页对应锚点，不猜内容
            card["deep_link"] = f"/workout/plans/{plan.id}#week-{week_no}"
        cards.append(card)
    return {"cards": cards}


def _heatmap_ctx(db: Session, months: int) -> dict[str, Any]:
    """服务端 CSS grid 热力图：按周列布局，当日总时长分 5 档 emerald 深浅。"""
    if months not in HEATMAP_MONTHS_OPTIONS:
        months = 3
    today = today_local()
    start = today - timedelta(days=round(months * 30.44))
    start -= timedelta(days=start.isoweekday() - 1)  # 对齐周一

    by_day: dict[date, tuple[int, int]] = {}
    for d, minutes, count in db.execute(
        select(
            WorkoutLog.log_date,
            func.coalesce(func.sum(WorkoutLog.duration_min), 0),
            func.count(),
        )
        .where(WorkoutLog.log_date.between(start, today))
        .group_by(WorkoutLog.log_date)
    ):
        by_day[d] = (int(minutes), int(count))

    weeks: list[dict[str, Any]] = []
    col_start = start
    prev_month: int | None = None
    while col_start <= today:
        days: list[dict[str, Any]] = []
        for i in range(7):
            d = col_start + timedelta(days=i)
            if d > today:
                days.append({"future": True})
                continue
            minutes, count = by_day.get(d, (0, 0))
            if count == 0:
                level = 0
            elif minutes < 30:
                level = 1
            elif minutes < 60:
                level = 2
            elif minutes < 90:
                level = 3
            else:
                level = 4
            days.append(
                {"future": False, "date": d, "minutes": minutes, "count": count, "level": level}
            )
        month_label = ""
        if col_start.month != prev_month:
            month_label = f"{col_start.month}月"
            prev_month = col_start.month
        weeks.append({"days": days, "month_label": month_label})
        col_start += timedelta(days=7)

    return {
        "hm": {
            "months": months,
            "options": HEATMAP_MONTHS_OPTIONS,
            "weeks": weeks,
            "level_classes": HEATMAP_LEVEL_CLASSES,
            "total_days": len(by_day),
            "total_min": sum(m for m, _ in by_day.values()),
        }
    }


def _day_ctx(db: Session, d: date) -> dict[str, Any]:
    logs = (
        db.execute(
            select(WorkoutLog).where(WorkoutLog.log_date == d).order_by(WorkoutLog.id)
        )
        .scalars()
        .all()
    )
    return {
        "day": d.isoformat(),
        "day_logs": logs,
        "plan_names": _plan_names(db),
        "source_labels": SOURCE_LABELS,
    }


def _enroll_panel_ctx(db: Session, plan: WorkoutPlan, error: str | None = None) -> dict[str, Any]:
    today = today_local()
    enrollments = (
        db.execute(
            select(PlanEnrollment)
            .where(PlanEnrollment.plan_id == plan.id)
            .order_by(PlanEnrollment.id.desc())
        )
        .scalars()
        .all()
    )
    active_items = []
    for e in enrollments:
        if e.status != "active":
            continue
        days = (today - e.start_date).days
        active_items.append({"e": e, "week_no": days // 7 + 1 if days >= 0 else None})
    return {
        "plan": plan,
        "active_items": active_items,
        "history": [e for e in enrollments if e.status != "active"],
        "today": today.isoformat(),
        "enroll_error": error,
        "status_labels": STATUS_LABELS,
    }


# ---------- markdown 渲染（计划详情） ----------
_CN_NUMS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
_WEEK_TXT_RE = re.compile(r"第\s*(\d+)\s*(?:[–\-~—～至到]\s*(\d+)\s*)?周")
_HEAD_RE = re.compile(r"<h([1-4])>(.*?)</h\1>", re.S)
_LI_WEEK_RE = re.compile(r"<li>((?:\s|<strong>|<em>)*第\s*(\d+)\s*(?:[–\-~—～至到]\s*(\d+)\s*)?周)")
_TAG_STRIP_RE = re.compile(r"<[^>]+>")

# markdown 输出的标签 → Tailwind 类（app.css 无 typography 插件，逐标签上样式）
_TAG_CLASSES = {
    "h1": "text-xl font-bold text-slate-100 mt-6 mb-3 scroll-mt-24",
    "h2": "text-lg font-bold text-emerald-400 mt-6 mb-2 scroll-mt-24",
    "h3": "text-base font-semibold text-slate-100 mt-5 mb-2 scroll-mt-24",
    "h4": "text-sm font-semibold text-slate-200 mt-4 mb-1.5 scroll-mt-24",
    "p": "my-2 text-sm leading-relaxed text-slate-300",
    "ul": "list-disc pl-5 my-2 space-y-1 text-sm text-slate-300",
    "ol": "list-decimal pl-5 my-2 space-y-1 text-sm text-slate-300",
    "li": "leading-relaxed scroll-mt-24",
    "blockquote": "border-l-4 border-emerald-800 bg-slate-950/60 rounded-r-lg px-3 py-2 my-3 text-xs text-slate-400",
    "table": "w-full text-xs border-collapse",
    "th": "border border-slate-700 bg-slate-800/80 px-2 py-1.5 text-left text-slate-200 whitespace-nowrap",
    "td": "border border-slate-800 px-2 py-1.5 text-slate-300 align-top",
    "hr": "border-slate-800 my-5",
    "code": "bg-slate-800 rounded px-1 text-emerald-300 text-[0.85em]",
    "a": "text-emerald-400 underline underline-offset-2",
    "strong": "text-slate-100 font-semibold",
}


def _heading_anchor(m: re.Match, used: set[str]) -> str:
    level, inner = m.group(1), m.group(2)
    plain = _TAG_STRIP_RE.sub("", inner)
    anchor_id: str | None = None
    wm = _WEEK_TXT_RE.search(plain)
    if wm:
        anchor_id = f"week-{int(wm.group(1))}"
    else:
        pm = re.search(r"计划([一二三四五])", plain)
        if pm:
            anchor_id = f"plan-{_CN_NUMS[pm.group(1)]}"
    if not anchor_id or anchor_id in used:
        return m.group(0)
    used.add(anchor_id)
    extra = ""
    if wm and wm.group(2):  # 「第 5–8 周」：给区间内其余周补隐形锚点
        for k in range(int(wm.group(1)) + 1, int(wm.group(2)) + 1):
            span_id = f"week-{k}"
            if span_id not in used:
                used.add(span_id)
                extra += f'<span id="{span_id}"></span>'
    return f'<h{level} id="{anchor_id}">{extra}{inner}</h{level}>'


def _li_anchor(m: re.Match, used: set[str]) -> str:
    a = int(m.group(2))
    b = int(m.group(3)) if m.group(3) else a
    anchor_id = f"week-{a}"
    if anchor_id in used:
        return m.group(0)
    used.add(anchor_id)
    spans = ""
    for k in range(a + 1, b + 1):
        span_id = f"week-{k}"
        if span_id not in used:
            used.add(span_id)
            spans += f'<span id="{span_id}"></span>'
    return f'<li id="{anchor_id}">{spans}{m.group(1)}'


_LIST_LINE_RE = re.compile(r"^\s{0,3}(?:[-*+]\s|\d+[.)]\s)")


def _ensure_list_blank_lines(text: str) -> str:
    """python-markdown 要求列表前有空行；素材常紧跟段落写列表，这里补空行。"""
    out: list[str] = []
    for line in text.splitlines():
        if (
            _LIST_LINE_RE.match(line)
            and out
            and out[-1].strip()
            and not _LIST_LINE_RE.match(out[-1])
        ):
            out.append("")
        out.append(line)
    return "\n".join(out)


def render_plan_md(content: str) -> str:
    """content_md → 带锚点（#plan-N / #week-N）与 Tailwind 类的 HTML。"""
    html = md_lib.markdown(_ensure_list_blank_lines(content or ""), extensions=["tables"])
    used: set[str] = set()
    html = _HEAD_RE.sub(lambda m: _heading_anchor(m, used), html)
    html = _LI_WEEK_RE.sub(lambda m: _li_anchor(m, used), html)
    # 表格横向滚动容器（移动端窄屏）
    html = html.replace("<table>", '<div class="overflow-x-auto my-3"><table>').replace(
        "</table>", "</table></div>"
    )
    for tag, cls in _TAG_CLASSES.items():
        html = re.sub(rf"<{tag}(?=[\s>])", f'<{tag} class="{cls}"', html)
    return html


def _load_ctx(db: Session) -> dict[str, Any]:
    """本周训练负荷（sRPE 法：负荷 = RPE × 分钟）+ 强度带分钟分布 + 环比。

    只有带 RPE 的记录计入负荷（自动同步无 RPE，归入「未评级」分钟单列）。
    强度带：低 1-3 / 中 4-6 / 高 7-10。
    """
    today = today_local()
    ws = today - timedelta(days=today.isoweekday() - 1)
    prev_ws = ws - timedelta(days=7)
    rows = db.execute(
        select(WorkoutLog.log_date, WorkoutLog.duration_min, WorkoutLog.rpe).where(
            WorkoutLog.log_date >= prev_ws,
            WorkoutLog.log_date <= today,
            WorkoutLog.duration_min.is_not(None),
        )
    ).all()

    def agg(lo: date, hi: date) -> dict[str, int]:
        out = {"load": 0, "low": 0, "mid": 0, "high": 0, "unrated": 0, "sessions": 0}
        for d, dur, rpe in rows:
            if not (lo <= d <= hi):
                continue
            out["sessions"] += 1
            if rpe:
                out["load"] += rpe * dur
                band = "low" if rpe <= 3 else ("mid" if rpe <= 6 else "high")
                out[band] += dur
            else:
                out["unrated"] += dur
        return out

    cur = agg(ws, today)
    prev = agg(prev_ws, ws - timedelta(days=1))
    delta_pct = (
        round((cur["load"] - prev["load"]) * 100 / prev["load"]) if prev["load"] else None
    )
    rated_total = cur["low"] + cur["mid"] + cur["high"]
    return {
        "wl": {
            **cur,
            "rated_total": rated_total,
            "prev_load": prev["load"],
            "delta_pct": delta_pct,
        }
    }


# ---------- 页面 ----------
@router.get("/workout")
def workout_page(request: Request, db: Session = Depends(get_db)):
    ctx: dict[str, Any] = {}
    ctx.update(_plan_cards_ctx(db))
    ctx.update(_manual_form_ctx())
    ctx.update(_recent_ctx(db))
    ctx.update(_load_ctx(db))
    ctx.update(_heatmap_ctx(db, 3))
    return templates.TemplateResponse(request, "workout.html", ctx)


@router.get("/workout/timer")
def timer_page(request: Request):
    """训练计时器（纯前端：动作/休息/组数循环、蜂鸣提示、屏幕常亮）。"""
    return templates.TemplateResponse(request, "workout_timer.html", {})


@router.get("/workout/plans")
def plans_page(request: Request, db: Session = Depends(get_db)):
    today = today_local()
    plans = db.execute(select(WorkoutPlan).order_by(WorkoutPlan.id)).scalars().all()
    active = db.execute(
        select(PlanEnrollment).where(PlanEnrollment.status == "active")
    ).scalars().all()
    label_by_plan: dict[int, str] = {}
    for e in active:
        days = (today - e.start_date).days
        label_by_plan[e.plan_id] = (
            f"执行中 · 第 {days // 7 + 1} 周" if days >= 0 else f"{e.start_date.isoformat()} 开始"
        )
    items = [{"plan": p, "enrolled_label": label_by_plan.get(p.id)} for p in plans]
    return templates.TemplateResponse(request, "plans.html", {"items": items})


@router.get("/workout/plans/{plan_id}")
def plan_detail_page(plan_id: int, request: Request, db: Session = Depends(get_db)):
    plan = db.get(WorkoutPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    ctx = _enroll_panel_ctx(db, plan)
    ctx["content_html"] = render_plan_md(plan.content_md or "")
    return templates.TemplateResponse(request, "plan_detail.html", ctx)


# ---------- 计划启停 ----------
@router.post("/workout/plans/{plan_id}/enroll")
async def plan_enroll(plan_id: int, request: Request, db: Session = Depends(get_db)):
    plan = db.get(WorkoutPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    form = await request.form()
    start_date = _parse_date(form.get("start_date"))
    exists = db.execute(
        select(PlanEnrollment.id)
        .where(PlanEnrollment.plan_id == plan_id, PlanEnrollment.status == "active")
        .limit(1)
    ).scalar_one_or_none()
    error = None
    if exists is not None:
        error = "该计划已在执行中，先完成/放弃当前周期再重开"
    else:
        db.add(PlanEnrollment(plan_id=plan_id, start_date=start_date, status="active"))
        db.flush()
    return templates.TemplateResponse(
        request, "fragments/workout_enroll_panel.html", _enroll_panel_ctx(db, plan, error)
    )


def _end_enrollment(request: Request, db: Session, enrollment_id: int, status: str):
    enr = db.get(PlanEnrollment, enrollment_id)
    if enr is None:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    if enr.status == "active":
        enr.status = status
        enr.end_date = today_local()
        db.flush()
    if request.query_params.get("fragment") == "plan-cards":
        return templates.TemplateResponse(
            request, "fragments/workout_plan_cards.html", _plan_cards_ctx(db)
        )
    plan = db.get(WorkoutPlan, enr.plan_id)
    return templates.TemplateResponse(
        request, "fragments/workout_enroll_panel.html", _enroll_panel_ctx(db, plan)
    )


@router.post("/workout/enrollments/{enrollment_id}/finish")
def enrollment_finish(enrollment_id: int, request: Request, db: Session = Depends(get_db)):
    return _end_enrollment(request, db, enrollment_id, "done")


@router.post("/workout/enrollments/{enrollment_id}/abandon")
def enrollment_abandon(enrollment_id: int, request: Request, db: Session = Depends(get_db)):
    return _end_enrollment(request, db, enrollment_id, "abandoned")


# ---------- 记录：新建 + 编辑三件套 ----------
@router.post("/workout/logs")
async def log_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    fragment = str(form.get("fragment") or "manual")
    values, errors = _parse_log_form(form)

    saved = False
    if not errors:
        # 模板完成/计划归属：enrollment_id 合法时带上 plan_id
        raw_eid = str(form.get("enrollment_id") or "").strip()
        if raw_eid.isdigit():
            enr = db.get(PlanEnrollment, int(raw_eid))
            if enr is not None:
                values["enrollment_id"] = enr.id
                values["plan_id"] = enr.plan_id
        if str(form.get("from_template") or "") == "1":
            detail: dict[str, Any] = {"from_template": True}
            for key in ("slot", "pelvic"):
                v = str(form.get(key) or "").strip()
                if v:
                    detail[key] = v
            raw_week = str(form.get("week_no") or "").strip()
            if raw_week.isdigit():
                detail["week_no"] = int(raw_week)
            values["detail"] = detail
        db.add(WorkoutLog(source="manual", **values))
        db.flush()
        saved = True

    if fragment == "plan-cards":
        resp = templates.TemplateResponse(
            request, "fragments/workout_plan_cards.html", _plan_cards_ctx(db)
        )
    else:
        resp = templates.TemplateResponse(
            request,
            "fragments/workout_manual_form.html",
            _manual_form_ctx(saved=saved, errors=errors),
        )
    if saved:
        resp.headers.update(HX_TRIGGER)
    return resp


@router.get("/workout/logs/{log_id}/edit")
def log_edit_form(log_id: int, request: Request, db: Session = Depends(get_db)):
    log = _get_manual_log(db, log_id)
    return templates.TemplateResponse(
        request, "fragments/workout_log_edit.html", {"log": log, "errors": []}
    )


@router.put("/workout/logs/{log_id}")
async def log_update(log_id: int, request: Request, db: Session = Depends(get_db)):
    log = _get_manual_log(db, log_id)
    form = await request.form()
    values, errors = _parse_log_form(form)
    if errors:
        return templates.TemplateResponse(
            request, "fragments/workout_log_edit.html", {"log": log, "errors": errors}
        )
    for field, value in values.items():
        setattr(log, field, value)
    db.flush()
    return templates.TemplateResponse(
        request, "fragments/workout_log_row.html", _row_ctx(db, log), headers=dict(HX_TRIGGER)
    )


@router.delete("/workout/logs/{log_id}")
def log_delete(log_id: int, db: Session = Depends(get_db)):
    log = _get_manual_log(db, log_id)
    db.delete(log)
    db.flush()
    # 空响应 + outerHTML swap = 行消失；HX-Trigger 让热力图/列表被动刷新
    return Response(content="", headers=dict(HX_TRIGGER))


# ---------- 片段 ----------
@router.get("/fragments/workout/plan-cards")
def plan_cards_fragment(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "fragments/workout_plan_cards.html", _plan_cards_ctx(db)
    )


@router.get("/fragments/workout/recent")
def recent_fragment(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "fragments/workout_recent.html", _recent_ctx(db))


@router.get("/fragments/workout/heatmap")
def heatmap_fragment(request: Request, months: int = 3, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "fragments/workout_heatmap.html", _heatmap_ctx(db, months)
    )


@router.get("/fragments/workout/load")
def load_fragment(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "fragments/workout_load.html", _load_ctx(db))


@router.get("/fragments/workout/day")
def day_fragment(request: Request, d: str = "", db: Session = Depends(get_db)):
    if not d.strip():
        return Response(content="")  # 「收起」按钮：清空详情区
    try:
        day = date.fromisoformat(d.strip())
    except ValueError:
        return Response(content="")
    return templates.TemplateResponse(request, "fragments/workout_day_detail.html", _day_ctx(db, day))
