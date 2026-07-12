"""周期体测（V7 B5）：无器械自测协议 + 雷达图 + 分档，4-8 周复测看趋势。

四项协议（成年男性口径，锚点为「优秀」参考值，线性折 0-100 分）：
- 俯卧撑最大次数（力量耐力）  锚 40 次
- 平板支撑（核心）            锚 180 秒
- 坐位体前屈（柔韧）          锚 +15 cm（负值=够不到脚尖）
- 1 分钟心率恢复（心肺）      锚 40 bpm（运动后 1 分钟心率下降值）

同日重测同项 = 覆盖（ON CONFLICT）；digest 距上次体测 ≥6 周提醒复测。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import FitnessTest
from app.timeutil import today_local

router = APIRouter(dependencies=[Depends(require_login)])

# (key, 名称, 单位, 优秀锚点, 下限, 上限, 负值允许)
ITEMS: list[tuple[str, str, str, float, float, float]] = [
    ("pushup_max", "俯卧撑最大次数", "次", 40, 0, 200),
    ("plank_sec", "平板支撑", "秒", 180, 0, 1200),
    ("sit_reach_cm", "坐位体前屈", "cm", 15, -30, 45),
    ("hr_recovery", "1 分钟心率恢复", "bpm", 40, 0, 90),
]
_ITEM_KEYS = {i[0] for i in ITEMS}
RETEST_WEEKS = 6


def score_item(key: str, value: float) -> int:
    """0-100 分：对「优秀」锚点线性折算（坐位体前屈从 -10 起算量程）。"""
    spec = next((i for i in ITEMS if i[0] == key), None)
    if spec is None:
        return 0
    anchor = spec[3]
    if key == "sit_reach_cm":
        pct = (value + 10) / (anchor + 10) * 100  # -10cm=0 分，+15cm=100 分
    else:
        pct = value / anchor * 100
    return max(0, min(100, round(pct)))


def level_label(score: int) -> str:
    return "优秀" if score >= 70 else ("良好" if score >= 40 else "待提高")


def _tests_by_date(db: Session) -> dict[date, dict[str, float]]:
    out: dict[date, dict[str, float]] = {}
    for r in db.execute(select(FitnessTest).order_by(FitnessTest.test_date)).scalars():
        out.setdefault(r.test_date, {})[r.item] = float(r.value)
    return out


def _page_ctx(db: Session, saved: bool = False, error: str | None = None) -> dict[str, Any]:
    import json

    by_date = _tests_by_date(db)
    dates = sorted(by_date, reverse=True)
    latest_d = dates[0] if dates else None
    prev_d = dates[1] if len(dates) > 1 else None
    rows = []
    for key, name, unit, _anchor, _lo, _hi in ITEMS:
        latest_v = by_date.get(latest_d, {}).get(key) if latest_d else None
        prev_v = by_date.get(prev_d, {}).get(key) if prev_d else None
        score = score_item(key, latest_v) if latest_v is not None else None
        rows.append({
            "key": key, "name": name, "unit": unit,
            "latest": latest_v, "prev": prev_v,
            "delta": round(latest_v - prev_v, 1) if latest_v is not None and prev_v is not None else None,
            "score": score,
            "level": level_label(score) if score is not None else None,
        })
    radar = None
    if latest_d:
        datasets = [{
            "label": f"{latest_d}",
            "data": [score_item(k, by_date[latest_d][k]) if k in by_date[latest_d] else 0
                     for k, *_ in ITEMS],
        }]
        if prev_d:
            datasets.append({
                "label": f"{prev_d}",
                "data": [score_item(k, by_date[prev_d][k]) if k in by_date[prev_d] else 0
                         for k, *_ in ITEMS],
            })
        radar = json.dumps({
            "labels": [name for _k, name, *_ in ITEMS],
            "datasets": datasets,
        }, ensure_ascii=False)
    weeks_since = (
        round((today_local() - latest_d).days / 7, 1) if latest_d else None
    )
    return {
        "items": ITEMS,
        "rows": rows,
        "history_dates": dates[:12],
        "by_date": by_date,
        "latest_d": latest_d,
        "weeks_since": weeks_since,
        "retest_due": weeks_since is not None and weeks_since >= RETEST_WEEKS,
        "radar_json": radar,
        "today": today_local().isoformat(),
        "saved": saved,
        "error": error,
    }


def last_test_date(db: Session) -> date | None:
    """最近一次体测日期（digest 复测提醒用）。"""
    return db.execute(select(func.max(FitnessTest.test_date))).scalar_one_or_none()


@router.get("/fitness")
def fitness_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "fitness.html", _page_ctx(db))


@router.post("/fitness")
async def fitness_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        d = date.fromisoformat(str(form.get("test_date") or "").strip())
    except ValueError:
        d = today_local()
    d = min(d, today_local())
    saved_any = False
    for key, name, _unit, _anchor, lo, hi in ITEMS:
        raw = str(form.get(key) or "").strip()
        if not raw:
            continue
        try:
            v = Decimal(raw)
        except InvalidOperation:
            return templates.TemplateResponse(
                request, "fitness.html", _page_ctx(db, error=f"{name}格式不正确")
            )
        if not (Decimal(str(lo)) <= v <= Decimal(str(hi))):
            return templates.TemplateResponse(
                request, "fitness.html",
                _page_ctx(db, error=f"{name}超出合理范围（{lo:g}~{hi:g}）"),
            )
        stmt = pg_insert(FitnessTest).values(test_date=d, item=key, value=v)
        db.execute(stmt.on_conflict_do_update(
            index_elements=["test_date", "item"], set_={"value": stmt.excluded.value},
        ))
        saved_any = True
    if not saved_any:
        return templates.TemplateResponse(
            request, "fitness.html", _page_ctx(db, error="至少填一项再保存")
        )
    db.flush()
    return templates.TemplateResponse(request, "fitness.html", _page_ctx(db, saved=True))
