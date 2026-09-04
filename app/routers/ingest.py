"""Health Connect webhook 接收端（设计文档 §3.7 通道 2，M4a）。

POST /api/ingest/health_connect
- Bearer token 鉴权（secrets.compare_digest；token 未配置 503；失败 401 无 body 细节）；
  此路由豁免 session/CSRF（main.py 中间件已放行 /api/ingest/*），仅此一个。
- 请求体上限 5MB：读 body 前查 Content-Length，读后复核实际长度，超限 413。
- payload 结构防御式提取：顶层 list 视为记录数组；顶层 dict 按 'records'/'data'
  取数组，取不到把整个 dict 当单条记录。
- 每条记录：键名启发式推断 record_type；优先使用 clientRecordId/version，并保存
  payload hash 与来源 App/设备证据；没有稳定 ID 时回退确定性摘要。同版本不同 payload
  隔离为冲突，高版本才替换，原始记录先提交后再归一化。
- 归一化在同请求 try/except：
  * steps/weight/sleep/exercise/heart_rate 使用彼此独立的 cursor、历史 watermark、权限和
    来源指纹；权限撤销或来源变化只阻断对应类型；
  * steps/weight/sleep 的版本修订按当前 source records 重建受影响日期，不叠加旧版本；
    exercise 按类型分流并 upsert；
  * 单条失败置 parse_status='failed' 记 parse_error；整体失败只记
    sync_state.consecutive_failures，绝不 5xx（防手机端重发风暴）。
- heart_rate 会严格解析 sample 并按日重建 hr_min/hr_avg/hr_max；unknown 不猜测业务
  语义，保留原始证据、稳定 pending_reason，并可通过显式 replay 入口重新判定。
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
from sqlalchemy import cast, delete, func, literal_column, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    AppSetting,
    BodyMetrics,
    DailyActivity,
    ImportRaw,
    ImportRawRevision,
    ReleaseLog,
    SleepSession,
    SyncCursor,
    SyncState,
    WorkoutLog,
)
from app.services.autofill import autofill_fields
from app.services.discipline import is_release_session
from app.services.miscale import compute_body_metrics
from app.services.sleep import total_sleep_with_source
from app.timeutil import LOCAL_TZ, now_local

MAX_BODY_BYTES = 5 * 1024 * 1024
PARSER_VERSION = 2
SOURCE = "health_connect"
RAW_BATCH = 500  # shared by offline/agent bulk archival
HEALTH_CONNECT_TYPES = ("steps", "weight", "sleep", "exercise", "heart_rate")
PERMISSION_STATES = {"unknown", "granted", "denied", "revoked"}

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
    """Stable client identity, then provider id, then a deterministic payload id."""
    md = rec.get("metadata")
    if isinstance(md, dict):
        for k in ("clientRecordId", "client_record_id", "id", "uid"):
            v = md.get(k)
            if v not in (None, "") and not isinstance(v, (dict, list)):
                return str(v)
    for k in ("clientRecordId", "client_record_id", "id", "uid"):
        v = rec.get(k)
        if v not in (None, "") and not isinstance(v, (dict, list)):
            return str(v)
    return hashlib.sha1(
        json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _record_version(rec: dict) -> int:
    metadata = rec.get("metadata")
    candidates = []
    if isinstance(metadata, dict):
        candidates.extend(
            (metadata.get("clientRecordVersion"), metadata.get("client_record_version"))
        )
    candidates.extend((rec.get("clientRecordVersion"), rec.get("record_version")))
    for value in candidates:
        if isinstance(value, bool):
            continue
        try:
            version = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= version <= 2_147_483_647:
            return version
    return 0


def _payload_hash(rec: dict) -> str:
    encoded = json.dumps(
        rec, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_action(
    *,
    prior_version: int,
    prior_hash: str,
    prior_status: str,
    incoming_version: int,
    incoming_hash: str,
) -> str:
    if incoming_version > prior_version:
        return "update"
    if incoming_version < prior_version:
        return "skip"
    if incoming_hash != prior_hash:
        return "conflict"
    return "retry" if prior_status in {"pending", "failed"} else "skip"


def _record_provenance(rec: dict) -> dict[str, Any]:
    """Bounded, value-free device/app origin evidence from Health Connect metadata."""
    metadata = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
    origin = metadata.get("dataOrigin")
    if not isinstance(origin, dict):
        origin = rec.get("dataOrigin") if isinstance(rec.get("dataOrigin"), dict) else {}
    device = metadata.get("device")
    if not isinstance(device, dict):
        device = rec.get("device") if isinstance(rec.get("device"), dict) else {}
    out: dict[str, Any] = {"channel": SOURCE}
    package = origin.get("packageName") or origin.get("package_name")
    if isinstance(package, str) and package.strip():
        out["origin_package"] = package.strip()[:200]
    bounded_device = {
        key: str(device[key]).strip()[:120]
        for key in ("manufacturer", "model", "type")
        if device.get(key) not in (None, "")
    }
    if bounded_device:
        out["device"] = bounded_device
    recording = metadata.get("recordingMethod") or metadata.get("recording_method")
    if recording not in (None, ""):
        out["recording_method"] = str(recording)[:80]
    modified = metadata.get("lastModifiedTime") or metadata.get("last_modified_time")
    if modified not in (None, ""):
        out["last_modified_time"] = str(modified)[:80]
    return out


def _parse_sync_boundaries(payload: Any) -> list[dict[str, Any]]:
    """Validate optional per-record-type cursor/permission envelopes.

    Accepts a list or one object.  Tokens are opaque and never compared across
    types.  A changed source fingerprint requires ``reset: true`` before cursor
    advancement resumes.
    """
    if not isinstance(payload, dict) or "sync" not in payload:
        return []
    raw = payload.get("sync")
    items = raw if isinstance(raw, list) else [raw]
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("sync must be an object or list of objects")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        record_type = str(item.get("record_type") or "").strip().lower()
        permission = str(item.get("permission") or "unknown").strip().lower()
        cursor = item.get("cursor")
        fingerprint = item.get("source_fingerprint")
        reset = item.get("reset", False)
        if record_type not in HEALTH_CONNECT_TYPES or record_type in seen:
            raise ValueError("sync record_type must be unique and supported")
        if permission not in PERMISSION_STATES:
            raise ValueError("sync permission is invalid")
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048):
            raise ValueError("sync cursor must be a bounded opaque string")
        if fingerprint is not None and (
            not isinstance(fingerprint, str) or not 1 <= len(fingerprint) <= 256
        ):
            raise ValueError("sync source_fingerprint is invalid")
        if not isinstance(reset, bool):
            raise ValueError("sync reset must be boolean")
        seen.add(record_type)
        parsed.append({
            "record_type": record_type,
            "permission": permission,
            "cursor": cursor,
            "source_fingerprint": fingerprint,
            "reset": reset,
        })
    return parsed


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

class HealthConnectNormalizationError(ValueError):
    """A bounded, stable failure code plus operator-readable detail."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def _failure_reason(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, HealthConnectNormalizationError):
        return exc.code, f"{exc.code}: {exc}"[:500]
    return "normalization_failed", f"normalization_failed: {exc}"[:500]

def _extract_steps(rec: dict) -> tuple[date, int]:
    ts = _record_time(rec)
    if ts is None:
        raise ValueError("steps 记录缺少可解析时间")
    d = _local_date(ts, rec, "startZoneOffset", "zoneOffset", "endZoneOffset")
    for k in ("count", "steps", "stepCount", "step_count", "value"):
        c = _qty(rec.get(k))
        if c is not None and 0 <= c <= 200000:  # 上限防坏数据溢出毒化整批归一化
            return d, int(round(c))
    raise ValueError("steps 记录缺少合理步数字段")


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


