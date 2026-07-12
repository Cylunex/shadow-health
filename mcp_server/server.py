"""shadow-health MCP server（V3 批次 P2，docs/subpath-agent-plan.md §2.3）。

形态：官方 mcp SDK（FastMCP），双模式——streamable HTTP 常驻（缺省，
127.0.0.1:8180，容器内回环、不经 nginx 不对外）+ `--stdio`（本地 spawn 场景）。
鉴权以「仅回环监听」为主防线（MCP 层不再加 Bearer）。

工具内部全部调本机 REST（http://127.0.0.1:8080/api/...，Bearer=INGEST_TOKEN
从仓库根 .env 读），**不直连数据库**——校验/归一化/幂等/审计与手机离线通道
完全一致，agent 无法绕过任何口径。

反假确认（本服务存在的根本原因）：所有 record_* 工具返回服务器回执
{new, skipped, results}；**agent 的确认话术必须引用 new 计数**（见 README
话术规则）——没有回执 = 没写成功，从机制上消灭「说记了但没写库」。

同参数短窗去重（~60 秒，进程内存）：agent 端超时重调同一工具（参数完全一致）
时直接返回上次回执（标 "dedup": true），不再二次 POST。选「短窗去重」而不是
「client_id 按内容派生」的理由：内容派生会把真实的同日同参重复记录永久吞掉
（下午又吃了一份一模一样的加餐、同日两组同参数训练都是合法的），而超时重调
发生在秒级——60 秒窗口精确覆盖故障模式，又不误伤故意的重复记录。
代价：跨进程重启的重调不去重（HTTP 常驻模式下进程长活，可接受）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_TZ = ZoneInfo("Asia/Shanghai")  # 与 app.timeutil.LOCAL_TZ 同口径（不 import app，保持解耦）

MEALS = ("早餐", "午餐", "加餐", "晚餐")
# record_weight 字段白名单（服务端 metrics._FIELD_DEFS 是最终权威，这里挡明显笔误）
WEIGHT_FIELDS = (
    "weight_kg", "body_fat_pct", "mood_score",
    "waist_cm", "chest_cm", "arm_cm", "thigh_cm", "hip_cm",
)


def _load_dotenv() -> None:
    """读仓库根 .env（与 app.config 同规则：不覆盖已有环境变量）。"""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()
API_BASE = os.environ.get("SHEALTH_API_BASE", "http://127.0.0.1:8080").rstrip("/")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
MCP_HOST = os.environ.get("SHEALTH_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("SHEALTH_MCP_PORT", "8180"))

mcp = FastMCP(
    "shadow-health",
    instructions=(
        "shadow-health 健康记录工具集。记录饮食/体重/训练/打卡，查询当日汇总与周报。"
        "规则：1) 日期参数一律 YYYY-MM-DD，不传=今天；先和用户确认日期再补记历史。"
        "2) 记录后的确认话术必须引用返回的 new 计数（如「已入库 new=2」），"
        "没有回执不得声称已记录。3) 记错了用 delete_record 删（仅 diet/workout），"
        "row_id 从记录回执或 query_today_summary 里取。"
    ),
    host=MCP_HOST,
    port=MCP_PORT,
)


# ---------- REST 客户端 ----------

class ApiError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    if not INGEST_TOKEN:
        raise ApiError("INGEST_TOKEN 未配置（仓库根 .env）——无法调用 shadow-health API")
    return {"Authorization": f"Bearer {INGEST_TOKEN}"}


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        detail = resp.json().get("error", "")
    except ValueError:
        detail = resp.text[:200]
    raise ApiError(f"shadow-health API {resp.status_code}：{detail or '（无详情）'}")


def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    with httpx.Client(base_url=API_BASE, timeout=15.0) as client:
        resp = client.get(path, params=params or {}, headers=_headers())
    _raise_for(resp)
    return resp.json()


def _post(path: str, body: dict[str, Any]) -> dict:
    with httpx.Client(base_url=API_BASE, timeout=15.0) as client:
        resp = client.post(path, json=body, headers=_headers())
    _raise_for(resp)
    return resp.json()


def _ingest(records: list[dict]) -> dict:
    """POST /api/ingest/agent；503（留档/归一化批级失败）原样同参重试一次——
    client_id 不变，服务端 parse_status 门控保证不双写。"""
    body = {"records": records}
    with httpx.Client(base_url=API_BASE, timeout=15.0) as client:
        resp = client.post("/api/ingest/agent", json=body, headers=_headers())
        if resp.status_code == 503:
            time.sleep(2.0)
            resp = client.post("/api/ingest/agent", json=body, headers=_headers())
    _raise_for(resp)
    return resp.json()


# ---------- 短窗去重 ----------

_DEDUP_WINDOW_S = 60.0
_dedup_lock = threading.Lock()
_dedup_cache: dict[str, tuple[float, dict]] = {}


def _dedup_key(tool: str, args: dict[str, Any]) -> str:
    canon = json.dumps({"tool": tool, "args": args}, sort_keys=True,
                       ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _dedup_hit(key: str) -> dict | None:
    now = time.monotonic()
    with _dedup_lock:
        for k, (ts, _r) in list(_dedup_cache.items()):
            if now - ts >= _DEDUP_WINDOW_S:
                del _dedup_cache[k]
        hit = _dedup_cache.get(key)
    if hit is None:
        return None
    return {**hit[1], "dedup": True}


def _dedup_store(key: str, result: dict) -> None:
    with _dedup_lock:
        _dedup_cache[key] = (time.monotonic(), result)


# ---------- 公共小件 ----------

def _today_str() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def _norm_date(d: str | None) -> str:
    return (d or "").strip() or _today_str()


def _day_totals(date_str: str) -> dict:
    """记录后附带的当日累计（agent 复述不用再查一次）。"""
    s = _get("/api/agent/summary", {"date": date_str})
    return {
        "kcal": s["diet"]["kcal"],
        "protein_g": s["diet"]["protein_g"],
        "workout_min": s["workout_min"],
    }


def _record(rtype: str, date_str: str, payload: dict) -> dict:
    return {
        "type": rtype,
        "client_id": str(uuid.uuid4()),
        "date": date_str,
        "payload": payload,
    }


class DietItem(BaseModel):
    name: str = Field(description="食物名（自由文本，如「牛肉面」）")
    amount_g: float | None = Field(default=None, description="用量（克）")
    kcal: float | None = Field(default=None, description="热量 kcal")
    protein_g: float | None = Field(default=None, description="蛋白质 g")
    carb_g: float | None = Field(default=None, description="碳水 g")
    fat_g: float | None = Field(default=None, description="脂肪 g")


# ---------- 工具（9 个，宁少勿滥） ----------

@mcp.tool()
def record_diet(items: list[DietItem], meal: str, date: str | None = None) -> dict:
    """批量记录饮食条目。meal ∈ 早餐/午餐/加餐/晚餐；date 格式 YYYY-MM-DD（缺省=今天）。
    返回服务器回执 {new, skipped, results[{client_id,status,row_id}], day_totals}——
    确认话术必须引用 new 计数；row_id 供 delete_record 纠错。"""
    meal = meal.strip()
    if meal not in MEALS:
        raise ApiError(f"meal 必须是 {'/'.join(MEALS)} 之一：{meal!r}")
    if not items:
        raise ApiError("items 不能为空")
    date_str = _norm_date(date)
    args = {"items": [i.model_dump() for i in items], "meal": meal, "date": date_str}
    key = _dedup_key("record_diet", args)
    if (hit := _dedup_hit(key)) is not None:
        return hit
    records = [
        _record("diet", date_str, {
            "meal": meal,
            "free_text": i.name,
            "amount_g": i.amount_g,
            "kcal": i.kcal,
            "protein_g": i.protein_g,
            "carb_g": i.carb_g,
            "fat_g": i.fat_g,
        })
        for i in items
    ]
    out = _ingest(records)
    result = {"date": date_str, **out, "day_totals": _day_totals(date_str)}
    _dedup_store(key, result)
    return result


@mcp.tool()
def record_weight(
    weight_kg: float | None = None,
    body_fat_pct: float | None = None,
    mood_score: int | None = None,
    waist_cm: float | None = None,
    chest_cm: float | None = None,
    arm_cm: float | None = None,
    thigh_cm: float | None = None,
    hip_cm: float | None = None,
    date: str | None = None,
) -> dict:
    """记录体重/体脂/心情分（1~10）/围度，至少填一个字段；date=YYYY-MM-DD（缺省=今天）。
    同日重复保存 = 覆盖更新（与手动录入同语义）。确认话术必须引用返回的 new 计数。"""
    fields = {
        "weight_kg": weight_kg, "body_fat_pct": body_fat_pct, "mood_score": mood_score,
        "waist_cm": waist_cm, "chest_cm": chest_cm, "arm_cm": arm_cm,
        "thigh_cm": thigh_cm, "hip_cm": hip_cm,
    }
    payload = {k: v for k, v in fields.items() if v is not None}
    if not payload:
        raise ApiError(f"至少提供一个字段：{'/'.join(WEIGHT_FIELDS)}")
    date_str = _norm_date(date)
    args = {**payload, "date": date_str}
    key = _dedup_key("record_weight", args)
    if (hit := _dedup_hit(key)) is not None:
        return hit
    out = _ingest([_record("metric", date_str, payload)])
    result = {"date": date_str, **out, "day_totals": _day_totals(date_str)}
    _dedup_store(key, result)
    return result


@mcp.tool()
def record_workout(
    type: str,
    duration_min: int,
    date: str | None = None,
    distance_km: float | None = None,
    calories: int | None = None,
    rpe: int | None = None,
    notes: str | None = None,
) -> dict:
    """记录一次手动训练（type 如「跑步」「爆发循环」；rpe 1~10 可选）。
    date=YYYY-MM-DD（缺省=今天）。确认话术必须引用返回的 new 计数；
    回执 results[0].row_id 供 delete_record 纠错。"""
    if not type.strip():
        raise ApiError("type（训练类型）不能为空")
    date_str = _norm_date(date)
    payload = {
        "session_type": type.strip(),
        "duration_min": duration_min,
        "distance_km": distance_km,
        "calories": calories,
        "rpe": rpe,
        "notes": notes,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    args = {**payload, "date": date_str}
    key = _dedup_key("record_workout", args)
    if (hit := _dedup_hit(key)) is not None:
        return hit
    out = _ingest([_record("workout", date_str, payload)])
    result = {"date": date_str, **out, "day_totals": _day_totals(date_str)}
    _dedup_store(key, result)
    return result


@mcp.tool()
def record_habit(habit_name: str, date: str | None = None) -> dict:
    """按名称给习惯打卡（饮水也走这个）。名称对 active 习惯做精确→包含匹配，
    对不上或有歧义会报错并列出候选（可先用 list_habits 看清单）。
    同日已打过 → skipped=1（不会重复计数）。date=YYYY-MM-DD（缺省=今天）。"""
    name = habit_name.strip()
    if not name:
        raise ApiError("habit_name 不能为空")
    habits = _get("/api/offline/bootstrap")["habits"]
    exact = [h for h in habits if h["name"] == name]
    if exact:
        matched = exact[0]
    else:
        fuzzy = [h for h in habits if name.lower() in h["name"].lower()]
        if len(fuzzy) == 1:
            matched = fuzzy[0]
        elif not fuzzy:
            raise ApiError(
                f"没有匹配的习惯：{name!r}。现有：{'、'.join(h['name'] for h in habits)}"
            )
        else:
            raise ApiError(
                f"习惯名有歧义：{name!r} 命中 {'、'.join(h['name'] for h in fuzzy)}，请用全名"
            )
    date_str = _norm_date(date)
    args = {"habit_id": matched["id"], "date": date_str}
    key = _dedup_key("record_habit", args)
    if (hit := _dedup_hit(key)) is not None:
        return hit
    out = _ingest([_record("habit", date_str, {"habit_id": matched["id"]})])
    result = {"date": date_str, "habit": matched["name"], **out}
    _dedup_store(key, result)
    return result


@mcp.tool()
def query_today_summary(date: str | None = None) -> dict:
    """当日全景：饮食汇总+逐条明细（带 row_id）/步数/训练（带 row_id）/体重/
    心情分/打卡完成度。记录前查重、记录后向用户复述、找 delete_record 的
    row_id 都用它。date=YYYY-MM-DD（缺省=今天）。"""
    params = {"date": date.strip()} if date and date.strip() else {}
    return _get("/api/agent/summary", params)


@mcp.tool()
def query_weekly_report(week: str | None = None) -> dict:
    """周报数据（体重变化/日均热量蛋白/训练与有氧分钟/sRPE 负荷/打卡率/步数），
    与报告中心口径一致。week 格式 YYYY-Wnn（ISO 周，如 2026-W28），
    缺省=上一完整周；complete=false 表示该周还没走完。"""
    params = {"week": week.strip()} if week and week.strip() else {}
    return _get("/api/agent/report/weekly", params)


@mcp.tool()
def search_food(keyword: str) -> dict:
    """按关键词查食物库（每 100g 热量/蛋白/脂肪/碳水），热量估算辅助。
    常吃的排前面。记录时把换算好的数值填进 record_diet 的 items。"""
    return _get("/api/agent/foods", {"q": keyword})


@mcp.tool()
def list_habits() -> dict:
    """active 习惯清单（id/名称/周期/目标次数；auto=true 的由系统自动判定，
    一般不需要 agent 代打卡）。供 record_habit 对名。"""
    data = _get("/api/offline/bootstrap")
    return {"habits": data["habits"]}


@mcp.tool()
def delete_record(type: str, row_id: int) -> dict:
    """改口纠错：删除一条 diet 或 workout 记录（type ∈ diet/workout）。
    row_id 从 record_* 回执的 results[].row_id 或 query_today_summary 的
    entries[].id / workouts[].id 取。外部同步来源（三星/Keep）禁删（403）。
    删除前先向用户复述要删的内容并确认。"""
    rtype = type.strip()
    if rtype not in ("diet", "workout"):
        raise ApiError(f"type 仅支持 diet/workout：{rtype!r}")
    args = {"type": rtype, "row_id": row_id}
    key = _dedup_key("delete_record", args)
    if (hit := _dedup_hit(key)) is not None:
        return hit
    result = _post("/api/agent/delete", args)
    _dedup_store(key, result)
    return result
