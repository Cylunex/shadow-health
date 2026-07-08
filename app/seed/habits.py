"""habits seed（设计文档 §5.2：15 条，分层激活，ON CONFLICT (name) DO NOTHING 幂等）。

sort 按 time_hint 晨→日间→睡前→weekly 分段编号。
"""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Habit


def _h(
    name: str,
    *,
    period: str = "daily",
    target: int = 1,
    time_hint: str | None = None,
    auto_rule: str | None = None,
    source_doc: str | None = None,
    active: bool = True,
    sort: int = 0,
) -> dict:
    return {
        "name": name,
        "period": period,
        "target_per_period": target,
        "time_hint": time_hint,
        "auto_rule": auto_rule,
        "source_doc": source_doc,
        "active": active,
        "sort": sort,
    }


HABITS: list[dict] = [
    # ---- active=true：04 计划一基础 7 条 ----
    _h("晨起温水+提肛", time_hint="晨", source_doc="04#计划一", sort=10),
    _h("日间久坐起身", time_hint="日间", source_doc="04#计划一", sort=20),
    _h("睡前泡脚15分钟", time_hint="睡前", source_doc="04#计划一", sort=30),
    _h("按摩肾俞/涌泉", time_hint="睡前", source_doc="04#计划一", sort=31),
    _h("凯格尔", time_hint="睡前", source_doc="04#计划一", sort=32),
    _h("23点前睡", time_hint="睡前", source_doc="04#计划一", sort=33),
    _h("控烟酒", source_doc="04#计划一", sort=40),
    # ---- active=false：启动对应计划时 UI 提示一键激活（source_doc 关联） ----
    _h("放松呼吸/正念5-10分钟", source_doc="04#计划二", active=False, sort=50),
    _h("盆底早晚微练习", target=2, time_hint="早晚", source_doc="05§四", active=False, sort=51),
    _h("步数达标≥8000", auto_rule="steps>=8000", source_doc="06", active=False, sort=52),
    _h("饮水1.5-2L", source_doc="06§六", active=False, sort=53),
    # ---- weekly 类（active=false） ----
    _h("深海鱼×2", period="weekly", target=2, source_doc="04#计划一", active=False, sort=60),
    _h("牡蛎/贝类×1", period="weekly", target=1, source_doc="04#计划一", active=False, sort=61),
    _h("称重×2", period="weekly", target=2, source_doc="06§七", active=False, sort=62),
    _h("量腰围×1", period="weekly", target=1, source_doc="06§七", active=False, sort=63),
]


def seed(db: Session) -> int:
    """幂等 seed：返回新插入条数（已存在的按 name 冲突跳过，人工改过的内容不回滚）。

    用 RETURNING 精确统计新插入行（psycopg3 对 ON CONFLICT 的 rowcount 可能报 -1）。
    """
    inserted = 0
    for row in HABITS:
        new_id = db.execute(
            pg_insert(Habit)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["name"])
            .returning(Habit.id)
        ).scalar_one_or_none()
        if new_id is not None:
            inserted += 1
    return inserted
