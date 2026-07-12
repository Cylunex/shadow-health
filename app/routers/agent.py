"""多 Agent 统一接入通道（V3 批次 P2，docs/subpath-agent-plan.md §2）。

背景：Hermes 等 agent 此前直写旧库 personal_data 裸 SQL，假确认（说记了没记）与
日期错记反复发生。本模块给所有 agent（Hermes/OpenClaw/未来其他）一个带审计、
带幂等、带回执的统一入口，MCP server（mcp_server/）的工具全部落到这里。

端点（全部 Bearer=INGEST_TOKEN，不挂 session；写路径豁免 CSRF 的原因同秤/手表：
Authorization 头跨站伪造不了）：
- POST /api/ingest/agent      写通道：offline 管线薄别名（source='agent'，
  workout external_id='agent-{client_id}'），响应附 per-record 明细
  [{client_id, status, row_id}]——status ∈ new/skipped/failed，diet/workout
  的 new 带落库行 id（delete 纠错用），顶层保持 {received,new,skipped} 兼容
- GET  /api/agent/summary?date=YYYY-MM-DD   当日全景：饮食汇总+明细（带行 id）/
  步数/训练（带行 id）/体重/心情/打卡完成度。复用 diet._summary_ctx 与
  report._habit_checklist，口径与页面一致。date 非法直接 400——agent 通道
  绝不静默回退日期（旧伤疤就是日期错记）
- GET  /api/agent/report/weekly?week=YYYY-Wnn   周报数据（review._aggregate_week
  同一查询，口径与报告中心一致）；缺省=上一完整周
- GET  /api/agent/report/monthly?month=YYYY-MM   月报数据（report._aggregate_month
  同一查询）；缺省=上一完整月；complete=false 表示该月还没走完
- GET  /api/agent/context?days=N   全景上下文（llm.build_context 同一文本，
  与内置 AI 分析/问答注入的完全一致）——agent 做趋势分析一次拿全
- GET  /api/agent/metrics/series?field=&days=N   单指标逐日序列（体重/血压/
  心情等 metrics 页白名单字段 + steps），manual 标记字段是否手动值
- GET  /api/agent/foods?q=    食物库搜索（MCP search_food 工具；热量估算辅助），
  近 90 天使用频次优先，与饮食页搜索同排序
- POST /api/agent/delete      {type: diet|workout, row_id}：改口纠错删除。
  仅 diet/workout；workout 仅 source='manual'（含 agent/offline 写入的
  manual+external_id 行）可删，keep/三星等外部来源 403——同步数据删了会被
  下次同步复活，且不是 agent 记错的东西
- POST /api/agent/update      {type, row_id, fields}：改口修正（V5，不必删了
  重记）。只动 fields 里出现的键；校验与编辑表单同口径；workout 外部来源
  403 同 delete；食物关联 diet 行只能改 meal/amount_g（营养按食物库重算）
"""
from __future__ import annotations

import json
import re
import threading
# date 别名：summary 的查询参数就叫 date（对 agent 最直觉），避免遮蔽
from datetime import date as date_type, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AppSetting, BodyMetrics, DailyActivity, DietLog, Food, WorkoutLog,
)
from app.routers.diet import MEALS, _food_macros, _parse_decimal, _summary_ctx
from app.routers.ingest import _bearer_reject
from app.routers.metrics import _FIELD_DEFS, _bm_field_map
from app.routers.offline import ingest_batch, parse_workout_payload
from app.routers.report import _aggregate_month, _habit_checklist, _month_end, _month_start_of
from app.routers.review import _aggregate_week, _week_start_of
from app.services import llm
from app.timeutil import now_local, today_local

SOURCE = "agent"
FOOD_SEARCH_LIMIT = 10
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{1,2})$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})$")
CONTEXT_DAYS_MAX = 365
SERIES_DAYS_MAX = 366
# 序列字段白名单：metrics 页数值字段（字段 -> 中文名）+ steps（daily_activity）
_SERIES_FIELDS: dict[str, str] = {name: label for name, label, *_ in _FIELD_DEFS}
_SERIES_FIELDS["steps"] = "步数"

