"""personal_data 迁移脚本自测（V3 P3，scripts/migrate_personal_data.py）。

同一台临时 PG（Mac 55433）上建 legacy_sim schema 模拟 personal_data 源，
目标即本地 health schema。覆盖：meal_type 映射与兜底 / diet 查重 / weight
覆盖 autofill 但不覆盖手动值 / workout legacy-{id} 幂等 / mood 越界隔离 /
饮水无匹配习惯列报告、建习惯后重跑可补 / 围度 neck 并入 notes /
personal_info 仅当现值为空 / 重跑两遍全幂等 / dry-run 整体回滚。

数据库不可达自动跳过；测试数据用固定历史日期（2020-02-x，远离真实数据）
与高位 id（99xxxx），fixture 里精确自清理。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session

from scripts.migrate_personal_data import run_migration

SIM_SCHEMA = "legacy_sim_test"
D = {n: date(2020, 2, n) for n in range(1, 6)}
TEST_DATES = list(D.values())
# 高位 id 防与真实 legacy 留档撞 external_id（本地 dev 库理论上没有，仍求精确）
EXT_IDS = [
    "legacy-diet_records-990001", "legacy-diet_records-990002",
    "legacy-diet_records-990003", "legacy-diet_records-990004",
    "legacy-diet_records-990005",
    "legacy-weight_records-990011", "legacy-weight_records-990012",
    "legacy-weight_records-990013",
    "legacy-activity_records-990021", "legacy-activity_records-990022",
    "legacy-daily_summary-990031", "legacy-daily_summary-990032",
    "legacy-daily_summary-990033",
    "legacy-daily_drinks-990041", "legacy-daily_drinks-990042",
    "legacy-daily_drinks-990043",
    "legacy-body_measurements-990051",
    "legacy-personal_info-990061",
]
WORKOUT_EXT_IDS = ["legacy-990021", "legacy-990022"]
SETTING_KEYS = ["sex", "birth_date", "height_cm", "target_kcal"]

_DDL = f"""
CREATE SCHEMA {SIM_SCHEMA};
CREATE TABLE {SIM_SCHEMA}.diet_records (
    id int PRIMARY KEY, record_date date NOT NULL, meal_type text, food_name text,
    calories numeric, protein_g numeric, carbs_g numeric, fat_g numeric,
    portion_desc text, notes text);
CREATE TABLE {SIM_SCHEMA}.weight_records (
    id int PRIMARY KEY, record_date date NOT NULL, weight_kg numeric NOT NULL,
    body_fat_pct numeric, bmi numeric, notes text);
CREATE TABLE {SIM_SCHEMA}.activity_records (
    id int PRIMARY KEY, record_date date NOT NULL, activity_type text,
    duration_minutes numeric, calories_burned numeric, intensity text,
    distance_km numeric, steps int, avg_hr int, max_hr int, pace_kmh numeric, notes text);
CREATE TABLE {SIM_SCHEMA}.daily_summary (
    id int PRIMARY KEY, summary_date date NOT NULL, total_calories numeric,
    total_protein_g numeric, target_calories numeric, mood_score int, notes text);
CREATE TABLE {SIM_SCHEMA}.daily_drinks (
    id int PRIMARY KEY, drink_date date NOT NULL, water_ml numeric, notes text);
CREATE TABLE {SIM_SCHEMA}.body_measurements (
    id int PRIMARY KEY, measure_date date NOT NULL, waist_cm numeric, hip_cm numeric,
    chest_cm numeric, arm_cm numeric, thigh_cm numeric, neck_cm numeric);
CREATE TABLE {SIM_SCHEMA}.personal_info (
    id int PRIMARY KEY, gender text, birth_date date, height_cm numeric);

INSERT INTO {SIM_SCHEMA}.diet_records VALUES
 (990001,'2020-02-01','breakfast','燕麦粥',320,12,55,6,'一碗',NULL),
 (990002,'2020-02-01','lunch','牛肉面',550,25,70,15,NULL,NULL),
 (990003,'2020-02-01','夜宵','泡面',400,8,52,16,NULL,NULL),
 (990004,'2020-02-01','brunch','三明治',380,15,40,14,NULL,NULL),
 (990005,'2020-02-02','dinner','牛肉面',550,25,70,15,NULL,NULL);
INSERT INTO {SIM_SCHEMA}.weight_records VALUES
 (990011,'2020-02-02',82.5,24.0,26.1,'早晨空腹'),
 (990012,'2020-02-03',82.0,NULL,NULL,NULL),
 (990013,'2020-02-04',81.0,NULL,NULL,NULL);
