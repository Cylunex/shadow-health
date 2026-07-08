"""饮食（设计文档 §3.3、§四 /diet 与 /diet/recipes 行、「饮食 meal 默认值」段）。

端点契约：
- GET    /diet?d=YYYY-MM-DD        按天页面（四餐分组、日汇总 vs 目标、常吃 chips、搜索添加）
- POST   /diet/logs                新增：food_id 路径服务端按 foods × amount_g 算 kcal/protein 冗余；
                                   free_text 路径手填 kcal；meal 缺省按当前本地时间预选
- GET    /diet/logs/{id}/edit      行内编辑表单片段（hx-swap="outerHTML"）
- GET    /diet/logs/{id}/row       （契约外补充）显示行片段，编辑「取消」按钮用
- PUT    /diet/logs/{id}           保存；food 行按新用量重算冗余值
- DELETE /diet/logs/{id}           删除（hx-confirm，返回空文档 → 行消失）
- GET    /diet/foods/search?q=     搜索联想片段（选中预填该食物上次记录用量）
- POST   /diet/quick/{food_id}     chip 一击记录（amount 取上次用量，meal 按时间预选）
- GET    /fragments/diet/day?d=    （契约外补充）四餐分组列表片段，diet-changed 被动刷新
- GET    /fragments/diet/summary?d= 日汇总片段（今日面板也用）
- GET    /fragments/diet/chips     近 30 天频次 top8 chips 片段
- GET    /diet/recipes?tag=        药膳库；HX-Request 时仅返回列表片段（tag 筛选局部刷新）

所有写操作响应带 HX-Trigger: diet-changed；summary / chips / day 片段以
hx-trigger="diet-changed from:body" 被动刷新。
注：/fragments/* 不在 /diet 前缀下，故本路由不设 prefix，路径写全。
"""
from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import AppSetting, DietLog, Food, Recipe
from app.timeutil import now_local, today_local

router = APIRouter(dependencies=[Depends(require_login)])

HX_TRIGGER = {"HX-Trigger": "diet-changed"}
# 展示顺序按时段：早餐 → 午餐 → 加餐 → 晚餐（DB CHECK 词表一致）
MEALS = ("早餐", "午餐", "加餐", "晚餐")
# 药膳 effect_tags 受控词表（设计文档 §5.4）
EFFECT_TAGS = ("平补", "温阳", "滋阴", "填精", "固精", "健脾", "强腰")
DEFAULT_AMOUNT_G = Decimal("100")


# ---------- 通用小工具 ----------
def _fmt(value: Any) -> str:
    """数值 → 去尾零显示字符串；None → ''（供模板与 input value 用）。"""
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    s = f"{float(value):.1f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _parse_date(raw: Any) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return today_local()


def _parse_decimal(raw: Any, label: str, hi: float) -> Decimal | None:
    """空 → None；非法/超界 → ValueError（中文提示）。"""
    s = str(raw).strip() if raw is not None else ""
    if not s:
        return None
    try:
        v = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{label}格式不正确")
    if not (Decimal(0) <= v <= Decimal(str(hi))):
        raise ValueError(f"{label}超出合理范围（0-{hi:g}）")
    return v.quantize(Decimal("0.1"))


def _default_meal() -> str:
    """meal 缺省：<10:30 早餐、10:30-15:00 午餐、15:00-17:00 加餐、之后晚餐（§四）。"""
    t = now_local().time()
    if t < time(10, 30):
        return "早餐"
    if t < time(15, 0):
        return "午餐"
    if t < time(17, 0):
        return "加餐"
    return "晚餐"