_HEART_RATE_VALUE_KEYS = (
    "beatsPerMinute",
    "beats_per_minute",
    "bpm",
    "heartRate",
    "heart_rate",
    "value",
)
_HEART_RATE_TIME_KEYS = ("time", "timestamp", "startTime", "start_time")


def _extract_heart_rate_samples(rec: dict) -> list[tuple[datetime, date, int]]:
    """Parse a Health Connect HeartRateRecord without discarding bad samples.

    Android Health Connect normally serializes ``samples`` as objects containing
    ``time`` and ``beatsPerMinute``. The fallback single-value shape keeps the
    server compatible with thin device bridges. If a declared sample is malformed,
    the whole raw record remains failed/replayable rather than silently producing a
    deceptively complete daily aggregate.
    """
    raw_samples = rec.get("samples")
    if raw_samples is None:
        raw_samples = rec.get("measurements")
    if raw_samples is None:
        candidates: list[Any] = [rec]
        record_fallback = _record_time(rec)
    elif isinstance(raw_samples, list):
        if not raw_samples:
            raise HealthConnectNormalizationError(
                "heart_rate_samples_empty", "heart_rate 记录的 samples 为空"
            )
        candidates = raw_samples
        record_fallback = _record_time(rec) if len(raw_samples) == 1 else None
    else:
        raise HealthConnectNormalizationError(
            "heart_rate_samples_invalid", "heart_rate samples 必须是数组"
        )

    parsed: list[tuple[datetime, date, int]] = []
    for index, sample in enumerate(candidates):
        if not isinstance(sample, dict):
            raise HealthConnectNormalizationError(
                "heart_rate_sample_invalid", f"heart_rate sample[{index}] 不是对象"
            )
        timestamp = next(
            (
                parsed_time
                for key in _HEART_RATE_TIME_KEYS
                if (parsed_time := _parse_ts(sample.get(key))) is not None
            ),
            record_fallback,
        )
        if timestamp is None:
            raise HealthConnectNormalizationError(
                "heart_rate_time_missing", f"heart_rate sample[{index}] 缺少可解析时间"
            )
        bpm = next(
            (
                parsed_value
                for key in _HEART_RATE_VALUE_KEYS
                if (parsed_value := _qty(
                    sample.get(key), "beatsPerMinute", "beats_per_minute", "bpm"
                )) is not None
            ),
            None,
        )
        if bpm is None or not 20 <= bpm <= 250:
            raise HealthConnectNormalizationError(
                "heart_rate_value_invalid",
                f"heart_rate sample[{index}] 缺少 20..250 bpm 的合理值",
            )
        day = _local_date(
            timestamp, rec, "startZoneOffset", "zoneOffset", "endZoneOffset"
        )
        parsed.append((timestamp, day, round(bpm)))
    return parsed


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


def _mark_raw(
    db: Session,
    source: str,
    record_type: str,
    ext_id: str,
    status: str,
    error: str | None = None,
    version: int = PARSER_VERSION,
    blob_patch: dict | None = None,
    normalized: dict[str, Any] | None = None,
    pending_reason: str | None = None,
    attempted: bool = False,
) -> None:
    """import_raw 行解析状态统一更新（HC/秤/三星直读/offline/agent 各通道共享）。

    blob_patch：合并进 blob（JSONB ||，同键覆盖、他键保留）——agent 通道用来
    记归一化行 id 与 agent 名，供 /agent-log 精确撤销与归属展示。
    """
    values: dict[str, Any] = dict(parse_status=status, parse_error=error, parse_version=version)
    if status in {"parsed", "skipped"}:
        values["pending_reason"] = None
    elif pending_reason is not None:
        values["pending_reason"] = pending_reason
    if attempted:
        values["normalization_attempts"] = ImportRaw.normalization_attempts + 1
        values["last_normalization_at"] = now_local()
    if normalized is not None:
        values["normalized"] = normalized
    if blob_patch:
        values["blob"] = func.coalesce(
            ImportRaw.blob, cast({}, JSONB)
        ).op("||")(cast(blob_patch, JSONB))
    db.execute(
        update(ImportRaw)
        .where(
            ImportRaw.source == source,
            ImportRaw.record_type == record_type,
            ImportRaw.external_id == ext_id,
        )
        .values(**values)
    )


def _archive_raw_revision(
    db: Session,
    current: ImportRaw,
    *,
    evidence_kind: str,
    raw: dict | None = None,
    record_version: int | None = None,
    payload_hash: str | None = None,
    provenance: dict | None = None,
) -> None:
    """Append displaced/conflicting evidence without duplicating retransmits."""
    evidence_raw = raw if raw is not None else current.raw
    evidence_hash = payload_hash or current.payload_hash or _payload_hash(evidence_raw)
    values = {
        "import_raw_id": current.id,
        "record_version": (
            record_version if record_version is not None else int(current.record_version or 0)
        ),
        "payload_hash": evidence_hash,
        "evidence_kind": evidence_kind,
        "raw": evidence_raw,
        "provenance": provenance if raw is not None else current.provenance,
        "normalized": current.normalized if evidence_kind == "superseded" else None,
        "parse_status": (
            current.parse_status if evidence_kind == "superseded" else "failed"
        ),
        "parse_error": (
            current.parse_error
            if evidence_kind == "superseded"
            else "同一 client_record_id/version 的 payload 不一致"
        ),
        "pending_reason": (
            current.pending_reason if evidence_kind == "superseded" else "version_conflict"
        ),
        "parse_version": int(current.parse_version or 0),
    }
    db.execute(
        pg_insert(ImportRawRevision)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["import_raw_id", "record_version", "payload_hash"]
        )
    )


def _touch_sync_state(
    db: Session, source: str, ok: bool, error: str | None = None,
    *, now: datetime | None = None,
) -> None:
    """sync_state 通道状态统一更新（各通道共享）：成功记 last_success_at 并清失败
    计数；失败累加 consecutive_failures 记 last_error。不触碰 watermark；
    不 commit（跟随调用方事务边界）。"""
    state = db.get(SyncState, source)
    if state is None:
        state = SyncState(source=source)
        db.add(state)
    if ok:
        state.last_success_at = now if now is not None else now_local()
        state.last_error = None
        state.consecutive_failures = 0
    else:
        state.consecutive_failures = (state.consecutive_failures or 0) + 1
        state.last_error = error[:2000] if error else None


# ---------- 端点 ----------

def _bearer_reject(request: Request) -> Response | None:
    """Bearer 鉴权：token 未配置 503；比对失败 401（无 body 细节）；通过返回 None。

    备用头 X-Ingest-Token：frp 等入口开了 HTTP Basic 验证时 Authorization 头
    被 Basic 凭据占用，壳自动把 token 挪到这个头（Authorization Bearer 优先）。"""
    settings = get_settings()
    if not settings.ingest_token:
        return Response(status_code=503)
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    candidate = token.strip() if scheme.lower() == "bearer" else ""
    if not candidate:
        candidate = request.headers.get("X-Ingest-Token", "").strip()
    if not candidate or not secrets.compare_digest(
        candidate.encode("utf-8"), settings.ingest_token.encode("utf-8")
    ):
        return Response(status_code=401)
    return None


def _apply_sync_boundaries(
    db: Session, specs: list[dict[str, Any]], now: datetime
) -> dict[str, SyncCursor]:
    states: dict[str, SyncCursor] = {}
    for spec in specs:
        key = (SOURCE, spec["record_type"])
        state = db.get(SyncCursor, key)
        if state is None:
            state = SyncCursor(source=SOURCE, record_type=spec["record_type"])
            db.add(state)
            db.flush()
        old_fingerprint = state.source_fingerprint
        new_fingerprint = spec["source_fingerprint"]
        changed = bool(
            old_fingerprint and new_fingerprint and old_fingerprint != new_fingerprint
        )
        if changed:
            state.cursor_token = None
            state.needs_resync = True
            state.source_changed_at = now
        if spec["reset"]:
            state.cursor_token = None
            state.needs_resync = False
            state.source_changed_at = now if changed else state.source_changed_at
        if new_fingerprint:
            state.source_fingerprint = new_fingerprint
        state.permission_state = spec["permission"]
        if spec["permission"] in {"denied", "revoked"}:
            state.cursor_token = None
            state.needs_resync = True
            state.last_error = f"Health Connect permission {spec['permission']}"
        states[spec["record_type"]] = state
    return states


