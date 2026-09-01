"""统一的当日有效活动消耗口径。

DailyActivity 通常已包含手表汇总的训练消耗，因此绝不能与 WorkoutLog 直接相加。
当日汇总缺失或小于已记录训练时取两者较大值，既补齐 Agent/手动训练，也避免双算。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DailyActivity, WorkoutLog

MAX_ACTIVE_KCAL_PER_DAY = 20_000.0


@dataclass(frozen=True, slots=True)
class EffectiveActivityEnergy:
    kcal: float | None
    source: Literal[
        "daily_activity", "workouts_higher", "workouts_fallback", "none"
    ]
    daily_activity_kcal: float | None
    workout_kcal: float | None
    used_fallback: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _valid_kcal(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0 or number > MAX_ACTIVE_KCAL_PER_DAY:
        return None
    return number


def _resolve(daily: object, workouts: list[object]) -> EffectiveActivityEnergy:
    daily_kcal = _valid_kcal(daily)
    valid_workouts = [value for raw in workouts if (value := _valid_kcal(raw)) is not None]
    workout_total_raw = sum(valid_workouts)
    workout_kcal = _valid_kcal(workout_total_raw) if valid_workouts else None
    if daily_kcal is not None and (workout_kcal is None or daily_kcal >= workout_kcal):
        return EffectiveActivityEnergy(
            daily_kcal, "daily_activity", daily_kcal, workout_kcal, False
        )
    if workout_kcal is not None and daily_kcal is not None:
        return EffectiveActivityEnergy(
            workout_kcal, "workouts_higher", daily_kcal, workout_kcal, False
        )
    if workout_kcal is not None:
        return EffectiveActivityEnergy(
            workout_kcal, "workouts_fallback", None, workout_kcal, True
        )
    return EffectiveActivityEnergy(None, "none", daily_kcal, workout_kcal, False)


def effective_activity_energy(db: Session, day: date) -> EffectiveActivityEnergy:
    daily = db.scalar(
        select(DailyActivity.active_kcal).where(DailyActivity.log_date == day)
    )
    workouts = list(
        db.scalars(select(WorkoutLog.calories).where(WorkoutLog.log_date == day))
    )
    return _resolve(daily, workouts)


def effective_activity_energy_map(
    db: Session, start: date, end: date
) -> dict[date, EffectiveActivityEnergy]:
    daily = {
        day: value
        for day, value in db.execute(
            select(DailyActivity.log_date, DailyActivity.active_kcal).where(
                DailyActivity.log_date.between(start, end)
            )
        )
    }
    workouts: dict[date, list[object]] = {}
    for day, value in db.execute(
        select(WorkoutLog.log_date, WorkoutLog.calories).where(
            WorkoutLog.log_date.between(start, end)
        )
    ):
        workouts.setdefault(day, []).append(value)
    return {
        day: _resolve(daily.get(day), workouts.get(day, []))
        for day in daily.keys() | workouts.keys()
    }
