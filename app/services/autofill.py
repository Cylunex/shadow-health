"""body_metrics 字段级自动回填（设计文档 §3.1）。

规则：同步/导入任务仅允许更新「字段 IS NULL 或 autofilled 里登记过来源」的字段，
写入时登记 {"字段名": "来源"}；用户手动保存某字段时调用 mark_manual 移除登记。
手动值永不被覆盖、重放幂等、上游修正可传播。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BodyMetrics


def get_or_create_day(db: Session, log_date: date) -> BodyMetrics:
    row = db.execute(
        select(BodyMetrics).where(BodyMetrics.log_date == log_date)
    ).scalar_one_or_none()
    if row is None:
        row = BodyMetrics(log_date=log_date, autofilled={})
        db.add(row)
        db.flush()
    return row


def autofill_fields(db: Session, log_date: date, source: str, values: dict[str, Any]) -> list[str]:
    """回填一批字段，返回实际写入的字段名。"""
    row = get_or_create_day(db, log_date)
    autofilled = dict(row.autofilled or {})
    written: list[str] = []
    for field, value in values.items():
        if value is None:
            continue
        current = getattr(row, field)
        if current is None or field in autofilled:
            setattr(row, field, value)
            autofilled[field] = source
            written.append(field)
    if written:
        row.autofilled = autofilled
    return written


def mark_manual(row: BodyMetrics, fields: list[str]) -> None:
    """用户手动保存字段后调用：解除自动登记，此后同步不再触碰这些字段。"""
    autofilled = dict(row.autofilled or {})
    changed = False
    for field in fields:
        if autofilled.pop(field, None) is not None:
            changed = True
    if changed or row.autofilled != autofilled:
        row.autofilled = autofilled