router = APIRouter()


def _num(v: Any) -> Any:
    """Decimal → float（JSON 可序列化）；int/None 原样。"""
    if v is None or isinstance(v, (bool, int)):
        return v
    return float(v)


# ---------- 写通道 ----------

@router.post("/api/ingest/agent")
async def ingest_agent(request: Request, db: Session = Depends(get_db)) -> Response:
    return await ingest_batch(request, db, source=SOURCE, with_results=True)


# ---------- 读端点 ----------

def summary_data(db: Session, d: date_type) -> dict[str, Any]:
    """当日全景数据（Bearer 端点与内置 AI query_summary 工具共用）。"""
    s = _summary_ctx(db, d)  # 与饮食页/日报同一聚合（含目标与能量缺口）
    entries = [
        {
            "id": log.id,
            "meal": log.meal,
            "name": fname or log.free_text or "—",
            "amount_g": _num(log.amount_g),
            "kcal": _num(log.kcal),
            "protein_g": _num(log.protein_g),
        }
        for log, fname in db.execute(
            select(DietLog, Food.name)
            .outerjoin(Food, DietLog.food_id == Food.id)
            .where(DietLog.log_date == d)
            .order_by(DietLog.id)
        )
    ]

    activity = db.get(DailyActivity, d)
    wlogs = db.execute(
        select(WorkoutLog).where(WorkoutLog.log_date == d).order_by(WorkoutLog.id)
    ).scalars().all()
    workouts = [
        {
            "id": w.id,
            "type": w.session_type,
            "duration_min": w.duration_min,
            "distance_km": _num(w.distance_km),
            "kcal": w.calories,
            "rpe": w.rpe,
            "source": w.source,
        }
        for w in wlogs
    ]

    bm = db.execute(select(BodyMetrics).where(BodyMetrics.log_date == d)).scalar_one_or_none()
    # 当日全部非空指标字段（白名单同 metrics 页）：血压/血氧等写得进就读得出
    metric_values: dict[str, Any] = {}
    if bm is not None:
        for name, *_ in _FIELD_DEFS:
            v = getattr(bm, name)
            if v is not None:
                metric_values[name] = _num(v)
    habit_items, habit_done = _habit_checklist(db, d)

    return {
        "date": d.isoformat(),
        "diet": {
            "kcal": _num(s["total_kcal"]),
            "protein_g": _num(s["total_protein"]),
            "fat_g": _num(s["total_fat"]),
            "carb_g": _num(s["total_carb"]),
            "target_kcal": _num(s["target_kcal"]),
            "target_protein_g": _num(s["target_protein"]),
            "streak_days": s["diet_streak"],
            "energy_gap_kcal": s["energy_gap"],  # 摄入−(BMR+活动)；无秤 BMR 为 null
            "entries": entries,
        },
        "steps": activity.steps if activity is not None else None,
        "workouts": workouts,
        "workout_min": sum(w.duration_min or 0 for w in wlogs),
        "weight_kg": _num(bm.weight_kg) if bm is not None else None,
        "body_fat_pct": _num(bm.body_fat_pct) if bm is not None else None,
        "mood_score": bm.mood_score if bm is not None else None,
        "sleep_hours": _num(bm.sleep_hours) if bm is not None else None,
        "metrics": metric_values,
        "habits": {
            "done": habit_done,
            "total": len(habit_items),
            "items": [
                {"id": i["id"], "name": i["name"], "count": i["count"],
                 "target": i["target"], "done": i["done"]}
                for i in habit_items
            ],
        },
        "generated_at": now_local().isoformat(),
    }