def _setting_number(db: Session, key: str) -> float | None:
    """读 app_settings 数值型目标；缺失/非法/非正 → None。"""
    row = db.get(AppSetting, key)
    if row is None:
        return None
    value = row.value
    if isinstance(value, dict):  # 容错 {"value": 2000} 形式
        value = value.get("value")
    try:
        n = float(value)  # type: ignore[arg-type]
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _food_macros(food: Food, amount_g: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """foods × amount_g → (kcal, protein_g) 冗余值；食物库缺值时对应项为 None。"""
    if amount_g is None:
        return None, None
    kcal = protein = None
    if food.kcal_per_100g is not None:
        kcal = (Decimal(food.kcal_per_100g) * amount_g / 100).quantize(Decimal("0.1"))
    if food.protein_g is not None:
        protein = (Decimal(food.protein_g) * amount_g / 100).quantize(Decimal("0.1"))
    return kcal, protein


def _last_amount(db: Session, food_id: int) -> Decimal:
    """该食物最近一次记录的用量；无记录默认 100g。"""
    amount = db.execute(
        select(DietLog.amount_g)
        .where(DietLog.food_id == food_id, DietLog.amount_g.is_not(None))
        .order_by(DietLog.log_date.desc(), DietLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return amount if amount is not None else DEFAULT_AMOUNT_G


def _load_log(db: Session, log_id: int) -> DietLog:
    log = db.get(DietLog, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return log


# ---------- 片段上下文 ----------
def _row_ctx(db: Session, log: DietLog) -> dict:
    name = None
    if log.food_id is not None:
        food = db.get(Food, log.food_id)
        name = food.name if food is not None else None
    return {"log": log, "name": name or log.free_text or "—", "fmt": _fmt}


def _day_ctx(db: Session, d: date) -> dict:
    rows = db.execute(
        select(DietLog, Food.name)
        .outerjoin(Food, DietLog.food_id == Food.id)
        .where(DietLog.log_date == d)
        .order_by(DietLog.id)
    ).all()
    by_meal: dict[str, list[dict]] = {m: [] for m in MEALS}
    for log, fname in rows:
        by_meal.setdefault(log.meal, []).append(
            {"log": log, "name": fname or log.free_text or "—"}
        )
    meal_groups = [
        {
            "meal": meal,
            "rows": items,
            "kcal": sum((r["log"].kcal or Decimal(0)) for r in items),
        }
        for meal, items in by_meal.items()
    ]
    return {"d": d, "meal_groups": meal_groups, "fmt": _fmt}


def _summary_ctx(db: Session, d: date) -> dict:
    total_kcal, total_protein = db.execute(
        select(
            func.coalesce(func.sum(DietLog.kcal), 0),
            func.coalesce(func.sum(DietLog.protein_g), 0),
        ).where(DietLog.log_date == d)
    ).one()
    target_kcal = _setting_number(db, "target_kcal")
    target_protein = _setting_number(db, "target_protein_g")

    def pct(total: Any, target: float | None) -> int:
        if not target:
            return 0
        return min(100, round(float(total) * 100 / target))

    return {
        "d": d,
        "total_kcal": total_kcal,
        "total_protein": total_protein,
        "target_kcal": target_kcal,
        "target_protein": target_protein,
        "kcal_pct": pct(total_kcal, target_kcal),
        "protein_pct": pct(total_protein, target_protein),
        "kcal_over": bool(target_kcal) and float(total_kcal) > float(target_kcal or 0),
        "protein_ok": bool(target_protein) and float(total_protein) >= float(target_protein or 0),
        "fmt": _fmt,
    }


def _chips_ctx(db: Session) -> dict:
    """近 30 天记录频次 top8（仅 food_id 记录），附上次用量供一击记录。"""
    since = today_local() - timedelta(days=29)
    rows = db.execute(
        select(DietLog.food_id, func.count().label("n"))
        .where(DietLog.food_id.is_not(None), DietLog.log_date >= since)
        .group_by(DietLog.food_id)
        .order_by(func.count().desc(), DietLog.food_id)
        .limit(8)
    ).all()
    chips = []
    for food_id, _n in rows:
        food = db.get(Food, food_id)
        if food is None:
            continue
        chips.append({"id": food.id, "name": food.name, "amount": _last_amount(db, food.id)})
    return {"chips": chips, "fmt": _fmt}


def _edit_ctx(db: Session, log: DietLog, error: str | None = None) -> dict:
    ctx = _row_ctx(db, log)
    ctx.update({"meals": MEALS, "error": error})
    return ctx


def _form_msg(request: Request, *, ok: str | None = None, error: str | None = None):
    """记录表单的提交反馈；成功时带 HX-Trigger 让 day/summary/chips 被动刷新。"""
    headers = dict(HX_TRIGGER) if ok else None
    return templates.TemplateResponse(
        request, "fragments/diet_form_msg.html", {"ok": ok, "error": error}, headers=headers
    )


# ---------- 页面 ----------
@router.get("/diet")
def diet_page(request: Request, d: str | None = None, db: Session = Depends(get_db)):
    """按天页面：默认今天，可前后翻天（不允许未来）。"""
    today = today_local()
    day = min(_parse_date(d), today)
    ctx: dict = {
        "d": day,
        "prev_d": day - timedelta(days=1),
        "next_d": day + timedelta(days=1),
        "is_today": day == today,
        "meals": MEALS,
        "default_meal": _default_meal(),
    }
    ctx.update(_summary_ctx(db, day))
    ctx.update(_chips_ctx(db))
    ctx.update(_day_ctx(db, day))
    return templates.TemplateResponse(request, "diet.html", ctx)


@router.get("/diet/recipes")
def recipes_page(request: Request, tag: str | None = None, db: Session = Depends(get_db)):
    """药膳库：卡片列表 + effect_tags 受控词表筛选；HTMX 请求只回列表片段。"""
    tag = tag if tag in EFFECT_TAGS else None
    stmt = select(Recipe).order_by(Recipe.id)
    if tag:
        stmt = stmt.where(Recipe.effect_tags.any(tag))
    recipes = db.execute(stmt).scalars().all()
    ctx = {"recipes": recipes, "tag": tag, "effect_tags": EFFECT_TAGS}
    is_htmx = (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )
    if is_htmx:
        return templates.TemplateResponse(request, "fragments/diet_recipes_list.html", ctx)
    return templates.TemplateResponse(request, "recipes.html", ctx)


# ---------- 写操作 ----------
@router.post("/diet/logs")
async def diet_log_create(request: Request, db: Session = Depends(get_db)):
    """新增记录：food_id 路径服务端计算冗余营养；free_text 路径手填。"""
    form = await request.form()
    log_date = min(_parse_date(form.get("log_date")), today_local())
    meal = str(form.get("meal") or "")
    if meal not in MEALS:
        meal = _default_meal()
    try:
        amount = _parse_decimal(form.get("amount_g"), "用量", 5000)
        kcal = _parse_decimal(form.get("kcal"), "热量", 20000)
        protein = _parse_decimal(form.get("protein_g"), "蛋白质", 1000)
    except ValueError as exc:
        return _form_msg(request, error=str(exc))

    food_id_raw = str(form.get("food_id") or "").strip()
    if food_id_raw:
        try:
            food = db.get(Food, int(food_id_raw))
        except ValueError:
            food = None
        if food is None:
            return _form_msg(request, error="所选食物不存在，请重新搜索选择")
        if amount is None:  # 正常会预填上次用量；被清空时兜底
            amount = _last_amount(db, food.id)
        kcal, protein = _food_macros(food, amount)
        log = DietLog(
            log_date=log_date, meal=meal, food_id=food.id,
            amount_g=amount, kcal=kcal, protein_g=protein,
        )
        name = food.name
    else:
        free_text = str(form.get("free_text") or form.get("q") or "").strip()
        if not free_text:
            return _form_msg(request, error="请输入吃了什么，或从联想中选择食物")
        log = DietLog(
            log_date=log_date, meal=meal, free_text=free_text,
            amount_g=amount, kcal=kcal, protein_g=protein,
        )
        name = free_text
    db.add(log)
    db.flush()
    return _form_msg(request, ok=f"已记录 {meal}·{name}")


@router.get("/diet/logs/{log_id}/edit")
def diet_log_edit(log_id: int, request: Request, db: Session = Depends(get_db)):
    log = _load_log(db, log_id)
    return templates.TemplateResponse(request, "fragments/diet_log_edit.html", _edit_ctx(db, log))


@router.get("/diet/logs/{log_id}/row")
def diet_log_row(log_id: int, request: Request, db: Session = Depends(get_db)):
    """显示行片段（编辑「取消」回退用，契约外补充）。"""
    log = _load_log(db, log_id)
    return templates.TemplateResponse(request, "fragments/diet_log_row.html", _row_ctx(db, log))


@router.put("/diet/logs/{log_id}")
async def diet_log_update(log_id: int, request: Request, db: Session = Depends(get_db)):
    log = _load_log(db, log_id)
    form = await request.form()
    meal = str(form.get("meal") or "")
    try:
        amount = _parse_decimal(form.get("amount_g"), "用量", 5000)
        kcal = _parse_decimal(form.get("kcal"), "热量", 20000)
        protein = _parse_decimal(form.get("protein_g"), "蛋白质", 1000)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "fragments/diet_log_edit.html", _edit_ctx(db, log, error=str(exc))
        )
    if meal in MEALS:
        log.meal = meal
    if log.food_id is not None:
        food = db.get(Food, log.food_id)
        if amount is not None:
            log.amount_g = amount
        if food is not None:  # 冗余值一律按用量重算，保持与食物库一致
            log.kcal, log.protein_g = _food_macros(food, log.amount_g)
    else:
        free_text = str(form.get("free_text") or "").strip()
        if free_text:
            log.free_text = free_text
        log.amount_g = amount
        log.kcal = kcal
        log.protein_g = protein
    db.flush()
    return templates.TemplateResponse(
        request, "fragments/diet_log_row.html", _row_ctx(db, log), headers=dict(HX_TRIGGER)
    )


@router.delete("/diet/logs/{log_id}")
def diet_log_delete(log_id: int, db: Session = Depends(get_db)):
    log = _load_log(db, log_id)
    db.delete(log)
    db.flush()
    # 200 + 空文档：outerHTML swap 直接移除该行（204 会被 htmx 忽略不 swap）
    return Response(status_code=200, content="", headers=dict(HX_TRIGGER))


@router.post("/diet/quick/{food_id}")
async def diet_quick(food_id: int, request: Request, db: Session = Depends(get_db)):
    """chip 一击记录：amount 取该食物上次用量，meal 按当前时间预选。

    d 参数（hx-include="#diet-date"）：饮食页记到当前查看的那天；无该输入时记今天。
    """
    food = db.get(Food, food_id)
    if food is None:
        raise HTTPException(status_code=404, detail="食物不存在")
    form = await request.form()
    log_date = min(_parse_date(form.get("d")), today_local())
    amount = _last_amount(db, food.id)
    kcal, protein = _food_macros(food, amount)
    db.add(
        DietLog(
            log_date=log_date, meal=_default_meal(), food_id=food.id,
            amount_g=amount, kcal=kcal, protein_g=protein,
        )
    )
    db.flush()
    # chips 按钮 hx-swap="none"，只吃 HX-Trigger
    return Response(status_code=200, content="", headers=dict(HX_TRIGGER))


# ---------- 搜索联想 ----------
@router.get("/diet/foods/search")
def diet_food_search(request: Request, q: str = "", db: Session = Depends(get_db)):
    """搜索联想（hx-trigger="input changed delay:300ms"），返回可点选项。"""
    q = q.strip()
    items: list[dict] = []
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        foods = db.execute(
            select(Food)
            .where(Food.name.ilike(f"%{esc}%", escape="\\"))
            .order_by(func.length(Food.name), Food.name)
            .limit(8)
        ).scalars().all()
        items = [{"food": f, "last_amount": _last_amount(db, f.id)} for f in foods]
    return templates.TemplateResponse(
        request, "fragments/diet_search.html", {"q": q, "items": items, "fmt": _fmt}
    )


# ---------- 片段 ----------
@router.get("/fragments/diet/day")
def diet_day_fragment(request: Request, d: str | None = None, db: Session = Depends(get_db)):
    day = min(_parse_date(d), today_local())
    return templates.TemplateResponse(request, "fragments/diet_day.html", _day_ctx(db, day))


@router.get("/fragments/diet/summary")
def diet_summary_fragment(request: Request, d: str | None = None, db: Session = Depends(get_db)):
    """日汇总片段（今日面板也用；diet-changed 被动刷新）。"""
    day = min(_parse_date(d), today_local())
    return templates.TemplateResponse(request, "fragments/diet_summary.html", _summary_ctx(db, day))


@router.get("/fragments/diet/chips")
def diet_chips_fragment(request: Request, db: Session = Depends(get_db)):
    """近 30 天频次 top8 chips 片段。"""
    return templates.TemplateResponse(request, "fragments/diet_chips.html", _chips_ctx(db))
