"""Health Connect webhook 接收端（设计文档 §3.7 通道 2，M4a）。

POST /api/ingest/health_connect
- Bearer token 鉴权（secrets.compare_digest；token 未配置 503；失败 401 无 body 细节）；
  此路由豁免 session/CSRF（main.py 中间件已放行 /api/ingest/*），仅此一个。
- 请求体上限 5MB：读 body 前查 Content-Length，读后复核实际长度，超限 413。
- payload 结构防御式提取：顶层 list 视为记录数组；顶层 dict 按 'records'/'data'
  取数组，取不到把整个 dict 当单条记录。
- 每条记录：键名启发式推断 record_type；external_id 取 metadata.id / id / uid，
  都没有回退 sha1(记录 JSON)；先整包落 import_raw（ON CONFLICT 只刷新
  last_seen_at ≈ DO NOTHING），随即固定返回 200 {"received": N}。
- 归一化在同请求 try/except：
  * 水位线：记录时间 <= sync_state('health_connect').watermark 置 parse_status='skipped'
   （留档可审计，与三星 zip 历史导入一刀切去重）；
  * steps → daily_activity 按日增量累加（HC Steps 是区间记录，import_raw 去重
    保证重发不重复累计）；weight → body_metrics autofill；sleep → sleep_sessions
    upsert + 按 wake_date 汇总回填 sleep_hours；exercise → workout_logs upsert；
  * 单条失败置 parse_status='failed' 记 parse_error；整体失败只记
    sync_state.consecutive_failures，绝不 5xx（防手机端重发风暴）。
- heart_rate / unknown 类型仅留档（parse_status 保持 pending，供后续解析器重放）。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import DailyActivity, ImportRaw, SleepSession, SyncState, WorkoutLog
from app.services.autofill import autofill_fields
from app.timeutil import LOCAL_TZ, now_local

MAX_BODY_BYTES = 5 * 1024 * 1024
PARSER_VERSION = 1
SOURCE = "health_connect"
RAW_BATCH = 500

# webhook 走 Bearer 鉴权，不挂 require_login（设计文档 §3.7/§7.2 豁免项）
router = APIRouter(prefix="/api/ingest")


# ---------- 防御式字段提取 ----------

_TYPE_HINT_KEYS = ("recordtype", "record_type", "type", "datatype", "data_type", "kind", "name")


def _infer_record_type(rec: dict) -> str:
    """键名启发式（含 recordType 等键的字符串值）推断记录类型。"""
    hay: list[str] = []
    for k, v in rec.items():
        kl = str(k).lower()
        hay.append(kl)
        if kl in _TYPE_HINT_KEYS and isinstance(v, str):
            hay.append(v.lower())
    blob = " ".join(hay)
    if "steps" in blob or "stepcount" in blob or "step_count" in blob:
        return "steps"
    if "sleepsession" in blob or "sleep" in blob:
        return "sleep"
    if "exercise" in blob or "workout" in blob:
        return "exercise"
    if "weight" in blob:
        return "weight"
    if "heartrate" in blob or "heart_rate" in blob:
        return "heart_rate"
    return "unknown"


def _external_id(rec: dict) -> str:
    """metadata.id / id / uid，都没有回退 sha1(记录 JSON)。"""
    md = rec.get("metadata")
    if isinstance(md, dict):
        for k in ("id", "uid"):
            v = md.get(k)
            if v not in (None, "") and not isinstance(v, (dict, list)):
                return str(v)
    for k in ("id", "uid"):
        v = rec.get(k)
        if v not in (None, "") and not isinstance(v, (dict, list)):
            return str(v)
    return hashlib.sha1(
        json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _parse_ts(v: Any) -> datetime | None:
    """ISO8601（含 Z）/ epoch 秒或毫秒 / Instant dict → aware UTC datetime；解析不了返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        sec = v / 1000.0 if abs(v) >= 1e11 else float(v)  # >=1e11 视为毫秒
        try:
            return datetime.fromtimestamp(sec, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, dict):
        ms = v.get("epochMilli")
        sec = v.get("epochSecond")
        raw_sec = ms / 1000.0 if isinstance(ms, (int, float)) else (
            float(sec) if isinstance(sec, (int, float)) else None
        )
        if raw_sec is None:
            return None
        try:
            return datetime.fromtimestamp(raw_sec, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if re.fullmatch(r"-?\d{10,}", s):
            return _parse_ts(int(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return None


_TIME_KEYS = ("startTime", "start_time", "time", "timestamp", "startDateTime", "date",
              "endTime", "end_time")


def _record_time(rec: dict) -> datetime | None:
    """记录代表时间（优先 start 侧），供水位线比较与落日期。"""
    for k in _TIME_KEYS:
        if k in rec:
            ts = _parse_ts(rec[k])
            if ts is not None:
                return ts
    return None


_OFFSET_STR_RE = re.compile(r"(?:UTC|GMT)?([+-])(\d{1,2}):?(\d{2})?")


def _tzinfo_from(v: Any) -> timezone | None:
    """'+08:00' / 'UTC+0800' / {'totalSeconds':28800} / 秒数 → timezone。"""
    if isinstance(v, str) and v.strip():
        s = v.strip()
        if s in ("Z", "UTC", "GMT"):
            return timezone.utc
        m = _OFFSET_STR_RE.fullmatch(s)
        if m:
            hours, minutes = int(m.group(2)), int(m.group(3) or 0)
            if hours <= 18:
                sign = 1 if m.group(1) == "+" else -1
                return timezone(sign * timedelta(hours=hours, minutes=minutes))
        return None
    if isinstance(v, dict):
        return _tzinfo_from(v.get("totalSeconds"))
    if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) <= 18 * 3600:
        return timezone(timedelta(seconds=int(v)))
    return None


def _local_date(ts: datetime, rec: dict, *zone_keys: str) -> date:
    """按记录自带 zoneOffset 折算本地日期，缺失回退 Asia/Shanghai（§3.0 口径）。"""
    for k in zone_keys:
        tz = _tzinfo_from(rec.get(k))
        if tz is not None:
            return ts.astimezone(tz).date()
    return ts.astimezone(LOCAL_TZ).date()


def _qty(v: Any, *unit_keys: str) -> float | None:
    """数值 / 数字字符串 / 量纲 dict（如 {'inKilograms':70.5}）→ float。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    if isinstance(v, dict):
        for k in (*unit_keys, "value"):
            if k in v:
                got = _qty(v[k])
                if got is not None:
                    return got
    return None


# ---------- 各类型归一化 ----------

def _extract_steps(rec: dict) -> tuple[date, int]:
    ts = _record_time(rec)
    if ts is None:
        raise ValueError("steps 记录缺少可解析时间")
    d = _local_date(ts, rec, "startZoneOffset", "zoneOffset", "endZoneOffset")
    for k in ("count", "steps", "stepCount", "step_count", "value"):
        c = _qty(rec.get(k))
        if c is not None and c >= 0:
            return d, int(round(c))
    raise ValueError("steps 记录缺少步数字段")


def _extract_weight(rec: dict) -> tuple[date, datetime, float]:
    ts = _record_time(rec)
    if ts is None:
        raise ValueError("weight 记录缺少可解析时间")
    d = _local_date(ts, rec, "zoneOffset", "startZoneOffset")
    w = _qty(rec.get("weight"), "inKilograms", "kilograms")
    if w is None:
        for k in ("weightKg", "weight_kg", "value"):
            w = _qty(rec.get(k), "inKilograms")
            if w is not None:
                break
    if w is None or not (10 <= w <= 500):
        raise ValueError("weight 记录缺少合理体重值")
    return d, ts, round(w, 2)


# Health Connect SleepSessionRecord stage 常量：1/3/7=清醒类、4=浅睡、5=深睡、6=REM、2=泛化 SLEEPING
_STAGE_INT = {1: "awake", 2: "sleeping", 3: "awake", 4: "light", 5: "deep", 6: "rem", 7: "awake"}


def _stage_bucket(v: Any) -> str | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return _STAGE_INT.get(int(v))
    if isinstance(v, str):
        s = v.lower()
        if "rem" in s:
            return "rem"
        if "deep" in s:
            return "deep"
        if "light" in s:
            return "light"
        if "awake" in s or "wake" in s or "out_of_bed" in s:
            return "awake"
        if "unknown" in s:
            return None
        if "sleep" in s:
            return "sleeping"
    return None


def _normalize_sleep(db: Session, rec: dict, ext_id: str) -> date:
    """sleep → sleep_sessions upsert；返回 wake_date 供 sleep_hours 回填。"""
    start = _parse_ts(rec.get("startTime")) or _parse_ts(rec.get("start_time"))
    end = _parse_ts(rec.get("endTime")) or _parse_ts(rec.get("end_time"))
    if start is None or end is None or end <= start:
        raise ValueError("sleep 记录缺少有效起止时间")
    wake_date = _local_date(end, rec, "endZoneOffset", "startZoneOffset", "zoneOffset")

    secs = {"awake": 0.0, "light": 0.0, "deep": 0.0, "rem": 0.0, "sleeping": 0.0}
    has_stage = False
    stages = rec.get("stages") or rec.get("sleepStages") or rec.get("stage")
    if isinstance(stages, list):
        for st in stages:
            if not isinstance(st, dict):
                continue
            s0 = _parse_ts(st.get("startTime")) or _parse_ts(st.get("start_time"))
            s1 = _parse_ts(st.get("endTime")) or _parse_ts(st.get("end_time"))
            if s0 is None or s1 is None or s1 <= s0:
                continue
            bucket = _stage_bucket(st["stage"] if "stage" in st else st.get("type", st.get("stageType")))
            if bucket is None:
                continue
            secs[bucket] += (s1 - s0).total_seconds()
            has_stage = True
    if has_stage:
        awake_min = round(secs["awake"] / 60)
        light_min = round(secs["light"] / 60)
        deep_min = round(secs["deep"] / 60)
        rem_min = round(secs["rem"] / 60)
        # 总时长 = 浅+深+REM + 泛化 SLEEPING 段；清醒段不计入（§3.2 口径）
        total = light_min + deep_min + rem_min + round(secs["sleeping"] / 60)
    else:
        awake_min = light_min = deep_min = rem_min = None
        total = round((end - start).total_seconds() / 60)  # 无分期设备：整段视为睡眠

    ins = pg_insert(SleepSession).values(
        source=SOURCE, external_id=ext_id, start_at=start, end_at=end, wake_date=wake_date,
        awake_min=awake_min, light_min=light_min, deep_min=deep_min, rem_min=rem_min,
        total_sleep_min=total,
    )
    db.execute(ins.on_conflict_do_update(
        index_elements=["source", "external_id"],
        set_={
            "start_at": ins.excluded.start_at,
            "end_at": ins.excluded.end_at,
            "wake_date": ins.excluded.wake_date,
            "awake_min": ins.excluded.awake_min,
            "light_min": ins.excluded.light_min,
            "deep_min": ins.excluded.deep_min,
            "rem_min": ins.excluded.rem_min,
            "total_sleep_min": ins.excluded.total_sleep_min,
        },
    ))
    return wake_date


# Health Connect ExerciseSessionRecord EXERCISE_TYPE_* 常量中可靠可映射的子集；
# 未知代码归 'other'，原码保留在 detail（与三星导入同策略）
_HC_EXERCISE_TYPE = {
    8: "cycling",
    37: "hiking",
    56: "running",
    57: "running",   # treadmill
    73: "swimming",  # open water
    74: "swimming",  # pool
    79: "walking",
}


def _normalize_exercise(db: Session, rec: dict, ext_id: str) -> None:
    start = _parse_ts(rec.get("startTime")) or _parse_ts(rec.get("start_time")) or _record_time(rec)
    if start is None:
        raise ValueError("exercise 记录缺少可解析时间")
    end = _parse_ts(rec.get("endTime")) or _parse_ts(rec.get("end_time"))
    log_date = _local_date(start, rec, "startZoneOffset", "zoneOffset")

    ex_type = rec.get("exerciseType", rec.get("workoutType", rec.get("activityType")))
    session_type = "other"
    detail: dict[str, Any] = {}
    if isinstance(ex_type, str) and ex_type.strip():
        session_type = ex_type.strip().lower()[:50]
        detail["exercise_type"] = ex_type
    elif isinstance(ex_type, (int, float)) and not isinstance(ex_type, bool):
        code = int(ex_type)
        session_type = _HC_EXERCISE_TYPE.get(code, "other")
        detail["exercise_type"] = code
    title = rec.get("title")
    if isinstance(title, str) and title.strip():
        detail["title"] = title.strip()

    duration_min: int | None = None
    if end is not None and end > start:
        duration_min = round((end - start).total_seconds() / 60)
    else:
        dur = _qty(rec.get("duration"), "seconds")
        if dur is not None and dur > 0:
            # 启发式：<1e5 视为秒（≈27.7h 内），更大视为毫秒
            duration_min = round(dur / 60) if dur < 1e5 else round(dur / 60000)

    dist = _qty(rec.get("distance"), "inMeters", "meters")
    cal = (_qty(rec.get("totalEnergyBurned"), "inKilocalories", "kilocalories")
           or _qty(rec.get("energy"), "inKilocalories", "kilocalories")
           or _qty(rec.get("calories")))

    ins = pg_insert(WorkoutLog).values(
        log_date=log_date,
        started_at=start,
        session_type=session_type,
        duration_min=duration_min,
        distance_km=(round(dist / 1000, 2) if dist is not None and dist > 0 else None),
        calories=(round(cal) if cal is not None and cal > 0 else None),
        detail=(detail or None),
        source=SOURCE,
        external_id=ext_id,
    )
    db.execute(ins.on_conflict_do_update(
        index_elements=["source", "external_id"],
        index_where=text("external_id IS NOT NULL"),
        set_={
            "log_date": ins.excluded.log_date,
            "started_at": ins.excluded.started_at,
            "session_type": ins.excluded.session_type,
            "duration_min": ins.excluded.duration_min,
            "distance_km": ins.excluded.distance_km,
            "calories": ins.excluded.calories,
            "detail": ins.excluded.detail,
            "updated_at": text("now()"),
        },
    ))


def _mark(db: Session, record_type: str, ext_id: str, status: str, error: str | None = None) -> None:
    """更新 import_raw 行的解析状态。"""
    db.execute(
        update(ImportRaw)
        .where(
            ImportRaw.source == SOURCE,
            ImportRaw.record_type == record_type,
            ImportRaw.external_id == ext_id,
        )
        .values(parse_status=status, parse_error=error, parse_version=PARSER_VERSION)
    )


# ---------- 端点 ----------

@router.post("/health_connect")
async def ingest_health_connect(request: Request, db: Session = Depends(get_db)) -> Response:
    # 1. 鉴权：token 未配置 503；Bearer 比对失败 401（无 body 细节）
    settings = get_settings()
    if not settings.ingest_token:
        return Response(status_code=503)
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token.strip().encode("utf-8"), settings.ingest_token.encode("utf-8")
    ):
        return Response(status_code=401)

    # 2. 请求体上限 5MB：先查 Content-Length，读后复核（兼容 chunked 无长度头）
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)

    # 3. 解析 JSON + 防御式提取记录数组
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        arr = None
        for key in ("records", "data"):
            if isinstance(payload.get(key), list):
                arr = payload[key]
                break
        records = arr if arr is not None else [payload]
    else:
        return JSONResponse({"error": "unsupported payload"}, status_code=400)
    received = len(records)

    # 4. 整包落 import_raw（批内去重；冲突只刷新 last_seen_at）
    now = now_local()
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in records:
        rec = item if isinstance(item, dict) else {"value": item}
        record_type = _infer_record_type(rec)
        ext_id = _external_id(rec)
        if (record_type, ext_id) in seen:
            continue
        seen.add((record_type, ext_id))
        offset_val = rec.get("startZoneOffset") or rec.get("zoneOffset") or rec.get("endZoneOffset")
        entries.append({
            "rec": rec,
            "rtype": record_type,
            "ext_id": ext_id,
            "raw": {
                "source": SOURCE,
                "record_type": record_type,
                "external_id": ext_id,
                "raw": rec,
                "time_offset": offset_val if isinstance(offset_val, str) else None,
                "parse_status": "pending",
                "parse_version": 0,
                "last_seen_at": now,
            },
        })

    new_keys: set[tuple[str, str]] = set()
    for i in range(0, len(entries), RAW_BATCH):
        chunk = [e["raw"] for e in entries[i:i + RAW_BATCH]]
        ins = pg_insert(ImportRaw).values(chunk)
        stmt = ins.on_conflict_do_update(
            index_elements=["source", "record_type", "external_id"],
            set_={"last_seen_at": now},
        ).returning(ImportRaw.record_type, ImportRaw.external_id, literal_column("(xmax = 0)"))
        for rtype, ext_id, is_new in db.execute(stmt):
            if is_new:
                new_keys.add((rtype, ext_id))
    db.commit()  # 原始留档先落盘：后续归一化再出错也不丢数据，响应恒 200

    # 5. 归一化（同请求 try/except，绝不 5xx）
    try:
        state = db.get(SyncState, SOURCE)
        watermark = state.watermark if state is not None else None

        day_steps: dict[date, int] = {}
        weight_by_day: dict[date, tuple[datetime, float]] = {}
        sleep_dates: set[date] = set()

        for e in entries:
            if (e["rtype"], e["ext_id"]) not in new_keys:
                continue  # 重复推送：只刷新 last_seen_at，不重复归一化
            rec, rtype, ext_id = e["rec"], e["rtype"], e["ext_id"]
            rec_ts = _record_time(rec)
            if watermark is not None and rec_ts is not None and rec_ts <= watermark:
                _mark(db, rtype, ext_id, "skipped")  # 水位线以内：zip 历史已覆盖
                continue
            if rtype not in ("steps", "weight", "sleep", "exercise"):
                continue  # heart_rate / unknown：留档 pending，供后续解析器重放
            try:
                with db.begin_nested():  # 单条失败回滚到 SAVEPOINT，不毒化整个事务
                    if rtype == "steps":
                        d, cnt = _extract_steps(rec)
                        day_steps[d] = day_steps.get(d, 0) + cnt
                    elif rtype == "weight":
                        d, ts, kg = _extract_weight(rec)
                        prev = weight_by_day.get(d)
                        if prev is None or ts >= prev[0]:  # 同日多条取最后一次
                            weight_by_day[d] = (ts, kg)
                    elif rtype == "sleep":
                        sleep_dates.add(_normalize_sleep(db, rec, ext_id))
                    else:
                        _normalize_exercise(db, rec, ext_id)
                _mark(db, rtype, ext_id, "parsed")
            except Exception as exc:
                _mark(db, rtype, ext_id, "failed", str(exc)[:500])

        # steps：按日增量累加进 daily_activity（区间记录求和）
        for d in sorted(day_steps):
            ins = pg_insert(DailyActivity).values(log_date=d, steps=day_steps[d], source=SOURCE)
            db.execute(ins.on_conflict_do_update(
                index_elements=["log_date"],
                set_={
                    "steps": func.coalesce(DailyActivity.__table__.c.steps, 0) + ins.excluded.steps,
                    "source": ins.excluded.source,
                    "updated_at": text("now()"),
                },
            ))
        # weight：同日最后一次 → body_metrics 字段级回填（手动值不覆盖）
        for d in sorted(weight_by_day):
            autofill_fields(db, d, SOURCE, {"weight_kg": weight_by_day[d][1]})
        # sleep：受影响 wake_date 汇总回填 sleep_hours（同夜多会话取总和）
        for d in sorted(sleep_dates):
            total = db.execute(
                select(func.coalesce(func.sum(SleepSession.total_sleep_min), 0))
                .where(SleepSession.wake_date == d)
            ).scalar_one()
            if total and total > 0:
                autofill_fields(db, d, SOURCE, {"sleep_hours": round(total / 60.0, 1)})

        # 6. 成功：sync_state 记 last_success_at、清零失败计数（不触碰 watermark）
        state = db.get(SyncState, SOURCE)
        if state is None:
            state = SyncState(source=SOURCE)
            db.add(state)
        state.last_success_at = now
        state.last_error = None
        state.consecutive_failures = 0
        db.commit()
    except Exception as exc:  # 归一化整体失败：raw 已落盘可重放，只记状态
        db.rollback()
        try:
            state = db.get(SyncState, SOURCE)
            if state is None:
                state = SyncState(source=SOURCE)
                db.add(state)
            state.consecutive_failures = (state.consecutive_failures or 0) + 1
            state.last_error = str(exc)[:2000]
            db.commit()
        except Exception:
            db.rollback()

    return JSONResponse({"received": received})