@router.get("/api/agent/summary")
def agent_summary(request: Request, date: str = "", db: Session = Depends(get_db)) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    if date.strip():
        try:
            d = date_type.fromisoformat(date.strip())
        except ValueError:
            return JSONResponse({"error": f"date 不是合法日期：{date!r}"}, status_code=400)
    else:
        d = today_local()
    return JSONResponse(summary_data(db, d))


@router.get("/api/agent/report/weekly")
def agent_weekly_report(
    request: Request, week: str = "", db: Session = Depends(get_db)
) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    today = today_local()
    if week.strip():
        m = _WEEK_RE.match(week.strip())
        if m is None:
            return JSONResponse(
                {"error": f"week 格式应为 YYYY-Wnn：{week!r}"}, status_code=400
            )
        try:
            ws = date_type.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            return JSONResponse({"error": f"不存在的 ISO 周：{week!r}"}, status_code=400)
    else:
        ws = _week_start_of(today) - timedelta(days=7)  # 缺省 = 上一完整周

    iso = ws.isocalendar()
    snap = _aggregate_week(db, ws)  # 与报告中心周报同一查询
    return JSONResponse({
        "week": f"{iso.year}-W{iso.week:02d}",
        # 该周是否已走完（进行中的周数据不全，agent 播报时应注明）
        "complete": ws + timedelta(days=7) <= today,
        **snap,
    })


@router.get("/api/agent/report/monthly")
def agent_monthly_report(
    request: Request, month: str = "", db: Session = Depends(get_db)
) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    today = today_local()
    if month.strip():
        m = _MONTH_RE.match(month.strip())
        if m is None or not (1 <= int(m.group(2)) <= 12):
            return JSONResponse(
                {"error": f"month 格式应为 YYYY-MM：{month!r}"}, status_code=400
            )
        ms = date_type(int(m.group(1)), int(m.group(2)), 1)
    else:
        ms = _month_start_of(_month_start_of(today) - timedelta(days=1))  # 上一完整月
    if ms > today:
        return JSONResponse({"error": f"month 是未来月份：{ms:%Y-%m}"}, status_code=400)

    snap = _aggregate_month(db, ms)  # 与报告中心月报同一查询
    return JSONResponse({
        "month": f"{ms:%Y-%m}",
        # 该月是否已走完（进行中的月份数据不全，agent 播报时应注明）
        "complete": _month_end(ms) < today,
        **snap,
    })


@router.get("/api/agent/context")
def agent_context(request: Request, days: int = 30, db: Session = Depends(get_db)) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    if not (1 <= days <= CONTEXT_DAYS_MAX):
        return JSONResponse(
            {"error": f"days 超出范围（1~{CONTEXT_DAYS_MAX}）：{days}"}, status_code=400
        )
    # 与内置 AI 分析/问答注入的上下文完全一致（目标/体重/围度/心情/睡眠/步数/
    # 训练/饮食/习惯/周月报快照），agent 做趋势分析一次拿全
    return JSONResponse({
        "days": days,
        "context": llm.build_context(db, days),
        "generated_at": now_local().isoformat(),
    })


@router.get("/api/agent/metrics/series")
def agent_metric_series(
    request: Request, field: str = "", days: int = 30, db: Session = Depends(get_db)
) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    field = field.strip()
    if field not in _SERIES_FIELDS:
        return JSONResponse(
            {"error": f"field 不在白名单内：{field!r}。可用：{'/'.join(_SERIES_FIELDS)}"},
            status_code=400,
        )
    if not (1 <= days <= SERIES_DAYS_MAX):
        return JSONResponse(
            {"error": f"days 超出范围（1~{SERIES_DAYS_MAX}）：{days}"}, status_code=400
        )
    today = today_local()
    start = today - timedelta(days=days - 1)
    series: list[dict[str, Any]] = []
    if field == "steps":
        rows = db.execute(
            select(DailyActivity.log_date, DailyActivity.steps)
            .where(DailyActivity.log_date.between(start, today), DailyActivity.steps.is_not(None))
            .order_by(DailyActivity.log_date)
        ).all()
        series = [{"date": d.isoformat(), "value": v} for d, v in rows]
    else:
        by_day = _bm_field_map(db, field, start, today)  # 与指标页图表同一取数
        series = [
            {"date": d.isoformat(), "value": v, "manual": manual}
            for d, (v, manual) in sorted(by_day.items())
        ]
    return JSONResponse({
        "field": field,
        "label": _SERIES_FIELDS[field],
        "days": days,
        "since": start.isoformat(),
        "series": series,
    })


