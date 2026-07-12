"""personal_data 旧库 → shadow-health 一次性迁移（V3 批次 P3，docs/subpath-agent-plan.md §3）。

幂等可重跑：全部源行先落 import_raw source='legacy'（external_id='legacy-{表}-{id}'，
(source,record_type,external_id) 唯一，重跑只刷 last_seen_at），归一化按 parse_status
门控——'parsed' 绝不重放；'pending'/'failed'/'skipped' 每次重试（skipped 的原因可能
消失，如后来建了饮水习惯）。归一化本身也幂等（diet 查重 / body_metrics upsert /
workout 部分唯一索引 / habit_logs ON CONFLICT），双保险。

十表决策（§3.1，字段级映射见各 _migrate_* docstring）：
  diet_records / weight_records / activity_records / daily_summary / daily_drinks /
  body_measurements / personal_info 迁；exercise_summary / monthly_activity /
  life_memories 不迁（派生数据 / 无明细支撑 / 非健康数据）。

用法（NAS 执行手册见 docs/legacy-migration-runbook.md）：
  uv run python scripts/migrate_personal_data.py \
      --source-dsn postgresql://USER:PASS@HOST:PORT/personal_data [--dry-run]
  源连接串也可用环境变量 LEGACY_SOURCE_DSN 传（避免进 shell history）。
  目标默认 .env 的 DATABASE_URL（应用同款），可用 --target-dsn 覆盖。
  --dry-run：完整跑一遍管线出报告，最后整体回滚，不落任何数据。

安全护栏：源连接强制只读（default_transaction_read_only=on）；启动先自检目标库
迁移 12 已就位（body_metrics.mood_score 列 + import_raw 词表含 'legacy'），缺则拒跑。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    AppSetting,
    BodyMetrics,
    DietLog,
    Habit,
    HabitLog,
    ImportRaw,
    SyncState,
    WorkoutLog,
)
from app.services.autofill import get_or_create_day
from app.timeutil import now_local

SOURCE = "legacy"
PARSE_VERSION = 1

# 归一化的七张源表（顺序即处理顺序）；不迁：exercise_summary/monthly_activity/life_memories
MIGRATED_TABLES = [
    "diet_records",
    "weight_records",
    "activity_records",
    "daily_summary",
    "daily_drinks",
    "body_measurements",
    "personal_info",
]

# 日期列解析候选（information_schema 实测优先，候选没有再唯一 date 型列兜底）
_DATE_CANDIDATES = [
    "record_date", "log_date", "date", "entry_date", "summary_date",
    "drink_date", "measure_date", "measurement_date", "recorded_on", "day",
]

_MEAL_MAP = {
    "breakfast": "早餐", "早餐": "早餐", "早饭": "早餐", "早点": "早餐",
    "lunch": "午餐", "午餐": "午餐", "午饭": "午餐", "中餐": "午餐", "中饭": "午餐",
    "dinner": "晚餐", "晚餐": "晚餐", "晚饭": "晚餐", "supper": "晚餐",
    "snack": "加餐", "加餐": "加餐", "夜宵": "加餐", "宵夜": "加餐",
    "下午茶": "加餐", "other": "加餐", "extra": "加餐",
}

_SEX_MAP = {
    "male": "male", "m": "male", "男": "male",
    "female": "female", "f": "female", "女": "female",
}


def _normalize_dsn(dsn: str) -> str:
    """裸 postgresql:// 归到 psycopg3 驱动（psycopg2 未安装）。"""
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return dsn


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _dec(v: Any) -> Decimal | None:
    """源数值 → Decimal（Numeric 列入参）；None/空串返回 None。"""
    if v is None or v == "":
        return None
    return Decimal(str(v))


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    return round(float(v))


def _same(a: Any, b: Any) -> bool:
    """目标已有值与迁入值是否等价（Decimal/float/int 混比）。"""
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a) == str(b)


class TableReport:
    """单表计数器：迁入/跳过（按原因）/失败/前次已迁/留档新增。"""

    def __init__(self) -> None:
        self.source_rows = 0
        self.archived_new = 0
        self.already = 0     # 前次运行已 parsed，本次不重放
        self.migrated = 0
        self.failed = 0
        self.skipped: dict[str, int] = {}

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_rows": self.source_rows,
            "archived_new": self.archived_new,
            "already": self.already,
            "migrated": self.migrated,
            "skipped": dict(self.skipped),
            "failed": self.failed,
        }


