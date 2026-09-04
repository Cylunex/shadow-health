"""体检化验档案（V7 G6）：结构化指标多年趋势——敏感数据留在 NAS 是相对云 App 的优势。

- 手动录入：常见项目词表（带默认单位与参考范围）或自定义项目
- AI 解析：拍化验单照片 → vision 结构化 → 可编辑预览 → 确认入库
  （无 AI Key 时该入口隐藏，手动录入永远可用——断网原则）
- 展示：按项目分组的历史序列，超参考范围标黄（提示就医确认，不做诊断）
"""
from __future__ import annotations

import json as json_lib
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_login, templates
from app.models import LabResult
from app.services import llm
from app.timeutil import today_local

router = APIRouter(dependencies=[Depends(require_login)])

# 常见项目词表：(key, 名称, 单位, 参考下限, 参考上限)——范围为常用成人参考，
# 化验单上的范围优先（录入时可改）
COMMON_ITEMS: list[tuple[str, str, str, float | None, float | None]] = [
    ("total_chol", "总胆固醇", "mmol/L", None, 5.2),
    ("triglycerides", "甘油三酯", "mmol/L", None, 1.7),
    ("ldl_c", "低密度脂蛋白 LDL-C", "mmol/L", None, 3.4),
    ("hdl_c", "高密度脂蛋白 HDL-C", "mmol/L", 1.0, None),
    ("fasting_glucose", "空腹血糖", "mmol/L", 3.9, 6.1),
    ("hba1c", "糖化血红蛋白", "%", 4.0, 6.0),
    ("uric_acid", "尿酸", "μmol/L", 208, 428),
    ("alt", "谷丙转氨酶 ALT", "U/L", None, 40),
    ("ast", "谷草转氨酶 AST", "U/L", None, 40),
    ("creatinine", "肌酐", "μmol/L", 57, 97),
]
_COMMON_BY_KEY = {i[0]: i for i in COMMON_ITEMS}

LAB_PHOTO_PROMPT_VERSION = "lab-photo-v2"
LAB_PHOTO_PROMPT = """你是化验单结构化助手。读取这张体检/化验单照片，提取数值型指标。

只返回一个 JSON 对象（不要 markdown 代码块），格式：
{"report_date": "YYYY-MM-DD 或空字符串", "confidence": 0到1的整体置信度, "items": [{"label": "指标中文名", "value": 数值, "unit": "单位", "ref_low": 参考下限或null, "ref_high": 参考上限或null, "confidence": 0到1的单项置信度}]}

注意：只提取有明确数值的指标；参考范围按化验单原样；识别不出日期就留空。"""


def flag(value: float, low: float | None, high: float | None) -> str | None:
    """超参考范围方向：'high'/'low'/None。"""
    if high is not None and value > high:
        return "high"
    if low is not None and value < low:
        return "low"
    return None


