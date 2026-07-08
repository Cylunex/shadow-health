"""三星健康官方导出 zip 历史导入器（设计文档 §3.0 时间口径 / §3.2 取数规则 / §3.7 通道 1 / 附录 B）。

CLI：python -m app.importers.samsung_zip <zip路径> [--dry-run]
Web 上传（settings 模块）调用 import_zip(zip_path, db=..., job_id=..., progress_cb=...)。

处理的表：
  exercise → workout_logs；weight → body_metrics（autofill）；
  sleep_stage → sleep_sessions → 回填 sleep_hours；step_daily_trend → daily_activity；
  heart_rate → 日聚合 daily_activity.hr_*（原始行不入 import_raw，5 万行仅流式聚合）；
  oxygen_saturation → body_metrics.spo2_pct（autofill）；user_profile/height → app_settings。

CSV 格式（附录 B 实测）：首行 `表名,版本,N` 元数据，第二行才是列头；列名剥
`com.samsung.health.xxx.` 前缀；事件表 UTC 时间 + 行级 time_offset；day_time 为
本地日字符串（兼容毫秒 epoch，见 app.timeutil.parse_day_time）。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from sqlalchemy import literal_column, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    AppSetting,
    DailyActivity,
    ImportJob,
    ImportRaw,
    SleepSession,
    SyncState,
    WorkoutLog,
)
from app.services.autofill import autofill_fields
from app.timeutil import now_local, parse_day_time, parse_event_time, parse_time_offset

PARSER_VERSION = 1
SOURCE = "samsung_zip"
BATCH_SIZE = 800

# binning_data 等列可能非常长；Windows 上 C long 为 32 位，不能用 sys.maxsize
csv.field_size_limit(2**31 - 1)

# ---- exercise_type → session_type（Samsung Health SDK HealthConstants.Exercise 公开常量表）----
# 实测核对：10007 全部 distance/speed=0（室内循环训练），11007 有里程与速度（骑行），
# 确认 SDK 口径 10007=Circuit training、11007=Cycling（v1.0 文档猜的 10007=cycling 不成立）。
ACTIVITY_TYPE_MAP: dict[int, str] = {
    1001: "walking",           # Walking
    1002: "running",           # Running
    10007: "circuit_training",  # Circuit training
    10012: "squats",           # Squats
    11007: "cycling",          # Cycling
    13001: "hiking",           # Hiking
    13003: "backpacking",      # Backpacking
    15006: "elliptical",       # Elliptical trainer
}
# 0=自定义训练：不在硬编码清单内，按设计归 'other'，原码存 detail

# sleep_stage.stage 码（§3.2 已实测验证）
STAGE_FIELD = {40001: "awake_min", 40002: "light_min", 40003: "deep_min", 40004: "rem_min"}

# 表名（CSV 文件名前缀）
T_EXERCISE = "com.samsung.shealth.exercise"
T_WEIGHT = "com.samsung.health.weight"
T_SLEEP_STAGE = "com.samsung.health.sleep_stage"
T_STEP_TREND = "com.samsung.shealth.step_daily_trend"
T_HEART_RATE = "com.samsung.shealth.tracker.heart_rate"
T_SPO2 = "com.samsung.shealth.tracker.oxygen_saturation"
T_USER_PROFILE = "com.samsung.health.user_profile"
T_HEIGHT = "com.samsung.health.height"
T_PEDOMETER = "com.samsung.shealth.tracker.pedometer_day_summary"

ProgressCb = Callable[[dict[str, Any]], None] | None


# ---------- 基础工具 ----------

def _strip_col(col: str) -> str:
    """剥 com.samsung.health.xxx. / com.samsung.shealth.xxx. 列名前缀。"""
    return col.rsplit(".", 1)[-1] if col.startswith("com.samsung.") else col


def _fnum(row: dict, key: str) -> float | None:
    v = row.get(key)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _pos(v: float | None) -> float | None:
    """0/负值视为无数据（三星单点心率行 min/max 常填 0.0）。"""
    return v if v is not None and v > 0 else None


def _fallback_id(row: dict) -> str:
    return hashlib.sha1(
        json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _job_update(job_id: int | None, **fields: Any) -> None:
    """import_jobs 进度用独立短会话立即提交，供 Web 轮询可见。"""
    if not job_id:
        return
    try:
        with SessionLocal() as s:
            job = s.get(ImportJob, job_id)
            if job is not None:
                for k, v in fields.items():
                    setattr(job, k, v)
                s.commit()
    except Exception:
        pass  # 进度写失败不影响导入本身


class _Importer:
    def __init__(self, zf: zipfile.ZipFile, db: Session | None, dry_run: bool,
                 progress_cb: ProgressCb, report: dict):
        self.zf = zf
        self.db = db
        self.dry = dry_run
        self.cb = progress_cb
        self.report = report
        self.max_ts: datetime | None = None  # 数据最大事件时间戳（UTC），完成后写 watermark

    # ---- 通用 ----
    def emit(self, **kw: Any) -> None:
        if self.cb:
            try:
                self.cb(kw)
            except Exception:
                pass

    def member(self, table: str) -> str | None:
        pat = re.compile(re.escape(table) + r"\.\d+\.csv$")
        for name in self.zf.namelist():
            if pat.search(name.rsplit("/", 1)[-1]):
                return name
        return None

    def iter_csv(self, member: str) -> Iterator[dict[str, str]]:
        """流式读 CSV：跳首行元数据，第二行列头（剥前缀），空串值剔除。"""
        with self.zf.open(member) as fh:
            txt = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
            txt.readline()  # 首行：表名,版本号,N
            reader = csv.reader(txt)
            header = next(reader, None)
            if not header:
                return
            cols = [_strip_col(c) for c in header]
            ncols = len(cols)
            for parts in reader:
                if not parts:
                    continue
                row: dict[str, str] = {}
                for i in range(min(ncols, len(parts))):
                    v = parts[i].strip()
                    if v != "":
                        row[cols[i]] = v
                yield row

    def touch_ts(self, ts: datetime | None) -> None:
        if ts is not None and (self.max_ts is None or ts > self.max_ts):
            self.max_ts = ts

    def local_date(self, ts: datetime, offset: str | None) -> date:
        return ts.astimezone(parse_time_offset(offset)).date()

    def track_range(self, ranges: dict, d: date) -> None:
        if "min" not in ranges or d < ranges["min"]:
            ranges["min"] = d
        if "max" not in ranges or d > ranges["max"]:
            ranges["max"] = d

    def set_range(self, record_type: str, ranges: dict) -> None:
        if "min" in ranges:
            self.report["date_ranges"][record_type] = [
                ranges["min"].isoformat(), ranges["max"].isoformat()
            ]

    # ---- import_raw 留档 ----
    def flush_raw(self, record_type: str, batch: list[dict], stats: dict) -> None:
        """ON CONFLICT (source,record_type,external_id) 只刷新 last_seen_at（≈DO NOTHING）。"""
        if not batch or self.dry:
            batch.clear()
            return
        # 同批内去重，避免 ON CONFLICT 二次命中同行
        seen: set[str] = set()
        uniq: list[dict] = []
        for item in batch:
            if item["external_id"] in seen:
                stats["raw_skipped"] = stats.get("raw_skipped", 0) + 1
                continue
            seen.add(item["external_id"])
            uniq.append(item)
        batch.clear()
        if not uniq:
            return
        now = now_local()
        ins = pg_insert(ImportRaw).values(uniq)
        stmt = ins.on_conflict_do_update(
            index_elements=["source", "record_type", "external_id"],
            set_={"last_seen_at": now},
        ).returning(literal_column("(xmax = 0)"))
        flags = [r[0] for r in self.db.execute(stmt)]
        ins_n = sum(1 for f in flags if f)
        stats["raw_inserted"] = stats.get("raw_inserted", 0) + ins_n
        stats["raw_skipped"] = stats.get("raw_skipped", 0) + (len(uniq) - ins_n)

    def raw_item(self, record_type: str, row: dict, external_id: str | None,
                 status: str = "parsed", error: str | None = None) -> dict:
        return {
            "source": SOURCE,
            "record_type": record_type,
            "external_id": external_id or _fallback_id(row),
            "raw": row,  # 已剥前缀、剔空值；sidecar 文件不读（blob 留空）
            "time_offset": row.get("time_offset"),
            "parse_status": status,
            "parse_error": error,
            "parse_version": PARSER_VERSION,
            "last_seen_at": now_local(),
        }

    # ---- 1. exercise → workout_logs ----
    def import_exercise(self) -> None:
        stats = {"rows": 0, "inserted": 0, "skipped": 0, "failed": 0}
        self.report["tables"]["exercise"] = stats
        member = self.member(T_EXERCISE)
        if not member:
            stats["error"] = "CSV 缺失"
            return
        type_counts: dict[str, int] = defaultdict(int)
        unmapped: set[int] = set()
        ranges: dict = {}
        raw_batch: list[dict] = []
        norm_batch: list[dict] = []
        seen_ids: set[str] = set()

        def flush_norm() -> None:
            if not norm_batch or self.dry:
                norm_batch.clear()
                return
            ins = pg_insert(WorkoutLog).values(list(norm_batch))
            norm_batch.clear()
            stmt = ins.on_conflict_do_update(
                index_elements=["source", "external_id"],
                index_where=text("external_id IS NOT NULL"),
                set_={
                    "log_date": ins.excluded.log_date,
                    "started_at": ins.excluded.started_at,
                    "session_type": ins.excluded.session_type,
                    "duration_min": ins.excluded.duration_min,
                    "distance_km": ins.excluded.distance_km,
                    "calories": ins.excluded.calories,
                    "avg_hr": ins.excluded.avg_hr,
                    "max_hr": ins.excluded.max_hr,
                    "detail": ins.excluded.detail,
                    "notes": ins.excluded.notes,
                    "updated_at": text("now()"),
                },
            ).returning(literal_column("(xmax = 0)"))
            flags = [r[0] for r in self.db.execute(stmt)]
            n = sum(1 for f in flags if f)
            stats["inserted"] += n
            stats["skipped"] += len(flags) - n

        for row in self.iter_csv(member):
            stats["rows"] += 1
            ext_id = row.get("datauuid")
            try:
                started = parse_event_time(row["start_time"])
                self.touch_ts(started)
                log_date = self.local_date(started, row.get("time_offset"))
                self.track_range(ranges, log_date)
                code_f = _fnum(row, "exercise_type")
                code = int(code_f) if code_f is not None else None
                type_counts[str(code)] += 1
                session_type = ACTIVITY_TYPE_MAP.get(code, "other")
                if code not in ACTIVITY_TYPE_MAP:
                    unmapped.add(code)
                dur = _fnum(row, "duration")
                dist = _fnum(row, "distance")
                cal = _fnum(row, "calorie")
                detail: dict[str, Any] = {"exercise_type": code}
                if row.get("title"):
                    detail["title"] = row["title"]
                item = {
                    "log_date": log_date,
                    "started_at": started,
                    "session_type": session_type,
                    "duration_min": round(dur / 60000) if dur is not None else None,
                    "distance_km": round(dist / 1000, 2) if _pos(dist) else None,
                    "calories": round(cal) if _pos(cal) else None,
                    "avg_hr": (round(v) if (v := _pos(_fnum(row, "mean_heart_rate"))) else None),
                    "max_hr": (round(v) if (v := _pos(_fnum(row, "max_heart_rate"))) else None),
                    "detail": detail,
                    "source": SOURCE,
                    "external_id": ext_id or _fallback_id(row),
                    "notes": None,
                }
                if item["external_id"] in seen_ids:
                    stats["skipped"] += 1
                else:
                    seen_ids.add(item["external_id"])
                    norm_batch.append(item)
                raw_batch.append(self.raw_item("exercise", row, ext_id))
            except Exception as e:
                stats["failed"] += 1
                raw_batch.append(self.raw_item("exercise", row, ext_id, "failed", str(e)[:500]))
            if len(raw_batch) >= BATCH_SIZE:
                self.flush_raw("exercise", raw_batch, stats)
            if len(norm_batch) >= BATCH_SIZE:
                flush_norm()
            if stats["rows"] % 2000 == 0:
                self.emit(table="exercise", done=stats["rows"])
        self.flush_raw("exercise", raw_batch, stats)
        flush_norm()
        stats["activity_type_counts"] = dict(type_counts)
        self.report["unmapped_activity_types"] = sorted(c for c in unmapped if c is not None)
        self.set_range("exercise", ranges)
        self.emit(table="exercise", done=stats["rows"], finished=True)

    # ---- 2. weight → body_metrics（autofill 回填）----
    def import_weight(self) -> None:
        stats = {"rows": 0, "inserted": 0, "skipped": 0, "failed": 0}
        self.report["tables"]["weight"] = stats
        member = self.member(T_WEIGHT)
        if not member:
            stats["error"] = "CSV 缺失"
            return
        ranges: dict = {}
        raw_batch: list[dict] = []
        # 同日多条取本地时间最后一条：log_date → (event_ts, values)
        by_day: dict[date, tuple[datetime, dict]] = {}
        for row in self.iter_csv(member):
            stats["rows"] += 1
            ext_id = row.get("datauuid")
            try:
                ts = parse_event_time(row["start_time"])
                self.touch_ts(ts)
                d = self.local_date(ts, row.get("time_offset"))
                self.track_range(ranges, d)
                values = {
                    "weight_kg": (round(v, 2) if (v := _fnum(row, "weight")) is not None else None),
                    "body_fat_pct": (round(v, 1) if (v := _pos(_fnum(row, "body_fat"))) else None),
                    "muscle_mass_kg": (round(v, 2) if (v := _pos(_fnum(row, "muscle_mass"))) else None),
                    "skeletal_muscle_kg": (round(v, 2) if (v := _pos(_fnum(row, "skeletal_muscle_mass"))) else None),
                    "bmr_kcal": (round(v) if (v := _pos(_fnum(row, "basal_metabolic_rate"))) else None),
                    "body_water_kg": (round(v, 2) if (v := _pos(_fnum(row, "total_body_water"))) else None),
                    "visceral_fat_level": (round(v) if (v := _pos(_fnum(row, "vfa_level"))) else None),
                }
                prev = by_day.get(d)
                if prev is None or ts > prev[0]:
                    by_day[d] = (ts, values)
                raw_batch.append(self.raw_item("weight", row, ext_id))
            except Exception as e:
                stats["failed"] += 1
                raw_batch.append(self.raw_item("weight", row, ext_id, "failed", str(e)[:500]))
        self.flush_raw("weight", raw_batch, stats)
        stats["days"] = len(by_day)
        if not self.dry:
            for d in sorted(by_day):
                written = autofill_fields(self.db, d, SOURCE, by_day[d][1])
                if written:
                    stats["inserted"] += 1
                else:
                    stats["skipped"] += 1
        self.set_range("weight", ranges)
        self.emit(table="weight", done=stats["rows"], finished=True)

    # ---- 3. sleep_stage → sleep_sessions → 回填 sleep_hours ----
    def import_sleep(self) -> None:
        stats = {"rows": 0, "inserted": 0, "skipped": 0, "failed": 0}
        self.report["tables"]["sleep_stage"] = stats
        member = self.member(T_SLEEP_STAGE)
        if not member:
            stats["error"] = "CSV 缺失"
            return
        raw_batch: list[dict] = []
        # sleep_id → 会话累加器
        sessions: dict[str, dict] = {}
        for row in self.iter_csv(member):
            stats["rows"] += 1
            ext_id = row.get("datauuid")
            try:
                sid = row["sleep_id"]
                start = parse_event_time(row["start_time"])
                end = parse_event_time(row["end_time"])
                self.touch_ts(end)
                stage = int(_fnum(row, "stage") or 0)
                sess = sessions.setdefault(sid, {
                    "start": start, "end": end, "offset": row.get("time_offset"),
                    "secs": defaultdict(float),
                })
                if start < sess["start"]:
                    sess["start"] = start
                if end > sess["end"]:
                    sess["end"] = end
                    sess["offset"] = row.get("time_offset")
                field = STAGE_FIELD.get(stage)
                if field:
                    sess["secs"][field] += (end - start).total_seconds()
                else:
                    sess["secs"]["unknown"] += (end - start).total_seconds()
                raw_batch.append(self.raw_item("sleep_stage", row, ext_id))
            except Exception as e:
                stats["failed"] += 1
                raw_batch.append(self.raw_item("sleep_stage", row, ext_id, "failed", str(e)[:500]))
            if len(raw_batch) >= BATCH_SIZE:
                self.flush_raw("sleep_stage", raw_batch, stats)
        self.flush_raw("sleep_stage", raw_batch, stats)

        # 合成一夜一条
        ranges: dict = {}
        norm: list[dict] = []
        stage_total = defaultdict(float)
        hours_by_wake: dict[date, int] = defaultdict(int)
        for sid, sess in sessions.items():
            mins = {f: round(sess["secs"].get(f, 0) / 60) for f in
                    ("awake_min", "light_min", "deep_min", "rem_min")}
            total = mins["light_min"] + mins["deep_min"] + mins["rem_min"]
            wake_date = self.local_date(sess["end"], sess["offset"])
            self.track_range(ranges, wake_date)
            for f, s in sess["secs"].items():
                stage_total[f] += s
            hours_by_wake[wake_date] += total
            norm.append({
                "source": SOURCE, "external_id": sid,
                "start_at": sess["start"], "end_at": sess["end"], "wake_date": wake_date,
                "awake_min": mins["awake_min"], "light_min": mins["light_min"],
                "deep_min": mins["deep_min"], "rem_min": mins["rem_min"],
                "total_sleep_min": total,
            })
        stats["sessions"] = len(norm)
        # 分期分布 sanity（浅睡占比应最高）
        known = {k: v for k, v in stage_total.items() if k != "unknown"}
        tot = sum(known.values()) or 1
        stats["stage_distribution_pct"] = {
            k.removesuffix("_min"): round(v * 100 / tot, 1) for k, v in known.items()
        }
        if not self.dry and norm:
            for i in range(0, len(norm), BATCH_SIZE):
                chunk = norm[i:i + BATCH_SIZE]
                ins = pg_insert(SleepSession).values(chunk)
                stmt = ins.on_conflict_do_update(
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
                ).returning(literal_column("(xmax = 0)"))
                flags = [r[0] for r in self.db.execute(stmt)]
                n = sum(1 for f in flags if f)
                stats["inserted"] += n
                stats["skipped"] += len(flags) - n
            # 每个 wake_date 回填 sleep_hours（同日多会话取总和；单会话即设计口径 total/60）
            backfilled = 0
            for d in sorted(hours_by_wake):
                total = hours_by_wake[d]
                if total <= 0:
                    continue
                if autofill_fields(self.db, d, SOURCE, {"sleep_hours": round(total / 60.0, 1)}):
                    backfilled += 1
            stats["sleep_hours_backfilled"] = backfilled
        self.set_range("sleep_stage", ranges)
        self.emit(table="sleep_stage", done=stats["rows"], finished=True)

    # ---- 4. step_daily_trend → daily_activity（只取 source_type=-2）----
    def import_steps(self) -> None:
        stats = {"rows": 0, "inserted": 0, "skipped": 0, "failed": 0}
        self.report["tables"]["step_daily_trend"] = stats
        member = self.member(T_STEP_TREND)
        if not member:
            stats["error"] = "CSV 缺失"
            return
        ranges: dict = {}
        raw_batch: list[dict] = []
        kept = 0
        dropped = 0
        # 同日多条 -2 行取 update_time 最新：day → (update_ts, row)
        by_day: dict[date, tuple[datetime, dict]] = {}
        for row in self.iter_csv(member):
            stats["rows"] += 1
            try:
                if row.get("source_type") != "-2":
                    dropped += 1  # 分设备行丢弃，不留档
                    continue
                kept += 1
                d = parse_day_time(row["day_time"])
                self.track_range(ranges, d)
                upd = parse_event_time(row.get("update_time") or row["day_time"])
                self.touch_ts(upd)
                prev = by_day.get(d)
                if prev is None or upd > prev[0]:
                    by_day[d] = (upd, row)
                raw_batch.append(self.raw_item("step_daily_trend", row, row.get("datauuid")))
            except Exception as e:
                stats["failed"] += 1
                raw_batch.append(self.raw_item(
                    "step_daily_trend", row, row.get("datauuid"), "failed", str(e)[:500]))
            if len(raw_batch) >= BATCH_SIZE:
                self.flush_raw("step_daily_trend", raw_batch, stats)
        self.flush_raw("step_daily_trend", raw_batch, stats)
        stats["agg_rows_kept"] = kept
        stats["device_rows_dropped"] = dropped
        stats["days"] = len(by_day)
        stats["dup_day_rows"] = kept - len(by_day)
        if not self.dry and by_day:
            items = []
            for d in sorted(by_day):
                row = by_day[d][1]
                cnt = _fnum(row, "count")
                dist = _fnum(row, "distance")
                cal = _fnum(row, "calorie")
                items.append({
                    "log_date": d,
                    "steps": round(cnt) if cnt is not None else None,
                    "distance_m": round(dist) if dist is not None else None,
                    "active_kcal": round(cal, 1) if cal is not None else None,
                    "source": SOURCE,
                })
            for i in range(0, len(items), BATCH_SIZE):
                chunk = items[i:i + BATCH_SIZE]
                ins = pg_insert(DailyActivity).values(chunk)
                stmt = ins.on_conflict_do_update(
                    index_elements=["log_date"],
                    set_={
                        "steps": ins.excluded.steps,
                        "distance_m": ins.excluded.distance_m,
                        "active_kcal": ins.excluded.active_kcal,
                        "source": ins.excluded.source,
                        "updated_at": text("now()"),
                    },
                ).returning(literal_column("(xmax = 0)"))
                flags = [r[0] for r in self.db.execute(stmt)]
                n = sum(1 for f in flags if f)
                stats["inserted"] += n
                stats["skipped"] += len(flags) - n
        self.set_range("step_daily_trend", ranges)
        self.emit(table="step_daily_trend", done=stats["rows"], finished=True)

    # ---- 5. heart_rate → 日聚合 daily_activity.hr_*（不入 import_raw）----
    def import_heart_rate(self) -> None:
        stats = {"rows": 0, "inserted": 0, "skipped": 0, "failed": 0,
                 "note": "原始行不留 import_raw（5 万行仅流式聚合，性能取舍见设计文档 §3.7）"}
        self.report["tables"]["heart_rate"] = stats
        member = self.member(T_HEART_RATE)
        if not member:
            stats["error"] = "CSV 缺失"
            return
        ranges: dict = {}
        # d → [min, max, sum, n]
        agg: dict[date, list[float]] = {}
        for row in self.iter_csv(member):
            stats["rows"] += 1
            try:
                ts = parse_event_time(row["start_time"])
                self.touch_ts(parse_event_time(row.get("end_time") or row["start_time"]))
                d = self.local_date(ts, row.get("time_offset"))
                hr = _pos(_fnum(row, "heart_rate"))
                lo = _pos(_fnum(row, "min")) or hr
                hi = _pos(_fnum(row, "max")) or hr
                if hr is None and lo is None and hi is None:
                    continue  # 只有 binning 引用、无数值的行
                self.track_range(ranges, d)
                cur = agg.get(d)
                if cur is None:
                    agg[d] = [lo or hi, hi or lo, hr or 0.0, 1 if hr is not None else 0]
                else:
                    if lo is not None and lo < cur[0]:
                        cur[0] = lo
                    if hi is not None and hi > cur[1]:
                        cur[1] = hi
                    if hr is not None:
                        cur[2] += hr
                        cur[3] += 1
            except Exception:
                stats["failed"] += 1
            if stats["rows"] % 10000 == 0:
                self.emit(table="heart_rate", done=stats["rows"])
        stats["days"] = len(agg)
        if not self.dry and agg:
            items = []
            for d in sorted(agg):
                lo, hi, s, n = agg[d]
                items.append({
                    "log_date": d,
                    "hr_min": round(lo) if lo else None,
                    "hr_avg": round(s / n) if n else None,
                    "hr_max": round(hi) if hi else None,
                })
            for i in range(0, len(items), BATCH_SIZE):
                chunk = items[i:i + BATCH_SIZE]
                ins = pg_insert(DailyActivity).values(chunk)
                stmt = ins.on_conflict_do_update(
                    index_elements=["log_date"],
                    set_={
                        "hr_min": ins.excluded.hr_min,
                        "hr_avg": ins.excluded.hr_avg,
                        "hr_max": ins.excluded.hr_max,
                        "updated_at": text("now()"),
                    },
                ).returning(literal_column("(xmax = 0)"))
                flags = [r[0] for r in self.db.execute(stmt)]
                n = sum(1 for f in flags if f)
                stats["inserted"] += n
                stats["skipped"] += len(flags) - n
        self.set_range("heart_rate", ranges)
        self.emit(table="heart_rate", done=stats["rows"], finished=True)

    # ---- 6. oxygen_saturation → body_metrics.spo2_pct（autofill）----
    def import_spo2(self) -> None:
        stats = {"rows": 0, "inserted": 0, "skipped": 0, "failed": 0}
        self.report["tables"]["oxygen_saturation"] = stats
        member = self.member(T_SPO2)
        if not member:
            stats["error"] = "CSV 缺失"
            return
        ranges: dict = {}
        raw_batch: list[dict] = []
        by_day: dict[date, tuple[datetime, float]] = {}
        for row in self.iter_csv(member):
            stats["rows"] += 1
            ext_id = row.get("datauuid")
            try:
                ts = parse_event_time(row["start_time"])
                self.touch_ts(ts)
                d = self.local_date(ts, row.get("time_offset"))
                spo2 = _pos(_fnum(row, "spo2"))
                if spo2 is not None:
                    self.track_range(ranges, d)
                    prev = by_day.get(d)
                    if prev is None or ts > prev[0]:
                        by_day[d] = (ts, round(spo2, 1))
                raw_batch.append(self.raw_item("oxygen_saturation", row, ext_id))
            except Exception as e:
                stats["failed"] += 1
                raw_batch.append(self.raw_item(
                    "oxygen_saturation", row, ext_id, "failed", str(e)[:500]))
        self.flush_raw("oxygen_saturation", raw_batch, stats)
        stats["days"] = len(by_day)
        if not self.dry:
            for d in sorted(by_day):
                if autofill_fields(self.db, d, SOURCE, {"spo2_pct": by_day[d][1]}):
                    stats["inserted"] += 1
                else:
                    stats["skipped"] += 1
        self.set_range("oxygen_saturation", ranges)
        self.emit(table="oxygen_saturation", done=stats["rows"], finished=True)

    # ---- 7. user_profile + height → app_settings ----
    def import_profile(self) -> None:
        stats = {"rows": 0, "inserted": 0, "skipped": 0, "failed": 0}
        self.report["tables"]["user_profile"] = stats
        candidates: list[tuple[datetime, float]] = []  # (update_time, height_cm)
        member = self.member(T_USER_PROFILE)
        if member:
            for row in self.iter_csv(member):
                stats["rows"] += 1
                try:
                    if row.get("key") == "height" and (v := _fnum(row, "float_value")):
                        candidates.append((parse_event_time(row.get("update_time") or "0"), v))
                except Exception:
                    stats["failed"] += 1
        member = self.member(T_HEIGHT)
        if member:
            for row in self.iter_csv(member):
                stats["rows"] += 1
                try:
                    if (v := _fnum(row, "height")) is not None:
                        candidates.append((
                            parse_event_time(row.get("update_time") or row["start_time"]), v))
                except Exception:
                    stats["failed"] += 1
        if candidates:
            height = max(candidates, key=lambda t: t[0])[1]
            height_val = int(height) if float(height).is_integer() else round(height, 1)
            stats["height_cm"] = height_val
            self.report.setdefault("settings", {})["height_cm"] = height_val
            if not self.dry:
                # 回填语义：已有值（可能是用户手填）不覆盖
                stmt = pg_insert(AppSetting).values(
                    key="height_cm", value=height_val
                ).on_conflict_do_nothing(index_elements=["key"]).returning(AppSetting.key)
                if self.db.execute(stmt).first() is not None:
                    stats["inserted"] += 1
                else:
                    stats["skipped"] += 1
        # user_profile 的 weight 键是当前体重非目标值，无目标体重键 → 不写 target_weight_kg
        stats["note"] = "user_profile 无目标体重键，仅回填 height_cm"
        self.emit(table="user_profile", done=stats["rows"], finished=True)

    # ---- dry-run 附加：pedometer_day_summary 交叉校验（不入库）----
    def crosscheck_pedometer(self) -> None:
        member = self.member(T_PEDOMETER)
        if not member:
            return
        days: set[date] = set()
        rows = 0
        for row in self.iter_csv(member):
            rows += 1
            try:
                if row.get("day_time"):
                    days.add(parse_day_time(row["day_time"]))
            except Exception:
                pass
        trend = self.report["tables"].get("step_daily_trend", {})
        self.report["tables"]["pedometer_day_summary"] = {
            "rows": rows, "inserted": 0, "skipped": rows, "failed": 0,
            "days": len(days),
            "note": f"仅 dry-run 交叉校验不入库；step_daily_trend 覆盖 {trend.get('days')} 天",
        }

    # ---- 收尾：sync_state 水位线 ----
    def update_sync_state(self) -> None:
        if self.dry:
            if self.max_ts is not None:
                self.report["watermark"] = self.max_ts.isoformat()
            return
        now = now_local()
        if self.max_ts is not None:
            ins = pg_insert(SyncState).values(source="health_connect", watermark=self.max_ts)
            self.db.execute(ins.on_conflict_do_update(
                index_elements=["source"], set_={"watermark": ins.excluded.watermark}))
            self.report["watermark"] = self.max_ts.isoformat()
        ins = pg_insert(SyncState).values(
            source=SOURCE, last_success_at=now, consecutive_failures=0)
        self.db.execute(ins.on_conflict_do_update(
            index_elements=["source"],
            set_={"last_success_at": ins.excluded.last_success_at,
                  "last_error": None, "consecutive_failures": 0}))


# ---------- 公共入口（settings 模块 Web 上传调用同一函数） ----------

def import_zip(zip_path: str | Path, db: Session | None = None, dry_run: bool = False,
               job_id: int | None = None, progress_cb: ProgressCb = None) -> dict:
    """导入三星健康导出 zip。

    返回报告 dict：{"tables": {表名: {"rows","inserted","skipped","failed",...}},
    "date_ranges": {表名: [起, 止]}, "unmapped_activity_types": [...], "watermark": ...}。
    job_id 非空时把进度/结果写 health.import_jobs（独立会话即时提交，供轮询）。
    dry_run 只读 zip 出报告，不写任何业务表。
    """
    zip_path = Path(zip_path)
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "tables": {},
        "date_ranges": {},
        "unmapped_activity_types": [],
        "watermark": None,
    }
    own_session = db is None and not dry_run
    if own_session:
        db = SessionLocal()
    _job_update(job_id, status="running", started_at=now_local())
    try:
        with zipfile.ZipFile(zip_path) as zf:
            imp = _Importer(zf, db, dry_run, progress_cb, report)
            imp.import_profile()
            imp.import_exercise()
            imp.import_weight()
            imp.import_sleep()
            imp.import_steps()
            imp.import_heart_rate()
            imp.import_spo2()
            if dry_run:
                imp.crosscheck_pedometer()
            imp.update_sync_state()
            if own_session:
                db.commit()
            elif db is not None:
                db.flush()  # 外部会话由调用方（get_db）提交
        totals = {k: sum(t.get(k, 0) for t in report["tables"].values())
                  for k in ("rows", "inserted", "skipped", "failed")}
        report["totals"] = totals
        _job_update(job_id, status="done", finished_at=now_local(), report=report,
                    total=totals["rows"], inserted=totals["inserted"],
                    skipped=totals["skipped"], failed=totals["failed"])
        return report
    except Exception as e:
        if own_session and db is not None:
            db.rollback()
        _job_update(job_id, status="failed", finished_at=now_local(),
                    error=str(e)[:2000], report=report)
        raise
    finally:
        if own_session and db is not None:
            db.close()


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m app.importers.samsung_zip",
        description="三星健康导出 zip 历史导入（设计文档 §3.7 通道 1）",
    )
    ap.add_argument("zip_path", help="SamsungHealth.zip 路径")
    ap.add_argument("--dry-run", action="store_true", help="只读 zip 输出报告，不写库")
    args = ap.parse_args(argv)

    def cb(ev: dict) -> None:
        if ev.get("finished"):
            print(f"  [{ev['table']}] 完成，共 {ev['done']} 行")

    report = import_zip(args.zip_path, dry_run=args.dry_run, progress_cb=cb)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