INSERT INTO {SIM_SCHEMA}.activity_records VALUES
 (990021,'2020-02-01','跑步',30,280,'medium',5.2,6100,150,172,10.4,'晨跑'),
 (990022,'2020-02-03','徒步',60,NULL,NULL,NULL,8000,NULL,NULL,NULL,NULL);
INSERT INTO {SIM_SCHEMA}.daily_summary VALUES
 (990031,'2020-02-01',1800,90,2200,8,NULL),
 (990032,'2020-02-02',1900,NULL,NULL,NULL,NULL),
 (990033,'2020-02-03',NULL,NULL,2100,15,NULL);
INSERT INTO {SIM_SCHEMA}.daily_drinks VALUES
 (990041,'2020-02-01',1800,NULL),
 (990042,'2020-02-02',0,NULL),
 (990043,'2020-02-03',1500,NULL);
INSERT INTO {SIM_SCHEMA}.body_measurements VALUES
 (990051,'2020-02-05',88.5,98.0,100.0,32.0,55.0,38.0);
INSERT INTO {SIM_SCHEMA}.personal_info VALUES
 (990061,'male','1990-05-20',175);
"""


def _dsn() -> str:
    from app.config import get_settings

    return get_settings().database_url


def _db_ready(dsn: str) -> bool:
    try:
        eng = create_engine(dsn)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


@pytest.fixture()
def env():
    """建模拟源 schema + 目标预置行 + 设置快照；结束精确还原/清理。"""
    dsn = _dsn()
    if not _db_ready(dsn):
        pytest.skip("临时 PG 不可达（uv run docker/pg 未启动）")
    from app.models import AppSetting, BodyMetrics, DietLog, Habit, HabitLog, ImportRaw, SyncState, WorkoutLog

    eng = create_engine(dsn)
    db = Session(eng)

    # 脏库防御：测试日期段已有数据说明不是干净 dev 库，跳过而非误删
    dirty = db.execute(
        select(BodyMetrics.id).where(BodyMetrics.log_date.in_(TEST_DATES))
    ).first() or db.execute(
        select(DietLog.id).where(DietLog.log_date.in_(TEST_DATES))
    ).first() or db.execute(
        select(ImportRaw.id).where(ImportRaw.source == "legacy", ImportRaw.external_id.in_(EXT_IDS))
    ).first()
    if dirty:
        db.close()
        eng.dispose()
        pytest.skip("测试日期段/external_id 已有数据，先清理再跑")

    db.execute(text(f"DROP SCHEMA IF EXISTS {SIM_SCHEMA} CASCADE"))
    for stmt in _DDL.split(";"):
        if stmt.strip():
            db.execute(text(stmt))

    # app_settings 快照 → 强制成可预期状态：sex/birth_date/target_kcal 置空，height 已有值
    snapshot = {
        k: db.execute(
            text("SELECT value::text FROM health.app_settings WHERE key=:k"), {"k": k}
        ).scalar_one_or_none()
        for k in SETTING_KEYS
    }
    for k, forced in [("sex", "null"), ("birth_date", "null"), ("target_kcal", "null"), ("height_cm", "178")]:
        db.execute(
            text(
                "INSERT INTO health.app_settings(key, value) VALUES (:k, CAST(:v AS jsonb)) "
                "ON CONFLICT (key) DO UPDATE SET value = CAST(:v AS jsonb)"
            ),
            {"k": k, "v": forced},
        )

    # 现有 active 饮水/喝水习惯先停用（保证「无匹配」场景），结束恢复
    water_ids = list(
        db.execute(
            select(Habit.id).where(
                Habit.active, (Habit.name.contains("饮水")) | (Habit.name.contains("喝水"))
            )
        ).scalars()
    )
    if water_ids:
        db.execute(text("UPDATE health.habits SET active=false WHERE id = ANY(:ids)"), {"ids": water_ids})

    # 目标预置：02-02 体重 autofill（迁移应覆盖）；02-04 手动体重（迁移不得覆盖）；
    # 02-02 一条与 990005 同文本同 kcal 的 diet（迁移应查重跳过）
    db.add(BodyMetrics(log_date=D[2], weight_kg=Decimal("83.00"), autofilled={"weight_kg": "samsung_zip"}))
    db.add(BodyMetrics(log_date=D[4], weight_kg=Decimal("80.50"), autofilled={}))
    db.add(DietLog(log_date=D[2], meal="晚餐", free_text="牛肉面", kcal=Decimal("550.0")))
    sync_preexisting = db.execute(
        select(SyncState.source).where(SyncState.source == "legacy")
    ).scalar_one_or_none()
    db.commit()

    yield {"dsn": dsn, "db": db}

    db.rollback()
    test_habits = list(
        db.execute(select(Habit.id).where(Habit.name.like("测试饮水-%"))).scalars()
    )
    if test_habits:
        db.execute(delete(HabitLog).where(HabitLog.habit_id.in_(test_habits)))
        db.execute(delete(Habit).where(Habit.id.in_(test_habits)))
    db.execute(delete(ImportRaw).where(ImportRaw.source == "legacy", ImportRaw.external_id.in_(EXT_IDS)))
    db.execute(
        delete(WorkoutLog).where(WorkoutLog.source == "manual", WorkoutLog.external_id.in_(WORKOUT_EXT_IDS))
    )
    db.execute(delete(DietLog).where(DietLog.log_date.in_(TEST_DATES)))
    db.execute(delete(BodyMetrics).where(BodyMetrics.log_date.in_(TEST_DATES)))
    for k, old in snapshot.items():
        if old is None:
            db.execute(text("DELETE FROM health.app_settings WHERE key=:k"), {"k": k})
        else:
            db.execute(
                text("UPDATE health.app_settings SET value = CAST(:v AS jsonb) WHERE key=:k"),
                {"k": k, "v": old},
            )
    if water_ids:
        db.execute(text("UPDATE health.habits SET active=true WHERE id = ANY(:ids)"), {"ids": water_ids})
    if sync_preexisting is None:
        db.execute(delete(SyncState).where(SyncState.source == "legacy"))
    db.execute(text(f"DROP SCHEMA IF EXISTS {SIM_SCHEMA} CASCADE"))
    db.commit()
    db.close()
    eng.dispose()


def _run(env) -> dict:
    return run_migration(env["dsn"], env["dsn"], source_schema=SIM_SCHEMA)


def test_full_migration_idempotent_and_report(env):
    from app.models import AppSetting, BodyMetrics, DietLog, Habit, HabitLog, ImportRaw, SyncState, WorkoutLog

    db: Session = env["db"]
    r1 = _run(env)

    # ---- diet：4 迁入（含 brunch 兜底加餐），1 查重跳过 ----
    t = r1["tables"]["diet_records"]
    assert (t["source_rows"], t["archived_new"], t["migrated"], t["failed"]) == (5, 5, 4, 0)
    assert t["skipped"] == {"同日+同文本+同 kcal 已存在": 1}
    assert r1["meal_fallback"] == {"brunch": 1}
    db.expire_all()
    meals = dict(
        db.execute(
            select(DietLog.free_text, DietLog.meal).where(DietLog.log_date == D[1])
        ).all()
    )
    assert meals == {"燕麦粥（一碗）": "早餐", "牛肉面": "午餐", "泡面": "加餐", "三明治": "加餐"}
    assert db.execute(select(DietLog).where(DietLog.log_date == D[2])).scalars().one()  # 仍只有预置那条

    # ---- weight：覆盖 autofill、放过手动值、新日建行 ----
    t = r1["tables"]["weight_records"]
    assert (t["migrated"], sum(t["skipped"].values())) == (2, 1)
    bm2 = db.execute(select(BodyMetrics).where(BodyMetrics.log_date == D[2])).scalar_one()
    assert float(bm2.weight_kg) == 82.5 and float(bm2.body_fat_pct) == 24.0
    assert "weight_kg" not in (bm2.autofilled or {})  # autofill 标记已解除=转手动
    assert "早晨空腹" in (bm2.notes or "")
    bm4 = db.execute(select(BodyMetrics).where(BodyMetrics.log_date == D[4])).scalar_one()
    assert float(bm4.weight_kg) == 80.5  # 手动值不被旧库覆盖
    assert any("2020-02-04 weight_kg" in s for s in r1["manual_blocked"])
    bm3 = db.execute(select(BodyMetrics).where(BodyMetrics.log_date == D[3])).scalar_one()
    assert float(bm3.weight_kg) == 82.0

    # ---- activity：legacy-{id} 入库，steps/intensity/pace 进 detail ----
    t = r1["tables"]["activity_records"]
    assert (t["migrated"], t["failed"]) == (2, 0)
    w = db.execute(select(WorkoutLog).where(WorkoutLog.external_id == "legacy-990021")).scalar_one()
    assert w.source == "manual" and w.session_type == "跑步" and w.duration_min == 30
    assert w.detail == {"steps": 6100, "intensity": "medium", "pace_kmh": 10.4}
    assert w.calories == 280 and float(w.distance_km) == 5.2 and w.notes == "晨跑"

    # ---- summary：mood 落列、越界隔离为 failed、target_kcal 取最新非空 ----
    t = r1["tables"]["daily_summary"]
    assert (t["migrated"], t["failed"]) == (1, 1)
    assert t["skipped"] == {"无 mood_score": 1}
    bm1 = db.execute(select(BodyMetrics).where(BodyMetrics.log_date == D[1])).scalar_one()
    assert bm1.mood_score == 8
    err = db.execute(
        select(ImportRaw.parse_status, ImportRaw.parse_error).where(
            ImportRaw.external_id == "legacy-daily_summary-990033"
        )
    ).one()
    assert err[0] == "failed" and "越界" in err[1]
    assert r1["settings"]["target_kcal"] == "写入 2100（源 2020-02-03）"

    # ---- drinks：无 active 饮水习惯 → 全跳过并列报告 ----
    t = r1["tables"]["daily_drinks"]
    assert t["migrated"] == 0 and sum(t["skipped"].values()) == 3
    assert r1["water_habit"] is None
    assert r1["unmatched_drinks"] == ["2020-02-01", "2020-02-03"]

    # ---- measurements：围度直映 + neck 并入 notes ----
    bm5 = db.execute(select(BodyMetrics).where(BodyMetrics.log_date == D[5])).scalar_one()
    assert [float(x) for x in (bm5.waist_cm, bm5.chest_cm, bm5.arm_cm, bm5.thigh_cm, bm5.hip_cm)] == [
        88.5, 100.0, 32.0, 55.0, 98.0,
    ]
    assert "颈围 38" in bm5.notes

    # ---- personal_info：空则写、有则跳 ----
    assert r1["settings"]["sex"] == "写入 male"
    assert r1["settings"]["birth_date"] == "写入 1990-05-20"
    assert "178 保留" in r1["settings"]["height_cm"]
    assert db.execute(select(AppSetting.value).where(AppSetting.key == "sex")).scalar_one() == "male"

    assert db.execute(select(SyncState.source).where(SyncState.source == "legacy")).scalar_one_or_none()

    # ---- 第二遍：parsed 门控全跳，库内行数不变，failed 重试后仍 failed ----
    r2 = _run(env)
    db.expire_all()
    for name, t2 in r2["tables"].items():
        assert t2["archived_new"] == 0, name
        assert t2["already"] == r1["tables"][name]["migrated"], name
        assert t2["migrated"] == 0, name
    assert r2["tables"]["daily_summary"]["failed"] == 1  # 重试仍越界
    assert len(db.execute(select(DietLog.id).where(DietLog.log_date.in_(TEST_DATES))).all()) == 5
    assert len(db.execute(select(WorkoutLog.id).where(WorkoutLog.external_id.in_(WORKOUT_EXT_IDS))).all()) == 2
    bm2 = db.execute(select(BodyMetrics).where(BodyMetrics.log_date == D[2])).scalar_one()
    assert float(bm2.weight_kg) == 82.5

    # ---- 建「饮水」习惯后第三遍：skipped 的饮水被补上（skipped 可重试语义） ----
    habit = Habit(name="测试饮水-migrate", period="daily", target_per_period=1, active=True)
    db.add(habit)
    db.commit()
    r3 = _run(env)
    db.expire_all()
    assert r3["water_habit"] == "测试饮水-migrate"
    assert r3["tables"]["daily_drinks"]["migrated"] == 2
    logged = sorted(
        db.execute(select(HabitLog.log_date).where(HabitLog.habit_id == habit.id)).scalars()
    )
    assert logged == [D[1], D[3]]  # water_ml=0 的 02-02 不打卡

    # 第四遍确认 habit_logs ON CONFLICT 幂等
    r4 = _run(env)
    assert r4["tables"]["daily_drinks"]["migrated"] == 0
    assert r4["tables"]["daily_drinks"]["already"] == 2


def test_dry_run_rolls_back_everything(env):
    from app.models import DietLog, ImportRaw

    db: Session = env["db"]
    report = run_migration(env["dsn"], env["dsn"], source_schema=SIM_SCHEMA, dry_run=True)
    assert report["dry_run"] is True
    assert report["tables"]["diet_records"]["migrated"] == 4  # 报告照常产出
    db.expire_all()
    assert db.execute(select(ImportRaw.id).where(ImportRaw.external_id.in_(EXT_IDS))).first() is None
    assert (
        db.execute(
            select(DietLog.id).where(DietLog.log_date == D[1])
        ).first()
        is None
    )
