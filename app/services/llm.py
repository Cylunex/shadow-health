"""LLM 智能分析：健康数据聚合 → Claude / OpenAI 分析、问答与餐照识别。

配置优先级：设置页（app_settings['llm_config']，运行时可改）→ .env 回退：
- Claude：ANTHROPIC_API_KEY（或 ANTHROPIC_AUTH_TOKEN）、ANTHROPIC_BASE_URL、LLM_MODEL
- OpenAI：OPENAI_API_KEY、OPENAI_BASE_URL（兼容端点如 DeepSeek/Ollama 也走这条）、
  OPENAI_MODEL

app_settings['llm_config'] 结构（设置页「AI 模型」表单维护）：
{"provider": "claude"|"openai",
 "claude": {"model": "", "api_key": "", "base_url": ""},
 "openai": {"model": "", "api_key": "", "base_url": ""}}
字段留空即回退 .env / 内置默认；两家配置各自保存，切换 provider 不丢。

单用户自用尺度：同步客户端 + FastAPI 线程池路由，分析结果缓存进 app_settings。
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import date, timedelta
from typing import Any, Callable

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
    PlanEnrollment,
    WeeklyReview,
    WorkoutLog,
    WorkoutPlan,
)
from app.timeutil import today_local

CONFIG_KEY = "llm_config"
PROVIDERS = ("claude", "openai")
PROVIDER_LABELS = {"claude": "Claude", "openai": "OpenAI"}
DEFAULT_MODELS = {"claude": "claude-opus-4-8", "openai": "gpt-5.1"}

SYSTEM_PROMPT = """你是 shadow-health 应用内置的私人健康数据分析师。用户是一名希望减脂、改善体能与作息的成年男性，长期用本应用记录身体指标、饮食、训练与养生习惯打卡。

