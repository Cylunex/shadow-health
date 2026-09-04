"""Shared review boundary for browser and machine callers; no model may approve."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.machine_auth import MachineAPIError, MachinePrincipal
from app.models import AgentRecordDraft, BodyMetrics, DietLog, DietPhoto, Food


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def target_snapshot(db, payload):
    """Lock the compared row before hashing; values and timestamp both matter."""
    if payload.get("target_id"):
        row = db.scalars(select(DietLog).where(DietLog.id == payload["target_id"]).with_for_update()).first()
        if not row or str(row.log_date) != payload["effective_date"]:
            raise MachineAPIError(409, "target_changed", "原记录不存在或日期已改变，请重新生成草案。")
        if (row.provenance or {}).get("source") not in (None, "manual", "agent", "shadow-nexus", "health-assistant"):
            raise MachineAPIError(403, "source_read_only", "不能修改设备或导入来源的记录。")
        return {k: str(getattr(row, k)) for k in (
            "id", "log_date", "meal", "free_text", "amount_g", "kcal", "protein_g", "fat_g", "carb_g", "notes", "updated_at")}
    if payload["record_type"] == "metric":
        columns = [getattr(BodyMetrics, k) for k in sorted(payload["fields"])]
        row = db.execute(select(*columns, BodyMetrics.updated_at, BodyMetrics.autofilled).where(
            BodyMetrics.log_date == date.fromisoformat(payload["effective_date"])).with_for_update()).first()
        if row and any((row[-1] or {}).get(key) not in (None, "manual", "agent", "shadow-nexus") for key in payload["fields"]):
            raise MachineAPIError(403, "source_read_only", "该指标已有设备来源，请保留自动采集值，在来源设置中手动处理。")
        return list(map(str, row[:-1])) if row else None
    return None


def prepare_review(db, payload):
    if payload["record_type"] == "meal":
        fields = dict(payload["fields"])
        items = fields.get("items", [fields])
        calculated = [meal_item(db, i) for i in items]
        sources = ["food_catalog" if i.get("food_id") else "estimate" for i in items]
        if "items" in fields:
            fields["items"] = calculated
        elif fields.get("food_id"):
            fields = {"meal": fields["meal"], **calculated[0]}
        payload = {**payload, "fields": fields, "_nutrition_sources": sources,
                   "_food_ids": [i.get("food_id") for i in items]}
    return {**payload, "_review_version": 2,
            "_target": target_snapshot(db, payload),
            "_expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()}


def locked_review(db, draft):
    # Refresh even if this Session loaded the object before another request committed.
    return db.scalars(select(AgentRecordDraft).where(AgentRecordDraft.draft_id == draft.draft_id)
                      .with_for_update().execution_options(populate_existing=True)).one()


def validate_review(db, draft):
    if draft.payload.get("_photo_id") and db.get(DietPhoto, draft.payload["_photo_id"]) is None:
        raise MachineAPIError(409, "photo_removed", "原照片已移除，请重新生成草案。")
    if draft.payload.get("_review_version") != 2:
        raise MachineAPIError(409, "review_upgrade_required", "旧草案需要重新生成后审核。")
    if datetime.fromisoformat(draft.payload["_expires_at"]) <= datetime.now(UTC):
        raise MachineAPIError(409, "draft_expired", "草案已过期，请重新生成。")
    if digest(target_snapshot(db, draft.payload)) != digest(draft.payload.get("_target")):
        raise MachineAPIError(409, "target_changed", "原记录已改变，请重新核对差异。")


def approve_match(draft, supplied):
    if not isinstance(supplied, str) or not supplied or supplied.strip('"') != draft.payload_hash:
        raise MachineAPIError(409, "review_changed", "审核内容不匹配，请刷新后确认。")


def meal_item(db, item):
    """Food catalog calculations are authoritative; unknown meals remain estimates."""
    clean = {k: v for k, v in item.items() if k in {
        "name", "amount_g", "kcal", "protein_g", "fat_g", "carb_g", "notes"} and v is not None}
    if item.get("food_id"):
        food = db.get(Food, int(item["food_id"]))
        amount = float(item.get("amount_g") or 0)
        if food is None or not 0 < amount <= 5000:
            raise MachineAPIError(400, "invalid_food", "食物库条目不存在或重量无效。")
        clean["name"] = food.name
        for out, field in (("kcal", "kcal_per_100g"), ("protein_g", "protein_g"), ("fat_g", "fat_g"), ("carb_g", "carb_g")):
            value = getattr(food, field)
            if value is not None:
                clean[out] = round(float(value) * amount / 100, 2)
            else:
                clean.pop(out, None)
    return clean


def propose_tool(db, name, args, owner, key):
    from app.routers.machine_agent import MachineHealthService, _validate_draft_payload
    if not owner:
        return {"error": "缺少已认证用户，不能生成草案。"}
    day = args.get("date")
    if not day:
        return {"error": "请明确记录日期（YYYY-MM-DD）。"}
    if name in ("record_diet", "update_diet"):
        if not isinstance(args.get("items"), list) or any(not isinstance(i, dict) or not isinstance(i.get("name"), str) for i in args["items"]):
            return {"error": "请提供食物名称和份量列表。"}
        items = [{k: v for k, v in item.items() if v is not None} for item in args.get("items", [])]
        if not items:
            return {"error": "请提供食物和份量。"}
        fields = {"name": "、".join(i["name"] for i in items)[:120], "meal": args.get("meal"), "items": items}
        kind = "meal"
    elif name == "record_weight":
        fields = {k: v for k, v in args.items() if k in {"weight_kg", "sleep_hours", "mood_score"} and v is not None}
        kind = "metric"
    elif name == "record_workout":
        fields = {"session_type": args.get("type"), "duration_min": args.get("duration_min")}
        for k in ("distance_km", "rpe", "notes"):
            if args.get(k) is not None:
                fields[k] = args[k]
        kind = "workout"
    else:
        return {"error": "此操作不向模型开放，请在对应记录页面手动处理。"}
    payload = {"record_type": kind, "effective_date": day, "fields": fields,
               "note": "AI 生成，食物库计算或模型估算；请核对日期、份量和数值。"}
    if name == "update_diet":
        payload.update(operation="update", target_id=args.get("row_id"))
    payload = _validate_draft_payload(payload)
    principal = MachinePrincipal(owner, frozenset({"health"}), frozenset(), {})
    draft, replayed = MachineHealthService(db).create_draft(
        principal=principal, profile_id="primary", idempotency_key=key,
        payload=payload, payload_hash=digest(payload))
    return {"draft_id": draft.draft_id, "status": draft.status, "direct_domain_write": False,
            "new": 0, "replayed": replayed, "review_url": f"/companion/drafts/{draft.draft_id}",
            "summary": "已生成待审核草案，尚未写入健康记录。"}