def food_search_items(db: Session, q: str) -> list[dict[str, Any]]:
    """食物库搜索（Bearer 端点与内置 AI search_food 工具共用）：
    与 diet_food_search 同款——近 90 天使用频次优先，同频按名字长度。"""
    q = q.strip()
    if not q:
        return []
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    since = today_local() - timedelta(days=89)
    usage = (
        select(DietLog.food_id.label("fid"), func.count().label("n"))
        .where(DietLog.food_id.is_not(None), DietLog.log_date >= since)
        .group_by(DietLog.food_id)
        .subquery()
    )
    rows = db.execute(
        select(Food, func.coalesce(usage.c.n, 0))
        .outerjoin(usage, Food.id == usage.c.fid)
        .where(Food.name.ilike(f"%{esc}%", escape="\\"))
        .order_by(func.coalesce(usage.c.n, 0).desc(), func.length(Food.name), Food.name)
        .limit(FOOD_SEARCH_LIMIT)
    ).all()
    return [
        {
            "id": f.id,
            "name": f.name,
            "category": f.category,
            "kcal_per_100g": _num(f.kcal_per_100g),
            "protein_g_per_100g": _num(f.protein_g),
            "fat_g_per_100g": _num(f.fat_g),
            "carb_g_per_100g": _num(f.carb_g),
        }
        for f, _n in rows
    ]


@router.get("/api/agent/foods")
def agent_food_search(request: Request, q: str = "", db: Session = Depends(get_db)) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    return JSONResponse({"q": q.strip(), "items": food_search_items(db, q)})


# ---------- 改口纠错删除 ----------

def delete_record(db: Session, rtype: str, row_id: int) -> tuple[int, dict[str, Any]]:
    """diet/workout 纠错删除的共享实现（Bearer 端点与 /agent-log 撤销共用）。

    返回 (HTTP 状态码, 响应体)；200 时已 flush（commit 交给请求收尾）。
    """
    if rtype == "diet":
        log = db.get(DietLog, row_id)
        if log is None:
            return 404, {"error": f"diet 记录不存在：{row_id}"}
        summary = f"{log.log_date.isoformat()} {log.meal} {log.free_text or log.food_id}"
        if log.kcal is not None:
            summary += f" {_num(log.kcal):g}kcal"
        db.delete(log)
    else:
        w = db.get(WorkoutLog, row_id)
        if w is None:
            return 404, {"error": f"workout 记录不存在：{row_id}"}
        if w.source != "manual":
            # 外部同步源禁删：删了会被下次同步复活，且不是 agent 记错的东西
            return 403, {"error": f"外部来源（{w.source}）禁删，仅手动/agent 记录可删"}
        summary = f"{w.log_date.isoformat()} {w.session_type or '训练'}"
        if w.duration_min:
            summary += f" {w.duration_min}分钟"
        db.delete(w)

    db.flush()
    return 200, {"deleted": True, "type": rtype, "row_id": row_id, "summary": summary}


# ---------- 内置 AI 分析报告（读取 + 触发，V5） ----------