你会收到一份从应用数据库聚合出的数据快照。请：
1. 只基于数据说话——引用具体数字与日期；数据缺失就明说缺什么，不要编造。
2. 输出结构化 markdown（小标题 + 少量列表），先给一句话总评，再分板块分析（体重体脂/活动步数/训练/饮食/睡眠/习惯），最后给 3-5 条下一步可执行建议（具体到数字，如"每日步数从 X 提到 Y"）。
3. 语气专业、直接、鼓励但不吹捧；用中文。
4. 建议要贴合用户目标（减脂：热量缺口 300-500kcal、蛋白 1.6-2.0g/kg、周有氧 ≥150 分钟、日步数 8000-10000）。
5. 若数据出现值得就医的信号（体重骤变、静息心率异常、血压异常等），温和提醒就医；结尾一句简短声明：分析仅供参考，不替代医疗建议。"""


# ---------- 配置解析（设置页 → .env 回退） ----------

def resolve_config(raw: Any) -> dict[str, Any]:
    """app_settings['llm_config'] 原始值 → 生效配置（纯函数，pytest 直测）。

    api_key/base_url 只取设置页显式填的值；为空时 SDK 会自己读对应环境变量
    （Claude 的 AUTH_TOKEN 形态只有 SDK 自动读取才正确，不能塞进 api_key 参数），
    configured 则把两处来源都算上。
    """
    cfg = raw if isinstance(raw, dict) else {}
    provider = cfg.get("provider")
    if provider not in PROVIDERS:
        provider = "claude"
    sub = cfg.get(provider) if isinstance(cfg.get(provider), dict) else {}
    api_key = str(sub.get("api_key") or "").strip()
    base_url = str(sub.get("base_url") or "").strip()
    model = str(sub.get("model") or "").strip()
    if provider == "claude":
        env_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        model = model or os.environ.get("LLM_MODEL", "") or DEFAULT_MODELS["claude"]
    else:
        env_key = bool(os.environ.get("OPENAI_API_KEY"))
        model = model or os.environ.get("OPENAI_MODEL", "") or DEFAULT_MODELS["openai"]
    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,        # 仅设置页的值；空 = 交给 SDK 读环境
        "base_url": base_url,
        "configured": bool(api_key) or env_key,
        "key_from_env": not api_key and env_key,
    }


def get_config(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, CONFIG_KEY)
    return resolve_config(row.value if row is not None else None)


def is_configured(db: Session) -> bool:
    return get_config(db)["configured"]


def model_name(db: Session) -> str:
    return get_config(db)["model"]


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

    # 围度：月度采样趋势（每月最后一次测量，近 12 个月）——围度变化慢，
    # 短区间首末对比几乎恒为「无变化」，月粒度才看得出趋势
    girth_lines: list[str] = []
    girth_since = today - timedelta(days=365)
    for field, label in (
        ("waist_cm", "腰围"), ("chest_cm", "胸围"), ("hip_cm", "臀围"),
        ("thigh_cm", "大腿围"), ("arm_cm", "臂围"),
    ):
        col = getattr(BodyMetrics, field)
        pts = db.execute(
            select(BodyMetrics.log_date, col)
            .where(BodyMetrics.log_date >= girth_since, col.is_not(None))
            .order_by(BodyMetrics.log_date)
        ).all()
        if not pts:
            continue
        by_month: dict[str, Any] = {}
        for d, v in pts:  # 已按日期升序：同月后写胜出 = 每月最后一次
            by_month[f"{d:%Y-%m}"] = v
        seg = " ".join(f"{m}:{_fmt(v)}" for m, v in by_month.items())
        change = ""
        if len(pts) >= 2:
            change = f"（{pts[0][0]}→{pts[-1][0]} 变化 {float(pts[-1][1]) - float(pts[0][1]):+.1f}cm）"
        girth_lines.append(f"- {label}(cm): {seg}{change}")
    if girth_lines:
        lines.append("\n## 围度（近一年月度采样，每月最后一次测量）")
        lines.extend(girth_lines)

    # 心情分（1-10 手记/agent，近 30 天全序列，紧凑单行）
    mood_since = today - timedelta(days=29)
    moods = db.execute(
        select(BodyMetrics.log_date, BodyMetrics.mood_score)
        .where(BodyMetrics.log_date >= mood_since, BodyMetrics.mood_score.is_not(None))
        .order_by(BodyMetrics.log_date)
    ).all()
    if moods:
        avg_mood = sum(m for _, m in moods) / len(moods)
        lines.append(
            f"\n## 心情分（1-10，近30天 {len(moods)} 天有记录，均值 {_fmt(avg_mood)}）"
        )
        lines.append("- " + " ".join(f"{d:%m-%d}:{m}" for d, m in moods))

    # 生命体征（血压/静息心率/血氧/内脏脂肪）：有记录才输出——这是 SYSTEM_PROMPT
    # 「值得就医的信号」提示的数据来源，此前一直没注入
    vitals_lines: list[str] = []
    for field, label, nd in (
        ("bp_systolic", "收缩压", 0), ("bp_diastolic", "舒张压", 0),
        ("resting_hr", "静息心率", 0), ("spo2_pct", "血氧%", 1),
        ("visceral_fat_level", "内脏脂肪等级", 0),
    ):
        col = getattr(BodyMetrics, field)
        pts = db.execute(
            select(BodyMetrics.log_date, col)
            .where(BodyMetrics.log_date >= since, col.is_not(None))
            .order_by(BodyMetrics.log_date)
        ).all()
        if pts:
            avg = sum(float(v) for _, v in pts) / len(pts)
            vitals_lines.append(
                f"- {label}: 最近 {pts[-1][0]} 为 {_fmt(pts[-1][1], nd)}，"
                f"{len(pts)} 天均值 {_fmt(avg, nd)}"
            )
    if vitals_lines:
        lines.append("\n## 生命体征")
        lines.extend(vitals_lines)

    # 手记备注（body_metrics.notes）：用户当天的主观记录，分析时是重要线索
    notes_rows = db.execute(
        select(BodyMetrics.log_date, BodyMetrics.notes)
        .where(BodyMetrics.log_date >= since, BodyMetrics.notes.is_not(None))
        .order_by(BodyMetrics.log_date)
    ).all()
    notes_rows = [(d, n.strip()) for d, n in notes_rows if n and n.strip()]
    if notes_rows:
        lines.append("\n## 手记备注（最近 10 条）")
        lines.extend(f"- {d}: {n[:80]}" for d, n in notes_rows[-10:])

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

    # 进行中的训练计划（enrollment status='active'）
    active_plans = db.execute(
        select(WorkoutPlan.name, PlanEnrollment.start_date)
        .join(WorkoutPlan, PlanEnrollment.plan_id == WorkoutPlan.id)
        .where(PlanEnrollment.status == "active")
        .order_by(PlanEnrollment.start_date)
    ).all()
    if active_plans:
        lines.append(
            "- 进行中计划：" + "；".join(f"「{n}」（{sd} 开始）" for n, sd in active_plans)
        )

    # 力量动作 PR（detail.strength 组次明细汇总，按最近练过排前 8）
    from app.services.pr import exercise_prs

    prs = exercise_prs(db)
    if prs:
        lines.append("\n## 力量动作 PR（✓=达 3×15 可进阶加重）")
        top = sorted(
            prs.items(), key=lambda kv: kv[1]["last_date"] or date.min, reverse=True
        )[:8]
        for name, p in top:
            seg = f"- {name}: 最多 {p['max_reps']} 次"
            if p["max_weight"]:
                seg += f"，最重 {_fmt(p['max_weight'])}kg"
            seg += f"（{p['sessions']} 次会话，最近 {p['last_date']}{'，✓' if p['ready'] else ''}）"
            lines.append(seg)

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

    # 习惯（active）：daily = 达标天数；weekly = 达标周数（与打卡模块同口径：
    # 周一起算、周内 done_count 求和 ≥ target，而非单日行数）
    habits = db.execute(select(Habit).where(Habit.active.is_(True))).scalars().all()
    if habits:
        lines.append(f"\n## 习惯打卡（近{days}天，daily 计天 / weekly 计周）")
        for h in habits:
            target = h.target_per_period or 1
            if h.period == "weekly":
                weeks: dict[date, int] = {}
                for d, c in db.execute(
                    select(HabitLog.log_date, HabitLog.done_count).where(
                        HabitLog.habit_id == h.id, HabitLog.log_date >= since
                    )
                ):
                    ws = d - timedelta(days=d.isoweekday() - 1)
                    weeks[ws] = weeks.get(ws, 0) + c
                done = sum(1 for total in weeks.values() if total >= target)
                lines.append(f"- {h.name}: {done} 周")
            else:
                done = db.execute(
                    select(func.count()).where(
                        HabitLog.habit_id == h.id,
                        HabitLog.log_date >= since,
                        HabitLog.done_count >= target,
                    )
                ).scalar_one()
                lines.append(f"- {h.name}: {done} 天")
            # 饮水类习惯（计数型）：光「达标天数」看不出每天喝了几杯，补近 14 天
            # 逐日计数明细（0 = 否决/未喝，缺日 = 没记）
            if "水" in h.name:
                cnts = db.execute(
                    select(HabitLog.log_date, HabitLog.done_count).where(
                        HabitLog.habit_id == h.id,
                        HabitLog.log_date >= today - timedelta(days=13),
                    ).order_by(HabitLog.log_date)
                ).all()
                if cnts:
                    lines.append(
                        f"  · 近14天逐日计数（目标 {target}/天）："
                        + " ".join(f"{d:%m-%d}:{c}" for d, c in cnts)
                    )

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
            if r.summary and r.summary.strip():  # 手写复盘：主观感受是数字之外的关键线索
                lines.append(f"  · 复盘：{r.summary.strip()[:120]}")

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
            if r.summary and r.summary.strip():
                lines.append(f"  · 复盘：{r.summary.strip()[:120]}")

    return "\n".join(lines)


# ---------- LLM 调用（Claude / OpenAI 双通道） ----------
class LLMError(Exception):
    """带用户可读中文信息的 LLM 调用错误。"""


# tool use 循环轮数上限：正常一问最多两三轮（查→记→复核），8 轮兜住失控循环
MAX_TOOL_ROUNDS = 8

# 工具执行回调：(工具名, 参数 dict) -> JSON 可序列化结果 dict（错误折成 {"error": ...}）
ToolExecutor = Callable[[str, dict], dict]


def _tool_result_json(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


def _call(
    db: Session,
    system: str,
    user_text: str,
    images: list[tuple[str, str]] | None = None,
    max_tokens: int = 8000,
    tools: list[dict[str, Any]] | None = None,
    tool_executor: ToolExecutor | None = None,
) -> str:
    """统一入口：images 为 (media_type, base64) 列表，由各家适配器拼内容块。

    tools（Claude 原生 {name, description, input_schema} 形态，OpenAI 由适配器
    转换）+ tool_executor 一起给才启用工具调用循环；不传即与旧行为完全一致。
    """
    cfg = get_config(db)
    if not cfg["configured"]:
        raise LLMError(
            f"未配置 {PROVIDER_LABELS[cfg['provider']]} API Key——"
            "到 设置 →「AI 模型」填入（或写进 .env）后即可使用。"
        )
    if tools is not None and tool_executor is None:
        raise ValueError("tools 与 tool_executor 必须成对提供")
    if cfg["provider"] == "openai":
        return _call_openai(cfg, system, user_text, images, max_tokens, tools, tool_executor)
    return _call_claude(cfg, system, user_text, images, max_tokens, tools, tool_executor)


def _call_claude(
    cfg: dict, system: str, user_text: str,
    images: list[tuple[str, str]] | None, max_tokens: int,
    tools: list[dict[str, Any]] | None = None,
    tool_executor: ToolExecutor | None = None,
) -> str:
    import anthropic

    kwargs: dict[str, Any] = {}
    if cfg["api_key"]:
        kwargs["api_key"] = cfg["api_key"]
    if cfg["base_url"]:
        kwargs["base_url"] = cfg["base_url"]
    client = anthropic.Anthropic(**kwargs)  # 未显式给的项 SDK 自动读环境

    content: str | list = user_text
    if images:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}}
            for mt, b64 in images
        ] + [{"type": "text", "text": user_text}]
    create_kwargs: dict[str, Any] = {}
    if tools:
        create_kwargs["tools"] = tools  # 已是 Claude 原生 {name, description, input_schema}
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.messages.create(
                model=cfg["model"],
                max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                system=system,
                messages=messages,
                **create_kwargs,
            )
            if response.stop_reason == "tool_use" and tool_executor is not None:
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        out = tool_executor(block.name, dict(block.input or {}))
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _tool_result_json(out),
                        })
                # assistant 回合原样回传（含 thinking 块——adaptive thinking 下
                # 工具循环必须带回 signature，缺了 API 会拒）
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                continue
            break
        else:
            raise LLMError("工具调用超过轮数上限，请换个问法或分步来。")
    except anthropic.AuthenticationError:
        raise LLMError("API Key 无效或已失效（401）——检查 设置→AI 模型 或 .env 的 Key。")
    except anthropic.NotFoundError:
        raise LLMError(f"模型 {cfg['model']} 不存在（404）——到 设置→AI 模型 改模型名。")
    except anthropic.RateLimitError:
        raise LLMError("请求被限流（429）——稍等一分钟再试。")
    except anthropic.APIStatusError as e:
        raise LLMError(f"API 错误（{e.status_code}）：{e.message}")
    except anthropic.APIConnectionError:
        raise LLMError("连不上 Anthropic API——检查网络；走代理/网关时在设置里填 Base URL。")

    if response.stop_reason == "refusal":
        raise LLMError("模型拒绝了本次请求，请调整提问后重试。")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise LLMError("模型没有返回内容，请重试。")
    return text


def _call_openai(
    cfg: dict, system: str, user_text: str,
    images: list[tuple[str, str]] | None, max_tokens: int,
    tools: list[dict[str, Any]] | None = None,
    tool_executor: ToolExecutor | None = None,
) -> str:
    import openai

    kwargs: dict[str, Any] = {}
    if cfg["api_key"]:
        kwargs["api_key"] = cfg["api_key"]
    if cfg["base_url"]:
        kwargs["base_url"] = cfg["base_url"]
    client = openai.OpenAI(**kwargs)  # 未显式给的项 SDK 自动读环境

    content: str | list = user_text
    if images:
        content = [
            {"type": "image_url", "image_url": {"url": f"data:{mt};base64,{b64}"}}
            for mt, b64 in images
        ] + [{"type": "text", "text": user_text}]
    # gpt-5/o 系推理模型只认 max_completion_tokens；DeepSeek/Ollama 等兼容端点
    # 与旧模型普遍只认 max_tokens——按模型名分流
    model_l = cfg["model"].lower()
    reasoning = model_l.startswith(("gpt-5", "o1", "o3", "o4"))
    limit_kw = {"max_completion_tokens" if reasoning else "max_tokens": max_tokens}
    if tools:
        # Claude 原生工具形态 → OpenAI function calling
        limit_kw["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(
                model=cfg["model"], messages=messages, **limit_kw
            )
            if not response.choices:
                raise LLMError("模型没有返回内容，请重试。")
            choice = response.choices[0]
            tool_calls = getattr(choice.message, "tool_calls", None)
            if tool_calls and tool_executor is not None:
                messages.append({
                    "role": "assistant",
                    "content": choice.message.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in tool_calls
                    ],
                })
                for tc in tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except ValueError:
                        args = {}
                    out = tool_executor(
                        tc.function.name, args if isinstance(args, dict) else {}
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _tool_result_json(out),
                    })
                continue
            break
        else:
            raise LLMError("工具调用超过轮数上限，请换个问法或分步来。")
    except openai.AuthenticationError:
        raise LLMError("API Key 无效或已失效（401）——检查 设置→AI 模型 或 .env 的 Key。")
    except openai.NotFoundError:
        raise LLMError(
            f"模型 {cfg['model']} 不存在（404）——到 设置→AI 模型 改模型名"
            "（兼容端点还要确认 Base URL）。"
        )
    except openai.RateLimitError:
        raise LLMError("请求被限流（429）——稍等一分钟再试。")
    except openai.APIStatusError as e:
        raise LLMError(f"API 错误（{e.status_code}）：{getattr(e, 'message', e)}")
    except openai.APIConnectionError:
        raise LLMError("连不上 OpenAI API——检查网络；自建/代理端点在设置里填 Base URL。")

    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise LLMError("模型拒绝了本次请求，请调整提问后重试。")
    text = (choice.message.content or "").strip()
    if not text:
        raise LLMError("模型没有返回内容（可能推理预算耗尽），请重试或换模型。")
    return text


# ---------- 餐食照片营养估算（对标 Keep 拍照识别） ----------
MEAL_PHOTO_PROMPT = """你是营养估算助手。观察这张餐食照片，识别其中的食物并按照片中实际可见份量估算营养（中式家常口径；无法判断时按常见一人份）。