# ---------- 源库读取 ----------

class SourceTable:
    """一张源表：列集实测 + 日期/主键列解析 + 全行读取（按 id 升序，重跑顺序稳定）。"""

    def __init__(self, conn, schema: str, name: str, need_date: bool = True) -> None:
        self.name = name
        rows = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t"
            ),
            {"s": schema, "t": name},
        ).all()
        if not rows:
            raise SystemExit(f"源库缺表 {schema}.{name}——确认 --source-dsn/--source-schema 指向 personal_data")
        self.columns = {r[0]: r[1] for r in rows}
        if "id" not in self.columns:
            raise SystemExit(f"源表 {name} 没有 id 列（实际列：{sorted(self.columns)}），无法生成幂等 external_id")
        self.date_col: str | None = None
        if need_date:
            self.date_col = next((c for c in _DATE_CANDIDATES if c in self.columns), None)
            if self.date_col is None:
                date_typed = [c for c, t in self.columns.items() if t == "date"]
                if len(date_typed) == 1:
                    self.date_col = date_typed[0]
                else:
                    raise SystemExit(
                        f"源表 {name} 日期列无法确定（date 型列：{date_typed or '无'}，"
                        f"全部列：{sorted(self.columns)}）——请人工确认后在 _DATE_CANDIDATES 补上"
                    )
        self.rows = [
            dict(m)
            for m in conn.execute(
                text(f'SELECT * FROM "{schema}"."{name}" ORDER BY id')
            ).mappings()
        ]

    def log_date(self, row: dict) -> date:
        v = row[self.date_col]
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        return date.fromisoformat(str(v)[:10])


# ---------- 留档与门控 ----------

def _ext_id(table: str, row_id: Any) -> str:
    return f"legacy-{table}-{row_id}"


def _archive(db: Session, table: SourceTable, rep: TableReport) -> dict[str, str]:
    """全行落 import_raw（重复只刷 last_seen_at）；返回 {ext_id: 归一化前 parse_status}。"""
    now = now_local()
    status = {
        r[0]: r[1]
        for r in db.execute(
            select(ImportRaw.external_id, ImportRaw.parse_status).where(
                ImportRaw.source == SOURCE, ImportRaw.record_type == table.name
            )
        )
    }
    for row in table.rows:
        ext_id = _ext_id(table.name, row["id"])
        if ext_id not in status:
            rep.archived_new += 1
        db.execute(
            pg_insert(ImportRaw)
            .values(
                source=SOURCE,
                record_type=table.name,
                external_id=ext_id,
                raw={k: _jsonable(v) for k, v in row.items()},
                parse_status="pending",
                parse_version=PARSE_VERSION,
            )
            .on_conflict_do_update(
                index_elements=["source", "record_type", "external_id"],
                set_={"last_seen_at": now},
            )
        )
    return status


def _mark(db: Session, table: str, ext_id: str, status: str, error: str | None = None) -> None:
    db.execute(
        text(
            "UPDATE health.import_raw SET parse_status = :st, parse_error = :err, "
            "parse_version = :ver WHERE source = 'legacy' AND record_type = :rt "
            "AND external_id = :eid"
        ),
        {"st": status, "err": error, "ver": PARSE_VERSION, "rt": table, "eid": ext_id},
    )


def _each_row(db: Session, table: SourceTable, rep: TableReport, status: dict[str, str], handler) -> None:
    """行级循环：parsed 门控 + savepoint 隔离单行失败。

    handler(row, log_date) 返回 ('migrated'|'skipped', 跳过原因|None)。
    """
    for row in table.rows:
        ext_id = _ext_id(table.name, row["id"])
        if status.get(ext_id) == "parsed":
            rep.already += 1
            continue
        sp = db.begin_nested()
        try:
            outcome, reason = handler(row, table.log_date(row) if table.date_col else None)
            sp.commit()
        except Exception as e:  # noqa: BLE001 — 单行失败隔离，报告里点名
            sp.rollback()
            rep.failed += 1
            _mark(db, table.name, ext_id, "failed", str(e)[:500])
            continue
        if outcome == "migrated":
            rep.migrated += 1
            _mark(db, table.name, ext_id, "parsed")
        else:
            rep.skip(reason)
            _mark(db, table.name, ext_id, "skipped", reason)