def _cursor_payload(state: SyncCursor) -> dict[str, Any]:
    return {
        "record_type": state.record_type,
        "permission": state.permission_state,
        "cursor": state.cursor_token,
        "watermark": _iso_ts(state.watermark),
        "source_fingerprint": state.source_fingerprint,
        "needs_resync": bool(state.needs_resync),
        "source_changed_at": _iso_ts(state.source_changed_at),
        "last_success_at": _iso_ts(state.last_success_at),
        "last_error": state.last_error,
    }


def _iso_ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalized_date(value: Any) -> date | None:
    raw = value.get("date") if isinstance(value, dict) else None
    try:
        return date.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _normalized_dates(value: Any) -> set[date]:
    if not isinstance(value, dict):
        return set()
    raw_dates = value.get("dates")
    if not isinstance(raw_dates, list):
        single = _normalized_date(value)
        return {single} if single is not None else set()
    result: set[date] = set()
    for raw in raw_dates:
        try:
            if isinstance(raw, str):
                result.add(date.fromisoformat(raw))
        except ValueError:
            continue
    return result


def _existing_effective_date(record_type: str, row: ImportRaw) -> date | None:
    """Find the old day even for rows created before normalized snapshots existed."""
    normalized = _normalized_date(row.normalized)
    if normalized is not None:
        return normalized
    try:
        if record_type == "steps":
            return _extract_steps(row.raw)[0]
        if record_type == "weight":
            return _extract_weight(row.raw)[0]
        if record_type == "sleep":
            end = next(
                (
                    row.raw[key]
                    for key in ("endTime", "end_time", "end")
                    if row.raw.get(key) is not None
                ),
                None,
            )
            end_dt = _parse_ts(end)
            return _local_date(end_dt, row.raw) if end_dt is not None else None
    except (TypeError, ValueError):
        return None
    return None


def _existing_heart_rate_dates(row: ImportRaw) -> set[date]:
    normalized = _normalized_dates(row.normalized)
    if normalized:
        return normalized
    try:
        return {day for _, day, _ in _extract_heart_rate_samples(row.raw)}
    except (TypeError, ValueError):
        return set()


def _clear_source_autofill(db: Session, d: date, field: str) -> None:
    row = db.execute(select(BodyMetrics).where(BodyMetrics.log_date == d)).scalar_one_or_none()
    if row is None:
        return
    markers = dict(row.autofilled or {})
    if markers.get(field) != SOURCE:
        return
    setattr(row, field, None)
    markers.pop(field, None)
    row.autofilled = markers


def _rebuild_hc_steps(db: Session, affected: set[date]) -> None:
    if not affected:
        return
    totals = {d: 0 for d in affected}
    rows = db.execute(
        select(ImportRaw.raw).where(
            ImportRaw.source == SOURCE,
            ImportRaw.record_type == "steps",
            ImportRaw.parse_status == "parsed",
        )
    ).scalars()
    for raw in rows:
        try:
            d, count = _extract_steps(raw)
        except (TypeError, ValueError):
            continue
        if d in totals:
            totals[d] += count
    for d, count in totals.items():
        current = db.get(DailyActivity, d)
        if count <= 0:
            if current is not None and (current.field_sources or {}).get("steps") == SOURCE:
                current.steps = None
                current.field_sources = {
                    key: value
                    for key, value in (current.field_sources or {}).items()
                    if key != "steps"
                }
            continue
        ins = pg_insert(DailyActivity).values(
            log_date=d,
            steps=count,
            source=SOURCE,
            field_sources={"steps": SOURCE},
        )
        db.execute(ins.on_conflict_do_update(
            index_elements=["log_date"],
            set_={
                "steps": ins.excluded.steps,
                "source": ins.excluded.source,
                "field_sources": DailyActivity.__table__.c.field_sources.op("||")(
                    ins.excluded.field_sources
                ),
                "updated_at": text("now()"),
            },
            where=DailyActivity.__table__.c.source != "samsung_direct",
        ))


def _rebuild_hc_weight(db: Session, affected: set[date]) -> None:
    if not affected:
        return
    latest: dict[date, tuple[datetime, float]] = {}
    rows = db.execute(
        select(ImportRaw.raw).where(
            ImportRaw.source == SOURCE,
            ImportRaw.record_type == "weight",
            ImportRaw.parse_status == "parsed",
        )
    ).scalars()
    for raw in rows:
        try:
            d, ts, kg = _extract_weight(raw)
        except (TypeError, ValueError):
            continue
        if d in affected and (d not in latest or ts >= latest[d][0]):
            latest[d] = (ts, kg)
    for d in affected:
        if d in latest:
            autofill_fields(db, d, SOURCE, {"weight_kg": latest[d][1]})
        else:
            _clear_source_autofill(db, d, "weight_kg")


def _rebuild_hc_heart_rate(db: Session, affected: set[date]) -> None:
    """Rebuild daily heart-rate aggregates from current parsed raw evidence."""
    if not affected:
        return
    values: dict[date, list[int]] = {day: [] for day in affected}
    rows = db.execute(
        select(ImportRaw.raw).where(
            ImportRaw.source == SOURCE,
            ImportRaw.record_type == "heart_rate",
            ImportRaw.parse_status == "parsed",
        )
    ).scalars()
    for raw in rows:
        try:
            samples = _extract_heart_rate_samples(raw)
        except (TypeError, ValueError):
            continue
        for _, day, bpm in samples:
            if day in values:
                values[day].append(bpm)

    for day, samples in values.items():
        current = db.get(DailyActivity, day)
        if not samples:
            if current is not None and any(
                (current.field_sources or {}).get(field) == SOURCE
                for field in ("hr_min", "hr_avg", "hr_max")
            ):
                current.hr_min = current.hr_avg = current.hr_max = None
                current.field_sources = {
                    key: value
                    for key, value in (current.field_sources or {}).items()
                    if key not in {"hr_min", "hr_avg", "hr_max"}
                }
            continue
        aggregate = {
            "hr_min": min(samples),
            "hr_avg": round(sum(samples) / len(samples)),
            "hr_max": max(samples),
        }
        ins = pg_insert(DailyActivity).values(
            log_date=day,
            source=SOURCE,
            field_sources={field: SOURCE for field in aggregate},
            **aggregate,
        )
        db.execute(
            ins.on_conflict_do_update(
                index_elements=["log_date"],
                set_={
                    **{field: getattr(ins.excluded, field) for field in aggregate},
                    "source": ins.excluded.source,
                    "field_sources": DailyActivity.__table__.c.field_sources.op("||")(
                        ins.excluded.field_sources
                    ),
                    "updated_at": text("now()"),
                },
                # The direct Samsung daily aggregate has explicit source priority.
                where=DailyActivity.__table__.c.source != "samsung_direct",
            )
        )


def _refresh_sleep_days(db: Session, affected: set[date]) -> None:
    for d in affected:
        total, source = total_sleep_with_source(db, d)
        if total > 0 and source is not None:
            autofill_fields(db, d, source, {"sleep_hours": round(total / 60.0, 1)})
        else:
            _clear_source_autofill(db, d, "sleep_hours")


