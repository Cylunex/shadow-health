"""DietLog.notes 的 UI、食物关联、复制与组合模板契约。"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.diet_notes import DIET_NOTES_MAX_LENGTH, parse_diet_notes

D1 = date(2020, 5, 1)
D2 = date(2020, 5, 2)
D3 = date(2020, 5, 3)


def test_parse_diet_notes_normalizes_empty_and_rejects_oversized() -> None:
    assert parse_diet_notes(None) is None
    assert parse_diet_notes("  ") is None
    assert parse_diet_notes("  少油，米饭吃了一半  ") == "少油，米饭吃了一半"
    with pytest.raises(ValueError, match="必须是文本"):
        parse_diet_notes({"unexpected": "object"})
    with pytest.raises(ValueError, match="1000"):
        parse_diet_notes("x" * (DIET_NOTES_MAX_LENGTH + 1))


def _db_ready() -> bool:
    try:
        from sqlalchemy import text

        from app.db import engine
        with engine.connect() as connection:
            connection.execute(text("SELECT notes FROM health.diet_logs LIMIT 0"))
        return True
    except Exception:
        return False


@pytest.fixture()
def db():
    if not _db_ready():
        pytest.skip("临时 PG 不可达或尚未升级到 DietLog.notes 迁移")
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def page(db, sso_headers):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        client.headers.update(sso_headers)
        yield client


@pytest.fixture()
def note_env(db):
    from sqlalchemy import delete

    from app.models import DietLog, Food, MealTemplate

    token = uuid.uuid4().hex[:8]
    food = Food(
        name=f"备注测试食物-{token}",
        category="测试",
        kcal_per_100g=Decimal("120"),
        protein_g=Decimal("8"),
        fat_g=Decimal("3"),
        carb_g=Decimal("15"),
    )
    template_name = f"备注测试组合-{token}"
    db.add(food)
    db.commit()
    yield food, template_name
    db.rollback()
    db.execute(delete(DietLog).where(DietLog.log_date.in_([D1, D2, D3])))
    db.execute(delete(MealTemplate).where(MealTemplate.name == template_name))
    db.execute(delete(Food).where(Food.id == food.id))
    db.commit()


def test_ui_create_edit_and_display_notes_for_free_text_and_food_id(db, page, note_env) -> None:
    from sqlalchemy import select

    from app.models import DietLog, Food

    food, _ = note_env
    free_name = f"自由备注餐-{uuid.uuid4().hex[:6]}"
    free_response = page.post("/diet/logs", data={
        "log_date": D1.isoformat(),
        "meal": "午餐",
        "free_text": free_name,
        "notes": "小份，热量按包装估算",
    })
    food_response = page.post("/diet/logs", data={
        "log_date": D1.isoformat(),
        "meal": "午餐",
        "food_id": str(food.id),
        "q": food.name,
        "amount_g": "180",
        "notes": "称重为熟重",
    })
    assert free_response.status_code == food_response.status_code == 200
    db.expire_all()
    free_log = db.execute(
        select(DietLog).where(DietLog.log_date == D1, DietLog.free_text == free_name)
    ).scalar_one()
    food_log = db.execute(
        select(DietLog).where(DietLog.log_date == D1, DietLog.food_id == food.id)
    ).scalar_one()
    assert free_log.notes == "小份，热量按包装估算"
    assert food_log.free_text is None and food_log.notes == "称重为熟重"
    exported = page.get("/settings/export?table=diet_logs")
    assert exported.status_code == 200
    csv_text = exported.content.decode("utf-8-sig")
    assert "notes" in csv_text.splitlines()[0].split(",")
    assert "小份，热量按包装估算" in csv_text

    updated = page.put(f"/diet/logs/{food_log.id}", data={
        "meal": "晚餐",
        "amount_g": "150",
        "notes": "修正为净重 150g",
    })
    assert updated.status_code == 200
    assert "备注：修正为净重 150g" in updated.text
    db.expire_all()
    assert db.get(DietLog, food_log.id).notes == "修正为净重 150g"
    assert db.get(Food, food.id).name.startswith("备注测试食物-")


def test_meal_copy_and_template_round_trip_notes(db, page, note_env) -> None:
    from sqlalchemy import select

    from app.models import DietLog, MealTemplate

    food, template_name = note_env
    db.add_all([
        DietLog(
            log_date=D1, meal="早餐", food_id=food.id, amount_g=Decimal("100"),
            kcal=Decimal("120"), protein_g=Decimal("8"), fat_g=Decimal("3"),
            carb_g=Decimal("15"), notes="食物库行备注",
        ),
        DietLog(
            log_date=D1, meal="早餐", free_text="自由记录粥", kcal=Decimal("180"),
            notes="只喝了半碗",
        ),
    ])
    db.commit()

    copied = page.post("/diet/meals/copy", data={"d": D2.isoformat(), "meal": "早餐"})
    assert copied.status_code == 200
    db.expire_all()
    copied_rows = db.execute(
        select(DietLog).where(DietLog.log_date == D2).order_by(DietLog.id)
    ).scalars().all()
    assert [row.notes for row in copied_rows] == ["食物库行备注", "只喝了半碗"]

    saved = page.post("/diet/templates/save", data={
        "d": D1.isoformat(), "meal": "早餐", "name": template_name,
    })
    assert saved.status_code == 200
    template = db.execute(
        select(MealTemplate).where(MealTemplate.name == template_name)
    ).scalar_one()
    assert [item.get("notes") for item in template.items] == ["食物库行备注", "只喝了半碗"]

    logged = page.post(
        f"/diet/templates/{template.id}/log",
        data={"d": D3.isoformat(), "meal": "早餐"},
    )
    assert logged.status_code == 200
    db.expire_all()
    logged_rows = db.execute(
        select(DietLog).where(DietLog.log_date == D3).order_by(DietLog.id)
    ).scalars().all()
    assert [row.notes for row in logged_rows] == ["食物库行备注", "只喝了半碗"]
