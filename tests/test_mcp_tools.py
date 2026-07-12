"""MCP 工具本体（V5 批次，mcp_server/server.py 共 16 工具）：
record_habit 名称匹配三态与 count 语义 / record_diet 与 record_weight 入参校验
及 payload 组装 / _ingest 503 同批重试 / update_record 校验与短窗去重 /
query_metric_series field 白名单。

全部纯 mock、不打真网络也不碰 DB（故不需要错峰测试日期）：server 的
_get/_post/_ingest 每次新建 httpx.Client(base_url=API_BASE)，这里 monkeypatch
httpx.Client 为「强制带 MockTransport 的工厂」，所有出网请求都被 FakeApi 拦截
并记录（方法/路径/请求体），可对请求次数与请求体做精确断言。
进程内 60s 去重缓存（srv._dedup_cache）每个测试前后清空，避免串测试。
"""
from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip("mcp", reason="mcp 依赖组未装（uv sync --group mcp）")

from mcp_server import server as srv  # noqa: E402

TEST_DATE = "2026-07-01"  # 纯 mock，仅求请求体确定性，不落库
TEST_TOKEN = "test-token"

HABITS = [
    {"id": 1, "name": "喝水", "period": "daily", "target_per_period": 8},
    {"id": 2, "name": "早睡", "period": "daily", "target_per_period": 1},
    {"id": 3, "name": "晨间拉伸", "period": "daily", "target_per_period": 1},
    {"id": 4, "name": "晚间拉伸", "period": "daily", "target_per_period": 1},
]