@router.get("/health_connect/state")
def health_connect_state(request: Request, db: Session = Depends(get_db)) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    rows = db.execute(
        select(SyncCursor).where(SyncCursor.source == SOURCE).order_by(SyncCursor.record_type)
    ).scalars().all()
    queue_rows = db.execute(
        select(
            ImportRaw.record_type,
            ImportRaw.parse_status,
            ImportRaw.pending_reason,
            func.count(),
        )
        .where(
            ImportRaw.source == SOURCE,
            ImportRaw.parse_status.in_(("pending", "failed")),
        )
        .group_by(
            ImportRaw.record_type, ImportRaw.parse_status, ImportRaw.pending_reason
        )
        .order_by(ImportRaw.record_type, ImportRaw.parse_status)
    ).all()
    return JSONResponse({
        "source": SOURCE,
        "parser_version": PARSER_VERSION,
        "record_types": [_cursor_payload(row) for row in rows],
        "normalization_queue": [
            {
                "record_type": record_type,
                "status": status,
                "reason": reason or "reason_not_recorded",
                "count": count,
            }
            for record_type, status, reason, count in queue_rows
        ],
    })


@router.post("/health_connect")
async def ingest_health_connect(request: Request, db: Session = Depends(get_db)) -> Response:
    # 1. 鉴权
    reject = _bearer_reject(request)
    if reject is not None:
        return reject

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
        sync_specs: list[dict[str, Any]] = []
    elif isinstance(payload, dict):
        try:
            sync_specs = _parse_sync_boundaries(payload)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        arr = None
        for key in ("records", "data"):
            if isinstance(payload.get(key), list):
                arr = payload[key]
                break
        records = arr if arr is not None else ([] if "sync" in payload else [payload])
    else:
        return JSONResponse({"error": "unsupported payload"}, status_code=400)
    received = len(records)

    # 4. 整包落 import_raw。稳定 client id + 单调 version 决定是否重放；
    # 同版本不同 payload 隔离为冲突，绝不静默覆盖已归一化事实。
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
        record_version = _record_version(rec)
        payload_hash = _payload_hash(rec)
        provenance = _record_provenance(rec)
        pending_reason = (
            "unsupported_record_type" if record_type == "unknown" else "awaiting_normalization"
        )
        entries.append({
            "rec": rec,
            "rtype": record_type,
            "ext_id": ext_id,
            "record_version": record_version,
            "payload_hash": payload_hash,
            "provenance": provenance,
            "raw": {
                "source": SOURCE,
                "record_type": record_type,
                "external_id": ext_id,
                "record_version": record_version,
                "payload_hash": payload_hash,
                "provenance": provenance,
                "raw": rec,
                "time_offset": offset_val if isinstance(offset_val, str) else None,
                "parse_status": "pending",
                "pending_reason": pending_reason,
                "parse_version": 0,
                "last_seen_at": now,
            },
        })

    keys = [(entry["rtype"], entry["ext_id"]) for entry in entries]
    existing_rows = []
    if keys:
        existing_rows = db.execute(
            select(ImportRaw).where(
                ImportRaw.source == SOURCE,
                tuple_(ImportRaw.record_type, ImportRaw.external_id).in_(keys),
            )
        ).scalars().all()
    existing = {(row.record_type, row.external_id): row for row in existing_rows}
    process_keys: set[tuple[str, str]] = set()
    conflict_keys: set[tuple[str, str]] = set()
    affected_steps: set[date] = set()
    affected_weights: set[date] = set()
    affected_sleep: set[date] = set()
    affected_heart_rate: set[date] = set()
    new_raws: list[dict[str, Any]] = []
    for entry in entries:
        key = (entry["rtype"], entry["ext_id"])
        prior = existing.get(key)
        if prior is None:
            new_raws.append(entry["raw"])
            process_keys.add(key)
            continue
        if entry["rtype"] == "steps":
            old_date = _existing_effective_date("steps", prior)
            if old_date:
                affected_steps.add(old_date)
        elif entry["rtype"] == "weight":
            old_date = _existing_effective_date("weight", prior)
            if old_date:
                affected_weights.add(old_date)
        elif entry["rtype"] == "sleep":
            old_date = _existing_effective_date("sleep", prior)
            if old_date:
                affected_sleep.add(old_date)
        elif entry["rtype"] == "heart_rate":
            affected_heart_rate.update(_existing_heart_rate_dates(prior))
        prior_version = int(prior.record_version or 0)
        prior_hash = prior.payload_hash or _payload_hash(prior.raw)
        incoming_version = entry["record_version"]
        action = _record_action(
            prior_version=prior_version,
            prior_hash=prior_hash,
            prior_status=prior.parse_status,
            incoming_version=incoming_version,
            incoming_hash=entry["payload_hash"],
        )
        if action == "update":
            _archive_raw_revision(db, prior, evidence_kind="superseded")
            db.execute(
                update(ImportRaw).where(ImportRaw.id == prior.id).values(
                    raw=entry["rec"],
                    record_version=incoming_version,
                    payload_hash=entry["payload_hash"],
                    provenance=entry["provenance"],
                    normalized=None,
                    time_offset=entry["raw"]["time_offset"],
                    parse_status="pending",
                    parse_error=None,
                    pending_reason=(
                        "unsupported_record_type"
                        if entry["rtype"] == "unknown"
                        else "awaiting_normalization"
                    ),
                    parse_version=0,
                    last_seen_at=now,
                )
            )
            process_keys.add(key)
        elif action in {"retry", "skip"} and entry["payload_hash"] == prior_hash:
            db.execute(
                update(ImportRaw).where(ImportRaw.id == prior.id).values(
                    payload_hash=entry["payload_hash"],
                    provenance=entry["provenance"],
                    last_seen_at=now,
                )
            )
            if action == "retry":
                process_keys.add(key)
        elif action == "conflict":
            _archive_raw_revision(
                db,
                prior,
                evidence_kind="version_conflict",
                raw=entry["rec"],
                record_version=incoming_version,
                payload_hash=entry["payload_hash"],
                provenance=entry["provenance"],
            )
            # Keep the already normalized current version valid. The conflicting
            # payload is quarantined in import_raw_revisions and never normalized.
            db.execute(update(ImportRaw).where(ImportRaw.id == prior.id).values(last_seen_at=now))
            conflict_keys.add(key)
        else:
            db.execute(update(ImportRaw).where(ImportRaw.id == prior.id).values(last_seen_at=now))
    for index in range(0, len(new_raws), RAW_BATCH):
        db.execute(ImportRaw.__table__.insert(), new_raws[index:index + RAW_BATCH])
    _apply_sync_boundaries(db, sync_specs, now)
    db.commit()  # 原始留档先落盘：后续归一化再出错也不丢数据，响应恒 200

    # 5. 归一化（同请求 try/except，绝不 5xx）
    try:
        for e in entries:
            if (e["rtype"], e["ext_id"]) not in process_keys:
                continue
            rec, rtype, ext_id = e["rec"], e["rtype"], e["ext_id"]
            if rtype == "unknown":
                # Unknown evidence is never discarded by a legacy watermark: it
                # has no trusted semantic time/type until a parser recognizes it.
                _mark_raw(
                    db,
                    SOURCE,
                    rtype,
                    ext_id,
                    "pending",
                    version=PARSER_VERSION,
                    pending_reason="unsupported_record_type",
                    attempted=True,
                )
                continue
            rec_ts = _record_time(rec)
            cursor_state = db.get(SyncCursor, (SOURCE, rtype))
            legacy_state = db.get(SyncState, SOURCE)
            watermark = (
                cursor_state.watermark if cursor_state is not None
                else legacy_state.watermark if legacy_state is not None else None
            )
            if watermark is not None and rec_ts is not None and rec_ts <= watermark:
                _mark_raw(db, SOURCE, rtype, ext_id, "skipped")  # 水位线以内：zip 历史已覆盖
                continue
            try:
                with db.begin_nested():  # 单条失败回滚到 SAVEPOINT，不毒化整个事务
                    if rtype == "steps":
                        d, cnt = _extract_steps(rec)
                        affected_steps.add(d)
                        normalized = {"date": d.isoformat(), "steps": cnt}
                    elif rtype == "weight":
                        d, ts, kg = _extract_weight(rec)
                        affected_weights.add(d)
                        normalized = {"date": d.isoformat(), "observed_at": _iso_ts(ts), "weight_kg": kg}
                    elif rtype == "sleep":
                        d = _normalize_sleep(db, rec, ext_id)
                        affected_sleep.add(d)
                        normalized = {"date": d.isoformat()}
                    elif rtype == "exercise":
                        _normalize_exercise(db, rec, ext_id)
                        normalized = {"date": _local_date(rec_ts, rec).isoformat()} if rec_ts else {}
                    elif rtype == "heart_rate":
                        samples = _extract_heart_rate_samples(rec)
                        dates = {day for _, day, _ in samples}
                        affected_heart_rate.update(dates)
                        normalized = {
                            "dates": sorted(day.isoformat() for day in dates),
                            "sample_count": len(samples),
                        }
                    else:  # defensive: a classifier addition must add a parser explicitly
                        raise HealthConnectNormalizationError(
                            "parser_not_registered", f"{rtype} 尚未注册归一化解析器"
                        )
                _mark_raw(
                    db,
                    SOURCE,
                    rtype,
                    ext_id,
                    "parsed",
                    normalized=normalized,
                    attempted=True,
                )
            except Exception as exc:
                reason, detail = _failure_reason(exc)
                _mark_raw(
                    db,
                    SOURCE,
                    rtype,
                    ext_id,
                    "failed",
                    detail,
                    pending_reason=reason,
                    attempted=True,
                )

        # Rebuild affected derived days from the latest version of every source record.
        # This makes version updates idempotent instead of double-counting interval data.
        _rebuild_hc_steps(db, affected_steps)
        _rebuild_hc_weight(db, affected_weights)
        _rebuild_hc_heart_rate(db, affected_heart_rate)
        _refresh_sleep_days(db, affected_sleep)

        # 6. 成功：sync_state 记 last_success_at、清零失败计数（不触碰 watermark）
        _touch_sync_state(db, SOURCE, True, now=now)
        for spec in sync_specs:
            state = db.get(SyncCursor, (SOURCE, spec["record_type"]))
            if state is None:
                continue
            if spec["permission"] == "granted" and not state.needs_resync:
                if spec["cursor"] is not None:
                    state.cursor_token = spec["cursor"]
                state.last_success_at = now
                state.last_error = None
                state.consecutive_failures = 0
        db.commit()
    except Exception as exc:  # 归一化整体失败：本批 raw 标 failed（导入中心可见），只记状态
        db.rollback()
        try:
            for rtype, ext_id in process_keys:
                _mark_raw(
                    db,
                    SOURCE,
                    rtype,
                    ext_id,
                    "failed",
                    f"batch_normalization_failed: {str(exc)[:400]}",
                    pending_reason="batch_normalization_failed",
                )
            _touch_sync_state(db, SOURCE, False, str(exc))
            for spec in sync_specs:
                state = db.get(SyncCursor, (SOURCE, spec["record_type"]))
                if state is not None:
                    state.last_error = str(exc)[:500]
                    state.consecutive_failures = int(state.consecutive_failures or 0) + 1
            db.commit()
        except Exception:
            db.rollback()

    cursor_rows = db.execute(
        select(SyncCursor).where(SyncCursor.source == SOURCE).order_by(SyncCursor.record_type)
    ).scalars().all()
    return JSONResponse({
        "received": received,
        "processed": len(process_keys),
        "skipped": max(len(entries) - len(process_keys) - len(conflict_keys), 0),
        "conflicts": len(conflict_keys),
        "sync": [_cursor_payload(row) for row in cursor_rows],
    })