# ---------- 手动优先写入（照 autofill/mark_manual 语义） ----------

def _apply_manual(row: BodyMetrics, values: dict[str, Any], blocked: list[str], d: date) -> tuple[int, int]:
    """旧库值是手动记录：NULL 或 autofilled 登记过的字段可写（写后解除登记=转手动）；
    目标已是手动值则不覆盖（新库里用户亲手记的优先），计 blocked。
    返回 (写入字段数, 同值无操作字段数)。"""
    autofilled = dict(row.autofilled or {})
    written = same = 0
    for field, value in values.items():
        if value is None:
            continue
        current = getattr(row, field)
        if _same(current, value) and current is not None:
            same += 1
            autofilled.pop(field, None)  # 同值也转正为手动，防后续同步改动它
            continue
        if current is None or field in autofilled:
            setattr(row, field, value)
            autofilled.pop(field, None)
            written += 1
        else:
            blocked.append(f"{d} {field}（现值 {current} 保留，旧库 {value} 弃）")
    row.autofilled = autofilled
    return written, same


def _append_note(row: BodyMetrics, note: str) -> bool:
    if not note or (row.notes and note in row.notes):
        return False
    row.notes = f"{row.notes}\n{note}" if row.notes else note
    return True


# ---------- 各表归一化 ----------

def _migrate_diet(db: Session, table: SourceTable, rep: TableReport, status: dict, report: dict) -> None:
    """diet_records → diet_logs：free_text=food_name(+portion_desc)，kcal/蛋白/碳水/脂肪直映，
    meal_type 映射 CHECK 词表（未知值兜底加餐并列报告）；查重口径=同日+同文本+同 kcal。"""

    def handler(row: dict, d: date):
        food = (row.get("food_name") or "").strip()
        if not food:
            return "skipped", "food_name 为空"
        portion = (row.get("portion_desc") or "").strip()
        free_text = f"{food}（{portion}）" if portion else food
        raw_meal = str(row.get("meal_type") or "").strip()
        meal = _MEAL_MAP.get(raw_meal.lower()) or _MEAL_MAP.get(raw_meal)
        if meal is None:
            meal = "加餐"
            report["meal_fallback"].setdefault(raw_meal or "(空)", 0)
            report["meal_fallback"][raw_meal or "(空)"] += 1
        kcal = _dec(row.get("calories"))
        dup = db.execute(
            select(DietLog.id).where(
                DietLog.log_date == d,
                DietLog.free_text == free_text,
                text("kcal IS NOT DISTINCT FROM CAST(:k AS numeric(6,1))").bindparams(k=kcal),
            )
        ).first()
        if dup:
            return "skipped", "同日+同文本+同 kcal 已存在"
        db.add(
            DietLog(
                log_date=d,
                meal=meal,
                free_text=free_text,
                kcal=kcal,
                protein_g=_dec(row.get("protein_g")),
                carb_g=_dec(row.get("carbs_g")),
                fat_g=_dec(row.get("fat_g")),
            )
        )
        db.flush()
        return "migrated", None

    _each_row(db, table, rep, status, handler)


def _migrate_weight(db: Session, table: SourceTable, rep: TableReport, status: dict, report: dict) -> None:
    """weight_records → body_metrics 按 log_date upsert；weight_kg/body_fat_pct 手动优先
    （可覆盖三星 autofill 同日值）；bmi 不迁（app 自算）；notes 追加。"""

    def handler(row: dict, d: date):
        bm = get_or_create_day(db, d)
        written, same = _apply_manual(
            bm,
            {"weight_kg": _dec(row.get("weight_kg")), "body_fat_pct": _dec(row.get("body_fat_pct"))},
            report["manual_blocked"], d,
        )
        note_added = _append_note(bm, (row.get("notes") or "").strip())
        if written or same or note_added:
            return "migrated", None
        return "skipped", "同日已有手动值，未写入任何字段"

    _each_row(db, table, rep, status, handler)


