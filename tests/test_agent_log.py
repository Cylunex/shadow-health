"""/agent-log 撤销链路与列表视图（V5 Agent 深度使用批次测试补锁）。

对象：app/routers/agent_log.py —— V5 改造后的撤销定位（blob.row_id 优先直查、
内容匹配退化为老留档兜底）、revoke 端点（blob 合并/幂等/类型白名单）、
列表视图参数（_list_params 非法值与钳制）与筛选分页（_status_ctx limit+1 探测）。

结构照 test_agent_channel.py：前半纯函数直测；后半 DB 集成（Mac 临时 PG
55433），数据库不可达或 INGEST_TOKEN 未配置自动跳过。测试日期用 today-340
（错开 offline 的 -300、agent_channel 的 -320、其它批次的 -350 与 2020-01/02）。

登录：/agent-log 挂 require_login（session cookie），仓库没有现成 login
fixture——auth 的 session 表在进程内存（app.auth._sessions），测试同进程直接
auth.create_session() 签发合法 token 塞进 TestClient cookie，不依赖明文密码。
清理铁律：留档只按本轮写入的 external_id 精确删，绝不整删 source='agent'。
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.routers.agent_log import (
    LIST_LIMIT,
    LIST_LIMIT_MAX,
    _list_params,
    _resolve_row_id,
    _status_ctx,
)


# ---------- 纯函数：_list_params（t 非法当全部 / n 越界钳制） ----------

def _req(qs: str = ""):
    from fastapi import Request
    return Request({"type": "http", "query_string": qs.encode(), "headers": []})


@pytest.mark.parametrize("qs,expected", [
    ("", ""),                    # 缺省 = 全部
    ("t=diet", "diet"),
    ("t=workout", "workout"),
    ("t=metric", "metric"),
    ("t=habit", "habit"),
    ("t=bogus", ""),             # 非法值当全部
    ("t=DIET", ""),              # 大小写敏感（词表精确匹配）
    ("t=%20diet%20", "diet"),    # 前后空白 strip 后仍在词表
])
def test_list_params_type_filter(qs, expected):
    assert _list_params(_req(qs))[0] == expected


@pytest.mark.parametrize("qs,expected", [
    ("", LIST_LIMIT),            # 缺省 30
    ("n=", LIST_LIMIT),
    ("n=abc", LIST_LIMIT),       # 非整数回退缺省
    ("n=2.5", LIST_LIMIT),
    ("n=50", 50),
    ("n=1", 1),
    ("n=0", 1),                  # 下限钳 1
    ("n=-5", 1),
    ("n=300", LIST_LIMIT_MAX),
    ("n=301", LIST_LIMIT_MAX),   # 上限钳 300
    ("n=99999", LIST_LIMIT_MAX),
])
def test_list_params_limit_clamped(qs, expected):
    assert _list_params(_req(qs))[1] == expected


def test_list_params_combined():
    assert _list_params(_req("t=diet&n=999")) == ("diet", LIST_LIMIT_MAX)


# ---------- DB 集成（不可达自动跳过） ----------

def _test_date() -> date:
    from app.timeutil import today_local
    return today_local() - timedelta(days=340)


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
    """Bearer 写通道（造数用）：与 test_agent_channel 同款。"""
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


@pytest.fixture(scope="module")
def web():
    """带 session cookie 的页面客户端：进程内签发 token（require_login 放行）。"""
    if not _db_ready():
        pytest.skip("临时 PG 不可达（uv run docker/pg 未启动）")
    from fastapi.testclient import TestClient

    from app import auth
    from app.main import app
    token = auth.create_session()
    with TestClient(app) as c:
        c.cookies.set(auth.SESSION_COOKIE, token)
        yield c
    auth.destroy_session(token)


@pytest.fixture()
def db():
    if not _db_ready():
        pytest.skip("临时 PG 不可达（uv run docker/pg 未启动）")
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

    habit = Habit(name=f"测试-agentlog-{uuid.uuid4().hex[:8]}", period="daily", target_per_period=1)
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


def _get_raw(db, ext_id: str):
    from sqlalchemy import select

    from app.models import ImportRaw
    return db.execute(select(ImportRaw).where(
        ImportRaw.source == "agent", ImportRaw.external_id == ext_id
    )).scalar_one()


REVOKE_DENIED = "这条记录不能撤销"


# ---- _resolve_row_id：blob.row_id 优先 / 老留档内容匹配兜底 ----

def test_resolve_row_id_prefers_blob_row_id(client, db, agent_env):
    """V5 起 ingest 把归一化行 id 写进留档 blob：_resolve_row_id 按 id 直查；
    该行删掉后返回 None（不落回内容匹配——内容此时其实还能对上）。"""
    from app.models import DietLog

    rec = _rec("diet", {"meal": "午餐", "free_text": "blob定位牛肉面", "kcal": 550})
    j = client.post("/api/ingest/agent", json={"records": [rec]}).json()
    row_id = j["results"][0]["row_id"]
    assert isinstance(row_id, int)

    r = _get_raw(db, f"diet-{rec['client_id']}")
    assert r.parse_status == "parsed"
    assert r.blob["row_id"] == row_id           # 回执 id 同步落进留档 blob
    assert _resolve_row_id(db, r) == row_id     # 优先按 blob.row_id 直查

    db.delete(db.get(DietLog, row_id))
    db.flush()
    assert _resolve_row_id(db, r) is None       # 行已删 → None（视为已撤销）


def test_resolve_row_id_legacy_content_match(db, agent_env):
    """老留档（无 blob.row_id）兜底：free_text 留档按解析后全字段内容匹配命中；
    food_id 留档营养值是服务端算的，内容匹配对不上 → 一律返回 None（视为已撤销）。"""
    from app.models import DietLog, ImportRaw
    from app.timeutil import now_local

    cid = str(uuid.uuid4())
    ext = f"diet-{cid}"
    _SENT_EXT_IDS.append(ext)
    db.execute(ImportRaw.__table__.insert().values(
        source="agent", record_type="diet", external_id=ext,
        raw={"type": "diet", "client_id": cid, "date": TEST_DATE.isoformat(),
             "payload": {"meal": "午餐", "free_text": "老留档牛肉面", "kcal": 550}},
        parse_status="parsed", parse_version=1, last_seen_at=now_local(),
    ))
    # 对应的归一化行：列值与 parse_diet_payload 同口径（kcal 量化到 0.1，其余留空）
    log = DietLog(log_date=TEST_DATE, meal="午餐", free_text="老留档牛肉面",
                  kcal=Decimal("550.0"))
    db.add(log)
    db.commit()

    r = _get_raw(db, ext)
    assert r.blob is None                       # 老留档：没有 row_id 可直查
    assert _resolve_row_id(db, r) == log.id     # 同日同餐同文本同数值 → 内容匹配命中

    cid2 = str(uuid.uuid4())
    ext2 = f"diet-{cid2}"
    _SENT_EXT_IDS.append(ext2)
    db.execute(ImportRaw.__table__.insert().values(
        source="agent", record_type="diet", external_id=ext2,
        raw={"type": "diet", "client_id": cid2, "date": TEST_DATE.isoformat(),
             "payload": {"meal": "午餐", "food_id": 999999}},
        parse_status="parsed", parse_version=1, last_seen_at=now_local(),
    ))
    db.commit()
    assert _resolve_row_id(db, _get_raw(db, ext2)) is None


# ---- POST /agent-log/revoke：blob 合并 / 行真删 / 幂等 / 类型白名单 ----

def test_revoke_merges_blob_deletes_row_and_is_idempotent(client, web, db, agent_env):
    from app.models import DietLog

    rec = _rec("diet", {"meal": "加餐", "free_text": "记错的蛋糕", "kcal": 400})
    j = client.post(
        "/api/ingest/agent", json={"records": [rec], "agent_name": "pytest-agent"}
    ).json()
    row_id = j["results"][0]["row_id"]
    r = _get_raw(db, f"diet-{rec['client_id']}")
    raw_id = r.id
    assert r.blob == {"row_id": row_id, "agent": "pytest-agent"}

    resp = web.post("/agent-log/revoke", data={"raw_id": str(raw_id)})
    assert resp.status_code == 200
    assert REVOKE_DENIED not in resp.text
    # 撤销后的片段把该行渲染成「已撤销」（我这条最新，必在默认 30 条内）
    assert "记错的蛋糕" in resp.text and "已撤销" in resp.text

    db.rollback()
    db.expire_all()
    r = _get_raw(db, f"diet-{rec['client_id']}")
    assert r.blob["row_id"] == row_id               # 合并写：既有键原样保留
    assert r.blob["agent"] == "pytest-agent"
    assert r.blob["revoked_row_id"] == row_id       # 新增撤销标记
    revoked_at = r.blob["revoked_at"]
    assert revoked_at
    assert db.get(DietLog, row_id) is None          # 归一化行真删

    # 幂等：已撤销的再撤一次不报错、不再走删除（blob 原样，revoked_at 不变）
    resp2 = web.post("/agent-log/revoke", data={"raw_id": str(raw_id)})
    assert resp2.status_code == 200
    assert REVOKE_DENIED not in resp2.text
    db.rollback()
    db.expire_all()
    r = _get_raw(db, f"diet-{rec['client_id']}")
    assert r.blob["revoked_at"] == revoked_at
    assert r.blob["revoked_row_id"] == row_id


def test_revoke_rejects_habit_and_metric(client, web, db, agent_env):
    """habit（声明式打卡）/metric（覆盖写）不支持撤销：报错文案且归一化行原样。"""
    from sqlalchemy import select

    from app.models import BodyMetrics, HabitLog

    habit = agent_env
    recs = [
        _rec("habit", {"habit_id": habit.id}),
        _rec("metric", {"mood_score": 5}),
    ]
    j = client.post("/api/ingest/agent", json={"records": recs}).json()
    assert j["new"] == 2
    db.rollback()
    for rec in recs:
        raw_id = _get_raw(db, f"{rec['type']}-{rec['client_id']}").id
        resp = web.post("/agent-log/revoke", data={"raw_id": str(raw_id)})
        assert resp.status_code == 200
        assert REVOKE_DENIED in resp.text

    # 归一化行未被动过
    db.rollback()
    db.expire_all()
    assert db.execute(select(HabitLog).where(
        HabitLog.habit_id == habit.id, HabitLog.log_date == TEST_DATE
    )).scalar_one() is not None
    bm = db.execute(
        select(BodyMetrics).where(BodyMetrics.log_date == TEST_DATE)
    ).scalar_one()
    assert bm.mood_score == 5

    # 非法/不存在的 raw_id 同样报不能撤销（不 500）
    assert REVOKE_DENIED in web.post("/agent-log/revoke", data={"raw_id": "x"}).text
    assert REVOKE_DENIED in web.post("/agent-log/revoke", data={"raw_id": "99999999"}).text


# ---- _status_ctx：类型筛选 / limit+1 探测 has_more ----

def test_status_ctx_type_filter(client, db, agent_env):
    recs = [
        _rec("diet", {"meal": "早餐", "free_text": "筛选测试鸡蛋"}),
        _rec("diet", {"meal": "午餐", "free_text": "筛选测试面"}),
        _rec("workout", {"session_type": "跑步", "duration_min": 30}),
    ]
    client.post("/api/ingest/agent", json={"records": recs})
    db.rollback()
    my_diet_ids = {_get_raw(db, f"diet-{r['client_id']}").id for r in recs[:2]}
    workout_raw_id = _get_raw(db, f"workout-{recs[2]['client_id']}").id

    ctx = _status_ctx(db, rtype="diet", limit=10)
    assert ctx["rtype"] == "diet" and ctx["limit"] == 10
    assert all(i["type_label"] == "饮食" for i in ctx["items"])   # 只剩 diet 项
    ids = [i["raw_id"] for i in ctx["items"]]
    assert set(ids[:2]) == my_diet_ids     # id desc：刚写入的两条排最前
    assert workout_raw_id not in ids       # workout 被筛掉


def test_status_ctx_has_more_probe(client, db, agent_env):
    """limit+1 探测：刚好取完 has_more=False，还有下一页 has_more=True。"""
    from sqlalchemy import func, select

    from app.models import ImportRaw

    client.post("/api/ingest/agent", json={"records": [
        _rec("diet", {"meal": "午餐", "free_text": f"分页测试-{i}"}) for i in range(3)
    ]})
    db.rollback()
    total = db.execute(
        select(func.count()).select_from(ImportRaw).where(ImportRaw.source == "agent")
    ).scalar_one()
    assert total >= 3

    ctx = _status_ctx(db, limit=1)
    assert len(ctx["items"]) == 1 and ctx["has_more"] is True

    ctx = _status_ctx(db, limit=total - 1)
    assert len(ctx["items"]) == total - 1 and ctx["has_more"] is True

    ctx = _status_ctx(db, limit=total)     # 恰好取完：多取的 1 条落空
    assert len(ctx["items"]) == total and ctx["has_more"] is False


# ---- 登录守卫 ----

def test_agent_log_requires_login(web):
    """匿名 303 去 /login；带 session cookie 能打开页面与轮询片段。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, follow_redirects=False) as anon:
        r = anon.get("/agent-log")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert anon.post("/agent-log/revoke", data={"raw_id": "1"}).status_code == 303

    assert web.get("/agent-log?t=diet&n=5").status_code == 200
    assert web.get("/fragments/agent-log/status").status_code == 200
