"""内置 AI 的受控进程内工具：读取最小事实，写操作仅生成审核草案。

与机器合同复用 MachineHealthService；模型没有批准、删除或私密数据权限。
生产 app 不依赖 MCP SDK。保留的历史私有 helper 仅供既有兼容测试调用。
"""
from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Habit
from app.timeutil import today_local

AGENT_NAME = "内置AI"  # 落 import_raw.blob，/agent-log 归属展示

MEALS = ("早餐", "午餐", "加餐", "晚餐")
WRITE_TOOLS = ("record_diet", "update_diet", "record_workout", "record_weight", "record_habit", "delete_record")
TOOL_LABELS = {
    "record_diet": "记录饮食",
    "update_diet": "修正饮食草案",
    "record_workout": "记录训练",
    "record_weight": "记录指标",
    "record_habit": "习惯打卡",
    "delete_record": "删除记录",
    "query_summary": "查当日汇总",
    "search_food": "查食物库",
}

_DATE_PROP = {
    "type": "string",
    "description": "日期 YYYY-MM-DD，缺省=今天；补记历史必须先和用户确认日期",
}
# record_weight 字段（与 metrics 页白名单一致；服务端 _FIELD_DEFS 是最终权威）
_WEIGHT_PROPS = {
    "weight_kg": "体重 kg", "body_fat_pct": "体脂率 %", "mood_score": "心情分 1~10",
    "waist_cm": "腰围 cm", "chest_cm": "胸围 cm", "arm_cm": "臂围 cm",
    "thigh_cm": "大腿围 cm", "hip_cm": "臀围 cm",
    "bp_systolic": "收缩压 mmHg", "bp_diastolic": "舒张压 mmHg",
    "resting_hr": "静息心率", "spo2_pct": "血氧 %",
    "sleep_hours": "睡眠时长 h", "sleep_quality": "睡眠质量 1~5",
    "energy_level": "精力 1~5", "muscle_mass_kg": "肌肉量 kg",
    "skeletal_muscle_kg": "骨骼肌 kg", "bmr_kcal": "基础代谢 kcal",
    "body_water_kg": "体水分 kg", "visceral_fat_level": "内脏脂肪等级",
}

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "record_diet",
        "description": "批量记录饮食条目。优先 search_food 拿 food_id（营养按食物库自动算，"
        "只需用量）；库里没有才自报营养。回执 {new, skipped, results, day_totals}。",
        "input_schema": {
            "type": "object",
            "properties": {
                "meal": {"type": "string", "enum": list(MEALS)},
                "date": _DATE_PROP,
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "食物名"},
                            "food_id": {"type": "integer", "description": "食物库 id（search_food 结果）"},
                            "amount_g": {"type": "number", "description": "用量（克）"},
                            "kcal": {"type": "number"},
                            "protein_g": {"type": "number"},
                            "fat_g": {"type": "number"},
                            "carb_g": {"type": "number"},
                            "notes": {
                                "type": "string",
                                "maxLength": 1000,
                                "description": "本条饮食的补充说明，不要并入食物名",
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["items", "meal"],
        },
    },
    {
        "name": "record_workout",
        "description": "记录一次手动训练（type 如「跑步」「爆发循环」）。回执含 new 计数与 row_id。",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "训练类型"},
                "duration_min": {"type": "integer", "description": "时长（分钟）"},
                "date": _DATE_PROP,
                "distance_km": {"type": "number"},
                "calories": {"type": "integer"},
                "rpe": {"type": "integer", "description": "主观强度 1~10"},
                "notes": {"type": "string"},
            },
            "required": ["type", "duration_min"],
        },
    },
    {
        "name": "record_weight",
        "description": "记录身体指标（体重/体脂/围度/血压/心率/血氧/睡眠/心情等），"
        "至少填一个字段；同日重复保存=覆盖更新。",
        "input_schema": {
            "type": "object",
            "properties": {
                **{k: {"type": "number", "description": v} for k, v in _WEIGHT_PROPS.items()},
                "date": _DATE_PROP,
            },
        },
    },
    {
        "name": "record_habit",
        "description": "按名称给习惯打卡。count 缺省=声明式打卡（同日重复 skipped）；"
        "count=N（1~99）=计数累计 +N（喝水等 target>1 的习惯）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "habit_name": {"type": "string"},
                "date": _DATE_PROP,
                "count": {"type": "integer", "minimum": 1, "maximum": 99},
            },
            "required": ["habit_name"],
        },
    },
    {
        "name": "query_summary",
        "description": "当日全景：饮食汇总+明细（带 row_id）/训练（带 row_id）/体重心情/"
        "打卡完成度。记录前查重、删除前找 row_id 都用它。",
        "input_schema": {
            "type": "object",
            "properties": {"date": _DATE_PROP},
        },
    },
    {
        "name": "search_food",
        "description": "按关键词查食物库（每 100g 营养 + food_id），常吃的排前面；"
        "记录饮食前先查，拿 food_id 交给 record_diet。",
        "input_schema": {
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
        },
    },
    {
        "name": "delete_record",
        "description": "删除一条记错的 diet/workout 记录。row_id 从记录回执或 "
        "query_summary 取；删除前必须向用户复述内容并得到确认。",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["diet", "workout"]},
                "row_id": {"type": "integer"},
            },
            "required": ["type", "row_id"],
        },
    },
]