def _migrate_activity(db: Session, table: SourceTable, rep: TableReport, status: dict, report: dict) -> None:
    """activity_records → workout_logs source='manual'、external_id='legacy-{id}'
    （部分唯一索引天然幂等）；steps/intensity/pace_kmh 并入 detail JSONB。"""

    def handler(row: dict, d: date):
        ext_id = f"legacy-{row['id']}"
        detail = {
            k: _jsonable(row[k])
            for k in ("steps", "intensity", "pace_kmh")
            if row.get(k) not in (None, "")
        }
        inserted = db.execute(
            pg_insert(WorkoutLog)
            .values(
                log_date=d,
                session_type=(row.get("activity_type") or "未知").strip() or "未知",
                duration_min=_int(row.get("duration_minutes")),
                distance_km=_dec(row.get("distance_km")),
                calories=_int(row.get("calories_burned")),
                avg_hr=_int(row.get("avg_hr")),
                max_hr=_int(row.get("max_hr")),
                detail=detail or None,
                source="manual",
                external_id=ext_id,
                notes=(row.get("notes") or "").strip() or None,
            )
            .on_conflict_do_nothing(
                index_elements=["source", "external_id"],
                index_where=WorkoutLog.__table__.c.external_id.isnot(None),
            )
            .returning(WorkoutLog.id)
        ).scalar_one_or_none()
        if inserted is None:
            return "skipped", "external_id 已存在（前次已迁入）"
        return "migrated", None

    _each_row(db, table, rep, status, handler)


def _migrate_summary(db: Session, table: SourceTable, rep: TableReport, status: dict, report: dict) -> None:
    """daily_summary：mood_score → body_metrics.mood_score（1~10 校验，越界报 failed）；
    target_calories → app_settings.target_kcal 仅当现值为空（取最新一天的值，循环外处理）；
    宏量汇总不迁（app 自算）。"""

    def handler(row: dict, d: date):
        mood = row.get("mood_score")
        if mood is None:
            return "skipped", "无 mood_score"
        mood = int(mood)
        if not 1 <= mood <= 10:
            raise ValueError(f"mood_score={mood} 越界（合法 1~10）")
        bm = get_or_create_day(db, d)
        if bm.mood_score is None:
            bm.mood_score = mood
            return "migrated", None
        if bm.mood_score == mood:
            return "migrated", None
        return "skipped", f"当日 mood_score 已有值 {bm.mood_score}，旧库 {mood} 弃"

    _each_row(db, table, rep, status, handler)

    # target_kcal：与行级门控无关，幂等由「仅当现值为空」保证；坏值不拖垮整轮
    sp = db.begin_nested()
    try:
        latest = max(
            (r for r in table.rows if r.get("target_calories") not in (None, "")),
            key=lambda r: table.log_date(r),
            default=None,
        )
        if latest is None:
            report["settings"]["target_kcal"] = "源无 target_calories"
            sp.commit()
            return
        setting = db.get(AppSetting, "target_kcal")
        if setting is not None and setting.value is not None:
            report["settings"]["target_kcal"] = (
                f"现值 {setting.value} 保留（旧库 {latest['target_calories']} 弃）"
            )
            sp.commit()
            return
        value = _int(latest["target_calories"])
        if setting is None:
            db.add(AppSetting(key="target_kcal", value=value))
        else:
            setting.value = value
        report["settings"]["target_kcal"] = f"写入 {value}（源 {table.log_date(latest)}）"
        sp.commit()
    except Exception as e:  # noqa: BLE001
        sp.rollback()
        report["settings"]["target_kcal"] = f"处理失败：{e}"


def _migrate_drinks(db: Session, table: SourceTable, rep: TableReport, status: dict, report: dict) -> None:
    """daily_drinks：water_ml>0 的日期 → 按名称匹配 active「饮水/喝水」习惯写 habit_logs
    （ON CONFLICT DO NOTHING）；无匹配习惯 → 全部跳过并列报告，建习惯后可重跑补上。"""
    habit = db.execute(
        select(Habit)
        .where(Habit.active, or_(Habit.name.contains("饮水"), Habit.name.contains("喝水")))
        .order_by(Habit.sort, Habit.id)
    ).scalars().first()
    report["water_habit"] = habit.name if habit else None

    def handler(row: dict, d: date):
        ml = row.get("water_ml")
        if ml is None or float(ml) <= 0:
            return "skipped", "water_ml 为空或 0"
        if habit is None:
            report["unmatched_drinks"].append(str(d))
            return "skipped", "无 active 饮水类习惯可挂"
        db.execute(
            pg_insert(HabitLog)
            .values(habit_id=habit.id, log_date=d, done_count=1)
            .on_conflict_do_nothing(index_elements=["habit_id", "log_date"])
        )
        return "migrated", None

    _each_row(db, table, rep, status, handler)


