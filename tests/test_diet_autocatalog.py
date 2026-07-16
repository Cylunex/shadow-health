"""V8.1：free_text 新食物自动进食物库（_auto_catalog_food）+ 三通道接线。

全部 DB 集成（Mac 临时 PG 55433，不可达自动跳过）。测试日期用 2020-04
（错开已占用的 today-300/-320/-340/-350/-360 与 2020-01/02/03）；
食物/记录按本轮写入的名字与 id 精确清理，不整删。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.routers.diet import _auto_catalog_food

D1 = date(2020, 4, 1)
D2 = date(2020, 4, 2)


def _db_ready() -> bool:
    try:
        from sqlalchemy import text

        from app.db import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture()
def db():
    if not _db_ready():
        pytest.skip("临时 PG 不可达")
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def cleanup(db):
    """收尾按名字精确删本轮建的食物 + 按日期删测试日的饮食记录。"""
    names: list[str] = []
    yield names
    from sqlalchemy import delete

    from app.models import DietLog, Food
    db.rollback()
    db.execute(delete(DietLog).where(DietLog.log_date.in_([D1, D2])))
    if names:
        db.execute(delete(Food).where(Food.name.in_(names)))
    db.commit()


def _food(db, name):
    from sqlalchemy import select

    from app.models import Food
    return db.execute(select(Food).where(Food.name == name)).scalar_one_or_none()


# ---------- 折算与门槛 ----------

def test_catalog_converts_to_per_100g(db, cleanup):
    name = "测试香辣肉丝"
    cleanup.append(name)
    # 午餐示例口径：131g / 420 kcal / 蛋白22 脂肪34 碳水10
    assert _auto_catalog_food(db, name, Decimal("131"), Decimal("420"),
                              Decimal("22"), Decimal("34"), Decimal("10")) is True
    f = _food(db, name)
    assert f is not None
    assert float(f.kcal_per_100g) == pytest.approx(320.6, abs=0.05)
    assert float(f.protein_g) == pytest.approx(16.8, abs=0.05)
    assert float(f.fat_g) == pytest.approx(26.0, abs=0.05)
    assert float(f.carb_g) == pytest.approx(7.6, abs=0.05)
    assert "自动建档" in (f.notes or "")


def test_catalog_accepts_float_inputs(db, cleanup):
    # AI 餐照路径传 float，不能因 Decimal×float 混算炸掉
    name = "测试毛豆鸡丁"
    cleanup.append(name)
    assert _auto_catalog_food(db, name, 50.0, 115.0, 9.5, 6.5, 6.5) is True
    f = _food(db, name)
    assert float(f.kcal_per_100g) == pytest.approx(230.0, abs=0.05)


def test_catalog_skips_existing_name(db, cleanup):
    name = "测试炖牛肉"
    cleanup.append(name)
    assert _auto_catalog_food(db, name, 57, 135, 14.5, 7.5, 2.5) is True
    before = float(_food(db, name).kcal_per_100g)
    # 重名不覆盖：第二次不同数值也不动原值
    assert _auto_catalog_food(db, name, 100, 999, 1, 1, 1) is False
    assert float(_food(db, name).kcal_per_100g) == before


@pytest.mark.parametrize("kwargs", [
    {"amount_g": None, "kcal": 420},           # 缺克数折算不出每100g
    {"amount_g": 131, "kcal": None},           # 缺热量没有复用价值
    {"amount_g": 0, "kcal": 420},              # 0 克
    {"amount_g": 10, "kcal": 420},             # 4200 kcal/100g 超生理上限
    {"amount_g": 100, "kcal": 400, "protein_g": 150},  # 单宏量 >100g/100g
])
def test_catalog_guards(db, kwargs):
    args = {"protein_g": None, "fat_g": None, "carb_g": None, **kwargs}
    assert _auto_catalog_food(db, "测试门槛食物", args["amount_g"], args["kcal"],
                              args["protein_g"], args["fat_g"], args["carb_g"]) is False
    assert _food(db, "测试门槛食物") is None


def test_catalog_rejects_sentence_names(db, cleanup):
    long_name = "中午在单位食堂吃了一大碗香辣肉丝盖浇饭还有汤"  # 22 字 >20 视为整句
    cleanup.append(long_name)  # 防实现回归时残留
    assert _auto_catalog_food(db, long_name, 300, 500) is False
    assert _food(db, long_name) is None


# ---------- 通道接线 ----------

@pytest.fixture()
def page(db):
    from fastapi.testclient import TestClient

    from app import auth
    from app.main import app

    token = auth.create_session()
    with TestClient(app) as c:
        c.cookies.set(auth.SESSION_COOKIE, token)
        yield c


def test_ui_free_text_create_catalogs(db, page, cleanup):
    name = "测试凉拌肚丝"
    cleanup.append(name)
    resp = page.post("/diet/logs", data={
        "log_date": D1.isoformat(), "meal": "午餐", "free_text": name,
        "amount_g": "50", "kcal": "105", "protein_g": "9.5",
        "fat_g": "7", "carb_g": "1.5",
    })
    assert resp.status_code == 200
    assert "新食物已入库" in resp.text
    db.expire_all()
    f = _food(db, name)
    assert f is not None
    assert float(f.kcal_per_100g) == pytest.approx(210.0, abs=0.05)


def test_ui_free_text_without_macros_no_catalog(db, page, cleanup):
    name = "测试白开水"
    cleanup.append(name)  # 防实现回归时残留
    resp = page.post("/diet/logs", data={
        "log_date": D1.isoformat(), "meal": "加餐", "free_text": name,
    })
    assert resp.status_code == 200
    assert "新食物已入库" not in resp.text
    db.expire_all()
    assert _food(db, name) is None


def test_offline_channel_catalogs(db, cleanup):
    from app.routers.offline import _normalize_diet

    name = "测试杏鲍菇炒肉"
    cleanup.append(name)
    row_id = _normalize_diet(db, {
        "meal": "午餐", "free_text": name, "amount_g": 60,
        "kcal": 125, "protein_g": 6.5, "fat_g": 9.5, "carb_g": 4.5,
    }, D2)
    assert row_id is not None
    db.commit()
    f = _food(db, name)
    assert f is not None
    assert float(f.kcal_per_100g) == pytest.approx(208.3, abs=0.05)
