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
- GET  /api/agent/foods?q=    食物库搜索（MCP search_food 工具；热量估算辅助），
  近 90 天使用频次优先，与饮食页搜索同排序
- POST /api/agent/delete      {type: diet|workout, row_id}：改口纠错删除。
  仅 diet/workout；workout 仅 source='manual'（含 agent/offline 写入的
  manual+external_id 行）可删，keep/三星等外部来源 403——同步数据删了会被
  下次同步复活，且不是 agent 记错的东西
"""
from __future__ import annotations

import json
import re
# date 别名：summary 的查询参数就叫 date（对 agent 最直觉），避免遮蔽
from datetime import date as date_type, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    BodyMetrics, DailyActivity, DietLog, Food, WorkoutLog,
)
from app.routers.diet import _summary_ctx
from app.routers.ingest import _bearer_reject
from app.routers.offline import ingest_batch
from app.routers.report import _habit_checklist
from app.routers.review import _aggregate_week, _week_start_of
from app.timeutil import now_local, today_local

SOURCE = "agent"
FOOD_SEARCH_LIMIT = 10
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{1,2})$")

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
    habit_items, habit_done = _habit_checklist(db, d)

    return JSONResponse({
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
    })


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


@router.get("/api/agent/foods")
def agent_food_search(request: Request, q: str = "", db: Session = Depends(get_db)) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    q = q.strip()
    items: list[dict[str, Any]] = []
    if q:
        # 与 diet_food_search 同款：近 90 天使用频次优先，同频按名字长度
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
        items = [
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
    return JSONResponse({"q": q, "items": items})


# ---------- 改口纠错删除 ----------

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

    if rtype == "diet":
        log = db.get(DietLog, row_id)
        if log is None:
            return JSONResponse({"error": f"diet 记录不存在：{row_id}"}, status_code=404)
        summary = f"{log.log_date.isoformat()} {log.meal} {log.free_text or log.food_id}"
        if log.kcal is not None:
            summary += f" {_num(log.kcal):g}kcal"
        db.delete(log)
    else:
        w = db.get(WorkoutLog, row_id)
        if w is None:
            return JSONResponse({"error": f"workout 记录不存在：{row_id}"}, status_code=404)
        if w.source != "manual":
            # 外部同步源禁删：删了会被下次同步复活，且不是 agent 记错的东西
            return JSONResponse(
                {"error": f"外部来源（{w.source}）禁删，仅手动/agent 记录可删"},
                status_code=403,
            )
        summary = f"{w.log_date.isoformat()} {w.session_type or '训练'}"
        if w.duration_min:
            summary += f" {w.duration_min}分钟"
        db.delete(w)

    db.flush()
    return JSONResponse({"deleted": True, "type": rtype, "row_id": row_id, "summary": summary})