def _page_ctx(db: Session, saved: bool = False, error: str | None = None,
              parsed: dict | None = None) -> dict[str, Any]:
    rows = db.execute(
        select(LabResult).order_by(LabResult.item_key, LabResult.report_date)
    ).scalars().all()
    units_by_key: dict[str, set[str | None]] = {}
    for row in rows:
        units_by_key.setdefault(row.item_key, set()).add(row.unit)
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        group_key = r.item_key if len(units_by_key[r.item_key]) == 1 else f"{r.item_key}::{r.unit or '无单位'}"
        g = groups.setdefault(group_key, {
            "label": r.item_label, "unit": r.unit, "points": [],
        })
        g["points"].append({
            "id": r.id,
            "date": r.report_date,
            "value": float(r.value),
            "flag": flag(float(r.value),
                         float(r.ref_low) if r.ref_low is not None else None,
                         float(r.ref_high) if r.ref_high is not None else None),
            "ref_low": float(r.ref_low) if r.ref_low is not None else None,
            "ref_high": float(r.ref_high) if r.ref_high is not None else None,
            "provenance": r.provenance if isinstance(r.provenance, dict) else None,
        })
    latest_date = max((r.report_date for r in rows), default=None)
    latest_rows = [r for r in rows if r.report_date == latest_date]
    latest_abnormal = sum(
        flag(
            float(r.value),
            float(r.ref_low) if r.ref_low is not None else None,
            float(r.ref_high) if r.ref_high is not None else None,
        ) is not None
        for r in latest_rows
    )
    if parsed is not None:
        parsed = dict(parsed)
        metadata = dict(parsed.get("metadata") or {})
        metadata["trace"] = metadata.get("trace") if isinstance(metadata.get("trace"), dict) else {}
        parsed["metadata"] = metadata
        parsed["metadata_json"] = json_lib.dumps(metadata, ensure_ascii=False)
    return {
        "groups": groups,
        "common_items": COMMON_ITEMS,
        "today": today_local().isoformat(),
        "ai_on": llm.is_vision_configured(db),
        "saved": saved,
        "error": error,
        "parsed": parsed,
        "summary": {
            "item_count": len(groups),
            "latest_date": latest_date,
            "latest_count": len(latest_rows),
            "latest_abnormal": latest_abnormal,
        },
    }


def _save_row(db: Session, report_date: date, key: str, label: str, value: Decimal,
              unit: str | None, ref_low: Decimal | None, ref_high: Decimal | None,
              provenance: dict[str, Any] | None = None) -> None:
    stmt = pg_insert(LabResult).values(
        report_date=report_date, item_key=key, item_label=label, value=value,
        unit=unit, ref_low=ref_low, ref_high=ref_high, provenance=provenance,
    )
    db.execute(stmt.on_conflict_do_update(
        index_elements=["report_date", "item_key"],
        set_={"value": stmt.excluded.value, "item_label": stmt.excluded.item_label,
              "unit": stmt.excluded.unit, "ref_low": stmt.excluded.ref_low,
              "ref_high": stmt.excluded.ref_high,
              "provenance": stmt.excluded.provenance},
    ))


def _dec(raw: Any) -> Decimal | None:
    s = str(raw if raw is not None else "").strip()
    if not s:
        return None
    try:
        v = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"数值格式不正确：{s!r}")
    if not v.is_finite() or not (Decimal("-100000") <= v <= Decimal("100000")):
        raise ValueError(f"数值超出范围：{s!r}")
    return v


def _validate_range(low, high):
    if low is not None and high is not None and low > high:
        raise ValueError("参考下限不能大于上限，请按化验单核对")


@router.get("/labs")
def labs_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "labs.html", _page_ctx(db))


@router.post("/labs")
async def labs_save(request: Request, db: Session = Depends(get_db)):
    """手动录入单条（同日同项覆盖）。item=词表 key 或 'custom'+自定义名。"""
    form = await request.form()
    try:
        d = date.fromisoformat(str(form.get("report_date") or "").strip())
    except ValueError:
        return templates.TemplateResponse(
            request, "labs.html", _page_ctx(db, error="日期格式不正确")
        )
    item = str(form.get("item") or "").strip()
    try:
        value = _dec(form.get("value"))
        if value is None:
            raise ValueError("请填数值")
        ref_low = _dec(form.get("ref_low"))
        ref_high = _dec(form.get("ref_high"))
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "labs.html", _page_ctx(db, error=str(exc))
        )
    if item in _COMMON_BY_KEY:
        key, label, default_unit, dlow, dhigh = _COMMON_BY_KEY[item]
        unit = str(form.get("unit") or "").strip() or default_unit
        if unit == default_unit and ref_low is None and dlow is not None:
            ref_low = Decimal(str(dlow))
        if unit == default_unit and ref_high is None and dhigh is not None:
            ref_high = Decimal(str(dhigh))
    else:
        label = str(form.get("custom_label") or "").strip()[:50]
        if not label:
            return templates.TemplateResponse(
                request, "labs.html", _page_ctx(db, error="自定义项目要填名称")
            )
        key = f"custom_{label}"
        unit = str(form.get("unit") or "").strip() or None
    try:
        _validate_range(ref_low, ref_high)
    except ValueError as exc:
        return templates.TemplateResponse(request, "labs.html", _page_ctx(db, error=str(exc)))
    _save_row(db, d, key, label, value, unit, ref_low, ref_high)
    db.flush()
    return templates.TemplateResponse(request, "labs.html", _page_ctx(db, saved=True))


