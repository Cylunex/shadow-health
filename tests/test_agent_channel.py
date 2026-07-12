"""多 Agent 通道（V3 P2）：幂等重放/词表/mood_score 白名单与界值/summary 口径/
delete 权限边界（外部来源禁删）/MCP 短窗去重。

结构照 test_offline_ingest.py：前半纯函数直测；后半带 DB 集成（Mac 临时 PG
55433），数据库不可达或 INGEST_TOKEN 未配置自动跳过。测试日期用 today-320
（在服务端一年下界内、错开 offline 测试的 today-300，互不踩数据）。
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.routers.offline import parse_metric_payload


# ---------- 纯函数：mood_score 进白名单（_FIELD_DEFS 自动继承） ----------

def test_mood_score_in_metric_whitelist():
    got = parse_metric_payload({"mood_score": 7})
    assert got == {"mood_score": 7}


@pytest.mark.parametrize("bad", [0, 11, "abc", 7.5])
def test_mood_score_bounds(bad):
    with pytest.raises(ValueError):
        parse_metric_payload({"mood_score": bad})


def test_mood_score_alongside_weight():
    got = parse_metric_payload({"weight_kg": 71.5, "mood_score": 3})
    assert got["mood_score"] == 3
    assert str(got["weight_kg"]) == "71.5"


# ---------- MCP 工具层：同参数短窗去重（不依赖 DB/网络） ----------

def test_mcp_dedup_window():
    pytest.importorskip("mcp", reason="mcp 依赖组未装（uv sync --group mcp）")
    from mcp_server import server as srv

    srv._dedup_cache.clear()
    key = srv._dedup_key("record_diet", {"meal": "午餐", "items": [{"name": "面"}]})
    assert srv._dedup_hit(key) is None          # 首调无缓存
    srv._dedup_store(key, {"new": 1, "skipped": 0})
    hit = srv._dedup_hit(key)
    assert hit == {"new": 1, "skipped": 0, "dedup": True}  # 窗口内重调 → 回执 + dedup 标记
    # 不同参数不去重
    other = srv._dedup_key("record_diet", {"meal": "晚餐", "items": [{"name": "面"}]})
    assert other != key and srv._dedup_hit(other) is None
    # 过窗即失效
    srv._dedup_cache[key] = (srv.time.monotonic() - srv._DEDUP_WINDOW_S - 1, {"new": 1})
    assert srv._dedup_hit(key) is None
    srv._dedup_cache.clear()


# ---------- DB 集成（不可达自动跳过） ----------

def _test_date() -> date:
    from app.timeutil import today_local
    return today_local() - timedelta(days=320)


TEST_DATE = _test_date()
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
def agent_env(db):
    """测试习惯 + 痕迹自清理（留档按 external_id 精确删，绝不整删 source='agent'）。"""
    from sqlalchemy import delete, select

    from app.models import (
        BodyMetrics, DietLog, Habit, HabitLog, ImportRaw, WorkoutLog,
    )

    habit = Habit(name=f"测试-agent-{uuid.uuid4().hex[:8]}", period="daily", target_per_period=1)
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
            ImportRaw.source == "agent", ImportRaw.external_id.in_(list(_SENT_EXT_IDS))
        ))
        _SENT_EXT_IDS.clear()
    db.execute(delete(DietLog).where(DietLog.log_date == TEST_DATE))
    db.execute(delete(WorkoutLog).where(WorkoutLog.log_date == TEST_DATE))
    if bm_preexisting is None:
        db.execute(delete(BodyMetrics).where(BodyMetrics.log_date == TEST_DATE))
    db.commit()


def _rec(rtype: str, payload: dict, d: date = TEST_DATE) -> dict:
    client_id = str(uuid.uuid4())
    _SENT_EXT_IDS.append(f"{rtype}-{client_id}")
    return {"type": rtype, "client_id": client_id, "date": d.isoformat(), "payload": payload}


def _by_cid(resp_json: dict) -> dict[str, dict]:
    return {r["client_id"]: r for r in resp_json["results"]}


# ---- 写通道：幂等重放 + per-record 明细 + 词表 ----

def test_agent_replay_idempotent_with_results(client, db, agent_env):
    """四类记录发两遍：第二遍全 skipped；明细带 row_id；留档 source='agent'；
    workout external_id 前缀 'agent-'。"""
    from sqlalchemy import select

    from app.models import BodyMetrics, DietLog, ImportRaw, WorkoutLog

    habit = agent_env
    records = [
        _rec("habit", {"habit_id": habit.id}),
        _rec("diet", {"meal": "午餐", "free_text": "agent牛肉面", "kcal": 550, "protein_g": 25}),
        _rec("workout", {"session_type": "跑步", "duration_min": 30, "distance_km": 5.2}),
        _rec("metric", {"weight_kg": 71.5, "mood_score": 7}),
    ]
    r1 = client.post("/api/ingest/agent", json={"records": records})
    assert r1.status_code == 200
    j1 = r1.json()
    # 顶层保持 {received,new,skipped} 兼容
    assert (j1["received"], j1["new"], j1["skipped"]) == (4, 4, 0)
    d1 = _by_cid(j1)
    assert all(d1[r["client_id"]]["status"] == "new" for r in records)
    diet_row_id = d1[records[1]["client_id"]]["row_id"]
    workout_row_id = d1[records[2]["client_id"]]["row_id"]
    assert isinstance(diet_row_id, int) and isinstance(workout_row_id, int)
    assert d1[records[0]["client_id"]]["row_id"] is None  # habit/metric 可为 null
    assert d1[records[3]["client_id"]]["row_id"] is None

    # 落库核对：diet/workout 的 row_id 是真实行；词表 source='agent' 被 CHECK 放行
    assert db.get(DietLog, diet_row_id).free_text == "agent牛肉面"
    w = db.get(WorkoutLog, workout_row_id)
    assert w.source == "manual"
    assert w.external_id == f"agent-{records[2]['client_id']}"
    bm = db.execute(select(BodyMetrics).where(BodyMetrics.log_date == TEST_DATE)).scalar_one()
    assert float(bm.weight_kg) == 71.5 and bm.mood_score == 7
    raws = db.execute(
        select(ImportRaw.source).where(
            ImportRaw.external_id.in_([f"{r['type']}-{r['client_id']}" for r in records])
        )
    ).scalars().all()
    assert len(raws) == 4 and set(raws) == {"agent"}

    r2 = client.post("/api/ingest/agent", json={"records": records})
    j2 = r2.json()
    assert (j2["received"], j2["new"], j2["skipped"]) == (4, 0, 4)
    d2 = _by_cid(j2)
    assert all(v["status"] == "skipped" for v in d2.values())
    # skipped 的 workout 仍能回查 row_id（diet 无从回查 → null）
    assert d2[records[2]["client_id"]]["row_id"] == workout_row_id
    assert d2[records[1]["client_id"]]["row_id"] is None
    # 不双写
    assert len(db.execute(select(DietLog).where(DietLog.log_date == TEST_DATE)).scalars().all()) == 1
    assert len(db.execute(select(WorkoutLog).where(WorkoutLog.log_date == TEST_DATE)).scalars().all()) == 1


def test_agent_workout_external_id_prefix(client, db, agent_env):
    from sqlalchemy import select

    from app.models import WorkoutLog

    rec = _rec("workout", {"session_type": "壶铃", "duration_min": 20})
    client.post("/api/ingest/agent", json={"records": [rec]})
    w = db.execute(select(WorkoutLog).where(WorkoutLog.log_date == TEST_DATE)).scalar_one()
    # 管线的 workout external_id = 留档 ext_id = '{source}-…' 语义：agent 通道
    # 以 'agent-' 开头（与 offline-… 区分，导入中心可辨来源）
    assert (w.external_id or "").startswith("agent-")


def test_agent_mood_score_bounds_via_ingest(client, db, agent_env):
    """mood_score=11 越界：该条 failed（带 error），不产生归一化行。"""
    from sqlalchemy import select

    from app.models import BodyMetrics

    bad = _rec("metric", {"mood_score": 11})
    r = client.post("/api/ingest/agent", json={"records": [bad]})
    j = r.json()
    assert (j["new"], j["skipped"]) == (0, 0)
    res = _by_cid(j)[bad["client_id"]]
    assert res["status"] == "failed" and "心情分" in res.get("error", "")
    assert db.execute(
        select(BodyMetrics).where(BodyMetrics.log_date == TEST_DATE)
    ).scalar_one_or_none() is None


# ---- 读端点：summary / weekly 口径 ----

def test_agent_summary_matches_seeded_data(client, agent_env):
    habit = agent_env
    client.post("/api/ingest/agent", json={"records": [
        _rec("diet", {"meal": "早餐", "free_text": "鸡蛋", "kcal": 80, "protein_g": 7}),
        _rec("diet", {"meal": "午餐", "free_text": "牛肉面", "kcal": 550, "protein_g": 25}),
        _rec("workout", {"session_type": "跑步", "duration_min": 30, "distance_km": 5.0}),
        _rec("metric", {"weight_kg": 71.5, "mood_score": 8}),
        _rec("habit", {"habit_id": habit.id}),
    ]})
    r = client.get(f"/api/agent/summary?date={TEST_DATE.isoformat()}")
    assert r.status_code == 200
    s = r.json()
    assert s["date"] == TEST_DATE.isoformat()
    assert s["diet"]["kcal"] == 630.0
    assert s["diet"]["protein_g"] == 32.0
    assert len(s["diet"]["entries"]) == 2
    assert all(isinstance(e["id"], int) for e in s["diet"]["entries"])  # 删除纠错要用
    assert s["workout_min"] == 30
    assert len(s["workouts"]) == 1 and isinstance(s["workouts"][0]["id"], int)
    assert s["weight_kg"] == 71.5
    assert s["mood_score"] == 8
    mine = [i for i in s["habits"]["items"] if i["id"] == habit.id]
    assert mine and mine[0]["done"] is True
    assert s["habits"]["done"] >= 1


def test_agent_summary_rejects_bad_date(client):
    assert client.get("/api/agent/summary?date=2026-13-40").status_code == 400
    assert client.get("/api/agent/summary?date=昨天").status_code == 400


def test_agent_weekly_report(client, agent_env):
    client.post("/api/ingest/agent", json={"records": [
        _rec("diet", {"meal": "午餐", "free_text": "周报测试餐", "kcal": 600}),
    ]})
    iso = TEST_DATE.isocalendar()
    week = f"{iso.year}-W{iso.week:02d}"
    r = client.get(f"/api/agent/report/weekly?week={week}")
    assert r.status_code == 200
    j = r.json()
    assert j["week"] == week
    assert j["complete"] is True  # 320 天前的周早已走完
    assert j["week_start"] == date.fromisocalendar(iso.year, iso.week, 1).isoformat()
    assert j["diet_days"] >= 1
    for key in ("workout_min", "cardio_min", "habit_rate", "avg_steps", "weight_change"):
        assert key in j


@pytest.mark.parametrize("bad", ["2026W28", "28", "2026-W99", "abc"])
def test_agent_weekly_rejects_bad_week(client, bad):
    assert client.get(f"/api/agent/report/weekly?week={bad}").status_code == 400


def test_agent_foods_search(client):
    r = client.get("/api/agent/foods?q=")
    assert r.status_code == 200 and r.json()["items"] == []


# ---- delete：权限边界 ----

def test_agent_delete_own_records(client, db, agent_env):
    """agent 写入的 diet/workout 可删；再删同 id → 404。"""
    from app.models import DietLog, WorkoutLog

    j = client.post("/api/ingest/agent", json={"records": [
        _rec("diet", {"meal": "加餐", "free_text": "记错的蛋糕", "kcal": 400}),
        _rec("workout", {"session_type": "跑步", "duration_min": 30}),
    ]}).json()
    diet_id, workout_id = (r["row_id"] for r in j["results"])

    r = client.post("/api/agent/delete", json={"type": "diet", "row_id": diet_id})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True and "记错的蛋糕" in body["summary"]
    assert db.get(DietLog, diet_id) is None
    assert client.post(
        "/api/agent/delete", json={"type": "diet", "row_id": diet_id}
    ).status_code == 404

    r = client.post("/api/agent/delete", json={"type": "workout", "row_id": workout_id})
    assert r.status_code == 200
    assert db.get(WorkoutLog, workout_id) is None


def test_agent_delete_external_source_forbidden(client, db, agent_env):
    """外部同步来源（三星/Keep）禁删：403 且行保留。"""
    from app.models import WorkoutLog

    ext = f"test-agent-guard-{uuid.uuid4().hex[:8]}"
    db.execute(
        WorkoutLog.__table__.insert().values(
            log_date=TEST_DATE, source="samsung_direct", external_id=ext,
            session_type="跑步", duration_min=25,
        )
    )
    db.commit()
    from sqlalchemy import select
    row_id = db.execute(
        select(WorkoutLog.id).where(
            WorkoutLog.source == "samsung_direct", WorkoutLog.external_id == ext
        )
    ).scalar_one()
    r = client.post("/api/agent/delete", json={"type": "workout", "row_id": row_id})
    assert r.status_code == 403
    assert "samsung_direct" in r.json()["error"]
    db.expire_all()
    assert db.get(WorkoutLog, row_id) is not None  # 行保留（fixture 按日期清理）


def test_agent_delete_rejects_bad_input(client, agent_env):
    assert client.post("/api/agent/delete", json={"type": "habit", "row_id": 1}).status_code == 400
    assert client.post("/api/agent/delete", json={"type": "diet", "row_id": "x"}).status_code == 400
    assert client.post(
        "/api/agent/delete", json={"type": "diet", "row_id": 99999999}
    ).status_code == 404


# ---- 鉴权 ----

def test_agent_endpoints_require_bearer(client):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as anon:
        assert anon.post("/api/ingest/agent", json={"records": []}).status_code == 401
        assert anon.get("/api/agent/summary").status_code == 401
        assert anon.get("/api/agent/report/weekly").status_code == 401
        assert anon.get("/api/agent/foods?q=a").status_code == 401
        assert anon.post(
            "/api/agent/delete", json={"type": "diet", "row_id": 1}
        ).status_code == 401
