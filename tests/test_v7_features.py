"""V7 批次回归锁：体测分档、化验超范围判定、条码归一化、OFF 导入解析、跑步判定。

纯函数直测为主 + 体测/化验两条 DB 集成链路（用 2020-03 错峰日期，
按本轮写入的键精确清理——铁律同 test_agent_channel）。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.routers.diet import _norm_barcode  # noqa: E402
from app.routers.fitness import ITEMS, level_label, score_item  # noqa: E402
from app.routers.labs import flag  # noqa: E402
from app.services.pr import _is_run  # noqa: E402

TEST_DAY = date(2020, 3, 1)  # 错峰日期（已占 today-300/-320/-340/-350/-360、2020-01/02）


# ---------- 纯函数 ----------

def test_fitness_score_anchors():
    assert score_item("pushup_max", 40) == 100  # 锚点=满分
    assert score_item("pushup_max", 20) == 50
    assert score_item("pushup_max", 0) == 0
    assert score_item("pushup_max", 999) == 100  # 超锚点钳 100
    # 坐位体前屈从 -10 起算量程：-10→0 分、+15→100 分
    assert score_item("sit_reach_cm", -10) == 0
    assert score_item("sit_reach_cm", 15) == 100
    assert score_item("sit_reach_cm", 2.5) == 50
    assert score_item("unknown_item", 50) == 0


def test_fitness_levels():
    assert level_label(85) == "优秀"
    assert level_label(50) == "良好"
    assert level_label(10) == "待提高"
    assert {i[0] for i in ITEMS} == {"pushup_max", "plank_sec", "sit_reach_cm", "hr_recovery"}


def test_lab_flag_directions():
    assert flag(6.0, None, 5.2) == "high"
    assert flag(0.8, 1.0, None) == "low"
    assert flag(4.5, 3.9, 6.1) is None
    assert flag(100.0, None, None) is None  # 无参考范围不判


def test_norm_barcode():
    assert _norm_barcode("6901234567892") == "6901234567892"
    assert _norm_barcode(" 69-0123 4567892 ") == "6901234567892"
    assert _norm_barcode("abc") == ""
    assert len(_norm_barcode("1" * 30)) == 14  # 截断


def test_is_run_keywords():
    assert _is_run("跑步") and _is_run("慢跑") and _is_run("Running")
    assert not _is_run("快走") and not _is_run(None) and not _is_run("力量")


def test_off_import_parsers():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from import_off_products import _dec, _is_cn

    assert _is_cn("6901234567892", "")            # 69 前缀
    assert _is_cn("4001234567890", "en:china")    # countries 命中
    assert not _is_cn("4001234567890", "en:germany")
    assert _dec("52.0", 900) is not None
    assert _dec("", 900) is None
    assert _dec("nan", 900) is None
    assert _dec("1200", 900) is None  # 越界拒收


# ---------- DB 集成（同 test_agent_channel：库不可达自动 skip） ----------

def _db_ready() -> bool:
    try:
        from app.db import engine
        from sqlalchemy import text

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture()
def db():
    if not _db_ready():
        pytest.skip("dev PG 不可达")
    from app.db import SessionLocal

    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


def test_fitness_upsert_and_page_ctx(db):
    from sqlalchemy import delete

    from app.models import FitnessTest
    from app.routers.fitness import _page_ctx

    try:
        db.add(FitnessTest(test_date=TEST_DAY, item="pushup_max", value=25))
        db.add(FitnessTest(test_date=TEST_DAY, item="plank_sec", value=90))
        db.flush()
        ctx = _page_ctx(db)
        row = next(r for r in ctx["rows"] if r["key"] == "pushup_max")
        # 2020 年的测试行是「最新」仅当库里没有更近的真实体测；两种情况都合法，
        # 只锁「该日期出现在历史列表里」这一不变量
        assert TEST_DAY in ctx["by_date"]
        assert ctx["by_date"][TEST_DAY]["pushup_max"] == 25.0
        assert row is not None
    finally:
        db.rollback()
        db.execute(delete(FitnessTest).where(FitnessTest.test_date == TEST_DAY))
        db.commit()


def test_lab_save_and_group(db):
    from sqlalchemy import delete, select

    from app.models import LabResult
    from app.routers.labs import _page_ctx, _save_row
    from decimal import Decimal

    try:
        _save_row(db, TEST_DAY, "uric_acid", "尿酸", Decimal("500"), "μmol/L",
                  Decimal("208"), Decimal("428"))
        _save_row(db, TEST_DAY, "uric_acid", "尿酸", Decimal("450"), "μmol/L",
                  Decimal("208"), Decimal("428"))  # 同日同项覆盖
        db.flush()
        rows = db.execute(
            select(LabResult).where(
                LabResult.report_date == TEST_DAY, LabResult.item_key == "uric_acid"
            )
        ).scalars().all()
        assert len(rows) == 1 and float(rows[0].value) == 450.0
        ctx = _page_ctx(db)
        pts = ctx["groups"]["uric_acid"]["points"]
        mine = next(p for p in pts if p["date"] == TEST_DAY)
        assert mine["flag"] == "high"  # 450 > 428
    finally:
        db.rollback()
        db.execute(delete(LabResult).where(LabResult.report_date == TEST_DAY))
        db.commit()