@router.get("/api/agent/analysis")
def agent_analysis(request: Request, db: Session = Depends(get_db)) -> Response:
    """最近一次内置 AI 分析报告 + 任务状态（job ∈ idle/running/done/failed）。"""
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    from app.routers.ai import _CACHE_KEY, _job_state

    row = db.get(AppSetting, _CACHE_KEY)
    analysis = None
    if row is not None and isinstance(row.value, dict):
        analysis = {
            "content": row.value.get("content", ""),
            "generated_at": row.value.get("generated_at", ""),
            "days": row.value.get("days"),
        }
    state = _job_state(db)
    return JSONResponse({
        "job": state.get("status") or "idle",
        "error": state.get("error"),
        "analysis": analysis,
    })


@router.post("/api/agent/analysis")
async def agent_analysis_run(request: Request, db: Session = Depends(get_db)) -> Response:
    """触发内置 AI 分析（后台线程 + app_settings 轮询，与 /ai/analyze 同一 job，
    互斥防并发覆盖）。agent 触发后轮询 GET /api/agent/analysis 取结果。"""
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    try:
        payload = json.loads(await request.body() or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "unsupported payload"}, status_code=400)
    from app.routers.ai import _DAYS_OPTIONS, _JOB_KEY, _job_state, _run_analysis, _set_setting

    try:
        days = int(str(payload.get("days", 30)).strip())
    except (TypeError, ValueError):
        days = 0
    if days not in _DAYS_OPTIONS:
        return JSONResponse(
            {"error": f"days 仅支持 {'/'.join(map(str, _DAYS_OPTIONS))}"}, status_code=400
        )
    state = _job_state(db)
    if state.get("status") == "running":
        return JSONResponse({"started": False, "job": "running", "days": state.get("days")})
    if not llm.is_configured(db):
        return JSONResponse(
            {"error": "未配置 AI 模型 API Key（设置→AI 模型）"}, status_code=400
        )
    _set_setting(db, _JOB_KEY, {
        "status": "running", "days": days, "started_at": now_local().isoformat(),
    })
    db.commit()  # 后台线程用独立会话读状态，必须先落库（与 /ai/analyze 同口径）
    threading.Thread(target=_run_analysis, args=(days,), daemon=True).start()
    return JSONResponse({"started": True, "job": "running", "days": days})


# ---------- 改口修正（不必删了重记） ----------

_DIET_UPDATE_FIELDS = ("meal", "free_text", "amount_g", "kcal", "protein_g", "fat_g", "carb_g")
_WORKOUT_UPDATE_FIELDS = ("session_type", "duration_min", "distance_km", "calories", "rpe", "notes")