@router.delete("/labs/{row_id}")
def labs_delete(row_id: int, db: Session = Depends(get_db)):
    from fastapi.responses import Response

    row = db.get(LabResult, row_id)
    if row is not None:
        db.delete(row)
        db.flush()
    # 200 + 空文档：outerHTML swap 直接移除该数据点（同 diet 删行模式）
    return Response(status_code=200, content="")


@router.post("/labs/photo")
async def labs_photo(request: Request, file: UploadFile, db: Session = Depends(get_db)):
    """化验单照片 → AI 结构化 → 可编辑预览（不直接入库，确认后走 /labs/bulk）。"""
    if not llm.is_vision_configured(db):
        raise HTTPException(status_code=400, detail="未配置 AI 模型")
    data = await file.read(8 * 1024 * 1024 + 1)
    if not data or len(data) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="请上传不超过 8 MB 的化验单照片")
    media_type = file.content_type or "image/jpeg"
    if media_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=400, detail="请上传 JPEG、PNG 或 WebP 照片")
    try:
        text = llm._call(
            db, LAB_PHOTO_PROMPT, "提取这张化验单的指标，按要求返回 JSON。",
            images=[(media_type, __import__("base64").b64encode(data).decode())],
            max_tokens=3000,
        )
    except llm.LLMError as exc:
        return templates.TemplateResponse(
            request, "labs.html", _page_ctx(db, error=str(exc))
        )
    start, end = text.find("{"), text.rfind("}")
    try:
        payload = json_lib.loads(text[start:end + 1]) if start != -1 else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
        payload = {}
    def confidence(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return round(number, 3) if 0 <= number <= 1 else None

    items = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict) or not str(raw.get("label") or "").strip():
            continue
        try:
            number = _dec(raw.get("value"))
            low, high = _dec(raw.get("ref_low")), _dec(raw.get("ref_high"))
            _validate_range(low, high)
        except ValueError:
            continue
        if number is None:
            continue
        items.append({
            "label": str(raw.get("label") or "").strip()[:50],
            "value": float(number),
            "unit": str(raw.get("unit") or "").strip()[:20],
            "ref_low": float(low) if low is not None else None,
            "ref_high": float(high) if high is not None else None,
            "confidence": confidence(raw.get("confidence")),
        })
        if len(items) >= 30:
            break
    if not items:
        return templates.TemplateResponse(
            request, "labs.html", _page_ctx(db, error="没识别出可用指标，试试手动录入")
        )
    parsed = {
        "report_date": str(payload.get("report_date") or "").strip()
        or today_local().isoformat(),
        "items": items,
        "metadata": {
            "trace": llm.vision_trace(
                db, LAB_PHOTO_PROMPT_VERSION, confidence=confidence(payload.get("confidence"))
            ),
            "items": items,
        },
    }
    return templates.TemplateResponse(request, "labs.html", _page_ctx(db, parsed=parsed))


