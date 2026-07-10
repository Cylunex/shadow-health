"""手机离线记录补发通道 + 本地启动页引导数据（docs/offline-plan.md 阶段一）。

POST /api/ingest/offline —— 壳本地队列批量补发（Bearer 鉴权，与秤/手表同通道）：
  {"records": [{"type", "client_id", "date", "payload"}]}
- 留档幂等：全部先落 import_raw，source='offline'、external_id=f"{type}-{client_id}"
  （client_id 由壳生成 UUID）——重复补发只刷新 last_seen_at；归一化按
  parse_status 门控：**parsed 绝不重放**（防 diet 直插双写），pending/failed
  随重发自动重试（上一轮批级失败后壳端重试即自愈，离线记录没有上游可重拉，
  不能像传感器通道那样只认新行）
- 归一化各类型幂等语义（方案 §3）：
  * habit   → habit_logs ON CONFLICT (habit_id, log_date) DO NOTHING：声明式
    「该日已做」，先到先得（含否决行/已补发计数，见方案 §4），重放安全；
    校验习惯存在且 active
  * diet    → DietLog 直插（parse_status 门控挡重复；数值复用 diet 页 _parse_decimal）
  * workout → WorkoutLog source='manual' + external_id='offline-{client_id}'
    （部分唯一索引现成，天然幂等）
  * metric  → 视同手动保存：直接覆盖 + mark_manual（队列 FIFO，同日多条后写胜出，
    与秤/手表同日取最后一次的口径一致；mark_manual 后自动同步不可覆盖）；
    字段限 metrics 页数值字段白名单
- 单条失败 begin_nested 隔离，raw 标 failed 留痕（导入中心可审计），响应仍 200
  {received, new, skipped}；**批级失败（DB 抖动等系统性错误）返回 503**——
  壳端保住队列按退避重试，配合 parse_status 门控做到不丢不重
- 本通道假定单客户端（个人手机）串行补发；并发双发同批次可能使 diet 双写

GET /api/offline/bootstrap —— 壳每次成功加载页面后拉取并缓存到 SharedPreferences
（Bearer）：active 习惯清单 + 常用训练类型（近 90 天手动记录频次，词表兜底）+
餐次词表，供离线本地启动页渲染打卡清单与快记表单。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import DietLog, Habit, HabitLog, ImportRaw, SyncState, WorkoutLog
from app.routers.diet import MEALS, _parse_decimal
from app.routers.ingest import MAX_BODY_BYTES, RAW_BATCH, _bearer_reject
from app.routers.metrics import _FIELD_DEFS
from app.routers.workout import SESSION_TYPE_HINTS
from app.services.autofill import get_or_create_day, mark_manual
from app.timeutil import now_local, today_local

SOURCE = "offline"
PARSER_VERSION = 1
RECORD_TYPES = ("habit", "diet", "workout", "metric")
BOOTSTRAP_TYPES_MAX = 12

# metric 白名单：metrics 页同一批数值字段（字段 -> (中文名, 类型, 下限, 上限)）
_METRIC_BOUNDS: dict[str, tuple[str, str, float, float]] = {
    name: (label, kind, lo, hi) for name, label, kind, lo, hi in _FIELD_DEFS
}

router = APIRouter()


# ---------- payload 校验（纯函数，pytest 直测） ----------

def parse_record_date(raw: Any, today: date) -> date:
    """壳本地日期（时钟即真相，但有 sanity 界）：容 1 天时钟偏差；一年以前视为
    坏时钟（RTC 掉电重置），拒收防污染历史统计（与 miscale 的 ts.year<2015 兜底同旨）。"""
    try:
        d = date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"date 不是合法日期：{raw!r}")
    if d > today + timedelta(days=1):
        raise ValueError(f"date 是未来日期：{d.isoformat()}")
    if d < today - timedelta(days=366):
        raise ValueError(f"date 过于久远（超过一年）：{d.isoformat()}")
    return d


def _payload_decimal(
    payload: dict, key: str, label: str, lo: float, hi: float, quant: str = "0.1"
) -> Decimal | None:
    """缺失/空 → None；非法或越界 → ValueError（与 metrics 表单口径一致）。"""
    raw = payload.get(key)
    s = str(raw).strip() if raw is not None else ""
    if not s:
        return None
    try:
        v = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{label}格式不正确")
    if not v.is_finite():
        raise ValueError(f"{label}格式不正确")
    if not (Decimal(str(lo)) <= v <= Decimal(str(hi))):
        raise ValueError(f"{label}超出合理范围（{lo:g}~{hi:g}）")
    return v.quantize(Decimal(quant))


def _payload_int(payload: dict, key: str, label: str, lo: int, hi: int) -> int | None:
    raw = payload.get(key)
    s = str(raw).strip() if raw is not None else ""
    if not s:
        return None
    try:
        v = int(s)
    except ValueError:
        raise ValueError(f"{label}格式不正确")
    if not (lo <= v <= hi):
        raise ValueError(f"{label}超出合理范围（{lo}~{hi}）")
    return v


def parse_diet_payload(payload: dict) -> dict[str, Any]:
    """diet 记录 → DietLog 列值；meal 必须在词表内、free_text 必填。
    数值直接复用 diet 页的 _parse_decimal（同界值、同舍入、同报错口径，防漂移）。"""
    meal = str(payload.get("meal") or "").strip()
    if meal not in MEALS:
        raise ValueError(f"meal 不在词表内：{meal!r}")
    free_text = str(payload.get("free_text") or "").strip()
    if not free_text:
        raise ValueError("diet 记录缺少 free_text")
    return {
        "meal": meal,
        "free_text": free_text[:500],
        "amount_g": _parse_decimal(payload.get("amount_g"), "用量", 5000),
        "kcal": _parse_decimal(payload.get("kcal"), "热量", 20000),
        "protein_g": _parse_decimal(payload.get("protein_g"), "蛋白质", 1000),
        "fat_g": _parse_decimal(payload.get("fat_g"), "脂肪", 1000),
        "carb_g": _parse_decimal(payload.get("carb_g"), "碳水", 2000),
    }


def parse_workout_payload(payload: dict) -> dict[str, Any]:
    """workout 记录 → WorkoutLog 列值；session_type 必填（界值与 workout 页
    _parse_log_form 对齐：时长 0-1440、距离 0-1000 两位小数、RPE 1-10）。"""
    session_type = str(payload.get("session_type") or "").strip()[:50]
    if not session_type:
        raise ValueError("workout 记录缺少 session_type")
    notes = str(payload.get("notes") or "").strip()
    distance = _payload_decimal(payload, "distance_km", "距离", 0, 1000, quant="0.01")
    return {
        "session_type": session_type,
        "duration_min": _payload_int(payload, "duration_min", "时长", 0, 1440),
        "distance_km": distance if distance is not None and distance > 0 else None,
        "calories": _payload_int(payload, "calories", "消耗", 1, 20000),
        "rpe": _payload_int(payload, "rpe", "RPE", 1, 10),
        "notes": notes[:2000] or None,
    }


def parse_metric_payload(payload: dict) -> dict[str, Any]:
    """metric 记录 → body_metrics 字段值；只允许 metrics 页数值字段白名单。"""
    values: dict[str, Any] = {}
    for field in payload:
        bounds = _METRIC_BOUNDS.get(field)
        if bounds is None:
            raise ValueError(f"不支持的指标字段：{field}")
        label, kind, lo, hi = bounds
        parsed = (
            _payload_int(payload, field, label, int(lo), int(hi))
            if kind == "int"
            else _payload_decimal(payload, field, label, lo, hi)
        )
        if parsed is not None:
            values[field] = parsed
    if not values:
        raise ValueError("metric 记录没有可写字段")
    return values


def scrub_nul(value: Any) -> Any:
    """递归剔除字符串里的 \\x00：PostgreSQL JSONB 拒收 \\u0000，一条毒记录
    会让整批留档 500、卡死壳端整个队列。"""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {scrub_nul(k): scrub_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_nul(v) for v in value]
    return value


# ---------- 归一化 ----------

def _normalize_habit(db: Session, payload: dict, d: date) -> None:
    try:
        # 经 str() 严格解析：裸 int() 会把 True 当 1、2.9 截成 2，静默打错习惯
        habit_id = int(str(payload.get("habit_id")).strip())
    except (TypeError, ValueError):
        raise ValueError("habit 记录缺少合法 habit_id")
    habit = db.get(Habit, habit_id)
    if habit is None:
        raise ValueError(f"习惯不存在：{habit_id}")
    if not habit.active:
        raise ValueError(f"习惯已停用：{habit.name}")
    done = _payload_int(payload, "done_count", "打卡次数", 1, 99)
    db.execute(
        pg_insert(HabitLog)
        .values(habit_id=habit_id, log_date=d, done_count=done if done is not None else 1)
        .on_conflict_do_nothing(index_elements=["habit_id", "log_date"])
    )


def _normalize_diet(db: Session, payload: dict, d: date) -> None:
    db.add(DietLog(log_date=d, **parse_diet_payload(payload)))
    db.flush()


def _normalize_workout(db: Session, payload: dict, d: date, client_id: str) -> None:
    ins = pg_insert(WorkoutLog).values(
        log_date=d,
        source="manual",
        external_id=f"offline-{client_id}",
        **parse_workout_payload(payload),
    )
    db.execute(
        ins.on_conflict_do_nothing(
            index_elements=["source", "external_id"],
            index_where=WorkoutLog.__table__.c.external_id.isnot(None),
        )
    )


def _normalize_metric(db: Session, payload: dict, d: date) -> None:
    """离线 metric = 手动保存：直接覆盖 + mark_manual（同步不可覆盖）。

    队列 FIFO 补发 → 同日多条后写胜出（早晨 71.5、晚上复称 72.3 → 记 72.3），
    与秤/手表「同日取最后一次」口径一致；若用 autofill 的不覆盖语义，第二条
    离线记录会被第一条的 mark_manual 挡住而静默丢弃。重复补发由 parse_status
    门控挡在归一化之外，不会双写。
    """
    values = parse_metric_payload(payload)
    row = get_or_create_day(db, d)
    for field, value in values.items():
        setattr(row, field, value)
    mark_manual(row, list(values))
    db.flush()


def _mark(db: Session, rtype: str, ext_id: str, status: str, error: str | None = None) -> None:
    db.execute(
        update(ImportRaw)
        .where(
            ImportRaw.source == SOURCE,
            ImportRaw.record_type == rtype,
            ImportRaw.external_id == ext_id,
        )
        .values(parse_status=status, parse_error=error, parse_version=PARSER_VERSION)
    )


# ---------- 端点 ----------

@router.post("/api/ingest/offline")
async def ingest_offline(request: Request, db: Session = Depends(get_db)) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    # 上限先查 Content-Length 再读 body 复核（与 health_connect 同口径，防内存打爆）
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        records = payload["records"]
    elif isinstance(payload, list):
        records = payload
    else:
        return JSONResponse({"error": "unsupported payload"}, status_code=400)
    received = len(records)

    now = now_local()
    today = today_local()

    # 1. 结构校验（type/client_id 是留档键，缺了没法幂等，直接丢弃计入 received）
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            continue
        rtype = str(item.get("type") or "").strip()
        client_id = str(item.get("client_id") or "").strip()
        if rtype not in RECORD_TYPES or not client_id or len(client_id) > 100:
            continue
        ext_id = f"{rtype}-{client_id}"
        if ext_id in seen:
            continue
        seen.add(ext_id)
        entries.append({
            "rtype": rtype,
            "ext_id": ext_id,
            "client_id": client_id,
            "rec": scrub_nul(item),  # JSONB 拒收 ，毒记录会卡死整个队列
        })

    if not entries:
        return JSONResponse({"received": received, "new": 0, "skipped": 0})

    # 2. 整包落 import_raw（重复补发只刷新 last_seen_at）。留档失败 503：
    #    壳端保队列重试，绝不能让队列在 4xx/2xx 下被清掉
    try:
        for i in range(0, len(entries), RAW_BATCH):
            chunk = [
                {
                    "source": SOURCE,
                    "record_type": e["rtype"],
                    "external_id": e["ext_id"],
                    "raw": e["rec"],
                    "parse_status": "pending",
                    "parse_version": 0,
                    "last_seen_at": now,
                }
                for e in entries[i:i + RAW_BATCH]
            ]
            ins = pg_insert(ImportRaw).values(chunk)
            db.execute(ins.on_conflict_do_update(
                index_elements=["source", "record_type", "external_id"],
                set_={"last_seen_at": now},
            ))
        db.commit()  # 原始留档先落盘：后续归一化再出错也不丢数据
    except Exception:
        db.rollback()
        return JSONResponse({"error": "archive failed"}, status_code=503)

    # 3. 归一化对象 = 本批中尚未成功归一化的：新行是 pending；上一轮批级失败留下的
    #    pending/failed 随本次补发自动重试（自愈）。parsed 绝不重放，防 diet 直插双写。
    status_by_ext: dict[str, str] = {
        ext: status
        for ext, status in db.execute(
            select(ImportRaw.external_id, ImportRaw.parse_status).where(
                ImportRaw.source == SOURCE,
                ImportRaw.external_id.in_([e["ext_id"] for e in entries]),
            )
        )
    }
    todo = [e for e in entries if status_by_ext.get(e["ext_id"]) != "parsed"]
    skipped = len(entries) - len(todo)
    new_ok = 0

    # 4. 归一化（单条失败 begin_nested 隔离，响应仍 200；批级失败 503 触发壳端重试）
    try:
        for e in todo:
            rtype, ext_id, rec = e["rtype"], e["ext_id"], e["rec"]
            try:
                with db.begin_nested():
                    d = parse_record_date(rec.get("date"), today)
                    pl = rec.get("payload")
                    if not isinstance(pl, dict):
                        raise ValueError("payload 缺失或不是对象")
                    if rtype == "habit":
                        _normalize_habit(db, pl, d)
                    elif rtype == "diet":
                        _normalize_diet(db, pl, d)
                    elif rtype == "workout":
                        _normalize_workout(db, pl, d, e["client_id"])
                    else:
                        _normalize_metric(db, pl, d)
                _mark(db, rtype, ext_id, "parsed")
                new_ok += 1
            except Exception as exc:
                _mark(db, rtype, ext_id, "failed", str(exc)[:500])

        state = db.get(SyncState, SOURCE)
        if state is None:
            state = SyncState(source=SOURCE)
            db.add(state)
        state.last_success_at = now
        state.last_error = None
        state.consecutive_failures = 0
        db.commit()
    except Exception as exc:
        # 批级失败（DB 抖动等系统性错误）：raw 标 failed 留痕后回 503——壳端保队列
        # 按退避重试，下一轮 failed 行会重新归一化（第 3 步门控），不丢不重
        db.rollback()
        try:
            for e in todo:
                _mark(db, e["rtype"], e["ext_id"], "failed", f"批次归一化失败：{str(exc)[:400]}")
            state = db.get(SyncState, SOURCE)
            if state is None:
                state = SyncState(source=SOURCE)
                db.add(state)
            state.consecutive_failures = (state.consecutive_failures or 0) + 1
            state.last_error = str(exc)[:2000]
            db.commit()
        except Exception:
            db.rollback()
        return JSONResponse({"error": "batch normalization failed"}, status_code=503)

    return JSONResponse({"received": received, "new": new_ok, "skipped": skipped})


@router.get("/api/offline/bootstrap")
def offline_bootstrap(request: Request, db: Session = Depends(get_db)) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject

    habits = db.execute(
        select(Habit).where(Habit.active.is_(True)).order_by(Habit.sort, Habit.id)
    ).scalars().all()
    habit_items = [
        {
            "id": h.id,
            "name": h.name,
            "period": h.period,
            "target": h.target_per_period or 1,
            "time_hint": h.time_hint,
            "auto": bool(h.auto_rule),  # 自动判定习惯：离线清单标注，避免重复手记
        }
        for h in habits
    ]

    # 常用训练类型：近 90 天手动记录按频次排序，词表兜底补足
    since = today_local() - timedelta(days=90)
    rows = db.execute(
        select(WorkoutLog.session_type, func.count().label("n"))
        .where(
            WorkoutLog.source == "manual",
            WorkoutLog.log_date >= since,
            WorkoutLog.session_type.is_not(None),
        )
        .group_by(WorkoutLog.session_type)
        .order_by(func.count().desc())
    ).all()
    workout_types: list[str] = [r[0] for r in rows if r[0]]
    for hint in SESSION_TYPE_HINTS:
        if len(workout_types) >= BOOTSTRAP_TYPES_MAX:
            break
        if hint not in workout_types:
            workout_types.append(hint)

    return JSONResponse({
        "habits": habit_items,
        "workout_types": workout_types[:BOOTSTRAP_TYPES_MAX],
        "meals": list(MEALS),
        "generated_at": now_local().isoformat(),
    })