def update_record(db: Session, rtype: str, row_id: int, fields: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """diet/workout 部分字段修正的共享实现（Bearer 端点与 MCP update_record 用）。

    只动 fields 里出现的键（显式传 null = 清空可选字段）；校验与对应编辑表单
    同口径（diet._parse_decimal / offline.parse_workout_payload）。
    返回 (HTTP 状态码, 响应体)；200 时已 flush（commit 交给请求收尾）。
    """
    if not fields:
        return 400, {"error": "fields 不能为空"}

    if rtype == "diet":
        unknown = set(fields) - set(_DIET_UPDATE_FIELDS)
        if unknown:
            return 400, {"error": f"diet 不支持的字段：{'/'.join(sorted(unknown))}"}
        log = db.get(DietLog, row_id)
        if log is None:
            return 404, {"error": f"diet 记录不存在：{row_id}"}
        try:
            if "meal" in fields:
                meal = str(fields["meal"] or "").strip()
                if meal not in MEALS:
                    raise ValueError(f"meal 不在词表内：{meal!r}")
                log.meal = meal
            if log.food_id is not None:
                # 食物关联行：营养值按食物库自动计算，只能改 meal/amount_g（UI 同约束）
                locked = set(fields) & {"free_text", "kcal", "protein_g", "fat_g", "carb_g"}
                if locked:
                    return 400, {"error": "食物关联记录的营养值按食物库计算，只能改 meal/amount_g"}
                if "amount_g" in fields:
                    amount = _parse_decimal(fields["amount_g"], "用量", 5000)
                    if amount is None:
                        raise ValueError("食物关联记录的用量不能清空")
                    log.amount_g = amount
                    food = db.get(Food, log.food_id)
                    if food is not None:  # 冗余值按用量重算，保持与食物库一致
                        log.kcal, log.protein_g, log.fat_g, log.carb_g = _food_macros(food, amount)
            else:
                if "free_text" in fields:
                    free_text = str(fields["free_text"] or "").strip()
                    if not free_text:
                        raise ValueError("free_text 不能清空")
                    log.free_text = free_text[:500]
                for key, label, hi in (
                    ("amount_g", "用量", 5000), ("kcal", "热量", 20000),
                    ("protein_g", "蛋白质", 1000), ("fat_g", "脂肪", 1000),
                    ("carb_g", "碳水", 2000),
                ):
                    if key in fields:
                        setattr(log, key, _parse_decimal(fields[key], label, hi))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        db.flush()
        summary = f"{log.log_date.isoformat()} {log.meal} {log.free_text or log.food_id}"
        if log.kcal is not None:
            summary += f" {_num(log.kcal):g}kcal"
        return 200, {"updated": True, "type": rtype, "row_id": row_id, "summary": summary}

    # workout
    unknown = set(fields) - set(_WORKOUT_UPDATE_FIELDS)
    if unknown:
        return 400, {"error": f"workout 不支持的字段：{'/'.join(sorted(unknown))}"}
    w = db.get(WorkoutLog, row_id)
    if w is None:
        return 404, {"error": f"workout 记录不存在：{row_id}"}
    if w.source != "manual":
        return 403, {"error": f"外部来源（{w.source}）禁改，仅手动/agent 记录可改"}
    merged: dict[str, Any] = {
        key: getattr(w, key) for key in _WORKOUT_UPDATE_FIELDS
    }
    merged.update({k: fields[k] for k in _WORKOUT_UPDATE_FIELDS if k in fields})
    try:
        cols = parse_workout_payload(merged)  # 整体重校验：界值与表单/ingest 同口径
    except ValueError as exc:
        return 400, {"error": str(exc)}
    for key, value in cols.items():
        setattr(w, key, value)
    db.flush()
    summary = f"{w.log_date.isoformat()} {w.session_type or '训练'}"
    if w.duration_min:
        summary += f" {w.duration_min}分钟"
    return 200, {"updated": True, "type": rtype, "row_id": row_id, "summary": summary}


@router.post("/api/agent/update")
async def agent_update(request: Request, db: Session = Depends(get_db)) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    try:
        payload = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "unsupported payload"}, status_code=400)
    rtype = str(payload.get("type") or "").strip()
    if rtype not in ("diet", "workout"):
        return JSONResponse(
            {"error": f"type 仅支持 diet/workout：{rtype!r}"}, status_code=400
        )
    try:
        row_id = int(str(payload.get("row_id")).strip())
    except (TypeError, ValueError):
        return JSONResponse({"error": "row_id 不是合法整数"}, status_code=400)
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        return JSONResponse({"error": "fields 缺失或不是对象"}, status_code=400)

    status, body = update_record(db, rtype, row_id, fields)
    return JSONResponse(body, status_code=status)


@router.post("/api/agent/delete")
async def agent_delete(request: Request, db: Session = Depends(get_db)) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    try:
        payload = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "unsupported payload"}, status_code=400)
    rtype = str(payload.get("type") or "").strip()
    if rtype not in ("diet", "workout"):
        return JSONResponse(
            {"error": f"type 仅支持 diet/workout：{rtype!r}"}, status_code=400
        )
    try:
        row_id = int(str(payload.get("row_id")).strip())
    except (TypeError, ValueError):
        return JSONResponse({"error": "row_id 不是合法整数"}, status_code=400)

    status, body = delete_record(db, rtype, row_id)
    return JSONResponse(body, status_code=status)
