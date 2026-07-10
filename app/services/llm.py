"""LLM 智能分析：健康数据聚合 → Claude 分析/问答。

配置（.env）：
- ANTHROPIC_API_KEY   必填，SDK 自动从环境读取（config._load_dotenv 已注入 os.environ）
- ANTHROPIC_BASE_URL  可选，走代理/网关时设置（SDK 自动读取）
- LLM_MODEL           可选，默认 claude-opus-4-8

单用户自用尺度：同步客户端 + FastAPI 线程池路由，分析结果缓存进 app_settings。
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AppSetting,
    BodyMetrics,
    DailyActivity,
    DietLog,
    Habit,
    HabitLog,
    MonthlyReview,
    WeeklyReview,
    WorkoutLog,
)
from app.timeutil import today_local

DEFAULT_MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """你是 shadow-health 应用内置的私人健康数据分析师。用户是一名希望减脂、改善体能与作息的成年男性，长期用本应用记录身体指标、饮食、训练与养生习惯打卡。

你会收到一份从应用数据库聚合出的数据快照。请：
1. 只基于数据说话——引用具体数字与日期；数据缺失就明说缺什么，不要编造。
2. 输出结构化 markdown（小标题 + 少量列表），先给一句话总评，再分板块分析（体重体脂/活动步数/训练/饮食/睡眠/习惯），最后给 3-5 条下一步可执行建议（具体到数字，如"每日步数从 X 提到 Y"）。
3. 语气专业、直接、鼓励但不吹捧；用中文。
4. 建议要贴合用户目标（减脂：热量缺口 300-500kcal、蛋白 1.6-2.0g/kg、周有氧 ≥150 分钟、日步数 8000-10000）。
5. 若数据出现值得就医的信号（体重骤变、静息心率异常、血压异常等），温和提醒就医；结尾一句简短声明：分析仅供参考，不替代医疗建议。"""


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def model_name() -> str:
    return os.environ.get("LLM_MODEL", DEFAULT_MODEL)


# ---------- 数据聚合：紧凑上下文包 ----------
def _fmt(v: Any, nd: int = 1) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:.{nd}f}".rstrip("0").rstrip(".")


def build_context(db: Session, days: int = 30) -> str:
    """聚合近 N 天健康数据为紧凑文本快照（控制在几千 token 内）。"""
    today = today_local()
    since = today - timedelta(days=days - 1)
    lines: list[str] = [f"# 数据快照（{since} ~ {today}，今天 {today}）"]

    # 目标与档案
    settings = {r.key: r.value for r in db.execute(select(AppSetting)).scalars()}
    lines.append("\n## 目标/档案")
    for key, label in (
        ("height_cm", "身高cm"), ("target_weight_kg", "目标体重kg"),
        ("target_kcal", "目标热量kcal/日"), ("target_protein_g", "目标蛋白g/日"),
        ("target_steps", "目标步数/日"), ("target_weekly_cardio_min", "周有氧目标min"),
    ):
        v = settings.get(key)
        if v is not None:
            lines.append(f"- {label}: {v}")

    # 体重/体成分（区间内全部有值记录，最多 40 条）
    rows = db.execute(
        select(BodyMetrics)
        .where(BodyMetrics.log_date >= since, BodyMetrics.weight_kg.is_not(None))
        .order_by(BodyMetrics.log_date)
    ).scalars().all()
    lines.append(f"\n## 体重记录（{len(rows)} 条）")
    for r in rows[-40:]:
        seg = f"- {r.log_date}: {_fmt(r.weight_kg)}kg"
        if r.body_fat_pct is not None:
            seg += f", 体脂{_fmt(r.body_fat_pct)}%"
        if r.skeletal_muscle_kg is not None:
            seg += f", 骨骼肌{_fmt(r.skeletal_muscle_kg)}kg"
        lines.append(seg)
    # 历史锚点：区间外最近一条 + 一年前最近一条，供趋势对比
    for label, cutoff in (("区间前最近", since), ("一年前", today - timedelta(days=365))):
        anchor = db.execute(
            select(BodyMetrics)
            .where(BodyMetrics.log_date < cutoff, BodyMetrics.weight_kg.is_not(None))
            .order_by(BodyMetrics.log_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if anchor:
            lines.append(f"- （{label}锚点）{anchor.log_date}: {_fmt(anchor.weight_kg)}kg")

    # 围度（区间内首末对比，只列有数据的部位）
    girth_lines: list[str] = []
    for field, label in (
        ("waist_cm", "腰围"), ("chest_cm", "胸围"), ("hip_cm", "臀围"),
        ("thigh_cm", "大腿围"), ("arm_cm", "臂围"),
    ):
        col = getattr(BodyMetrics, field)
        pts = db.execute(
            select(BodyMetrics.log_date, col)
            .where(BodyMetrics.log_date >= since, col.is_not(None))
            .order_by(BodyMetrics.log_date)
        ).all()
        if pts:
            seg = f"- {label}: {pts[0][0]} {_fmt(pts[0][1])}cm → {pts[-1][0]} {_fmt(pts[-1][1])}cm"
            if len(pts) >= 2:
                seg += f"（变化 {float(pts[-1][1]) - float(pts[0][1]):+.1f}cm）"
            girth_lines.append(seg)
    if girth_lines:
        lines.append("\n## 围度")
        lines.extend(girth_lines)

    # 睡眠（body_metrics.sleep_hours 已含自动回填）
    sleep_days, sleep_avg = db.execute(
        select(func.count(), func.avg(BodyMetrics.sleep_hours)).where(
            BodyMetrics.log_date >= since, BodyMetrics.sleep_hours.is_not(None)
        )
    ).one()
    lines.append(f"\n## 睡眠：{sleep_days} 天有数据，均值 {_fmt(sleep_avg)}h")

    # 步数/心率
    act = db.execute(
        select(
            func.count(), func.avg(DailyActivity.steps), func.max(DailyActivity.steps),
            func.min(DailyActivity.steps), func.avg(DailyActivity.hr_min),
        ).where(DailyActivity.log_date >= since, DailyActivity.steps.is_not(None))
    ).one()
    target_steps = settings.get("target_steps") or 8000
    ok_days = db.execute(
        select(func.count()).where(
            DailyActivity.log_date >= since, DailyActivity.steps >= int(target_steps)
        )
    ).scalar_one()
    lines.append(
        f"\n## 步数：{act[0]} 天有数据，日均 {_fmt(act[1], 0)}，最高 {act[2]}，最低 {act[3]}，"
        f"达标(≥{target_steps}) {ok_days} 天；日最低心率均值 {_fmt(act[4], 0)}"
    )

    # 训练（含 sRPE 负荷 = RPE × 分钟，未评级分钟单列）
    wl = db.execute(
        select(WorkoutLog.session_type, WorkoutLog.duration_min, WorkoutLog.log_date, WorkoutLog.rpe)
        .where(WorkoutLog.log_date >= since)
    ).all()
    total_min = sum(r[1] or 0 for r in wl)
    types = Counter(r[0] or "?" for r in wl)
    load = sum((r[3] or 0) * (r[1] or 0) for r in wl)
    unrated_min = sum(r[1] or 0 for r in wl if not r[3])
    lines.append(
        f"\n## 训练：{len(wl)} 次，共 {total_min} 分钟（≈{_fmt(total_min / max(days / 7, 1), 0)} 分钟/周）；"
        f"sRPE 负荷 {load}（未评级 {unrated_min} 分钟）；类型分布 {dict(types.most_common(8))}"
    )

    # 饮食（近 14 天更有代表性）
    diet_since = today - timedelta(days=13)
    diet = db.execute(
        select(
            DietLog.log_date,
            func.sum(DietLog.kcal),
            func.sum(DietLog.protein_g),
            func.count(),
        )
        .where(DietLog.log_date >= diet_since)
        .group_by(DietLog.log_date)
        .order_by(DietLog.log_date)
    ).all()
    lines.append(f"\n## 饮食（近14天，{len(diet)} 天有记录）")
    for d, kcal, protein, n in diet:
        lines.append(f"- {d}: {_fmt(kcal, 0)}kcal, 蛋白{_fmt(protein, 0)}g（{n}笔）")

    # 习惯（active，近 N 天完成天数）
    habits = db.execute(select(Habit).where(Habit.active.is_(True))).scalars().all()
    if habits:
        lines.append(f"\n## 习惯打卡（近{days}天完成天数）")
        for h in habits:
            done = db.execute(
                select(func.count()).where(
                    HabitLog.habit_id == h.id,
                    HabitLog.log_date >= since,
                    HabitLog.done_count >= (h.target_per_period or 1),
                )
            ).scalar_one()
            unit = "周" if h.period == "weekly" else "天"
            lines.append(f"- {h.name}: {done} {unit if h.period == 'weekly' else '天'}")

    # 周报快照（最近 4 份）
    reviews = db.execute(
        select(WeeklyReview).order_by(WeeklyReview.week_start.desc()).limit(4)
    ).scalars().all()
    if reviews:
        lines.append("\n## 周报快照（新→旧）")
        for r in reviews:
            snap = r.metrics_snapshot or {}
            lines.append(
                f"- {r.week_start} 起：体重变化{snap.get('weight_change', '-')}kg，"
                f"日均{snap.get('avg_kcal', '-')}kcal，训练{snap.get('workout_count', '-')}次/"
                f"{snap.get('workout_min', '-')}min（有氧{snap.get('cardio_min', '-')}min），"
                f"打卡率{snap.get('habit_rate', '-')}%，日均步数{snap.get('avg_steps', '-')}"
            )

    # 月报快照（最近 3 份）
    months = db.execute(
        select(MonthlyReview).order_by(MonthlyReview.month_start.desc()).limit(3)
    ).scalars().all()
    if months:
        lines.append("\n## 月报快照（新→旧）")
        for r in months:
            snap = r.metrics_snapshot or {}
            lines.append(
                f"- {r.month_start.year}年{r.month_start.month}月：体重变化{snap.get('weight_change', '-')}kg，"
                f"体脂变化{snap.get('body_fat_change', '-')}%，训练{snap.get('workout_count', '-')}次/"
                f"{snap.get('workout_min', '-')}min，有氧达标{snap.get('cardio_weeks_ok', '-')}/"
                f"{snap.get('cardio_weeks_total', '-')}周，打卡率{snap.get('habit_rate', '-')}%，"
                f"饮食记录{snap.get('diet_days', '-')}天（日均{snap.get('avg_kcal', '-')}kcal），"
                f"日均步数{snap.get('avg_steps', '-')}（达标{snap.get('steps_ok_days', '-')}天）"
            )

    return "\n".join(lines)


# ---------- Claude 调用 ----------
class LLMError(Exception):
    """带用户可读中文信息的 LLM 调用错误。"""


def _call(system: str, user_content: str | list, max_tokens: int = 8000) -> str:
    """user_content 传 str 为纯文本；传 list 为内容块数组（支持 image 块）。"""
    import anthropic

    if not is_configured():
        raise LLMError("未配置 ANTHROPIC_API_KEY——在 .env 里填入后重启应用即可使用 AI 分析。")

    client = anthropic.Anthropic()  # key/base_url 自动从环境读取
    try:
        response = client.messages.create(
            model=model_name(),
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.AuthenticationError:
        raise LLMError("API Key 无效或已失效（401）——检查 .env 的 ANTHROPIC_API_KEY。")
    except anthropic.NotFoundError:
        raise LLMError(f"模型 {model_name()} 不存在（404）——检查 .env 的 LLM_MODEL。")
    except anthropic.RateLimitError:
        raise LLMError("请求被限流（429）——稍等一分钟再试。")
    except anthropic.APIStatusError as e:
        raise LLMError(f"API 错误（{e.status_code}）：{e.message}")
    except anthropic.APIConnectionError:
        raise LLMError(
            "连不上 Anthropic API——检查网络；如需代理，在 .env 设置 ANTHROPIC_BASE_URL。"
        )

    if response.stop_reason == "refusal":
        raise LLMError("模型拒绝了本次请求，请调整提问后重试。")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise LLMError("模型没有返回内容，请重试。")
    return text


# ---------- 餐食照片营养估算（对标 Keep 拍照识别） ----------
MEAL_PHOTO_PROMPT = """你是营养估算助手。观察这张餐食照片，识别其中的食物并按照片中实际可见份量估算营养（中式家常口径；无法判断时按常见一人份）。