def _migrate_measurements(db: Session, table: SourceTable, rep: TableReport, status: dict, report: dict) -> None:
    """body_measurements → body_metrics 围度列（waist/chest/arm/thigh/hip 实列直映，
    手动优先；neck 无对应列 → 并入当日 notes）。"""

    def handler(row: dict, d: date):
        bm = get_or_create_day(db, d)
        written, same = _apply_manual(
            bm,
            {
                "waist_cm": _dec(row.get("waist_cm")),
                "chest_cm": _dec(row.get("chest_cm")),
                "arm_cm": _dec(row.get("arm_cm")),
                "thigh_cm": _dec(row.get("thigh_cm")),
                "hip_cm": _dec(row.get("hip_cm")),
            },
            report["manual_blocked"], d,
        )
        neck = _dec(row.get("neck_cm"))
        note_added = _append_note(bm, f"颈围 {neck}cm（迁自 personal_data）") if neck else False
        if written or same or note_added:
            return "migrated", None
        return "skipped", "无可写字段"

    _each_row(db, table, rep, status, handler)


def _migrate_personal_info(db: Session, table: SourceTable, rep: TableReport, status: dict, report: dict) -> None:
    """personal_info → app_settings sex/birth_date（仅当现值为空）；height_cm 已有则跳过
    （三星导入已回填）。"""

    def write_if_empty(key: str, value: Any) -> str:
        if value in (None, ""):
            return "源为空"
        setting = db.get(AppSetting, key)
        if setting is not None and setting.value is not None:
            return f"现值 {setting.value} 保留"
        if setting is None:
            db.add(AppSetting(key=key, value=value))
        else:
            setting.value = value
        return f"写入 {value}"

    def handler(row: dict, d: date | None):
        sex_raw = str(row.get("gender") or "").strip()
        sex = _SEX_MAP.get(sex_raw.lower())
        if sex_raw and sex is None:
            report["settings"]["sex"] = f"gender={sex_raw!r} 无法映射 male/female，未写"
        else:
            report["settings"]["sex"] = write_if_empty("sex", sex)
        birth = row.get("birth_date")
        report["settings"]["birth_date"] = write_if_empty(
            "birth_date", birth.isoformat() if isinstance(birth, (date, datetime)) else (birth or None)
        )
        height = _dec(row.get("height_cm"))
        report["settings"]["height_cm"] = write_if_empty(
            "height_cm", float(height) if height is not None else None
        )
        return "migrated", None

    _each_row(db, table, rep, status, handler)


_HANDLERS = {
    "diet_records": _migrate_diet,
    "weight_records": _migrate_weight,
    "activity_records": _migrate_activity,
    "daily_summary": _migrate_summary,
    "daily_drinks": _migrate_drinks,
    "body_measurements": _migrate_measurements,
    "personal_info": _migrate_personal_info,
}


# ---------- 目标库自检 ----------

def _check_target_ready(db: Session) -> None:
    """迁移 12 未跑的库拒绝迁入（NAS 上先 alembic upgrade head）。"""
    has_mood = db.execute(
        text(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'health' "
            "AND table_name = 'body_metrics' AND column_name = 'mood_score'"
        )
    ).first()
    legacy_ok = db.execute(
        text(
            "SELECT 1 FROM pg_constraint WHERE conname = 'ck_import_source' "
            "AND pg_get_constraintdef(oid) LIKE '%legacy%'"
        )
    ).first()
    if not (has_mood and legacy_ok):
        raise SystemExit(
            "目标库缺迁移 12（body_metrics.mood_score / import_raw 'legacy' 词表）——"
            "先在目标环境执行 `uv run alembic upgrade head` 再迁移"
        )


# ---------- 主流程 ----------