只返回一个 JSON 对象（不要 markdown 代码块、不要多余文字），格式：
{"items": [{"name": "食物名(≤12字中文)", "amount_g": 估算克数, "kcal": 该份量总热量, "protein_g": 蛋白克数, "fat_g": 脂肪克数, "carb_g": 碳水克数}], "note": "一句话说明份量假设或不确定性"}

注意：营养值均为照片中这一份的总值，不是每100g；识别不出食物时 items 给空数组并在 note 说明。"""

# Claude/OpenAI 视觉接口共同支持的图片格式（HEIC 都不支持）
VISION_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")


def analyze_meal_photo(db: Session, image_bytes: bytes, media_type: str) -> dict[str, Any]:
    """餐食照片 → {"items": [...], "note": str}；解析失败抛 LLMError。"""
    import base64
    import json

    if media_type not in VISION_MEDIA_TYPES:
        raise LLMError("该图片格式暂不支持识别（支持 jpg/png/webp/gif）。")
    if len(image_bytes) > 5 * 1024 * 1024:
        raise LLMError("图片超过 5MB，API 不接受——拍照时选较低分辨率或压缩后再传。")
    text = _call(
        db,
        MEAL_PHOTO_PROMPT,
        "估算这张餐食照片的营养，按要求返回 JSON。",
        images=[(media_type, base64.b64encode(image_bytes).decode())],
        max_tokens=3000,
    )
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
        db,
        SYSTEM_PROMPT,
        f"{context}\n\n请基于以上数据快照，输出这段时间的健康分析报告。",
    )


ASK_ACTION_RULES = """

