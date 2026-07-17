"""scripts/fix_miscale_ts.py 存量修复口径锁（V8.4）。

场景：秤 RTC 是 UTC，早上 07:10 的测量被记成前一天 23:10 → 体重归到错的日期。
脚本 +8h 修正 raw.ts 并重算 body_metrics 归属；重跑幂等（ts_fixed 标记）。

DB 集成（不可达自动跳过）；测试日期用 2020-05（错开 2020-01/02/03/04 与
today-3xx 系）；数据按本轮写入的 external_id / 日期精确清理。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fix_miscale_ts.py"
spec = importlib.util.spec_from_file_location("fix_miscale_ts", _SCRIPT)
fixer = importlib.util.module_from_spec(spec)
sys.modules["fix_miscale_ts"] = fixer
spec.loader.exec_module(fixer)

D_WRONG = date(2020, 5, 3)   # UTC 钟下被错误归属的日期（前一天）
D_RIGHT = date(2020, 5, 4)   # 修正后的正确日期
EXT_ID = "20200503T231000-14300"


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
def seeded(db):
    """种一条 UTC 钟的测量 raw + 错误日期上的 miscale 归属行。"""
    from sqlalchemy import delete

    from app.models import BodyMetrics, ImportRaw

    def _cleanup():
        db.rollback()
        db.execute(delete(ImportRaw).where(
            ImportRaw.source == "miscale", ImportRaw.external_id == EXT_ID))
        db.execute(delete(BodyMetrics).where(
            BodyMetrics.log_date.in_([D_WRONG, D_RIGHT])))
        db.commit()

    _cleanup()  # 上轮失败残留先清
    db.add(ImportRaw(
        source="miscale", record_type="measurement", external_id=EXT_ID,
        raw={"ts": "2020-05-03T23:10:00", "weight_kg": 71.5, "impedance": None},
        parse_status="parsed", parse_version=1,
    ))
    db.add(BodyMetrics(
        log_date=D_WRONG, weight_kg=71.5, autofilled={"weight_kg": "miscale"},
    ))
    db.commit()
    yield
    _cleanup()


def test_fix_moves_raw_and_metrics(db, seeded):
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import BodyMetrics, ImportRaw

    moved = fixer.shift_raw_ts(db, timedelta(hours=8), None)
    assert moved >= 1
    cleared, filled = fixer.rebuild_metrics(db)
    db.commit()
    db.expire_all()

    raw = db.execute(select(ImportRaw).where(
        ImportRaw.source == "miscale", ImportRaw.external_id == EXT_ID
    )).scalar_one()
    assert raw.raw["ts"] == "2020-05-04T07:10:00"   # +8h 落在正确日期的早晨
    assert "ts_fixed" in raw.raw
    assert raw.external_id == EXT_ID                 # 历史去重键不动

    wrong = db.execute(select(BodyMetrics).where(BodyMetrics.log_date == D_WRONG)).scalar_one()
    assert wrong.weight_kg is None                   # 旧日期归属被清
    assert "weight_kg" not in (wrong.autofilled or {})
    right = db.execute(select(BodyMetrics).where(BodyMetrics.log_date == D_RIGHT)).scalar_one()
    assert float(right.weight_kg) == 71.5            # 新日期回填
    assert right.autofilled.get("weight_kg") == "miscale"


def test_fix_rerun_is_idempotent(db, seeded):
    from datetime import timedelta

    fixer.shift_raw_ts(db, timedelta(hours=8), None)
    fixer.rebuild_metrics(db)
    db.commit()
    # 重跑：ts_fixed 标记挡住二次移动
    assert fixer.shift_raw_ts(db, timedelta(hours=8), None) == 0


def test_since_excludes_older_rows(db, seeded):
    from datetime import timedelta

    # since 晚于测量日期 → 不动
    assert fixer.shift_raw_ts(db, timedelta(hours=8), date(2020, 6, 1)) == 0