def run_migration(
    source_dsn: str,
    target_dsn: str,
    source_schema: str = "public",
    dry_run: bool = False,
) -> dict[str, Any]:
    """整库迁移一轮，返回报告 dict（CLI 打印用 / 测试断言用）。

    单事务：正常收尾一次 commit（含 failed 标记），--dry-run 收尾整体 rollback。
    """
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "tables": {},
        "meal_fallback": {},
        "manual_blocked": [],
        "unmatched_drinks": [],
        "water_habit": None,
        "settings": {},
    }
    src_engine = create_engine(
        _normalize_dsn(source_dsn),
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    tgt_engine = create_engine(_normalize_dsn(target_dsn))
    TargetSession = sessionmaker(bind=tgt_engine, expire_on_commit=False)

    with src_engine.connect() as src, TargetSession() as db:
        _check_target_ready(db)
        for name in MIGRATED_TABLES:
            table = SourceTable(src, source_schema, name, need_date=(name != "personal_info"))
            rep = TableReport()
            rep.source_rows = len(table.rows)
            status = _archive(db, table, rep)
            _HANDLERS[name](db, table, rep, status, report)
            report["tables"][name] = rep.as_dict()
        if not dry_run:
            db.execute(
                pg_insert(SyncState)
                .values(source=SOURCE, last_success_at=now_local())
                .on_conflict_do_update(
                    index_elements=["source"], set_={"last_success_at": now_local()}
                )
            )
            db.commit()
        else:
            db.rollback()
    src_engine.dispose()
    tgt_engine.dispose()
    return report


def print_report(report: dict[str, Any]) -> None:
    mode = "【DRY-RUN 试跑，未落库】" if report["dry_run"] else "【正式迁移】"
    print(f"\n==== personal_data 迁移报告 {mode} ====")
    header = f"{'表':<20}{'源行数':>6}{'留档新增':>8}{'迁入':>6}{'跳过':>6}{'失败':>6}{'前次已迁':>8}"
    print(header)
    for name, t in report["tables"].items():
        skipped = sum(t["skipped"].values())
        print(
            f"{name:<20}{t['source_rows']:>8}{t['archived_new']:>10}"
            f"{t['migrated']:>8}{skipped:>8}{t['failed']:>8}{t['already']:>10}"
        )
        for reason, n in t["skipped"].items():
            print(f"    └ 跳过 {n}：{reason}")
    if report["meal_fallback"]:
        print("meal_type 未识别（已兜底加餐）：", dict(report["meal_fallback"]))
    if report["manual_blocked"]:
        print("被现有手动值挡下的字段：")
        for item in report["manual_blocked"]:
            print(f"    - {item}")
    print(f"饮水习惯匹配：{report['water_habit'] or '无（daily_drinks 全部跳过，建习惯后重跑可补）'}")
    if report["unmatched_drinks"]:
        print(f"    无匹配习惯的饮水日期（{len(report['unmatched_drinks'])} 天）："
              f"{', '.join(report['unmatched_drinks'])}")
    for key, outcome in report["settings"].items():
        print(f"app_settings.{key}：{outcome}")
    failed = sum(t["failed"] for t in report["tables"].values())
    if failed:
        print(f"\n⚠ {failed} 行失败（import_raw parse_error 有详情），修复后可直接重跑补上")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--source-dsn",
        default=os.environ.get("LEGACY_SOURCE_DSN"),
        help="personal_data 连接串（或环境变量 LEGACY_SOURCE_DSN）",
    )
    parser.add_argument("--source-schema", default="public")
    parser.add_argument(
        "--target-dsn",
        default=None,
        help="目标库连接串，默认 .env 的 DATABASE_URL（应用同款）",
    )
    parser.add_argument("--dry-run", action="store_true", help="完整试跑出报告，最后整体回滚")
    args = parser.parse_args()
    if not args.source_dsn:
        parser.error("缺 --source-dsn（或环境变量 LEGACY_SOURCE_DSN）")
    target_dsn = args.target_dsn
    if target_dsn is None:
        from app.config import get_settings

        target_dsn = get_settings().database_url
    report = run_migration(
        args.source_dsn, target_dsn, source_schema=args.source_schema, dry_run=args.dry_run
    )
    print_report(report)
    return 1 if any(t["failed"] for t in report["tables"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
