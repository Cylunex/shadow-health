"""health schema 全量模型（设计文档 §3，M1 一次建全）。"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

SCHEMA = "health"
_tz_now = text("now()")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=_tz_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=_tz_now, onupdate=datetime.now
    )


# ---------- 3.1 身体指标 ----------
class BodyMetrics(TimestampMixin, Base):
    __tablename__ = "body_metrics"
    __table_args__ = (
        CheckConstraint("sleep_quality BETWEEN 1 AND 5", name="ck_sleep_quality"),
        CheckConstraint("energy_level BETWEEN 1 AND 5", name="ck_energy_level"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    log_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    weight_kg: Mapped[float | None] = mapped_column(Numeric(5, 2))
    body_fat_pct: Mapped[float | None] = mapped_column(Numeric(4, 1))
    muscle_mass_kg: Mapped[float | None] = mapped_column(Numeric(5, 2))
    skeletal_muscle_kg: Mapped[float | None] = mapped_column(Numeric(5, 2))
    bmr_kcal: Mapped[int | None] = mapped_column(Integer)
    body_water_kg: Mapped[float | None] = mapped_column(Numeric(5, 2))
    visceral_fat_level: Mapped[int | None] = mapped_column(Integer)
    waist_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    chest_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    arm_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    thigh_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    hip_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    bp_systolic: Mapped[int | None] = mapped_column(Integer)
    bp_diastolic: Mapped[int | None] = mapped_column(Integer)
    resting_hr: Mapped[int | None] = mapped_column(Integer)
    spo2_pct: Mapped[float | None] = mapped_column(Numeric(4, 1))
    sleep_hours: Mapped[float | None] = mapped_column(Numeric(3, 1))
    sleep_quality: Mapped[int | None] = mapped_column(Integer)
    morning_erection: Mapped[bool | None] = mapped_column(Boolean)
    energy_level: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    # 字段级来源标记 {"sleep_hours":"samsung_zip"}；自动回填只写 NULL 或 autofilled 内字段
    autofilled: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


# ---------- 3.2 每日活动与睡眠 ----------
class DailyActivity(Base):
    __tablename__ = "daily_activity"
    __table_args__ = ({"schema": SCHEMA},)

    log_date: Mapped[date] = mapped_column(Date, primary_key=True)
    steps: Mapped[int | None] = mapped_column(Integer)
    distance_m: Mapped[int | None] = mapped_column(Integer)
    active_kcal: Mapped[float | None] = mapped_column(Numeric(7, 1))
    hr_min: Mapped[int | None] = mapped_column(Integer)
    hr_avg: Mapped[int | None] = mapped_column(Integer)
    hr_max: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="samsung_zip")
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=_tz_now, onupdate=datetime.now
    )


class SleepSession(Base):
    __tablename__ = "sleep_sessions"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="ux_sleep_sessions_ext"),
        Index("ix_sleep_sessions_wake_date", "wake_date"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text)
    start_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    wake_date: Mapped[date] = mapped_column(Date, nullable=False)
    awake_min: Mapped[int | None] = mapped_column(Integer)
    light_min: Mapped[int | None] = mapped_column(Integer)
    deep_min: Mapped[int | None] = mapped_column(Integer)
    rem_min: Mapped[int | None] = mapped_column(Integer)
    total_sleep_min: Mapped[int | None] = mapped_column(Integer)


# ---------- 3.3 饮食与营养 ----------
class Food(Base):
    __tablename__ = "foods"
    __table_args__ = ({"schema": SCHEMA},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    category: Mapped[str | None] = mapped_column(Text)
    kcal_per_100g: Mapped[float | None] = mapped_column(Numeric(6, 1))
    protein_g: Mapped[float | None] = mapped_column(Numeric(5, 1))
    fat_g: Mapped[float | None] = mapped_column(Numeric(5, 1))
    carb_g: Mapped[float | None] = mapped_column(Numeric(5, 1))
    tcm_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=_tz_now
    )


class Recipe(Base):
    __tablename__ = "recipes"
    __table_args__ = ({"schema": SCHEMA},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    ingredients: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str | None] = mapped_column(Text)
    effects: Mapped[str | None] = mapped_column(Text)
    effect_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    contraindications: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=_tz_now
    )


class DietLog(TimestampMixin, Base):
    __tablename__ = "diet_logs"
    __table_args__ = (
        CheckConstraint("meal IN ('早餐','午餐','晚餐','加餐')", name="ck_meal"),
        CheckConstraint("food_id IS NOT NULL OR free_text IS NOT NULL", name="ck_diet_target"),
        Index("idx_diet_logs_date", "log_date"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    log_date: Mapped[date] = mapped_column(Date, nullable=False)
    meal: Mapped[str] = mapped_column(Text, nullable=False)
    food_id: Mapped[int | None] = mapped_column(ForeignKey(f"{SCHEMA}.foods.id"))
    free_text: Mapped[str | None] = mapped_column(Text)
    amount_g: Mapped[float | None] = mapped_column(Numeric(6, 1))
    kcal: Mapped[float | None] = mapped_column(Numeric(6, 1))
    protein_g: Mapped[float | None] = mapped_column(Numeric(5, 1))


class DietPhoto(Base):
    """餐次照片：按 (log_date, meal) 挂载，文件存 photo_dir，行内只记存储名。"""

    __tablename__ = "diet_photos"
    __table_args__ = (
        CheckConstraint("meal IN ('早餐','午餐','晚餐','加餐')", name="ck_photo_meal"),
        Index("idx_diet_photos_date", "log_date"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    log_date: Mapped[date] = mapped_column(Date, nullable=False)
    meal: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=_tz_now
    )


# ---------- 3.4 运动训练 ----------
class Exercise(Base):
    __tablename__ = "exercises"
    __table_args__ = ({"schema": SCHEMA},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    category: Mapped[str | None] = mapped_column(Text)
    progression_group: Mapped[str | None] = mapped_column(Text)
    level: Mapped[int] = mapped_column(SmallInteger, server_default="1")
    equipment: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=_tz_now
    )


class WorkoutPlan(Base):
    __tablename__ = "workout_plans"
    __table_args__ = ({"schema": SCHEMA},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    goal: Mapped[str | None] = mapped_column(Text)
    duration_weeks: Mapped[int | None] = mapped_column(Integer)  # NULL = 循环型计划
    source_doc: Mapped[str | None] = mapped_column(Text)
    content_md: Mapped[str | None] = mapped_column(Text)
    weekly_template: Mapped[list | None] = mapped_column(JSONB)
    phases: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=_tz_now
    )


class PlanEnrollment(TimestampMixin, Base):
    __tablename__ = "plan_enrollments"
    __table_args__ = (
        CheckConstraint("status IN ('active','done','abandoned')", name="ck_enroll_status"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.workout_plans.id"), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")


class WorkoutLog(TimestampMixin, Base):
    __tablename__ = "workout_logs"
    __table_args__ = (
        CheckConstraint(
            "source IN ('manual','keep','samsung_zip','health_connect','samsung_direct')",
            name="ck_workout_source",
        ),
        CheckConstraint("source = 'manual' OR external_id IS NOT NULL", name="ck_ext_required"),
        CheckConstraint("rpe BETWEEN 1 AND 10", name="ck_rpe"),
        # 部分唯一索引：manual 可多条（external_id NULL），外部源必带 id 且唯一
        Index(
            "ux_workout_logs_ext",
            "source",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index("idx_workout_logs_date", "log_date"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    log_date: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey(f"{SCHEMA}.workout_plans.id"))
    enrollment_id: Mapped[int | None] = mapped_column(ForeignKey(f"{SCHEMA}.plan_enrollments.id"))
    session_type: Mapped[str | None] = mapped_column(Text)
    duration_min: Mapped[int | None] = mapped_column(Integer)
    distance_km: Mapped[float | None] = mapped_column(Numeric(6, 2))
    calories: Mapped[int | None] = mapped_column(Integer)
    avg_hr: Mapped[int | None] = mapped_column(Integer)
    max_hr: Mapped[int | None] = mapped_column(Integer)
    rpe: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict | list | None] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual")
    external_id: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


# ---------- 3.5 养生任务打卡 ----------
class Habit(Base):
    __tablename__ = "habits"
    __table_args__ = (
        CheckConstraint("period IN ('daily','weekly')", name="ck_habit_period"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    period: Mapped[str] = mapped_column(Text, nullable=False, server_default="daily")
    target_per_period: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    time_hint: Mapped[str | None] = mapped_column(Text)
    auto_rule: Mapped[str | None] = mapped_column(Text)  # 如 'steps>=8000'
    source_doc: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sort: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=_tz_now
    )


class HabitLog(TimestampMixin, Base):
    __tablename__ = "habit_logs"
    __table_args__ = (
        UniqueConstraint("habit_id", "log_date", name="ux_habit_logs"),
        CheckConstraint("done_count > 0", name="ck_done_count"),
        Index("idx_habit_logs_date", "log_date"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    habit_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.habits.id"), nullable=False)
    log_date: Mapped[date] = mapped_column(Date, nullable=False)
    done_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class WeeklyReview(TimestampMixin, Base):
    __tablename__ = "weekly_reviews"
    __table_args__ = (
        CheckConstraint("extract(isodow FROM week_start) = 1", name="ck_week_monday"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    summary: Mapped[str | None] = mapped_column(Text)
    metrics_snapshot: Mapped[dict | None] = mapped_column(JSONB)


# ---------- 3.6 设置与导入基础设施 ----------
class AppSetting(Base):
    __tablename__ = "app_settings"
    __table_args__ = ({"schema": SCHEMA},)

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict | list | str | int | float | None] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=_tz_now, onupdate=datetime.now
    )


class ImportRaw(Base):
    __tablename__ = "import_raw"
    __table_args__ = (
        CheckConstraint(
            "source IN ('samsung_zip','health_connect','keep_api','keep_file',"
            "'miscale','samsung_direct')",
            name="ck_import_source",
        ),
        CheckConstraint(
            "parse_status IN ('pending','parsed','failed','skipped')", name="ck_parse_status"
        ),
        UniqueConstraint("source", "record_type", "external_id", name="ux_import_raw_ext"),
        Index(
            "ix_import_raw_replay",
            "source",
            "record_type",
            postgresql_where=text("parse_status IN ('pending','failed')"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    record_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    blob: Mapped[dict | list | None] = mapped_column(JSONB)
    time_offset: Mapped[str | None] = mapped_column(Text)
    parse_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    parse_error: Mapped[str | None] = mapped_column(Text)
    parse_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    imported_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=_tz_now
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class SyncState(Base):
    __tablename__ = "sync_state"
    __table_args__ = ({"schema": SCHEMA},)

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    needs_reauth: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    watermark: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','done','failed')", name="ck_import_job_status"
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    total: Mapped[int | None] = mapped_column(Integer)
    inserted: Mapped[int] = mapped_column(Integer, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, server_default="0")
    report: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
