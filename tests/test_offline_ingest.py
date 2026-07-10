"""离线补发通道（docs/offline-plan.md 阶段一）：payload 校验 / 幂等重放 / 单条失败隔离。

前半部分纯函数直测校验口径；后半部分带 DB 集成测试（Mac 临时 PG 55433），
数据库不可达或 INGEST_TOKEN 未配置时自动跳过，测试数据用固定历史日期
（2020-01-15，远离真实数据）并在 fixture 里自清理。
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.routers.offline import (
    parse_diet_payload,
    parse_metric_payload,
    parse_record_date,
    parse_workout_payload,
)

TODAY = date(2026, 7, 11)


# ---------- 纯函数：payload 校验口径 ----------

def test_record_date_valid_and_skew():
    assert parse_record_date("2026-07-10", TODAY) == date(2026, 7, 10)
    # 容 1 天时钟偏差
    assert parse_record_date("2026-07-12", TODAY) == date(2026, 7, 12)


@pytest.mark.parametrize("bad", [
    "2026-07-13",   # 未来（超 1 天偏差）
    "2024-01-01",   # 一年以前 = 坏时钟（RTC 重置）
    "not-a-date", "", None, "2026-13-40",
])
def test_record_date_rejects_future_ancient_and_garbage(bad):
    with pytest.raises(ValueError):
        parse_record_date(bad, TODAY)


def test_diet_payload_normal():
    got = parse_diet_payload({"meal": "午餐", "free_text": "牛肉面", "kcal": 550, "protein_g": 25})
    assert got["meal"] == "午餐"
    assert got["free_text"] == "牛肉面"
    assert got["kcal"] == Decimal("550.0")
    assert got["fat_g"] is None


@pytest.mark.parametrize("payload", [
    {"meal": "夜宵", "free_text": "泡面"},          # 餐次不在词表
    {"meal": "午餐"},                                # 缺 free_text
    {"meal": "午餐", "free_text": "面", "kcal": "nan"},   # 非法数值
    {"meal": "午餐", "free_text": "面", "kcal": 99999},   # 越界
])
def test_diet_payload_rejects(payload):
    with pytest.raises(ValueError):
        parse_diet_payload(payload)


def test_workout_payload_normal():
    got = parse_workout_payload(
        {"session_type": "跑步", "duration_min": 30, "distance_km": 5.2, "rpe": 6}
    )
    assert got["session_type"] == "跑步"
    assert got["duration_min"] == 30
    assert got["distance_km"] == Decimal("5.20")
    assert got["rpe"] == 6
    assert got["notes"] is None


@pytest.mark.parametrize("payload", [
    {"duration_min": 30},                              # 缺 session_type
    {"session_type": "跑步", "rpe": 11},               # RPE 越界
    {"session_type": "跑步", "duration_min": 1441},    # 时长越界（0 合法，与网页表单一致）
])
def test_workout_payload_rejects(payload):
    with pytest.raises(ValueError):
        parse_workout_payload(payload)


def test_workout_distance_keeps_two_decimals():
    # 网页表单不丢百分位：离线通道同口径（曾双重量化 5.25 → 5.20）
    got = parse_workout_payload({"session_type": "跑步", "distance_km": "5.25"})
    assert str(got["distance_km"]) == "5.25"


def test_metric_payload_whitelist():
    got = parse_metric_payload({"weight_kg": 71.5, "sleep_hours": 7.5, "resting_hr": 55})
    assert got["weight_kg"] == Decimal("71.5")
    assert got["resting_hr"] == 55
    with pytest.raises(ValueError):
        parse_metric_payload({"notes": "白名单外字段"})
    with pytest.raises(ValueError):
        parse_metric_payload({"weight_kg": ""})  # 全空 = 没有可写字段
    with pytest.raises(ValueError):
        parse_metric_payload({"weight_kg": 9999})  # 越界


# ---------- DB 集成：幂等重放 / 单条失败隔离（不可达自动跳过） ----------

def _test_date() -> date:
    """在服务端一年下界内、又早于开发库 90 天假数据区间的日期。"""
    from app.timeutil import today_local
    return today_local() - timedelta(days=300)


TEST_DATE = _test_date()

# 本轮测试发出的 external_id：teardown 只删自己的留档，不碰真实离线数据
_SENT_EXT_IDS: list[str] = []


def _db_ready() -> bool:
    try:
        from sqlalchemy import text

        from app.db import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def client():
    if not _db_ready():
        pytest.skip("临时 PG 不可达（uv run docker/pg 未启动）")
    from app.config import get_settings
    if not get_settings().ingest_token:
        pytest.skip("INGEST_TOKEN 未配置")
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {get_settings().ingest_token}"
        yield c


@pytest.fixture()
def db():
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def offline_env(db):
    """建一个测试习惯；结束后只清本轮产生的痕迹（留档按 external_id 精确删，
    绝不整删 source='offline'——那会连真机补发的留档一起毁掉）。"""
    from sqlalchemy import delete, select

    from app.models import (
        BodyMetrics, DietLog, Habit, HabitLog, ImportRaw, WorkoutLog,
    )

    habit = Habit(name=f"测试-离线-{uuid.uuid4().hex[:8]}", period="daily", target_per_period=1)
    db.add(habit)
    db.commit()
    bm_preexisting = db.execute(
        select(BodyMetrics.id).where(BodyMetrics.log_date == TEST_DATE)
    ).scalar_one_or_none()
    _SENT_EXT_IDS.clear()
    yield habit
    db.rollback()
    db.execute(delete(HabitLog).where(HabitLog.habit_id == habit.id))
    db.execute(delete(Habit).where(Habit.id == habit.id))
    if _SENT_EXT_IDS:
        db.execute(delete(ImportRaw).where(
            ImportRaw.source == "offline", ImportRaw.external_id.in_(list(_SENT_EXT_IDS))
        ))
        _SENT_EXT_IDS.clear()
    db.execute(delete(DietLog).where(DietLog.log_date == TEST_DATE))
    db.execute(delete(WorkoutLog).where(WorkoutLog.log_date == TEST_DATE))
    if bm_preexisting is None:
        db.execute(delete(BodyMetrics).where(BodyMetrics.log_date == TEST_DATE))
    # sync_state('offline') 是运行态（last_success_at 等），留着无害，不删
    db.commit()


def _rec(rtype: str, payload: dict, d: date = TEST_DATE) -> dict:
    client_id = str(uuid.uuid4())
    _SENT_EXT_IDS.append(f"{rtype}-{client_id}")
    return {
        "type": rtype,
        "client_id": client_id,
        "date": d.isoformat(),
        "payload": payload,
    }


def _raw_status(db, ext_id: str) -> tuple[str, str | None]:
    from sqlalchemy import select

    from app.models import ImportRaw
    row = db.execute(
        select(ImportRaw.parse_status, ImportRaw.parse_error).where(
            ImportRaw.source == "offline", ImportRaw.external_id == ext_id
        )
    ).one()
    return row[0], row[1]


def test_replay_idempotent(client, db, offline_env):
    """同一批四类记录补发两次：第二次全 skipped，落库不双写。"""
    from sqlalchemy import select

    from app.models import BodyMetrics, DietLog, HabitLog, WorkoutLog

    habit = offline_env
    records = [
        _rec("habit", {"habit_id": habit.id, "done_count": 1}),
        _rec("diet", {"meal": "午餐", "free_text": "牛肉面", "kcal": 550, "protein_g": 25}),
        _rec("workout", {"session_type": "跑步", "duration_min": 30, "distance_km": 5.2, "rpe": 6}),
        _rec("metric", {"weight_kg": 71.5, "sleep_hours": 7.5}),
    ]
    r1 = client.post("/api/ingest/offline", json={"records": records})
    assert r1.status_code == 200
    assert r1.json() == {"received": 4, "new": 4, "skipped": 0}

    r2 = client.post("/api/ingest/offline", json={"records": records})
    assert r2.json() == {"received": 4, "new": 0, "skipped": 4}

    assert db.execute(
        select(HabitLog).where(HabitLog.habit_id == habit.id, HabitLog.log_date == TEST_DATE)
    ).scalar_one().done_count == 1
    diets = db.execute(select(DietLog).where(DietLog.log_date == TEST_DATE)).scalars().all()
    assert len(diets) == 1 and diets[0].free_text == "牛肉面"
    workouts = db.execute(
        select(WorkoutLog).where(WorkoutLog.log_date == TEST_DATE)
    ).scalars().all()
    assert len(workouts) == 1
    assert workouts[0].source == "manual"
    assert (workouts[0].external_id or "").startswith("offline-")
    bm = db.execute(
        select(BodyMetrics).where(BodyMetrics.log_date == TEST_DATE)
    ).scalar_one()
    assert float(bm.weight_kg) == 71.5
    # 离线 metric = 手动录入：不留自动回填登记，此后同步不可覆盖
    assert "weight_kg" not in (bm.autofilled or {})


def test_habit_first_write_wins(client, db, offline_env):
    """habit 声明式语义：当日已有记录时 ON CONFLICT DO NOTHING（先到先得）。"""
    from sqlalchemy import select

    from app.models import HabitLog

    habit = offline_env
    client.post("/api/ingest/offline", json={"records": [
        _rec("habit", {"habit_id": habit.id, "done_count": 1}),
    ]})
    client.post("/api/ingest/offline", json={"records": [
        _rec("habit", {"habit_id": habit.id, "done_count": 5}),  # 不同 client_id、同日
    ]})
    row = db.execute(
        select(HabitLog).where(HabitLog.habit_id == habit.id, HabitLog.log_date == TEST_DATE)
    ).scalar_one()
    assert row.done_count == 1


def test_metric_last_write_wins(client, db, offline_env):
    """离线 metric 视同手动保存：队列 FIFO，同日后写胜出（晚上复称覆盖早晨）。"""
    from sqlalchemy import select

    from app.models import BodyMetrics

    client.post("/api/ingest/offline", json={"records": [
        _rec("metric", {"weight_kg": 71.5}),
    ]})
    r = client.post("/api/ingest/offline", json={"records": [
        _rec("metric", {"weight_kg": 72.3, "sleep_hours": 6.0}),
    ]})
    assert r.json()["new"] == 1
    bm = db.execute(
        select(BodyMetrics).where(BodyMetrics.log_date == TEST_DATE)
    ).scalar_one()
    assert float(bm.weight_kg) == 72.3         # 后写胜出（与秤/手表同日取最后一次同口径）
    assert float(bm.sleep_hours) == 6.0
    assert "weight_kg" not in (bm.autofilled or {})  # 手动语义：同步不可覆盖


def test_pending_raw_renormalized_on_repost(client, db, offline_env):
    """parse_status 门控自愈：批级失败留下的 pending/failed 行，重发时重新归一化。"""
    from sqlalchemy import select

    from app.models import DietLog, ImportRaw
    from app.timeutil import now_local

    rec = _rec("diet", {"meal": "午餐", "free_text": "自愈测试面"})
    # 模拟上一轮批级失败后的状态：raw 已留档但归一化没成（pending），无 DietLog 行
    db.execute(
        ImportRaw.__table__.insert().values(
            source="offline", record_type="diet", external_id=f"diet-{rec['client_id']}",
            raw=rec, parse_status="pending", parse_version=0, last_seen_at=now_local(),
        )
    )
    db.commit()

    r = client.post("/api/ingest/offline", json={"records": [rec]})
    assert r.json() == {"received": 1, "new": 1, "skipped": 0}  # 不因已留档而跳过
    row = db.execute(
        select(DietLog).where(DietLog.log_date == TEST_DATE, DietLog.free_text == "自愈测试面")
    ).scalar_one()
    assert row is not None
    status, _ = _raw_status(db, f"diet-{rec['client_id']}")
    assert status == "parsed"
    # 已 parsed 的绝不重放（防 diet 双写）
    r2 = client.post("/api/ingest/offline", json={"records": [rec]})
    assert r2.json() == {"received": 1, "new": 0, "skipped": 1}
    rows = db.execute(
        select(DietLog).where(DietLog.log_date == TEST_DATE, DietLog.free_text == "自愈测试面")
    ).scalars().all()
    assert len(rows) == 1


def test_single_failure_isolation(client, db, offline_env):
    """单条失败不毒化整批：坏 habit 标 failed，同批 diet 照常落库。"""
    from sqlalchemy import select

    from app.models import DietLog

    bad = _rec("habit", {"habit_id": 99999999})
    good = _rec("diet", {"meal": "晚餐", "free_text": "鸡胸沙拉"})
    r = client.post("/api/ingest/offline", json={"records": [bad, good]})
    assert r.status_code == 200
    assert r.json() == {"received": 2, "new": 1, "skipped": 0}

    status, error = _raw_status(db, f"habit-{bad['client_id']}")
    assert status == "failed" and "习惯不存在" in (error or "")
    status, _ = _raw_status(db, f"diet-{good['client_id']}")
    assert status == "parsed"
    assert db.execute(
        select(DietLog).where(DietLog.log_date == TEST_DATE)
    ).scalar_one().free_text == "鸡胸沙拉"


def test_bad_payloads_archived_as_failed(client, db, offline_env):
    """校验不过的记录留档标 failed（可审计），不产生归一化行。"""
    from sqlalchemy import select

    from app.models import DietLog, WorkoutLog

    records = [
        _rec("diet", {"meal": "夜宵", "free_text": "泡面"}),
        _rec("workout", {"duration_min": 30}),
        _rec("metric", {"hacker_field": 1}),
        _rec("habit", {"habit_id": offline_env.id},
             d=date.today() + timedelta(days=30)),  # 未来日期
    ]
    r = client.post("/api/ingest/offline", json={"records": records})
    assert r.json() == {"received": 4, "new": 0, "skipped": 0}
    for rec in records:
        status, _ = _raw_status(db, f"{rec['type']}-{rec['client_id']}")
        assert status == "failed"
    assert db.execute(select(DietLog).where(DietLog.log_date == TEST_DATE)).first() is None
    assert db.execute(select(WorkoutLog).where(WorkoutLog.log_date == TEST_DATE)).first() is None


def test_structure_garbage_counted_not_archived(client, db, offline_env):
    """缺 type/client_id 的记录没法幂等留档：计入 received 后丢弃。"""
    r = client.post("/api/ingest/offline", json={"records": [
        {"foo": 1}, "not-a-dict", {"type": "diet", "client_id": ""},
    ]})
    assert r.json() == {"received": 3, "new": 0, "skipped": 0}


def test_bootstrap_returns_habits_and_types(client, offline_env):
    r = client.get("/api/offline/bootstrap")
    assert r.status_code == 200
    data = r.json()
    assert data["meals"] == ["早餐", "午餐", "加餐", "晚餐"]
    mine = [h for h in data["habits"] if h["id"] == offline_env.id]
    assert mine and mine[0]["target"] == 1 and mine[0]["auto"] is False
    assert data["workout_types"], "训练类型清单不应为空（词表兜底）"


def test_bearer_required(client):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as anon:
        assert anon.post("/api/ingest/offline", json={"records": []}).status_code == 401
        assert anon.get("/api/offline/bootstrap").status_code == 401