你还配备了一组工具，可以查询与写入用户的健康数据（记录饮食/训练/身体指标/习惯打卡、查当日汇总、查食物库、删除记错的记录）。使用规则（历史伤疤，必须严格遵守）：
1. 写入后的确认话术必须引用工具回执的 new 计数（如「已入库 new=2，今日累计 1830 kcal」）；没有回执或 new=0 时**禁止**使用任何「已记录」措辞，如实说明失败原因。
2. skipped>0 时如实说明（重复补发/当日已打卡），不得把 skipped 说成新记录。
3. 用户要补记历史但没给日期时，先反问确认日期（YYYY-MM-DD），不要猜。
4. 删除前先用 query_summary 找到 row_id 并向用户复述要删的内容；工具报错原样告知，不得掩饰成成功。
5. 记饮食优先 search_food 拿 food_id（营养按食物库自动算）；纯咨询问题不要调用写入工具。"""


def ask(db: Session, question: str, days: int = 30) -> tuple[str, list[dict[str, Any]]]:
    """带数据上下文与工具调用的自由问答（V5：能真正动手记录/纠错）。

    返回 (回答文本, 执行的写操作列表 [{tool, label, summary, ok}])——写操作
    经 ai_tools 走 agent 通道（import_raw 留档，/agent-log 可核对撤销）。
    """
    from app.services import ai_tools  # 延迟 import：ai_tools → routers → llm，防环

    actions: list[dict[str, Any]] = []

    def _exec(name: str, args: dict) -> dict:
        result = ai_tools.run_tool(db, name, args)
        if name in ai_tools.WRITE_TOOLS:  # 只把写操作列给用户核对，查询不刷屏
            actions.append({
                "tool": name,
                "label": ai_tools.TOOL_LABELS.get(name, name),
                "summary": ai_tools.receipt_summary(name, result),
                "ok": "error" not in result,
            })
        return result

    context = build_context(db, days=days)
    answer = _call(
        db,
        SYSTEM_PROMPT
        + "\n\n当前任务是回答用户的具体问题：直接针对问题作答，简洁为先，不必输出完整报告结构。"
        + ASK_ACTION_RULES,
        f"{context}\n\n用户的问题：{question}",
        max_tokens=4000,
        tools=ai_tools.TOOL_DEFS,
        tool_executor=_exec,
    )
    return answer, actions
