"""V5 批次（Agent 深度使用）测试补锁：context/monthly/series 读端点、
summary.metrics 全字段、diet food_id 路径、habit increment、/api/agent/update
改口修正、agent_name 留档归属、ai_tools.run_tool 进程内工具面。

结构照 test_agent_channel.py：TestClient + Bearer；DB 不可达或 INGEST_TOKEN
未配置自动跳过。测试日期用 today-360（错开 offline 的 today-300、agent 的
today-320 等既有占用，在服务端一年下界 366 天以内）。清理铁律：留档只按本轮
external_id 精确删（绝不整删 source='agent'——有真实数据），归一化行按测试
日期/自建 id 删。
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest


def _test_date() -> date:
    from app.timeutil import today_local
    return today_local() - timedelta(days=360)


TEST_DATE = _test_date()

# 本轮测试发出的 external_id：teardown 只删自己的留档，不碰真实 agent 数据
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
def env(db):
    """测试习惯（target=8，increment 用）+ 测试食物 + 痕迹自清理。

    留档按 external_id 精确删；DietLog/WorkoutLog 删测试日期行（today-360，
    一年前无真实数据且本文件独占该日期）；BodyMetrics 仅删本轮新建的日行。
    """
    from sqlalchemy import delete, select

    from app.models import (
        BodyMetrics, DietLog, Food, Habit, HabitLog, ImportRaw, WorkoutLog,
    )

    habit = Habit(name=f"测试-v5-{uuid.uuid4().hex[:8]}", period="daily", target_per_period=8)
    food = Food(
        name=f"测试食物-v5-{uuid.uuid4().hex[:8]}",
        category="测试",
        kcal_per_100g=Decimal("150.0"),
        protein_g=Decimal("10.5"),
        fat_g=Decimal("5.0"),
        carb_g=Decimal("20.0"),
    )
    db.add_all([habit, food])
    db.commit()
    bm_preexisting = db.execute(
        select(BodyMetrics.id).where(BodyMetrics.log_date == TEST_DATE)
    ).scalar_one_or_none()
    _SENT_EXT_IDS.clear()
    yield habit, food
    db.rollback()
    db.execute(delete(HabitLog).where(HabitLog.habit_id == habit.id))
    db.execute(delete(Habit).where(Habit.id == habit.id))
    if _SENT_EXT_IDS:
        db.execute(delete(ImportRaw).where(
            ImportRaw.source == "agent", ImportRaw.external_id.in_(list(_SENT_EXT_IDS))
        ))
        _SENT_EXT_IDS.clear()
    db.execute(delete(DietLog).where(DietLog.log_date == TEST_DATE))  # 先删（FK→foods）
    db.execute(delete(WorkoutLog).where(WorkoutLog.log_date == TEST_DATE))
    if bm_preexisting is None:
        db.execute(delete(BodyMetrics).where(BodyMetrics.log_date == TEST_DATE))
    db.execute(delete(Food).where(Food.id == food.id))
    db.commit()


def _rec(rtype: str, payload: dict, d: date = TEST_DATE) -> dict:
    client_id = str(uuid.uuid4())
    _SENT_EXT_IDS.append(f"{rtype}-{client_id}")
    return {"type": rtype, "client_id": client_id, "date": d.isoformat(), "payload": payload}


def _blob(db, ext_id: str):
    from sqlalchemy import select

    from app.models import ImportRaw
    return db.execute(
        select(ImportRaw.blob).where(
            ImportRaw.source == "agent", ImportRaw.external_id == ext_id
        )
    ).scalar_one()


# ---------- GET /api/agent/context ----------

def test_agent_context_ok(client):
    r = client.get("/api/agent/context")
    assert r.status_code == 200
    j = r.json()
    assert j["days"] == 30  # 缺省 30 天
    assert "# 数据快照" in j["context"]  # 与内置 AI 注入的同一文本头
    assert j["generated_at"]
    r7 = client.get("/api/agent/context?days=7")
    assert r7.status_code == 200 and r7.json()["days"] == 7


@pytest.mark.parametrize("days", [0, 400])
def test_agent_context_rejects_bad_days(client, days):
    r = client.get(f"/api/agent/context?days={days}")
    assert r.status_code == 400
    assert "days" in r.json()["error"]


# ---------- GET /api/agent/report/monthly ----------

def test_agent_monthly_default_prev_complete_month(client):
    from app.timeutil import today_local

    today = today_local()
    prev_end = today.replace(day=1) - timedelta(days=1)  # 上月末
    r = client.get("/api/agent/report/monthly")
    assert r.status_code == 200
    j = r.json()
    assert j["month"] == f"{prev_end:%Y-%m}"
    assert j["complete"] is True
    assert j["month_start"] == prev_end.replace(day=1).isoformat()
    for key in ("workout_min", "cardio_min", "habit_rate", "weight_change", "days_in_month"):
        assert key in j


def test_agent_monthly_current_month_incomplete(client):
    from app.timeutil import today_local

    month = f"{today_local():%Y-%m}"
    r = client.get(f"/api/agent/report/monthly?month={month}")
    assert r.status_code == 200
    j = r.json()
    assert j["month"] == month and j["complete"] is False


def test_agent_monthly_rejects_bad_month(client):
    from app.timeutil import today_local

    assert client.get("/api/agent/report/monthly?month=2026-13").status_code == 400
    assert client.get("/api/agent/report/monthly?month=abc").status_code == 400
    future = (today_local().replace(day=1) + timedelta(days=62)).replace(day=1)
    r = client.get(f"/api/agent/report/monthly?month={future:%Y-%m}")
    assert r.status_code == 400
    assert "未来" in r.json()["error"]


# ---------- GET /api/agent/metrics/series ----------

def test_agent_metric_series_weight(client, env):
    client.post("/api/ingest/agent", json={"records": [
        _rec("metric", {"weight_kg": 71.5}),
    ]})
    r = client.get("/api/agent/metrics/series?field=weight_kg&days=366")
    assert r.status_code == 200
    j = r.json()
    assert j["field"] == "weight_kg" and j["label"] == "体重" and j["days"] == 366
    pts = {p["date"]: p for p in j["series"]}
    p = pts[TEST_DATE.isoformat()]
    assert p["value"] == 71.5
    # agent 写入登记 autofilled='agent'（非 mark_manual）：manual 标记存在且为 False，
    # 同日秤/手表实测仍可修正 agent 转述值
    assert p["manual"] is False


def test_agent_metric_series_steps_branch(client):
    r = client.get("/api/agent/metrics/series?field=steps&days=30")
    assert r.status_code == 200
    j = r.json()
    assert j["field"] == "steps" and j["label"] == "步数"
    assert isinstance(j["series"], list)  # daily_activity 分支不报错即可（内容随库）


def test_agent_metric_series_rejects_bad_input(client):
    r = client.get("/api/agent/metrics/series?field=hacker_field&days=30")
    assert r.status_code == 400
    assert "白名单" in r.json()["error"]
    assert client.get("/api/agent/metrics/series?field=weight_kg&days=0").status_code == 400
    assert client.get("/api/agent/metrics/series?field=weight_kg&days=367").status_code == 400


# ---------- summary.metrics：白名单字段写得进就读得出 ----------

def test_agent_summary_metrics_includes_bp(client, env):
    client.post("/api/ingest/agent", json={"records": [
        _rec("metric", {"bp_systolic": 120, "bp_diastolic": 80}),
    ]})
    r = client.get(f"/api/agent/summary?date={TEST_DATE.isoformat()}")
    assert r.status_code == 200
    m = r.json()["metrics"]
    assert m["bp_systolic"] == 120
    assert m["bp_diastolic"] == 80


# ---------- diet food_id 路径 ----------

def test_agent_diet_food_id_macros_from_library(client, db, env):
    from app.models import DietLog

    _, food = env
    rec = _rec("diet", {"meal": "午餐", "food_id": food.id, "amount_g": 200,
                        "kcal": 1})  # agent 自报的 kcal 应被忽略，按食物库重算
    r = client.post("/api/ingest/agent", json={"records": [rec]})
    j = r.json()
    assert j["new"] == 1
    res = j["results"][0]
    assert res["status"] == "new" and isinstance(res["row_id"], int)
    log = db.get(DietLog, res["row_id"])
    assert log.food_id == food.id
    assert log.free_text is None  # 关联行不存自由文本（与 UI 同约定）
    assert float(log.amount_g) == 200.0
    assert float(log.kcal) == 300.0       # 150 kcal/100g × 200g（服务端算的）
    assert float(log.protein_g) == 21.0   # 10.5 × 2
    assert float(log.carb_g) == 40.0


def test_agent_diet_food_id_missing_fails(client, db, env):
    from sqlalchemy import select

    from app.models import DietLog

    rec = _rec("diet", {"meal": "午餐", "food_id": 99999999, "amount_g": 100})
    j = client.post("/api/ingest/agent", json={"records": [rec]}).json()
    assert (j["new"], j["skipped"]) == (0, 0)
    res = j["results"][0]
    assert res["status"] == "failed"
    assert "食物不存在" in res["error"]
    assert db.execute(
        select(DietLog).where(DietLog.log_date == TEST_DATE)
    ).first() is None


# ---------- habit increment ----------

def test_agent_habit_increment_accumulates(client, db, env):
    from sqlalchemy import select

    from app.models import HabitLog

    habit, _ = env
    rec1 = _rec("habit", {"habit_id": habit.id, "mode": "increment", "done_count": 3})
    rec2 = _rec("habit", {"habit_id": habit.id, "mode": "increment", "done_count": 3})
    assert client.post("/api/ingest/agent", json={"records": [rec1]}).json()["new"] == 1
    assert client.post("/api/ingest/agent", json={"records": [rec2]}).json()["new"] == 1

    def _count() -> int:
        db.expire_all()
        return db.execute(
            select(HabitLog.done_count).where(
                HabitLog.habit_id == habit.id, HabitLog.log_date == TEST_DATE
            )
        ).scalar_one()

    assert _count() == 6  # 3 + 3 累加（不同 client_id）
    # 重放同 client_id：parse_status 门控挡下，不再累加
    j = client.post("/api/ingest/agent", json={"records": [rec2]}).json()
    assert (j["new"], j["skipped"]) == (0, 1)
    assert _count() == 6


# ---------- POST /api/agent/update ----------

def test_agent_update_workout_duration(client, db, env):
    from app.models import WorkoutLog

    rec = _rec("workout", {"session_type": "跑步", "duration_min": 30, "distance_km": 5.2})
    row_id = client.post("/api/ingest/agent", json={"records": [rec]}).json()["results"][0]["row_id"]
    r = client.post("/api/agent/update", json={
        "type": "workout", "row_id": row_id, "fields": {"duration_min": 45},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] is True and "45分钟" in body["summary"]
    db.expire_all()
    w = db.get(WorkoutLog, row_id)
    assert w.duration_min == 45
    assert w.session_type == "跑步" and float(w.distance_km) == 5.2  # 未提字段不动


def test_agent_update_external_source_forbidden(client, db, env):
    from app.models import WorkoutLog

    ext = f"test-v5-guard-{uuid.uuid4().hex[:8]}"
    db.execute(
        WorkoutLog.__table__.insert().values(
            log_date=TEST_DATE, source="samsung_zip", external_id=ext,
            session_type="跑步", duration_min=25,
        )
    )
    db.commit()
    from sqlalchemy import select
    row_id = db.execute(
        select(WorkoutLog.id).where(
            WorkoutLog.source == "samsung_zip", WorkoutLog.external_id == ext
        )
    ).scalar_one()
    r = client.post("/api/agent/update", json={
        "type": "workout", "row_id": row_id, "fields": {"duration_min": 60},
    })
    assert r.status_code == 403
    assert "samsung_zip" in r.json()["error"]
    db.expire_all()
    assert db.get(WorkoutLog, row_id).duration_min == 25  # 行未动（fixture 按日期清理）


def test_agent_update_rejects_bad_input(client, db, env):
    rec = _rec("workout", {"session_type": "跑步", "duration_min": 30})
    row_id = client.post("/api/ingest/agent", json={"records": [rec]}).json()["results"][0]["row_id"]
    # 非法字段（log_date 不在白名单——改日期只能删了重记）
    r = client.post("/api/agent/update", json={
        "type": "workout", "row_id": row_id, "fields": {"log_date": "2020-01-01"},
    })
    assert r.status_code == 400 and "不支持的字段" in r.json()["error"]
    # 合法字段非法值：整体重校验与表单同口径
    assert client.post("/api/agent/update", json={
        "type": "workout", "row_id": row_id, "fields": {"rpe": 11},
    }).status_code == 400
    # fields 缺失/空、type 越界、row_id 不存在
    assert client.post("/api/agent/update", json={
        "type": "workout", "row_id": row_id,
    }).status_code == 400
    assert client.post("/api/agent/update", json={
        "type": "workout", "row_id": row_id, "fields": {},
    }).status_code == 400
    assert client.post("/api/agent/update", json={
        "type": "habit", "row_id": 1, "fields": {"done_count": 2},
    }).status_code == 400
    assert client.post("/api/agent/update", json={
        "type": "workout", "row_id": 99999999, "fields": {"duration_min": 5},
    }).status_code == 404


def test_agent_update_food_linked_diet(client, db, env):
    from app.models import DietLog

    _, food = env
    rec = _rec("diet", {"meal": "午餐", "food_id": food.id, "amount_g": 200})
    row_id = client.post("/api/ingest/agent", json={"records": [rec]}).json()["results"][0]["row_id"]
    # 食物关联行：营养值按食物库计算，改 kcal → 400
    r = client.post("/api/agent/update", json={
        "type": "diet", "row_id": row_id, "fields": {"kcal": 500},
    })
    assert r.status_code == 400
    assert "食物关联" in r.json()["error"]
    # 改 amount_g 合法：冗余营养按食物库重算
    r2 = client.post("/api/agent/update", json={
        "type": "diet", "row_id": row_id, "fields": {"amount_g": 100},
    })
    assert r2.status_code == 200
    db.expire_all()
    log = db.get(DietLog, row_id)
    assert float(log.amount_g) == 100.0 and float(log.kcal) == 150.0


# ---------- agent_name 留档归属 ----------

def test_agent_name_lands_in_import_raw_blob(client, db, env):
    named = _rec("diet", {"meal": "早餐", "free_text": "v5归属测试蛋", "kcal": 80})
    j = client.post("/api/ingest/agent", json={
        "agent_name": "测试Agent", "records": [named],
    }).json()
    blob = _blob(db, f"diet-{named['client_id']}")
    assert blob["agent"] == "测试Agent"
    assert blob["row_id"] == j["results"][0]["row_id"]  # /agent-log 撤销据此定位

    anon_rec = _rec("diet", {"meal": "早餐", "free_text": "v5无名测试蛋", "kcal": 80})
    client.post("/api/ingest/agent", json={"records": [anon_rec]})
    blob2 = _blob(db, f"diet-{anon_rec['client_id']}")
    assert "agent" not in (blob2 or {})  # 不带 agent_name 则 blob 无 agent 键


# ---------- ai_tools.run_tool 进程内直测 ----------

def test_ai_tools_record_diet_then_delete(client, db, env):
    """record_diet 走通完整管线（source='agent'、blob.agent='内置AI'）→ delete_record 删掉。"""
    from app.models import DietLog
    from app.services.ai_tools import run_tool

    out = run_tool(db, "record_diet", {
        "meal": "午餐",
        "date": TEST_DATE.isoformat(),
        "items": [{"name": "测试AI面", "kcal": 500, "protein_g": 20}],
    })
    assert "error" not in out
    assert out["date"] == TEST_DATE.isoformat()
    assert (out["new"], out["skipped"]) == (1, 0)
    res = out["results"][0]
    assert res["status"] == "new" and isinstance(res["row_id"], int)
    _SENT_EXT_IDS.append(f"diet-{res['client_id']}")  # 留档纳入精确清理
    assert out["day_totals"]["kcal"] >= 500.0

    blob = _blob(db, f"diet-{res['client_id']}")
    assert blob["agent"] == "内置AI"
    assert db.get(DietLog, res["row_id"]).free_text == "测试AI面"

    deleted = run_tool(db, "delete_record", {"type": "diet", "row_id": res["row_id"]})
    assert deleted["deleted"] is True and "测试AI面" in deleted["summary"]
    db.commit()  # delete_record 只 flush，commit 是调用方（请求收尾）的事
    assert db.get(DietLog, res["row_id"]) is None
    # 再删同 id：错误折成 {"error": ...} 喂回模型，不抛异常
    again = run_tool(db, "delete_record", {"type": "diet", "row_id": res["row_id"]})
    assert "不存在" in again["error"]


def test_ai_tools_search_food_and_query_summary(client, db, env):
    from app.services.ai_tools import run_tool

    _, food = env
    found = run_tool(db, "search_food", {"keyword": food.name})
    assert found["q"] == food.name
    assert [it["id"] for it in found["items"]] == [food.id]  # uuid 名字全库唯一
    assert found["items"][0]["kcal_per_100g"] == 150.0
    assert run_tool(db, "search_food", {"keyword": " "})["error"]

    s = run_tool(db, "query_summary", {"date": TEST_DATE.isoformat()})
    assert "error" not in s
    assert s["date"] == TEST_DATE.isoformat()
    assert "diet" in s and "habits" in s and "metrics" in s
    assert "不是合法日期" in run_tool(db, "query_summary", {"date": "昨天"})["error"]


def test_ai_tools_unknown_tool(client, db):
    from app.services.ai_tools import run_tool

    out = run_tool(db, "hack_the_planet", {})
    assert "未知工具" in out["error"]


# ---------- 鉴权：V5 新端点全部要 Bearer ----------

def test_agent_v5_endpoints_require_bearer(client):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as anon:
        assert anon.get("/api/agent/context").status_code == 401
        assert anon.get("/api/agent/report/monthly").status_code == 401
        assert anon.get("/api/agent/metrics/series?field=weight_kg").status_code == 401
        assert anon.post("/api/agent/update", json={
            "type": "diet", "row_id": 1, "fields": {"kcal": 1},
        }).status_code == 401