@router.post("/labs/bulk")
async def labs_bulk(request: Request, db: Session = Depends(get_db)):
    """AI 预览确认入库：行式表单 label_i/value_i/unit_i/ref_low_i/ref_high_i。"""
    form = await request.form()
    try:
        d = date.fromisoformat(str(form.get("report_date") or "").strip())
    except ValueError:
        return templates.TemplateResponse(
            request, "labs.html", _page_ctx(db, error="日期格式不正确")
        )
    try:
        metadata = json_lib.loads(str(form.get("metadata") or "{}"))
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    raw_trace = metadata.get("trace") if isinstance(metadata.get("trace"), dict) else {}
    trace: dict[str, Any] = {}
    for key, limit in (
        ("provider", 20), ("model", 100), ("config_source", 20),
        ("prompt_version", 50), ("analyzed_at", 50),
    ):
        value = str(raw_trace.get(key) or "").strip()
        if value:
            trace[key] = value[:limit]
    try:
        trace_confidence = float(raw_trace.get("confidence"))
    except (TypeError, ValueError):
        trace_confidence = None
    if trace_confidence is not None and 0 <= trace_confidence <= 1:
        trace["confidence"] = round(trace_confidence, 3)
    original_items = metadata.get("items") if isinstance(metadata.get("items"), list) else []
    pending = []
    errors = []
    preview_items = [{"label": str(form.get(f"label_{i}") or ""),
                      **{k: str(form.get(f"{k}_{i}") or "") for k in ("value", "unit", "ref_low", "ref_high")}}
                     for i in range(30) if str(form.get(f"label_{i}") or "").strip() and not form.get(f"skip_{i}")]
    for i in range(30):
        label = str(form.get(f"label_{i}") or "").strip()[:50]
        if not label or form.get(f"skip_{i}"):
            continue
        try:
            value = _dec(form.get(f"value_{i}"))
            if value is None:
                raise ValueError("请填写数值，或勾选跳过该行")
            ref_low = _dec(form.get(f"ref_low_{i}"))
            ref_high = _dec(form.get(f"ref_high_{i}"))
            _validate_range(ref_low, ref_high)
        except ValueError as exc:
            errors.append(f"第 {i + 1} 行：{exc}")
            continue
        # 与词表按名称对齐（同名合并成同一 key，多年趋势才能连起来）
        match = next((c for c in COMMON_ITEMS if c[1] == label), None)
        key = match[0] if match else f"custom_{label}"
        unit = str(form.get(f"unit_{i}") or "").strip() or (match[2] if match else None)
        raw_original = (
            original_items[i]
            if i < len(original_items) and isinstance(original_items[i], dict)
            else {}
        )
        original = {
            key: raw_original.get(key)
            for key in ("label", "value", "unit", "ref_low", "ref_high", "confidence")
            if key in raw_original
        }
        row_trace = dict(trace)
        if isinstance(original.get("confidence"), (int, float)):
            row_trace["confidence"] = original["confidence"]
        provenance = {
            "method": "vision",
            "trace": row_trace,
            "input": original,
            "confirmed": {
                "label": label,
                "value": float(value),
                "unit": unit,
                "ref_low": float(ref_low) if ref_low is not None else None,
                "ref_high": float(ref_high) if ref_high is not None else None,
            },
        } if trace else None
        if any(row[0] == key for row in pending):
            errors.append(f"第 {i + 1} 行：项目重复，请核对名称，避免覆盖同日记录")
        pending.append((key, label if not match else match[1], value, unit, ref_low, ref_high, provenance))
    if errors:
        kept = [i for i in range(30) if str(form.get(f"label_{i}") or "").strip() and not form.get(f"skip_{i}")]
        metadata = {**metadata, "items": [original_items[i] if i < len(original_items) else {} for i in kept]}
        return templates.TemplateResponse(request, "labs.html", _page_ctx(db,
            error="未保存任何行。" + "；".join(errors), parsed={
                "report_date": str(d), "items": preview_items, "metadata": metadata}))
    for key, label, value, unit, low, high, provenance in pending:
        _save_row(db, d, key, label, value, unit, low, high, provenance=provenance)
    n = len(pending)
    if n:
        db.flush()
    return templates.TemplateResponse(
        request, "labs.html",
        _page_ctx(db, saved=bool(n), error=None if n else "没有可入库的行"),
    )