@router.post("/health_connect/replay")
async def replay_health_connect(request: Request, db: Session = Depends(get_db)) -> Response:
    """Explicitly re-run bounded pending/failed Health Connect raw evidence.

    The endpoint intentionally supports only parser-backed types. Unknown records
    can be inspected here without changing their status; when a future release adds
    a classifier/parser, extending the dispatch below makes the same retained rows
    replayable without asking the device to resend them.
    """
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    body = await request.body()
    if len(body) > 16 * 1024:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        payload = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "unsupported payload"}, status_code=400)
    record_type = str(payload.get("record_type") or "heart_rate").strip().lower()
    if record_type not in {"heart_rate", "unknown"}:
        return JSONResponse(
            {"error": "record_type must be heart_rate or unknown"}, status_code=400
        )
    try:
        limit = int(payload.get("limit", 250))
    except (TypeError, ValueError):
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)
    if not 1 <= limit <= 1000:
        return JSONResponse({"error": "limit must be between 1 and 1000"}, status_code=400)
    include_failed = payload.get("include_failed", True)
    if not isinstance(include_failed, bool):
        return JSONResponse({"error": "include_failed must be boolean"}, status_code=400)
    external_id = payload.get("external_id")
    if external_id is not None and (
        not isinstance(external_id, str) or not 1 <= len(external_id.strip()) <= 512
    ):
        return JSONResponse({"error": "external_id is invalid"}, status_code=400)
    external_id = external_id.strip() if isinstance(external_id, str) else None

    statuses = ["pending", "failed"] if include_failed else ["pending"]
    statement = (
        select(ImportRaw)
        .where(
            ImportRaw.source == SOURCE,
            ImportRaw.record_type == record_type,
            ImportRaw.parse_status.in_(statuses),
            # A version conflict requires corrected upstream evidence, not replay
            # of the previously stored version.
            func.coalesce(ImportRaw.pending_reason, "") != "version_conflict",
        )
        .order_by(ImportRaw.imported_at, ImportRaw.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if external_id is not None:
        statement = statement.where(ImportRaw.external_id == external_id)
    rows = db.execute(statement).scalars().all()

    parsed_count = failed_count = still_pending = 0
    affected_heart_rate: set[date] = set()
    for row in rows:
        if record_type == "unknown":
            # Re-run classification so the operation is useful evidence that the
            # current release still has no safe semantic parser for this payload.
            inferred = _infer_record_type(row.raw)
            reason = (
                "unsupported_record_type"
                if inferred == "unknown"
                else "reclassification_requires_migration"
            )
            _mark_raw(
                db,
                SOURCE,
                row.record_type,
                row.external_id,
                "pending",
                version=PARSER_VERSION,
                pending_reason=reason,
                attempted=True,
            )
            still_pending += 1
            continue

        affected_heart_rate.update(_existing_heart_rate_dates(row))
        try:
            with db.begin_nested():
                samples = _extract_heart_rate_samples(row.raw)
                dates = {day for _, day, _ in samples}
                affected_heart_rate.update(dates)
                normalized = {
                    "dates": sorted(day.isoformat() for day in dates),
                    "sample_count": len(samples),
                }
            _mark_raw(
                db,
                SOURCE,
                row.record_type,
                row.external_id,
                "parsed",
                normalized=normalized,
                attempted=True,
            )
            parsed_count += 1
        except Exception as exc:
            reason, detail = _failure_reason(exc)
            _mark_raw(
                db,
                SOURCE,
                row.record_type,
                row.external_id,
                "failed",
                detail,
                pending_reason=reason,
                attempted=True,
            )
            failed_count += 1

    _rebuild_hc_heart_rate(db, affected_heart_rate)
    db.commit()
    return JSONResponse({
        "record_type": record_type,
        "external_id": external_id,
        "parser_version": PARSER_VERSION,
        "selected": len(rows),
        "parsed": parsed_count,
        "failed": failed_count,
        "still_pending": still_pending,
    })


# ---------- 小米体脂秤（BLE 网关通道，设计文档外新增） ----------

MISCALE_SOURCE = "miscale"


def _miscale_ts(v: Any) -> datetime | None:
    """秤的 RTC 时间：naive ISO 视为本地时区（秤跟米家配对时对的是本地钟）。"""
    if isinstance(v, str):
        s = v.strip()
        if s and not re.fullmatch(r"-?\d{10,}", s):
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=LOCAL_TZ)
    return _parse_ts(v)


def _miscale_profile(db: Session) -> tuple[str | None, float | None, float | None]:
    """app_settings → (sex, age_years, height_cm)；缺项返回 None（只记体重）。"""
    stored = {
        r.key: r.value
        for r in db.execute(
            select(AppSetting).where(AppSetting.key.in_(["sex", "birth_date", "height_cm"]))
        ).scalars()
    }
    sex = stored.get("sex") if stored.get("sex") in ("male", "female") else None
    height = stored.get("height_cm")
    height_cm = float(height) if isinstance(height, (int, float)) and not isinstance(height, bool) else None
    age: float | None = None
    birth = stored.get("birth_date")
    if isinstance(birth, str):
        try:
            age = (now_local().date() - date.fromisoformat(birth)).days / 365.25
        except ValueError:
            age = None
    return sex, age, height_cm


@router.post("/miscale")
async def ingest_miscale(request: Request, db: Session = Depends(get_db)) -> Response:
    """体脂秤测量接收：手机/NAS 监听器解析 BLE 广播后 POST 解析结果。

    请求体：{"measurements":[{"ts": ISO8601|epoch, "weight_kg": float,
      "impedance": number?, "impedance_low": number?, "impedance_high": number?,
      "heart_rate": int?, "profile_id": int?, "model": str?}]}
    （或单个测量对象）。去重键 = 秤 RTC 时间戳 + 体重，两个监听器同时上报只记一条。
    体成分按档案（性别/生日/身高）计算；档案不全只记体重。响应恒 200（防重发风暴）。
    """
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if isinstance(payload, dict) and isinstance(payload.get("measurements"), list):
        records = payload["measurements"]
    elif isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = [payload]
    else:
        return JSONResponse({"error": "unsupported payload"}, status_code=400)

    now = now_local()
    # 解析 + 批内按去重键唯一
    parsed: dict[str, dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        w = _qty(item.get("weight_kg")) or _qty(item.get("weight"))
        if w is None or not (10 <= w <= 300):
            continue
        ts = _miscale_ts(item.get("ts") or item.get("timestamp"))
        # 秤 RTC 掉电/未对时的兜底：时间明显不对就用服务器当前时间
        if ts is None or ts.year < 2015 or ts > now + timedelta(days=1):
            ts = now
        # S400 的 50kHz 低频阻抗用于现有体成分公式；旧客户端仍只发 impedance。
        z = _qty(item.get("impedance_low"))
        if z is None:
            z = _qty(item.get("impedance"))
        impedance = float(z) if z is not None and 0 < z < 3000 else None
        zh = _qty(item.get("impedance_high"))
        impedance_high = float(zh) if zh is not None and 0 < zh < 3000 else None
        hr = _qty(item.get("heart_rate"))
        heart_rate = round(float(hr)) if hr is not None and 30 <= hr <= 240 else None
        profile_raw = item.get("profile_id")
        profile_id = profile_raw if isinstance(profile_raw, int) and 0 <= profile_raw <= 255 else None
        model_raw = item.get("model")
        model = model_raw.strip()[:64] if isinstance(model_raw, str) and model_raw.strip() else None
        attempt_id = str(item.get("attempt_id") or "")
        if not re.fullmatch(r"[0-9a-f-]{36}", attempt_id):
            attempt_id = ""
        # 秤 RTC 失效时监听器/服务端都以"当前时间取整到分钟"兜底，双端 key 才能对齐
        if ts == now:
            ts = now.replace(second=0, microsecond=0)
        ext_id = f"{ts.astimezone(LOCAL_TZ):%Y%m%dT%H%M%S}-{round(w * 200)}"
        parsed[ext_id] = {
            "ts": ts, "weight": round(float(w), 2), "impedance": impedance,
            "impedance_high": impedance_high, "heart_rate": heart_rate,
            "profile_id": profile_id, "model": model, "attempt_id": attempt_id,
        }

    received = len(records)
    if not parsed:
        return JSONResponse({"received": received, "new": 0})

    # 留档 + 去重（phone 与 NAS 双监听：同一次测量 ext_id 相同，冲突只刷新 last_seen_at）
    new_ids: set[str] = set()
    entries = [
        {
            "source": MISCALE_SOURCE,
            "record_type": "measurement",
            "external_id": ext_id,
            "provenance": {"received_at": now.isoformat(), **({
                "last_attempt_id": m["attempt_id"], "first_attempt_id": m["attempt_id"],
                "attempt_received_at": now.isoformat(),
            } if m["attempt_id"] else {})},
            "raw": {
                "ts": m["ts"].isoformat(), "weight_kg": m["weight"],
                "impedance": m["impedance"], "impedance_low": m["impedance"],
                "impedance_high": m["impedance_high"], "heart_rate": m["heart_rate"],
                "profile_id": m["profile_id"], "model": m["model"],
            },
            "parse_status": "pending",
            "parse_version": 0,
            "last_seen_at": now,
        }
        for ext_id, m in parsed.items()
    ]
    ins = pg_insert(ImportRaw).values(entries)
    stmt = ins.on_conflict_do_update(
        index_elements=["source", "record_type", "external_id"],
        set_={"last_seen_at": now, "provenance": func.coalesce(
            ImportRaw.provenance, cast({}, JSONB)).op("||")(
                ins.excluded.provenance.op("-")("first_attempt_id"))},
    ).returning(ImportRaw.external_id, literal_column("(xmax = 0)"))
    for ext_id, is_new in db.execute(stmt):
        if is_new:
            new_ids.add(ext_id)
    db.commit()

    try:
        # 同日多次测量：本批内取最后一次；跨批后到者覆盖（自动回填字段可重写）
        by_day: dict[date, tuple[datetime, str]] = {}
        for ext_id in new_ids:
            m = parsed[ext_id]
            d = m["ts"].astimezone(LOCAL_TZ).date()
            prev = by_day.get(d)
            if prev is None or m["ts"] >= prev[0]:
                by_day[d] = (m["ts"], ext_id)

        sex, age, height_cm = _miscale_profile(db)
        for d in sorted(by_day):
            _, ext_id = by_day[d]
            m = parsed[ext_id]
            values = compute_body_metrics(m["weight"], m["impedance"], sex, age, height_cm)
            autofill_fields(db, d, MISCALE_SOURCE, values)
        for ext_id in new_ids:
            _mark_raw(db, MISCALE_SOURCE, "measurement", ext_id, "parsed", attempted=True)

        _touch_sync_state(db, MISCALE_SOURCE, True, now=now)
        db.commit()
    except Exception as exc:  # 批次失败：raw 标 failed 留痕（重发不会重新归一化）；绝不 5xx
        db.rollback()
        try:
            for ext_id in new_ids:
                _mark_raw(db, MISCALE_SOURCE, "measurement", ext_id, "failed",
                          f"批次归一化失败：{str(exc)[:400]}")
            _touch_sync_state(db, MISCALE_SOURCE, False, str(exc))
            db.commit()
        except Exception:
            db.rollback()

    return JSONResponse({"received": received, "new": len(new_ids)})


# ---------- 三星健康 Data SDK 直读通道（docs/mobile-sync.md） ----------
#
# 与 HC 通道的关键差异：Android 端读到的是**已归一化的聚合值**（当日步数总数、
# 完整睡眠会话等），不是增量区间记录，所以 daily 走 SET 语义（不能累加）。
# 契约（Android 端 SamsungSyncWorker 发送）：
# {
#   "daily":          [{"date","steps"?,"distance_m"?,"active_kcal"?,"hr_min"?,"hr_avg"?,"hr_max"?}],
#   "sleep_sessions": [{"external_id","start","end","light_min"?,"deep_min"?,"rem_min"?,
#                        "awake_min"?,"total_sleep_min"?}],
#   "exercises":      [{"external_id","start","end"?,"type"?,"duration_min"?,"distance_km"?,
#                        "calories"?,"avg_hr"?,"max_hr"?}],
#   "body":           [{"ts","weight_kg"?,"body_fat_pct"?,"skeletal_muscle_kg"?,"muscle_mass_kg"?,
#                        "body_water_kg"?,"bmr_kcal"?}]
# }
# daily 的 external_id 含内容哈希：同日数值变化（步数增长）会作为新 raw 行重新归一化，
# 数值未变的重发则照常去重。水位线：记录时间 <= sync_state('samsung_direct').watermark
# 的置 skipped（与 zip 历史一刀切，同 HC 口径）。

SAMSUNG_DIRECT_SOURCE = "samsung_direct"

_SD_DAILY_FIELDS = ("steps", "distance_m", "active_kcal", "hr_min", "hr_avg", "hr_max")
_SD_BODY_FIELDS = (
    "weight_kg", "body_fat_pct", "skeletal_muscle_kg", "muscle_mass_kg",
    "body_water_kg", "bmr_kcal",
)


def _sd_date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v).strip())
    except (TypeError, ValueError):
        return None


