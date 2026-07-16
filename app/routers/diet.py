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
- POST   /diet/photos              餐次照片上传（multipart：d + meal + file，存 photo_dir）
- GET    /diet/photos/{id}         照片文件（登录后可见，FileResponse）
- DELETE /diet/photos/{id}         删除照片（文件 + 行）
- GET    /fragments/diet/day?d=    （契约外补充）四餐分组列表片段，diet-changed 被动刷新
- GET    /fragments/diet/summary?d= 日汇总片段（今日面板也用）
- GET    /fragments/diet/chips     近 30 天频次 top8 chips 片段
- GET    /diet/recipes?tag=        药膳库；HX-Request 时仅返回列表片段（tag 筛选局部刷新）
- GET    /diet/foods?q=&new=       食物库管理页（q 筛选，HX 请求回列表片段；new 预填新增名）
- POST   /diet/foods               新增自定义食物（重名拒绝）
- GET    /diet/foods/{id}/edit     行内编辑片段 / {id}/row 显示行片段
- PUT    /diet/foods/{id}          保存编辑（饮食记录冗余值不回溯，新记录用新值）
- DELETE /diet/foods/{id}          删除；已被饮食记录引用时拒绝（外键保护）

所有写操作响应带 HX-Trigger: diet-changed；summary / chips / day 片段以
hx-trigger="diet-changed from:body" 被动刷新。
注：/fragments/* 不在 /diet 前缀下，故本路由不设 prefix，路径写全。
"""
from __future__ import annotations

import json
import secrets
from datetime import date, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_login, templates
from app.models import (
    AppSetting, BodyMetrics, DailyActivity, DietLog, DietPhoto, Food, MealTemplate,
    OffProduct, Recipe, WorkoutLog,
)
from app.timeutil import now_local, today_local

router = APIRouter(dependencies=[Depends(require_login)])

HX_TRIGGER = {"HX-Trigger": "diet-changed"}


def _hx_trigger(msg: str | None = None) -> dict[str, str]:
    """diet-changed 触发头；带 msg 时同时触发全局成功 toast（base.html 监听）。

    JSON 走默认 ensure_ascii：HTTP 头必须 latin-1，中文以 \\uXXXX 转义、
    htmx 客户端 JSON.parse 后原样展示。
    """
    if not msg:
        return dict(HX_TRIGGER)
    return {"HX-Trigger": json.dumps({"diet-changed": "", "toast": msg})}
# 展示顺序按时段：早餐 → 午餐 → 加餐 → 晚餐（DB CHECK 词表一致）
MEALS = ("早餐", "午餐", "加餐", "晚餐")
# 药膳 effect_tags 受控词表（设计文档 §5.4）
EFFECT_TAGS = ("平补", "温阳", "滋阴", "填精", "固精", "健脾", "强腰")
DEFAULT_AMOUNT_G = Decimal("100")

# 餐次照片：大小上限与扩展名白名单（heic 为 iPhone 相册原格式，浏览器兼容有限但先收下）
PHOTO_MAX_BYTES = 15 * 1024 * 1024
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}


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
    if not v.is_finite():  # Decimal("nan"/"inf") 能构造成功，比较时才炸，先拦
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


def _food_macros(
    food: Food, amount_g: Decimal | None
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    """foods × amount_g → (kcal, protein, fat, carb) 冗余值；食物库缺值时对应项为 None。"""
    if amount_g is None:
        return None, None, None, None

    def scale(per100: Any) -> Decimal | None:
        if per100 is None:
            return None
        return (Decimal(per100) * amount_g / 100).quantize(Decimal("0.1"))

    return scale(food.kcal_per_100g), scale(food.protein_g), scale(food.fat_g), scale(food.carb_g)


def _auto_catalog_food(
    db: Session,
    name: Any,
    amount_g: Any,
    kcal: Any,
    protein_g: Any = None,
    fat_g: Any = None,
    carb_g: Any = None,
) -> bool:
    """自由文本记录自动进食物库（UI 手记 / AI 餐照识别 / offline·agent 通道共用）。

    门槛：名字 ≤20 字（更长多半是整句描述，会污染搜索）+ 带克数 + 带热量——
    否则折算不出每 100g，没营养值的档案没有复用价值；折算结果超出生理上限
    （热量 >900 kcal/100g 或单一宏量 >100g/100g）视为可疑数据不建档。
    重名跳过不覆盖（用户手工维护的值优先）。返回是否真的新建了。
    建档后该食物即出现在搜索联想/常吃 chips，营养按用量由服务端重算。"""
    food_name = str(name or "").strip()
    if not food_name or len(food_name) > 20:
        return False

    def dec(v: Any) -> Decimal | None:
        if v is None:
            return None
        try:
            return v if isinstance(v, Decimal) else Decimal(str(v))
        except InvalidOperation:
            return None

    amount = dec(amount_g)
    kcal_d = dec(kcal)
    if amount is None or amount <= 0 or kcal_d is None:
        return False

    def per100(v: Any) -> Decimal | None:
        d = dec(v)
        if d is None:
            return None
        return (d * 100 / amount).quantize(Decimal("0.1"))

    kcal100 = per100(kcal_d)
    macros100 = [per100(protein_g), per100(fat_g), per100(carb_g)]
    if kcal100 is None or kcal100 > 900 or any(m is not None and m > 100 for m in macros100):
        return False
    if db.execute(select(Food.id).where(Food.name == food_name)).first() is not None:
        return False
    db.add(Food(
        name=food_name,
        kcal_per_100g=kcal100,
        protein_g=macros100[0],
        fat_g=macros100[1],
        carb_g=macros100[2],
        notes="自动建档（来自饮食记录）",
    ))
    db.flush()
    return True


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
    photos_by_meal: dict[str, list[DietPhoto]] = {m: [] for m in MEALS}
    for p in db.execute(
        select(DietPhoto).where(DietPhoto.log_date == d).order_by(DietPhoto.id)
    ).scalars():
        photos_by_meal.setdefault(p.meal, []).append(p)
    meal_groups = [
        {
            "meal": meal,
            "rows": items,
            "photos": photos_by_meal[meal],
            "kcal": sum((r["log"].kcal or Decimal(0)) for r in items),
            "protein": sum((r["log"].protein_g or Decimal(0)) for r in items),
            "carb": sum((r["log"].carb_g or Decimal(0)) for r in items),
            "fat": sum((r["log"].fat_g or Decimal(0)) for r in items),
        }
        for meal, items in by_meal.items()
    ]
    from app.services import llm

    return {"d": d, "meal_groups": meal_groups, "ai_on": llm.is_configured(db), "fmt": _fmt}


def _diet_streak(db: Session, d: date) -> int:
    """连续记录天数（截至 d；d 当天还没记不破连击，从前一天起算）。"""
    days = {
        r[0]
        for r in db.execute(
            select(func.distinct(DietLog.log_date)).where(
                DietLog.log_date <= d, DietLog.log_date > d - timedelta(days=366)
            )
        )
    }
    cur = d if d in days else d - timedelta(days=1)
    n = 0
    while cur in days:
        n += 1
        cur -= timedelta(days=1)
    return n


def _summary_ctx(db: Session, d: date) -> dict:
    total_kcal, total_protein, total_fat, total_carb = db.execute(
        select(
            func.coalesce(func.sum(DietLog.kcal), 0),
            func.coalesce(func.sum(DietLog.protein_g), 0),
            func.coalesce(func.sum(DietLog.fat_g), 0),
            func.coalesce(func.sum(DietLog.carb_g), 0),
        ).where(DietLog.log_date == d)
    ).one()
    target_kcal = _setting_number(db, "target_kcal")
    target_protein = _setting_number(db, "target_protein_g")

    # 训练日热量偏移（V6 C3，可选）：设置了偏移且当日有训练记录 → 目标上浮 N%
    # （周总量意识由周 Check-in 承担，这里只做单日弹性）
    train_day_bonus = None
    offset = _setting_number(db, "energy_train_day_offset")
    if target_kcal and offset:
        trained = db.execute(
            select(func.count()).select_from(WorkoutLog).where(WorkoutLog.log_date == d)
        ).scalar_one() > 0
        if trained:
            target_kcal = round(target_kcal * (1 + offset / 100))
            train_day_bonus = round(offset)

    def pct(total: Any, target: float | None) -> int:
        if not target:
            return 0
        return min(100, round(float(total) * 100 / target))

    # 能量收支缺口 = 摄入 −（基础代谢 + 当日活动消耗）；BMR 取截至当日最近一次
    # 体脂秤回填值（无秤数据不显示，不做身高性别估算）
    bmr = db.execute(
        select(BodyMetrics.bmr_kcal)
        .where(BodyMetrics.log_date <= d, BodyMetrics.bmr_kcal.is_not(None))
        .order_by(BodyMetrics.log_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    activity = db.get(DailyActivity, d)
    active_kcal = (
        float(activity.active_kcal)
        if activity is not None and activity.active_kcal is not None else None
    )
    energy_gap = burn = None
    if bmr is not None:
        burn = round(bmr + (active_kcal or 0))
        energy_gap = round(float(total_kcal) - burn)

    return {
        "d": d,
        "total_kcal": total_kcal,
        "total_protein": total_protein,
        "total_fat": total_fat,
        "total_carb": total_carb,
        "target_kcal": target_kcal,
        "target_protein": target_protein,
        "kcal_pct": pct(total_kcal, target_kcal),
        "protein_pct": pct(total_protein, target_protein),
        "kcal_over": bool(target_kcal) and float(total_kcal) > float(target_kcal or 0),
        "protein_ok": bool(target_protein) and float(total_protein) >= float(target_protein or 0),
        "diet_streak": _diet_streak(db, d),
        "energy_gap": energy_gap,
        "burn": burn,
        "burn_has_active": active_kcal is not None,
        "train_day_bonus": train_day_bonus,
        "fmt": _fmt,
    }


def _chips_ctx(db: Session) -> dict:
    """常吃 chips：当前时段餐次的近 30 天频次优先（早上先看到早餐常吃的），
    不足 8 个再用全局频次补齐；附上次用量供一击记录。"""
    since = today_local() - timedelta(days=29)
    meal = _default_meal()

    def top_ids(meal_only: bool, limit: int, exclude: set[int]) -> list[int]:
        stmt = (
            select(DietLog.food_id, func.count().label("n"))
            .where(DietLog.food_id.is_not(None), DietLog.log_date >= since)
            .group_by(DietLog.food_id)
            .order_by(func.count().desc(), DietLog.food_id)
            .limit(limit + len(exclude))
        )
        if meal_only:
            stmt = stmt.where(DietLog.meal == meal)
        return [fid for fid, _n in db.execute(stmt) if fid not in exclude][:limit]

    ids = top_ids(True, 8, set())
    if len(ids) < 8:
        ids += top_ids(False, 8 - len(ids), set(ids))
    chips = []
    for food_id in ids:
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
        fat = _parse_decimal(form.get("fat_g"), "脂肪", 1000)
        carb = _parse_decimal(form.get("carb_g"), "碳水", 2000)
    except ValueError as exc:
        return _form_msg(request, error=str(exc))

    cataloged = False
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
        kcal, protein, fat, carb = _food_macros(food, amount)
        log = DietLog(
            log_date=log_date, meal=meal, food_id=food.id,
            amount_g=amount, kcal=kcal, protein_g=protein, fat_g=fat, carb_g=carb,
        )
        name = food.name
    else:
        free_text = str(form.get("free_text") or form.get("q") or "").strip()
        if not free_text:
            return _form_msg(request, error="请输入吃了什么，或从联想中选择食物")
        log = DietLog(
            log_date=log_date, meal=meal, free_text=free_text,
            amount_g=amount, kcal=kcal, protein_g=protein, fat_g=fat, carb_g=carb,
        )
        name = free_text
        # 带克数+热量的新食物自动进食物库（下次可搜索/chip 直选）
        cataloged = _auto_catalog_food(db, free_text, amount, kcal, protein, fat, carb)
    db.add(log)
    db.flush()
    return _form_msg(request, ok=f"已记录 {meal}·{name}"
                     + ("，新食物已入库" if cataloged else ""))


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
        fat = _parse_decimal(form.get("fat_g"), "脂肪", 1000)
        carb = _parse_decimal(form.get("carb_g"), "碳水", 2000)
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
            log.kcal, log.protein_g, log.fat_g, log.carb_g = _food_macros(food, log.amount_g)
    else:
        free_text = str(form.get("free_text") or "").strip()
        if free_text:
            log.free_text = free_text
        log.amount_g = amount
        log.kcal = kcal
        log.protein_g = protein
        log.fat_g = fat
        log.carb_g = carb
        # 事后补全克数/热量的自由文本行同样自动建档（重名跳过）
        _auto_catalog_food(db, log.free_text, amount, kcal, protein, fat, carb)
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
    kcal, protein, fat, carb = _food_macros(food, amount)
    meal = _default_meal()
    db.add(
        DietLog(
            log_date=log_date, meal=meal, food_id=food.id,
            amount_g=amount, kcal=kcal, protein_g=protein, fat_g=fat, carb_g=carb,
        )
    )
    db.flush()
    # chips 按钮 hx-swap="none"，只吃 HX-Trigger；toast 给一击操作一个可见确认
    return Response(
        status_code=200, content="",
        headers=_hx_trigger(f"已记录 {meal}·{food.name} {_fmt(amount)}g"),
    )


@router.post("/diet/meals/copy")
async def diet_meal_copy(request: Request, db: Session = Depends(get_db)):
    """整餐一键复制：找最近一个早于 d 且该餐次有记录的日子，整餐复制到 d。

    food_id 行按当前食物库值 × 原用量重算营养（食物库改过就用新值）；
    free_text 行原样复制。单人饮食高度重复，这是录入最大的省力点。
    """
    form = await request.form()
    day = min(_parse_date(form.get("d")), today_local())
    meal = str(form.get("meal") or "")
    if meal not in MEALS:
        raise HTTPException(status_code=400, detail="无效的餐次")
    src_day = db.execute(
        select(func.max(DietLog.log_date)).where(DietLog.meal == meal, DietLog.log_date < day)
    ).scalar_one_or_none()
    if src_day is None:
        raise HTTPException(status_code=404, detail=f"没有更早的{meal}记录可复制")
    rows = db.execute(
        select(DietLog)
        .where(DietLog.log_date == src_day, DietLog.meal == meal)
        .order_by(DietLog.id)
    ).scalars().all()
    for r in rows:
        food = db.get(Food, r.food_id) if r.food_id is not None else None
        if food is not None:
            kcal, protein, fat, carb = _food_macros(food, r.amount_g)
            db.add(DietLog(
                log_date=day, meal=meal, food_id=food.id, amount_g=r.amount_g,
                kcal=kcal, protein_g=protein, fat_g=fat, carb_g=carb,
            ))
        else:
            db.add(DietLog(
                log_date=day, meal=meal, free_text=r.free_text or "—", amount_g=r.amount_g,
                kcal=r.kcal, protein_g=r.protein_g, fat_g=r.fat_g, carb_g=r.carb_g,
            ))
    db.flush()
    return Response(
        status_code=200, content="",
        headers=_hx_trigger(f"已复制 {src_day:%m-%d} 的{meal}（{len(rows)} 项）"),
    )


# ---------- 组合菜谱（V6 P5）：常吃的一餐存成组合，一键整组记录 ----------

def _templates_ctx(db: Session) -> dict:
    rows = db.execute(
        select(MealTemplate).order_by(MealTemplate.id.desc())
    ).scalars().all()
    return {
        "meal_templates": [
            {"id": t.id, "name": t.name, "count": len(t.items or [])} for t in rows
        ],
    }


@router.get("/fragments/diet/templates")
def diet_templates_fragment(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "fragments/diet_templates.html", _templates_ctx(db)
    )


@router.post("/diet/templates/save")
async def diet_template_save(request: Request, db: Session = Depends(get_db)):
    """把 d 日的某餐存为组合：food_id 行存 (food_id, amount_g)——记录时按食物库
    现值重算营养；free_text 行原样快照。"""
    form = await request.form()
    day = min(_parse_date(form.get("d")), today_local())
    meal = str(form.get("meal") or "")
    name = str(form.get("name") or "").strip()[:30]
    if meal not in MEALS:
        raise HTTPException(status_code=400, detail="无效的餐次")
    if not name:
        raise HTTPException(status_code=400, detail="给组合起个名字（如「常规早餐」）")
    rows = db.execute(
        select(DietLog).where(DietLog.log_date == day, DietLog.meal == meal).order_by(DietLog.id)
    ).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"{day:%m-%d} 的{meal}没有记录")
    items = []
    for r in rows:
        if r.food_id is not None:
            items.append({"food_id": r.food_id, "amount_g": float(r.amount_g) if r.amount_g else None})
        else:
            items.append({
                "free_text": r.free_text,
                "amount_g": float(r.amount_g) if r.amount_g is not None else None,
                "kcal": float(r.kcal) if r.kcal is not None else None,
                "protein_g": float(r.protein_g) if r.protein_g is not None else None,
                "fat_g": float(r.fat_g) if r.fat_g is not None else None,
                "carb_g": float(r.carb_g) if r.carb_g is not None else None,
            })
    existing = db.execute(
        select(MealTemplate).where(MealTemplate.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        existing.items = items  # 同名覆盖更新（迭代自己的常规餐）
    else:
        db.add(MealTemplate(name=name, items=items))
    db.flush()
    return templates.TemplateResponse(
        request, "fragments/diet_templates.html", _templates_ctx(db),
        headers=_hx_trigger(f"已存为组合「{name}」（{len(items)} 项）"),
    )


@router.post("/diet/templates/{template_id}/log")
async def diet_template_log(template_id: int, request: Request, db: Session = Depends(get_db)):
    """一键记录整个组合到 d 日（餐次按当前时间预选，可传 meal 覆盖）。"""
    form = await request.form()
    day = min(_parse_date(form.get("d")), today_local())
    meal = str(form.get("meal") or "")
    if meal not in MEALS:
        meal = _default_meal()
    t = db.get(MealTemplate, template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="组合不存在")
    n = 0
    for it in t.items or []:
        if not isinstance(it, dict):
            continue
        fid = it.get("food_id")
        amount = Decimal(str(it["amount_g"])) if it.get("amount_g") is not None else None
        if fid is not None:
            food = db.get(Food, fid)
            if food is None:
                continue  # 食物已删：跳过该项（其余照记）
            if amount is None:
                amount = _last_amount(db, food.id)
            kcal, protein, fat, carb = _food_macros(food, amount)
            db.add(DietLog(
                log_date=day, meal=meal, food_id=food.id, amount_g=amount,
                kcal=kcal, protein_g=protein, fat_g=fat, carb_g=carb,
            ))
        else:
            db.add(DietLog(
                log_date=day, meal=meal, free_text=str(it.get("free_text") or "—")[:500],
                amount_g=amount,
                kcal=it.get("kcal"), protein_g=it.get("protein_g"),
                fat_g=it.get("fat_g"), carb_g=it.get("carb_g"),
            ))
        n += 1
    db.flush()
    return Response(
        status_code=200, content="",
        headers=_hx_trigger(f"已记录组合「{t.name}」到{meal}（{n} 项）"),
    )


@router.delete("/diet/templates/{template_id}")
def diet_template_delete(template_id: int, request: Request, db: Session = Depends(get_db)):
    t = db.get(MealTemplate, template_id)
    if t is not None:
        db.delete(t)
        db.flush()
    return templates.TemplateResponse(
        request, "fragments/diet_templates.html", _templates_ctx(db)
    )


# ---------- 条码（V7 D3）：foods 优先 → OFF 离线库一键建档 → 手输兜底 ----------

def _norm_barcode(code: str) -> str:
    return "".join(c for c in str(code) if c.isdigit())[:14]


@router.get("/diet/barcode/{code}")
def diet_barcode_lookup(code: str, request: Request, db: Session = Depends(get_db)):
    """扫码/手输条码查询：命中自建食物 → 直接可记；命中 OFF 缓存 → 一键建档；
    都没有 → 提示手动建档。返回扫码结果片段。"""
    code = _norm_barcode(code)
    ctx: dict[str, Any] = {"code": code, "food": None, "off": None}
    if len(code) >= 8:
        ctx["food"] = db.execute(
            select(Food).where(Food.barcode == code)
        ).scalar_one_or_none()
        if ctx["food"] is None:
            ctx["off"] = db.get(OffProduct, code)
    return templates.TemplateResponse(request, "fragments/diet_barcode_result.html", ctx)


@router.post("/diet/barcode/{code}/adopt")
def diet_barcode_adopt(code: str, request: Request, db: Session = Depends(get_db)):
    """从 OFF 缓存一键建档进自建食物库（挂条码，下次扫码直达）。"""
    code = _norm_barcode(code)
    off = db.get(OffProduct, code)
    if off is None:
        raise HTTPException(status_code=404, detail="离线库没有这个条码")
    existing = db.execute(select(Food).where(Food.barcode == code)).scalar_one_or_none()
    if existing is None:
        name = off.name if off.brand is None else f"{off.name}（{off.brand}）"
        name = name[:50]
        # 名称撞车（同名不同条码）时缀条码尾四位保唯一
        if db.execute(select(Food).where(Food.name == name)).scalar_one_or_none() is not None:
            name = f"{name[:44]} #{code[-4:]}"
        db.add(Food(
            name=name, category="包装食品", barcode=code,
            kcal_per_100g=off.kcal_per_100g, protein_g=off.protein_g,
            fat_g=off.fat_g, carb_g=off.carb_g,
        ))
        db.flush()
    return templates.TemplateResponse(
        request, "fragments/diet_barcode_result.html",
        {"code": code, "food": existing or db.execute(
            select(Food).where(Food.barcode == code)
        ).scalar_one(), "off": None, "adopted": existing is None},
        headers=dict(FOODS_HX_TRIGGER),
    )


# ---------- 食物库管理 ----------
# 数值字段：(字段, 中文名, 上限)；均为「每 100g」口径
_FOOD_NUM_FIELDS = [
    ("kcal_per_100g", "热量", 900),
    ("protein_g", "蛋白质", 100),
    ("fat_g", "脂肪", 100),
    ("carb_g", "碳水", 100),
]
FOODS_HX_TRIGGER = {"HX-Trigger": "foods-changed"}


def _parse_food_form(form: Any) -> tuple[dict[str, Any], str | None]:
    name = str(form.get("name") or "").strip()
    if not name:
        return {}, "请填写食物名称"
    if len(name) > 50:
        return {}, "名称太长（≤50 字）"
    values: dict[str, Any] = {
        "name": name,
        "category": str(form.get("category") or "").strip() or None,
        "notes": str(form.get("notes") or "").strip() or None,
    }
    for field, label, hi in _FOOD_NUM_FIELDS:
        try:
            values[field] = _parse_decimal(form.get(field), label, hi)
        except ValueError as exc:
            return {}, str(exc)
    return values, None


def _load_food(db: Session, food_id: int) -> Food:
    food = db.get(Food, food_id)
    if food is None:
        raise HTTPException(status_code=404, detail="食物不存在")
    return food


def _food_ref_count(db: Session, food_id: int) -> int:
    return db.execute(
        select(func.count()).select_from(DietLog).where(DietLog.food_id == food_id)
    ).scalar_one()


def _foods_list_ctx(db: Session, q: str = "") -> dict:
    stmt = select(Food)
    q = q.strip()
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Food.name.ilike(f"%{esc}%", escape="\\"))
    # 自定义食物 id 大（seed 在前），倒序让新建的排最上
    foods = db.execute(stmt.order_by(Food.id.desc()).limit(300)).scalars().all()
    return {"foods": foods, "q": q, "fmt": _fmt}


@router.get("/diet/foods")
def foods_page(request: Request, q: str = "", new: str = "", db: Session = Depends(get_db)):
    ctx = _foods_list_ctx(db, q)
    is_htmx = (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )
    if is_htmx:
        return templates.TemplateResponse(request, "fragments/foods_list.html", ctx)
    ctx.update({"new_name": new.strip()[:50], "f_saved": None, "f_error": None})
    return templates.TemplateResponse(request, "foods.html", ctx)


@router.post("/diet/foods")
async def food_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    values, error = _parse_food_form(form)
    saved = None
    if error is None:
        exists = db.execute(
            select(Food.id).where(Food.name == values["name"])
        ).scalar_one_or_none()
        if exists is not None:
            error = f"「{values['name']}」已在食物库中"
        else:
            db.add(Food(**values))
            db.flush()
            saved = values["name"]
    headers = dict(FOODS_HX_TRIGGER) if saved else None
    return templates.TemplateResponse(
        request,
        "fragments/food_new_form.html",
        {"new_name": "", "f_saved": saved, "f_error": error},
        headers=headers,
    )


@router.get("/diet/foods/{food_id}/edit")
def food_edit(food_id: int, request: Request, db: Session = Depends(get_db)):
    food = _load_food(db, food_id)
    return templates.TemplateResponse(
        request, "fragments/food_edit.html", {"food": food, "error": None, "fmt": _fmt}
    )


@router.get("/diet/foods/{food_id}/row")
def food_row(food_id: int, request: Request, db: Session = Depends(get_db)):
    food = _load_food(db, food_id)
    return templates.TemplateResponse(
        request, "fragments/food_row.html", {"food": food, "fmt": _fmt}
    )


@router.put("/diet/foods/{food_id}")
async def food_update(food_id: int, request: Request, db: Session = Depends(get_db)):
    food = _load_food(db, food_id)
    form = await request.form()
    values, error = _parse_food_form(form)
    if error is None and values["name"] != food.name:
        dup = db.execute(
            select(Food.id).where(Food.name == values["name"], Food.id != food_id)
        ).scalar_one_or_none()
        if dup is not None:
            error = f"「{values['name']}」已在食物库中"
    if error is not None:
        return templates.TemplateResponse(
            request, "fragments/food_edit.html", {"food": food, "error": error, "fmt": _fmt}
        )
    for field, value in values.items():
        setattr(food, field, value)
    db.flush()
    return templates.TemplateResponse(
        request, "fragments/food_row.html", {"food": food, "fmt": _fmt}
    )


@router.delete("/diet/foods/{food_id}")
def food_delete(food_id: int, request: Request, db: Session = Depends(get_db)):
    food = _load_food(db, food_id)
    refs = _food_ref_count(db, food_id)
    if refs:
        # 有引用不能删（外键保护）：回传原行 + 提示
        return templates.TemplateResponse(
            request,
            "fragments/food_row.html",
            {"food": food, "fmt": _fmt, "row_error": f"已有 {refs} 条饮食记录引用，不能删除"},
        )
    db.delete(food)
    db.flush()
    return Response(status_code=200, content="")


# ---------- 餐次照片 ----------
def _photo_dir() -> Path:
    d = get_settings().photo_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_photo(db: Session, photo_id: int) -> DietPhoto:
    photo = db.get(DietPhoto, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="照片不存在")
    return photo


@router.post("/diet/photos")
async def diet_photo_upload(
    db: Session = Depends(get_db),
    d: str = Form(""),
    meal: str = Form(""),
    file: UploadFile | None = File(None),
):
    """餐次照片上传：文件存 photo_dir（生成名防穿越），行记 (log_date, meal, filename)。

    meal 缺省按当前时间预选——今日页「拍照记一餐」直达按钮不带餐次。
    """
    log_date = min(_parse_date(d), today_local())
    if not meal.strip():
        meal = _default_meal()
    if meal not in MEALS:
        raise HTTPException(status_code=400, detail="无效的餐次")
    if file is None or not (file.filename or "").strip():
        raise HTTPException(status_code=400, detail="未选择图片")
    ext = Path(file.filename).suffix.lower()
    if ext not in PHOTO_EXTS or not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="仅支持图片（jpg/png/webp/gif/heic）")
    name = f"{log_date:%Y%m%d}_{secrets.token_hex(8)}{ext}"
    dest = _photo_dir() / name
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > PHOTO_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="图片超过 15MB")
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    db.add(
        DietPhoto(log_date=log_date, meal=meal, filename=name, content_type=file.content_type)
    )
    db.flush()
    # 上传表单 hx-swap="none"，day 片段经 HX-Trigger 被动刷新后出现新缩略图
    return Response(
        status_code=200, content="",
        headers=_hx_trigger(f"照片已存入{meal}，饮食页可 AI 识别"),
    )


@router.get("/diet/photos/{photo_id}")
def diet_photo_file(photo_id: int, db: Session = Depends(get_db)):
    """照片文件：走认证路由下发（不进 /static，外人拿不到）。"""
    photo = _load_photo(db, photo_id)
    path = get_settings().photo_dir / photo.filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="照片文件缺失")
    return FileResponse(str(path), media_type=photo.content_type or "application/octet-stream")


@router.delete("/diet/photos/{photo_id}")
def diet_photo_delete(photo_id: int, db: Session = Depends(get_db)):
    photo = _load_photo(db, photo_id)
    (get_settings().photo_dir / photo.filename).unlink(missing_ok=True)
    db.delete(photo)
    db.flush()
    return Response(status_code=200, content="", headers=dict(HX_TRIGGER))


@router.post("/diet/photos/{photo_id}/analyze")
def diet_photo_analyze(photo_id: int, request: Request, db: Session = Depends(get_db)):
    """AI 识别餐食照片（Claude Vision）：估算各食物营养 → 生成该餐次的饮食记录。

    识别结果按自由文本记录（kcal/protein 为该份量总值），可行内编辑修正。
    成功带 HX-Trigger: diet-changed，餐次列表立刻出现新行。
    """
    from app.services import llm

    photo = _load_photo(db, photo_id)

    def _msg(error: str | None = None, ok: str | None = None, note: str = ""):
        headers = dict(HX_TRIGGER) if ok else None
        return templates.TemplateResponse(
            request,
            "fragments/diet_ai_result.html",
            {"error": error, "ok": ok, "note": note},
            headers=headers,
        )

    if not llm.is_configured(db):
        return _msg(error="未配置 AI 模型 API Key——到 设置→AI 模型 填入即可使用识别。")
    path = get_settings().photo_dir / photo.filename
    if not path.is_file():
        return _msg(error="照片文件缺失。")
    try:
        result = llm.analyze_meal_photo(
            db, path.read_bytes(), photo.content_type or "image/jpeg"
        )
    except llm.LLMError as exc:
        return _msg(error=str(exc))

    items = result["items"]
    if not items:
        return _msg(error=f"没识别出食物。{result['note']}")
    total_kcal = 0.0
    new_foods = 0
    for it in items:
        db.add(DietLog(
            log_date=photo.log_date,
            meal=photo.meal,
            free_text=it["name"],
            amount_g=it["amount_g"],
            kcal=it["kcal"],
            protein_g=it["protein_g"],
            fat_g=it.get("fat_g"),
            carb_g=it.get("carb_g"),
        ))
        total_kcal += it["kcal"] or 0
        # 识别出的新菜自动进食物库（估算值按每 100g 折算，重名跳过）
        if _auto_catalog_food(db, it["name"], it["amount_g"], it["kcal"],
                              it["protein_g"], it.get("fat_g"), it.get("carb_g")):
            new_foods += 1
    db.flush()
    names = "、".join(it["name"] for it in items[:5]) + ("…" if len(items) > 5 else "")
    return _msg(
        ok=f"已识别 {len(items)} 项计入{photo.meal}：{names}（约 {round(total_kcal)} kcal）"
           + (f"，{new_foods} 个新食物已入库" if new_foods else ""),
        note=result["note"],
    )


# ---------- 搜索联想 ----------
@router.get("/diet/foods/search")
def diet_food_search(request: Request, q: str = "", db: Session = Depends(get_db)):
    """搜索联想（hx-trigger="input changed delay:300ms"），返回可点选项。

    按近 90 天使用频次优先排序（常吃的鸡胸肉排在名字更短的前面），
    同频次再按名字长度。命中过的条目模板上带「常吃」徽标。
    """
    q = q.strip()
    items: list[dict] = []
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        since = today_local() - timedelta(days=89)
        usage = (
            select(DietLog.food_id.label("fid"), func.count().label("n"))
            .where(DietLog.food_id.is_not(None), DietLog.log_date >= since)
            .group_by(DietLog.food_id)
            .subquery()
        )
        rows = db.execute(
            select(Food, func.coalesce(usage.c.n, 0))
            .outerjoin(usage, Food.id == usage.c.fid)
            .where(Food.name.ilike(f"%{esc}%", escape="\\"))
            .order_by(func.coalesce(usage.c.n, 0).desc(), func.length(Food.name), Food.name)
            .limit(8)
        ).all()
        items = [
            {"food": f, "last_amount": _last_amount(db, f.id), "used": n} for f, n in rows
        ]
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