def _norm_date(raw: Any) -> str:
    s = str(raw or "").strip()
    return s or today_local().isoformat()


def _record(rtype: str, date_str: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": rtype,
        "client_id": str(uuid.uuid4()),
        "date": date_str,
        "payload": {k: v for k, v in payload.items() if v is not None},
    }


def _ingest(db: Session, records: list[dict[str, Any]]) -> dict[str, Any]:
    """进程内直调核心管线；503（批级失败）作为 error 喂回模型，模型如实转述
    （同参重试交给用户/模型决定——client_id 幂等保证重试不双写这一批已成功的）。"""
    from app.routers.offline import ingest_records

    status, out = ingest_records(
        db, records, source="agent", agent_name=AGENT_NAME, with_results=True
    )
    if status != 200:
        return {"error": f"写入失败（{status}）：{out.get('error', '未知错误')}，可重试一次"}
    return out


def _day_totals(db: Session, date_str: str) -> dict[str, Any]:
    from app.routers.agent import summary_data

    try:
        d = date_type.fromisoformat(date_str)
    except ValueError:
        return {}
    s = summary_data(db, d)
    return {
        "kcal": s["diet"]["kcal"],
        "protein_g": s["diet"]["protein_g"],
        "workout_min": s["workout_min"],
    }


def _run_record_diet(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    meal = str(args.get("meal") or "").strip()
    if meal not in MEALS:
        return {"error": f"meal 必须是 {'/'.join(MEALS)} 之一：{meal!r}"}
    items = args.get("items")
    if not isinstance(items, list) or not items:
        return {"error": "items 不能为空"}
    date_str = _norm_date(args.get("date"))
    records = []
    for it in items:
        if not isinstance(it, dict) or not str(it.get("name") or "").strip():
            return {"error": "每个条目至少要有 name"}
        records.append(_record("diet", date_str, {
            "meal": meal,
            "food_id": it.get("food_id"),
            "free_text": str(it["name"]).strip(),
            "amount_g": it.get("amount_g"),
            "kcal": it.get("kcal"),
            "protein_g": it.get("protein_g"),
            "fat_g": it.get("fat_g"),
            "carb_g": it.get("carb_g"),
            "notes": it.get("notes"),
        }))
    out = _ingest(db, records)
    if "error" in out:
        return out
    return {"date": date_str, **out, "day_totals": _day_totals(db, date_str)}


def _run_record_workout(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    if not str(args.get("type") or "").strip():
        return {"error": "type（训练类型）不能为空"}
    date_str = _norm_date(args.get("date"))
    out = _ingest(db, [_record("workout", date_str, {
        "session_type": str(args["type"]).strip(),
        "duration_min": args.get("duration_min"),
        "distance_km": args.get("distance_km"),
        "calories": args.get("calories"),
        "rpe": args.get("rpe"),
        "notes": args.get("notes"),
    })])
    if "error" in out:
        return out
    return {"date": date_str, **out, "day_totals": _day_totals(db, date_str)}


def _run_record_weight(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    payload = {k: args.get(k) for k in _WEIGHT_PROPS if args.get(k) is not None}
    if not payload:
        return {"error": f"至少提供一个字段：{'/'.join(_WEIGHT_PROPS)}"}
    date_str = _norm_date(args.get("date"))
    out = _ingest(db, [_record("metric", date_str, payload)])
    if "error" in out:
        return out
    return {"date": date_str, **out}


def _run_record_habit(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("habit_name") or "").strip()
    if not name:
        return {"error": "habit_name 不能为空"}
    count = args.get("count")
    if count is not None and not (isinstance(count, int) and 1 <= count <= 99):
        return {"error": f"count 超出范围（1~99）：{count!r}"}
    habits = db.execute(
        select(Habit).where(Habit.active.is_(True)).order_by(Habit.sort, Habit.id)
    ).scalars().all()
    exact = [h for h in habits if h.name == name]
    if exact:
        matched = exact[0]
    else:
        fuzzy = [h for h in habits if name.lower() in h.name.lower()]
        if len(fuzzy) == 1:
            matched = fuzzy[0]
        elif not fuzzy:
            return {"error": f"没有匹配的习惯：{name!r}。现有：{'、'.join(h.name for h in habits)}"}
        else:
            return {"error": f"习惯名有歧义：{name!r} 命中 {'、'.join(h.name for h in fuzzy)}，请用全名"}
    date_str = _norm_date(args.get("date"))
    payload: dict[str, Any] = {"habit_id": matched.id}
    if count is not None:
        payload.update(mode="increment", done_count=count)
    out = _ingest(db, [_record("habit", date_str, payload)])
    if "error" in out:
        return out
    return {"date": date_str, "habit": matched.name, **out}


def _run_query_summary(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    from app.routers.agent import summary_data

    raw = str(args.get("date") or "").strip()
    if raw:
        try:
            d = date_type.fromisoformat(raw)
        except ValueError:
            return {"error": f"date 不是合法日期：{raw!r}"}
    else:
        d = today_local()
    return summary_data(db, d)


def _run_search_food(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    from app.routers.agent import food_search_items

    q = str(args.get("keyword") or "").strip()
    if not q:
        return {"error": "keyword 不能为空"}
    return {"q": q, "items": food_search_items(db, q)}


def _run_delete_record(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    from app.routers.agent import delete_record

    rtype = str(args.get("type") or "").strip()
    if rtype not in ("diet", "workout"):
        return {"error": f"type 仅支持 diet/workout：{rtype!r}"}
    try:
        row_id = int(str(args.get("row_id")).strip())
    except (TypeError, ValueError):
        return {"error": "row_id 不是合法整数"}
    status, body = delete_record(db, rtype, row_id)
    if status != 200:
        return {"error": str(body.get("error") or f"删除失败（{status}）")}
    return body


_RUNNERS = {
    "record_diet": _run_record_diet,
    "record_workout": _run_record_workout,
    "record_weight": _run_record_weight,
    "record_habit": _run_record_habit,
    "query_summary": _run_query_summary,
    "search_food": _run_search_food,
    "delete_record": _run_delete_record,
}


def run_tool(db: Session, name: str, args: dict[str, Any], *, owner: str = "", request_key: str = "") -> dict[str, Any]:
    """执行一个工具调用；一切错误都折成 {"error": ...} 喂回模型，不抛异常。"""
    if name in WRITE_TOOLS:
        from app.services.agent_drafts import propose_tool, digest
        try:
            return propose_tool(db, name, args, owner, digest([request_key, name, args]))
        except (ValueError, TypeError) as exc:
            return {"error": "草案参数不完整或超出范围，请核对日期、食物、份量。"}
    if name == "query_summary":
        from app.routers.machine_agent import MachineHealthService
        try:
            day = date_type.fromisoformat(args.get("date") or str(today_local()))
        except (ValueError, TypeError):
            return {"error": "不是合法日期，请使用 YYYY-MM-DD"}
        result = MachineHealthService(db).summary("primary", day)
        result["indicators"].pop("mood_score", None)
        result["data_quality"]["indicators"].pop("mood_score", None)
        return result
    runner = _RUNNERS.get(name) if name == "search_food" else None
    if runner is None:
        return {"error": f"未知工具：{name}"}
    try:
        return runner(db, args if isinstance(args, dict) else {})
    except Exception as exc:  # 工具内部意外错误也要喂回模型，不能炸断整轮问答
        return {"error": f"工具执行失败：{str(exc)[:200]}"}


def receipt_summary(name: str, result: dict[str, Any]) -> str:
    """给 UI「本次执行的操作」列表用的一句话回执摘要。"""
    if "error" in result:
        return f"失败：{result['error']}"
    if result.get("draft_id"):
        return "待审核，尚未写入健康记录"
    if name == "delete_record":
        return f"已删除：{result.get('summary', '')}".strip("：")
    parts = [f"new={result.get('new', 0)}"]
    if result.get("skipped"):
        parts.append(f"skipped={result['skipped']}")
    if result.get("habit"):
        parts.insert(0, str(result["habit"]))
    totals = result.get("day_totals") or {}
    if totals.get("kcal") is not None:
        parts.append(f"当日累计 {round(totals['kcal'])} kcal")
    return " · ".join(parts)


def review_tool_defs():
    import copy
    tools = [copy.deepcopy(t) for t in TOOL_DEFS if t["name"] not in {"record_habit", "delete_record"}]
    for tool in tools:
        if tool["name"] in WRITE_TOOLS:
            tool["description"] = "创建待用户审核的草案，绝不直接写入。" + tool["description"].split("回执")[0]
            tool["input_schema"].setdefault("required", []).append("date")
        if tool["name"] == "record_weight":
            tool["input_schema"]["properties"] = {k: v for k, v in tool["input_schema"]["properties"].items()
                if k in {"date", "weight_kg", "sleep_hours", "mood_score"}}
    update_tool = copy.deepcopy(next(t for t in tools if t["name"] == "record_diet"))
    update_tool["name"] = "update_diet"
    update_tool["description"] = "修正一条饮食记录：只生成差异草案，需用户审核。items 仅一项。"
    update_tool["input_schema"]["properties"]["row_id"] = {"type": "integer", "minimum": 1}
    update_tool["input_schema"]["required"].append("row_id")
    tools.append(update_tool)
    return tools