def _sd_int(v: Any, lo: float, hi: float) -> int | None:
    n = _qty(v)
    return round(n) if n is not None and lo <= n <= hi else None


def _sd_num(v: Any, lo: float, hi: float, ndigits: int = 1) -> float | None:
    n = _qty(v)
    return round(n, ndigits) if n is not None and lo <= n <= hi else None


def _upsert_samsung_exercise(db: Session, data: dict[str, Any]) -> None:
    """三星“起飞”写 release_logs，其余仍写 workout_logs；支持上游改名后双向搬移。"""
    start = datetime.fromisoformat(data["start"])
    common = {
        "log_date": start.astimezone(LOCAL_TZ).date(),
        "started_at": start,
        "duration_min": data["duration_min"],
        "calories": data["calories"],
        "avg_hr": data["avg_hr"],
        "max_hr": data["max_hr"],
        "source": SAMSUNG_DIRECT_SOURCE,
        "external_id": data["sid"],
    }
    if is_release_session(data["type"]):
        ins = pg_insert(ReleaseLog).values(**common)
        db.execute(ins.on_conflict_do_update(
            index_elements=["source", "external_id"],
            index_where=text("external_id IS NOT NULL"),
            set_={
                **{c: getattr(ins.excluded, c) for c in (
                    "log_date", "started_at", "duration_min", "calories", "avg_hr", "max_hr",
                )},
                "updated_at": text("now()"),
            },
        ))
        db.execute(delete(WorkoutLog).where(
            WorkoutLog.source == SAMSUNG_DIRECT_SOURCE,
            WorkoutLog.external_id == data["sid"],
        ))
        return

    ins = pg_insert(WorkoutLog).values(
        **common,
        session_type=data["type"],
        distance_km=data["distance_km"],
    )
    db.execute(ins.on_conflict_do_update(
        index_elements=["source", "external_id"],
        index_where=text("external_id IS NOT NULL"),
        set_={
            **{c: getattr(ins.excluded, c) for c in (
                "log_date", "started_at", "session_type", "duration_min",
                "distance_km", "calories", "avg_hr", "max_hr",
            )},
            "updated_at": text("now()"),
        },
    ))
    db.execute(delete(ReleaseLog).where(
        ReleaseLog.source == SAMSUNG_DIRECT_SOURCE,
        ReleaseLog.external_id == data["sid"],
    ))