class FakeApi:
    """MockTransport 后端：记录全部请求，按路径返回固定形状的回执。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []  # (method, path, body|params)
        self.habits: list[dict] = list(HABITS)
        self.ingest_statuses: list[int] = []  # 每次 POST /api/ingest/agent 弹一个，空=200

    # -- 断言辅助 --
    def paths(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.calls]

    def ingest_bodies(self) -> list[dict]:
        return [b for m, p, b in self.calls if (m, p) == ("POST", "/api/ingest/agent")]

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == f"Bearer {TEST_TOKEN}", "缺 Bearer 头"
        method, path = request.method, request.url.path
        if method == "POST":
            recorded: dict | None = json.loads(request.content.decode("utf-8"))
        else:
            recorded = dict(request.url.params)
        self.calls.append((method, path, recorded))

        if (method, path) == ("GET", "/api/offline/bootstrap"):
            return httpx.Response(200, json={"habits": self.habits})
        if (method, path) == ("POST", "/api/ingest/agent"):
            status = self.ingest_statuses.pop(0) if self.ingest_statuses else 200
            if status != 200:
                return httpx.Response(status, json={"error": "服务暂不可用"})
            recs = recorded["records"]
            return httpx.Response(200, json={
                "received": len(recs), "new": len(recs), "skipped": 0,
                "results": [
                    {"client_id": r["client_id"], "status": "new", "row_id": 1000 + i}
                    for i, r in enumerate(recs)
                ],
            })
        if (method, path) == ("GET", "/api/agent/summary"):
            return httpx.Response(200, json={
                "date": recorded.get("date", ""),
                "diet": {"kcal": 630.0, "protein_g": 32.0, "entries": []},
                "workout_min": 30,
            })
        if (method, path) == ("POST", "/api/agent/update"):
            return httpx.Response(200, json={
                "updated": True,
                "summary": f"{recorded['type']}#{recorded['row_id']} 已更新",
            })
        if (method, path) == ("GET", "/api/agent/metrics/series"):
            return httpx.Response(200, json={
                "field": recorded["field"], "days": int(recorded.get("days", 30)), "points": [],
            })
        raise AssertionError(f"未预期的请求：{method} {path}")


@pytest.fixture(autouse=True)
def _clean_dedup():
    """60s 进程内去重缓存会串测试——前后都清。"""
    srv._dedup_cache.clear()
    yield
    srv._dedup_cache.clear()


@pytest.fixture()
def api(monkeypatch) -> FakeApi:
    fake = FakeApi()
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(srv.httpx, "Client", factory)
    monkeypatch.setattr(srv, "INGEST_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(srv.time, "sleep", lambda _s: None)  # 503 重试不真等 2 秒
    return fake


# ---------- record_habit：名称匹配三态 + count 语义 ----------

def test_record_habit_exact_match(api):
    out = srv.record_habit("喝水", date=TEST_DATE)
    assert out["habit"] == "喝水"
    assert out["new"] == 1
    bodies = api.ingest_bodies()
    assert len(bodies) == 1
    rec = bodies[0]["records"][0]
    assert rec["type"] == "habit"
    assert rec["date"] == TEST_DATE
    # 声明式打卡：payload 只有 habit_id，没有 increment 模式
    assert rec["payload"] == {"habit_id": 1}


def test_record_habit_fuzzy_unique_match(api):
    out = srv.record_habit("睡", date=TEST_DATE)  # 无精确命中，包含匹配唯一命中「早睡」
    assert out["habit"] == "早睡"
    assert api.ingest_bodies()[0]["records"][0]["payload"] == {"habit_id": 2}


def test_record_habit_no_match_lists_candidates(api):
    with pytest.raises(srv.ApiError) as ei:
        srv.record_habit("跑步机", date=TEST_DATE)
    msg = str(ei.value)
    assert "没有匹配的习惯" in msg
    for name in ("喝水", "早睡", "晨间拉伸", "晚间拉伸"):
        assert name in msg  # 报错要列全候选
    assert api.ingest_bodies() == []  # 只查了 bootstrap，没有写入
    assert api.paths() == [("GET", "/api/offline/bootstrap")]


def test_record_habit_ambiguous_match(api):
    with pytest.raises(srv.ApiError) as ei:
        srv.record_habit("拉伸", date=TEST_DATE)  # 命中晨间/晚间两个
    msg = str(ei.value)
    assert "歧义" in msg
    assert "晨间拉伸" in msg and "晚间拉伸" in msg
    assert api.ingest_bodies() == []


@pytest.mark.parametrize("bad", [0, 100, -3])
def test_record_habit_count_out_of_range(api, bad):
    with pytest.raises(srv.ApiError, match="1~99"):
        srv.record_habit("喝水", date=TEST_DATE, count=bad)
    assert api.calls == []  # 越界在发任何请求之前就挡下


def test_record_habit_count_increment_payload(api):
    out = srv.record_habit("喝水", date=TEST_DATE, count=3)
    assert out["new"] == 1
    rec = api.ingest_bodies()[0]["records"][0]
    assert rec["payload"] == {"habit_id": 1, "mode": "increment", "done_count": 3}


def test_record_habit_empty_name(api):
    with pytest.raises(srv.ApiError, match="不能为空"):
        srv.record_habit("  ")
    assert api.calls == []


# ---------- record_diet：入参校验 + payload 组装 ----------

def test_record_diet_rejects_bad_meal(api):
    with pytest.raises(srv.ApiError, match="meal 必须是"):
        srv.record_diet([srv.DietItem(name="泡面")], meal="夜宵", date=TEST_DATE)
    assert api.calls == []


def test_record_diet_rejects_empty_items(api):
    with pytest.raises(srv.ApiError, match="items 不能为空"):
        srv.record_diet([], meal="午餐", date=TEST_DATE)
    assert api.calls == []


def test_record_diet_food_id_in_payload(api):
    out = srv.record_diet(
        [srv.DietItem(name="牛肉面", food_id=42, amount_g=300.0)],
        meal="午餐", date=TEST_DATE,
    )
    assert out["date"] == TEST_DATE and out["new"] == 1
    # 记录后附带当日累计（多打了一次 summary）
    assert out["day_totals"] == {"kcal": 630.0, "protein_g": 32.0, "workout_min": 30}
    assert api.paths() == [("POST", "/api/ingest/agent"), ("GET", "/api/agent/summary")]
    rec = api.ingest_bodies()[0]["records"][0]
    assert rec["type"] == "diet"
    assert rec["payload"]["food_id"] == 42
    assert rec["payload"]["free_text"] == "牛肉面"
    assert rec["payload"]["amount_g"] == 300.0
    assert rec["payload"]["meal"] == "午餐"


def test_record_diet_self_reported_nutrition(api):
    srv.record_diet(
        [srv.DietItem(name="蛋白棒", kcal=200.0, protein_g=20.0)],
        meal="加餐", date=TEST_DATE,
    )
    p = api.ingest_bodies()[0]["records"][0]["payload"]
    assert p["food_id"] is None
    assert p["kcal"] == 200.0 and p["protein_g"] == 20.0


# ---------- record_weight：全 None 报错 + 20 字段全进 payload ----------

def test_record_weight_all_none_rejected(api):
    with pytest.raises(srv.ApiError, match="至少提供一个字段"):
        srv.record_weight(date=TEST_DATE)
    assert api.calls == []


def test_record_weight_all_fields_in_payload(api):
    fields = {
        "weight_kg": 71.5, "body_fat_pct": 18.2, "mood_score": 7,
        "waist_cm": 80.0, "chest_cm": 95.0, "arm_cm": 32.0,
        "thigh_cm": 55.0, "hip_cm": 90.0,
        "bp_systolic": 118, "bp_diastolic": 76,
        "resting_hr": 55, "spo2_pct": 98.0,
        "sleep_hours": 7.5, "sleep_quality": 4, "energy_level": 4,
        "muscle_mass_kg": 55.2, "skeletal_muscle_kg": 31.0, "bmr_kcal": 1550,
        "body_water_kg": 40.1, "visceral_fat_level": 6,
    }
    assert set(fields) == set(srv.WEIGHT_FIELDS)  # 白名单全覆盖（含 V4/V5 新增字段）
    out = srv.record_weight(date=TEST_DATE, **fields)
    assert out["new"] == 1
    rec = api.ingest_bodies()[0]["records"][0]
    assert rec["type"] == "metric" and rec["date"] == TEST_DATE
    assert rec["payload"] == fields  # 20 个字段一个不落、值不变


# ---------- _ingest：503 同批重试（client_id 不变） ----------

def test_ingest_retry_503_then_200(api):
    api.ingest_statuses = [503, 200]
    rec = srv._record("diet", TEST_DATE, {"meal": "午餐", "free_text": "面"})
    out = srv._ingest([rec])
    assert (out["new"], out["skipped"]) == (1, 0)  # 最终成功
    bodies = api.ingest_bodies()
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]  # 同一批 records 原样重发
    assert bodies[0]["records"][0]["client_id"] == rec["client_id"]  # client_id 不变
    assert bodies[0]["agent_name"]  # 无 MCP 会话时也带兜底 agent_name


def test_ingest_both_503_raises(api):
    api.ingest_statuses = [503, 503]
    rec = srv._record("metric", TEST_DATE, {"weight_kg": 71.5})
    with pytest.raises(srv.ApiError, match="503"):
        srv._ingest([rec])
    bodies = api.ingest_bodies()
    assert len(bodies) == 2  # 只重试一次，不无限重试
    assert bodies[0] == bodies[1]


# ---------- update_record：校验 + 短窗去重 ----------

@pytest.mark.parametrize("bad_type", ["metric", "habit", " ", "DIET"])
def test_update_record_rejects_bad_type(api, bad_type):
    with pytest.raises(srv.ApiError, match="仅支持 diet/workout"):
        srv.update_record(bad_type, 5, {"kcal": 300})
    assert api.calls == []


@pytest.mark.parametrize("bad_fields", [{}, ["kcal"], "kcal=300", None])
def test_update_record_rejects_empty_fields(api, bad_fields):
    with pytest.raises(srv.ApiError, match="非空对象"):
        srv.update_record("diet", 5, bad_fields)
    assert api.calls == []


def test_update_record_dedup_window(api):
    r1 = srv.update_record("diet", 5, {"kcal": 300.0})
    assert r1["updated"] is True and "dedup" not in r1
    # 同参二调：窗口内直接回上次回执 + dedup 标记，不再发请求
    r2 = srv.update_record("diet", 5, {"kcal": 300.0})
    assert r2 == {**r1, "dedup": True}
    posts = [c for c in api.paths() if c == ("POST", "/api/agent/update")]
    assert len(posts) == 1
    # 不同参数不去重
    r3 = srv.update_record("diet", 6, {"kcal": 300.0})
    assert "dedup" not in r3
    posts = [c for c in api.paths() if c == ("POST", "/api/agent/update")]
    assert len(posts) == 2
    # 请求体走 /api/agent/update 且原样携参
    body = [b for m, p, b in api.calls if (m, p) == ("POST", "/api/agent/update")][0]
    assert body == {"type": "diet", "row_id": 5, "fields": {"kcal": 300.0}}


# ---------- query_metric_series：field 白名单 ----------

@pytest.mark.parametrize("bad", ["bmi", "weight", "体重", ""])
def test_query_metric_series_rejects_unknown_field(api, bad):
    with pytest.raises(srv.ApiError, match="白名单"):
        srv.query_metric_series(bad)
    assert api.calls == []  # 非法 field 不发任何请求


def test_query_metric_series_valid_field_passthrough(api):
    out = srv.query_metric_series("steps", days=7)
    assert out["field"] == "steps"
    assert api.calls == [("GET", "/api/agent/metrics/series", {"field": "steps", "days": "7"})]
    # strip 容错：带空白也走同一条白名单
    srv.query_metric_series(" weight_kg ")
    assert api.calls[-1][2]["field"] == "weight_kg"


def test_series_whitelist_superset_of_weight_fields():
    assert set(srv.WEIGHT_FIELDS) < set(srv.SERIES_FIELDS)
    assert "steps" in srv.SERIES_FIELDS


# ---------- _headers：缺 INGEST_TOKEN 直接报错，不发请求 ----------

def test_missing_ingest_token_raises(api, monkeypatch):
    monkeypatch.setattr(srv, "INGEST_TOKEN", "")
    with pytest.raises(srv.ApiError, match="INGEST_TOKEN"):
        srv.query_today_summary(TEST_DATE)
    assert api.calls == []