只返回一个 JSON 对象（不要 markdown 代码块、不要多余文字），格式：
{"items": [{"name": "食物名(≤12字中文)", "amount_g": 估算克数, "kcal": 该份量总热量, "protein_g": 蛋白克数, "fat_g": 脂肪克数, "carb_g": 碳水克数}], "note": "一句话说明份量假设或不确定性"}

注意：营养值均为照片中这一份的总值，不是每100g；识别不出食物时 items 给空数组并在 note 说明。"""

# Claude API 支持的图片格式（HEIC 不支持）
VISION_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")


def analyze_meal_photo(image_bytes: bytes, media_type: str) -> dict[str, Any]:
    """餐食照片 → {"items": [...], "note": str}；解析失败抛 LLMError。"""
    import base64
    import json

    if media_type not in VISION_MEDIA_TYPES:
        raise LLMError("该图片格式暂不支持识别（支持 jpg/png/webp/gif）。")
    if len(image_bytes) > 5 * 1024 * 1024:
        raise LLMError("图片超过 5MB，API 不接受——拍照时选较低分辨率或压缩后再传。")
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(image_bytes).decode(),
            },
        },
        {"type": "text", "text": "估算这张餐食照片的营养，按要求返回 JSON。"},
    ]
    text = _call(MEAL_PHOTO_PROMPT, content, max_tokens=3000)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("识别结果不是有效 JSON，请重试。")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        raise LLMError("识别结果解析失败，请重试。")

    def _num(v: Any, lo: float, hi: float) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return round(f, 1) if lo <= f <= hi else None

    items: list[dict[str, Any]] = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:20]
        if not name:
            continue
        items.append({
            "name": name,
            "amount_g": _num(it.get("amount_g"), 1, 5000),
            "kcal": _num(it.get("kcal"), 0, 5000),
            "protein_g": _num(it.get("protein_g"), 0, 500),
            "fat_g": _num(it.get("fat_g"), 0, 500),
            "carb_g": _num(it.get("carb_g"), 0, 1000),
        })
    return {"items": items, "note": str(data.get("note") or "").strip()[:200]}


def analyze(db: Session, days: int = 30) -> str:
    """生成健康数据分析报告（markdown）。"""
    context = build_context(db, days=days)
    return _call(
        SYSTEM_PROMPT,
        f"{context}\n\n请基于以上数据快照，输出这段时间的健康分析报告。",
    )


def ask(db: Session, question: str, days: int = 30) -> str:
    """带数据上下文的自由问答。"""
    context = build_context(db, days=days)
    return _call(
        SYSTEM_PROMPT
        + "\n\n当前任务是回答用户的具体问题：直接针对问题作答，简洁为先，不必输出完整报告结构。",
        f"{context}\n\n用户的问题：{question}",
        max_tokens=4000,
    )