@router.post("/samsung_direct")
async def ingest_samsung_direct(request: Request, db: Session = Depends(get_db)) -> Response:
    reject = _bearer_reject(request)
    if reject is not None:
        return reject
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "unsupported payload"}, status_code=400)

    now = now_local()
    entries: list[dict[str, Any]] = []   # {rtype, ext_id, rec_ts, data}
    received = 0

    def _collect(rtype: str, ext_id: str, rec_ts: datetime | None, data: dict[str, Any]) -> None:
        entries.append({"rtype": rtype, "ext_id": ext_id, "rec_ts": rec_ts, "data": data})

    # -- daily：SET 语义；ext_id 带内容哈希，数值变化才重新归一化
    for item in payload.get("daily") or []:
        if not isinstance(item, dict):
            continue
        received += 1
        d = _sd_date(item.get("date"))
        if d is None or d > now.date():
            continue
        data: dict[str, Any] = {"date": d.isoformat()}
        data["steps"] = _sd_int(item.get("steps"), 0, 200000)
        data["distance_m"] = _sd_int(item.get("distance_m"), 0, 500000)
        data["active_kcal"] = _sd_num(item.get("active_kcal"), 0, 20000)
        data["hr_min"] = _sd_int(item.get("hr_min"), 20, 250)
        data["hr_avg"] = _sd_int(item.get("hr_avg"), 20, 250)
        data["hr_max"] = _sd_int(item.get("hr_max"), 20, 250)
        if all(data[f] is None for f in _SD_DAILY_FIELDS):
            continue
        digest = hashlib.sha1(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]
        rec_ts = datetime.combine(d, datetime.min.time(), tzinfo=LOCAL_TZ)
        _collect("daily", f"daily-{d.isoformat()}-{digest}", rec_ts, data)

    # -- sleep_sessions：按 (source, 稳定 id) upsert；raw 去重键掺内容哈希，
    #    上游修订（三星醒后常修正时长）重推时才会重新归一化并覆盖
    for item in payload.get("sleep_sessions") or []:
        if not isinstance(item, dict):
            continue
        received += 1
        start = _parse_ts(item.get("start"))
        end = _parse_ts(item.get("end"))
        ext = str(item.get("external_id") or "").strip()
        if start is None or end is None or end <= start or not ext:
            continue
        data = {
            "sid": f"sd-{ext}",
            "start": start.isoformat(), "end": end.isoformat(),
            "awake_min": _sd_int(item.get("awake_min"), 0, 1440),
            "light_min": _sd_int(item.get("light_min"), 0, 1440),
            "deep_min": _sd_int(item.get("deep_min"), 0, 1440),
            "rem_min": _sd_int(item.get("rem_min"), 0, 1440),
            "total_sleep_min": _sd_int(item.get("total_sleep_min"), 0, 1440),
        }
        digest = hashlib.sha1(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]
        _collect("sleep", f"sd-{ext}-{digest}", end, data)

    # -- exercises：按 (source, 稳定 id) 分流 upsert；raw 键同样掺内容哈希
    for item in payload.get("exercises") or []:
        if not isinstance(item, dict):
            continue
        received += 1
        start = _parse_ts(item.get("start"))
        ext = str(item.get("external_id") or "").strip()
        if start is None or not ext:
            continue
        end = _parse_ts(item.get("end"))
        duration = _sd_int(item.get("duration_min"), 1, 1440)
        if duration is None and end is not None and end > start:
            duration = round((end - start).total_seconds() / 60)
        session_type = str(item.get("type") or "other").strip().lower()[:50] or "other"
        data = {
            "sid": f"sd-{ext}",
            "start": start.isoformat(), "type": session_type, "duration_min": duration,
            "distance_km": _sd_num(item.get("distance_km"), 0.01, 1000, 2),
            "calories": _sd_int(item.get("calories"), 1, 20000),
            "avg_hr": _sd_int(item.get("avg_hr"), 20, 250),
            "max_hr": _sd_int(item.get("max_hr"), 20, 250),
        }
        digest = hashlib.sha1(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]
        _collect("exercise", f"sd-{ext}-{digest}", start, data)

    # -- body：手表 BIA 体成分 → body_metrics 字段级回填（同日取最后一次）
    for item in payload.get("body") or []:
        if not isinstance(item, dict):
            continue
        received += 1
        ts = _parse_ts(item.get("ts"))
        if ts is None:
            continue
        data = {"ts": ts.isoformat()}
        data["weight_kg"] = _sd_num(item.get("weight_kg"), 10, 500, 2)
        data["body_fat_pct"] = _sd_num(item.get("body_fat_pct"), 1, 75)
        data["skeletal_muscle_kg"] = _sd_num(item.get("skeletal_muscle_kg"), 1, 300, 2)
        data["muscle_mass_kg"] = _sd_num(item.get("muscle_mass_kg"), 1, 300, 2)
        data["body_water_kg"] = _sd_num(item.get("body_water_kg"), 1, 300, 2)
        data["bmr_kcal"] = _sd_int(item.get("bmr_kcal"), 300, 10000)
        if all(data[f] is None for f in _SD_BODY_FIELDS):
            continue
        digest = hashlib.sha1(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]
        _collect("body", f"body-{ts:%Y%m%dT%H%M%S}-{digest}", ts, data)

    if not entries:
        return JSONResponse({"received": received, "new": 0})

    # 留档 + 去重（重发只刷新 last_seen_at）
    seen: set[tuple[str, str]] = set()
    raw_rows = []
    for e in entries:
        k = (e["rtype"], e["ext_id"])
        if k in seen:
            continue
        seen.add(k)
        raw_rows.append({
            "source": SAMSUNG_DIRECT_SOURCE,
            "record_type": e["rtype"],
            "external_id": e["ext_id"],
            "raw": e["data"],
            "parse_status": "pending",
            "parse_version": 0,
            "last_seen_at": now,
        })
    new_keys: set[tuple[str, str]] = set()
    for i in range(0, len(raw_rows), RAW_BATCH):
        ins = pg_insert(ImportRaw).values(raw_rows[i:i + RAW_BATCH])
        stmt = ins.on_conflict_do_update(
            index_elements=["source", "record_type", "external_id"],
            set_={"last_seen_at": now},
        ).returning(ImportRaw.record_type, ImportRaw.external_id, literal_column("(xmax = 0)"))
        for rtype, ext_id, is_new in db.execute(stmt):
            if is_new:
                new_keys.add((rtype, ext_id))
    db.commit()

    try:
        state = db.get(SyncState, SAMSUNG_DIRECT_SOURCE)
        watermark = state.watermark if state is not None else None
        sleep_dates: set[date] = set()
        body_by_day: dict[date, tuple[datetime, dict[str, Any]]] = {}

        for e in entries:
            if (e["rtype"], e["ext_id"]) not in new_keys:
                continue
            rtype, ext_id, data = e["rtype"], e["ext_id"], e["data"]
            if watermark is not None and e["rec_ts"] is not None:
                # daily 是天级 SET 语义（rec_ts 固定为当日 00:00）：按日期与水位线比较，
                # 等于水位线当天的照常放行（幂等覆盖）——否则 zip 导入当天（水位线≈导出
                # 时刻）之后的步数/心率增量会被冻结到次日
                if rtype == "daily":
                    if e["rec_ts"].astimezone(LOCAL_TZ).date() < watermark.astimezone(LOCAL_TZ).date():
                        _mark_raw(db, SAMSUNG_DIRECT_SOURCE, rtype, ext_id, "skipped")
                        continue
                elif e["rec_ts"] <= watermark:
                    _mark_raw(db, SAMSUNG_DIRECT_SOURCE, rtype, ext_id, "skipped")
                    continue
            try:
                with db.begin_nested():
                    if rtype == "daily":
                        d = date.fromisoformat(data["date"])
                        values = {f: data[f] for f in _SD_DAILY_FIELDS if data[f] is not None}
                        ins = pg_insert(DailyActivity).values(
                            log_date=d,
                            source=SAMSUNG_DIRECT_SOURCE,
                            field_sources={field: SAMSUNG_DIRECT_SOURCE for field in values},
                            **values,
                        )
                        db.execute(ins.on_conflict_do_update(
                            index_elements=["log_date"],
                            set_={
                                **{f: getattr(ins.excluded, f) for f in values},
                                "source": ins.excluded.source,
                                "field_sources": DailyActivity.__table__.c.field_sources.op("||")(
                                    ins.excluded.field_sources
                                ),
                                "updated_at": text("now()"),
                            },
                        ))
                    elif rtype == "sleep":
                        start = datetime.fromisoformat(data["start"])
                        end = datetime.fromisoformat(data["end"])
                        wake_date = end.astimezone(LOCAL_TZ).date()
                        total = data["total_sleep_min"]
                        if total is None:
                            stage_sum = sum(
                                data[f] or 0 for f in ("light_min", "deep_min", "rem_min")
                            )
                            total = stage_sum or round((end - start).total_seconds() / 60)
                        ins = pg_insert(SleepSession).values(
                            source=SAMSUNG_DIRECT_SOURCE, external_id=data["sid"],
                            start_at=start, end_at=end, wake_date=wake_date,
                            awake_min=data["awake_min"], light_min=data["light_min"],
                            deep_min=data["deep_min"], rem_min=data["rem_min"],
                            total_sleep_min=total,
                        )
                        db.execute(ins.on_conflict_do_update(
                            index_elements=["source", "external_id"],
                            set_={c: getattr(ins.excluded, c) for c in (
                                "start_at", "end_at", "wake_date", "awake_min",
                                "light_min", "deep_min", "rem_min", "total_sleep_min",
                            )},
                        ))
                        sleep_dates.add(wake_date)
                    elif rtype == "exercise":
                        _upsert_samsung_exercise(db, data)
                    else:  # body
                        ts = datetime.fromisoformat(data["ts"])
                        d = ts.astimezone(LOCAL_TZ).date()
                        prev = body_by_day.get(d)
                        if prev is None or ts >= prev[0]:
                            body_by_day[d] = (ts, data)
                _mark_raw(db, SAMSUNG_DIRECT_SOURCE, rtype, ext_id, "parsed")
            except Exception as exc:
                _mark_raw(db, SAMSUNG_DIRECT_SOURCE, rtype, ext_id, "failed", str(exc)[:500])

        for d in sorted(body_by_day):
            _, data = body_by_day[d]
            autofill_fields(db, d, SAMSUNG_DIRECT_SOURCE, {
                f: data[f] for f in _SD_BODY_FIELDS if data[f] is not None
            })
        for d in sorted(sleep_dates):
            total, source = total_sleep_with_source(db, d)  # 跨源去重，保留实际胜出的来源
            if total > 0 and source is not None:
                autofill_fields(db, d, source, {"sleep_hours": round(total / 60.0, 1)})

        _touch_sync_state(db, SAMSUNG_DIRECT_SOURCE, True, now=now)
        db.commit()
    except Exception as exc:  # 批次失败：raw 标 failed 留痕（重发不会重新归一化）
        db.rollback()
        try:
            for rtype, ext_id in new_keys:
                _mark_raw(db, SAMSUNG_DIRECT_SOURCE, rtype, ext_id, "failed",
                          f"批次归一化失败：{str(exc)[:400]}")
            _touch_sync_state(db, SAMSUNG_DIRECT_SOURCE, False, str(exc))
            db.commit()
        except Exception:
            db.rollback()

    return JSONResponse({"received": received, "new": len(new_keys)})
