"""app_settings seed（设计文档 §5.6：预置默认值，已存在的 key 跳过）。

- target_steps=8000（源 06）、target_weekly_cardio_min=150（源 04 计划一）
- target_protein_g = 最近体重 × 1.8（无体重记录默认 130；录入体重后设置页提示重算）
- target_kcal / target_weight_kg / height_cm 预置 JSON null：
  target_kcal 留空提示手填（维持热量因人而异）；height_cm 由三星导入回填。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import AppSetting, BodyMetrics

# app_settings.value NOT NULL——"未设定"用 JSON null 表达
_JSONB_NULL = text("'null'::jsonb")


def _default_protein(db: Session) -> int:
    last_weight = db.execute(
        select(BodyMetrics.weight_kg)
        .where(BodyMetrics.weight_kg.is_not(None))
        .order_by(BodyMetrics.log_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    return round(float(last_weight) * 1.8) if last_weight is not None else 130


def seed(db: Session) -> int:
    """幂等 seed：返回新插入条数（已存在的 key 跳过，人工改过的值不回滚）。"""
    defaults: dict[str, Any] = {
        "target_steps": 8000,
        "target_weekly_cardio_min": 150,
        "target_protein_g": _default_protein(db),
        "target_kcal": None,       # 留空提示手填
        "target_weight_kg": None,  # 用户自设
        "height_cm": None,         # 三星导入回填
    }
    inserted = 0
    for key, value in defaults.items():
        new_key = db.execute(
            pg_insert(AppSetting)
            .values(key=key, value=_JSONB_NULL if value is None else value)
            .on_conflict_do_nothing(index_elements=["key"])
            .returning(AppSetting.key)
        ).scalar_one_or_none()
        if new_key is not None:
            inserted += 1
    return inserted
