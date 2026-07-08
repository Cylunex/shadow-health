"""训练计划 seed（设计文档 §5.1，7 套）。

content_md 来源 app/seed/data/ 下 3 个 md 文件（私人内容素材，不入仓库，
见 app/seed/data/README.md）：
- 04 按「## 计划N」切成 5 套：每套 = 文档头部就医红线引用块 + 该计划章节原文
  + 文末「通用注意事项」整段 append（安全内容不可丢）；
- 05/06 整文件导入。
素材文件缺失时跳过对应计划（不报错），其余 seed 正常执行。
weekly_template / phases 按 §5.1、§3.4 的 JSON 结构手工结构化。
自然键 upsert：ON CONFLICT (name) DO NOTHING，幂等可重跑。
"""
from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import WorkoutPlan

DATA_DIR = Path(__file__).resolve().parent / "data"


def _read(name: str) -> str | None:
    path = DATA_DIR / name
    if not path.exists():
        print(f"seed plans: 缺少素材 {name}（不入仓库，见 app/seed/data/README.md），跳过相关计划")
        return None
    return path.read_text(encoding="utf-8-sig")


def _split_04(text: str) -> tuple[str, dict[str, str]]:
    """返回 (头部引用块, {heading行: 章节原文})。章节 = 从 '## ' 行到下一个 '## ' 前。"""
    matches = list(re.finditer(r"^## .*$", text, re.M))
    head = text[: matches[0].start()] if matches else text
    quote = "\n".join(line for line in head.splitlines() if line.lstrip().startswith(">"))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.start():end].rstrip()
        body = re.sub(r"\n-{3,}\s*$", "", body).rstrip()  # 去掉章节间分隔线
        sections[m.group(0)] = body
    return quote, sections


def _section_by_prefix(sections: dict[str, str], prefix: str) -> str:
    for heading, body in sections.items():
        if heading.startswith(prefix):
            return body
    raise ValueError(f"04_训练计划.md 未找到章节：{prefix}")


def _build_04_contents() -> dict[str, str]:
    """返回 {计划N: content_md}，N ∈ 一二三四五；素材缺失时返回空 dict。"""
    text = _read("04_训练计划.md")
    if text is None:
        return {}
    quote, sections = _split_04(text)
    notice = _section_by_prefix(sections, "## 通用注意事项")
    # 通用注意事项整段（列表部分），去掉其后文档页脚（--- + 配套阅读引用）
    idx = notice.find("\n---")
    if idx > 0:
        notice = notice[:idx].rstrip()
    contents: dict[str, str] = {}
    for cn in "一二三四五":
        section = _section_by_prefix(sections, f"## 计划{cn}")
        contents[f"计划{cn}"] = f"{quote}\n\n{section}\n\n{notice}\n"
    return contents


# ---------- 周表 / 分期 JSON（§5.1、§3.4 结构） ----------

# 04·计划一：weekday:0 每日项，三个 slot（晨/日间/睡前）
_P1_TEMPLATE = [
    {
        "weekday": 0,
        "sessions": [
            {"slot": "晨", "type": "起床喝温水；2 分钟提肛（收 3 秒/松 3 秒 ×20）"},
            {"slot": "日间", "type": "每久坐 1 小时起身活动 3–5 分钟；快走累计"},
            {
                "slot": "睡前",
                "type": "温水泡脚 15 分钟＋搓热按摩肾俞/命门＋涌泉各 100 下",
                "pelvic": "凯格尔基础：收 5 秒/松 5 秒 ×10 ×2 组",
            },
        ],
    },
]
_P1_PHASES = [
    {"weeks": [1, 1], "label": "第 1 周", "note": "先把「早睡 + 泡脚 + 提肛」养成习惯"},
    {"weeks": [2, 2], "label": "第 2 周", "note": "加满凯格尔 2 组；开始每周 3 次快走 30 分钟"},
    {"weeks": [3, 3], "label": "第 3 周", "note": "凯格尔加到 3 组；快走升级为快走/慢跑 150 分钟/周"},
    {"weeks": [4, 4], "label": "第 4 周", "note": "全部稳定执行，自评精力/睡眠/晨勃变化"},
]

# 04·计划二：无周表（降级深链），三主线周次表 → phases
_P2_PHASES = [
    {"weeks": [1, 2], "label": "盆底打底",
     "note": "盆底收 5 秒/松 5 秒 ×10 ×3 组；有氧 150–180 分/周＋力量 2–3 次/周；"
             "每天 5–10 分钟腹式呼吸/正念。第 2 周末：盆底找准发力、放松呼吸成习惯"},
    {"weeks": [3, 4], "label": "盆底进阶",
     "note": "收缩保持延长到 8 秒；加「快收快放」×10；继续有氧＋力量与放松训练。"
             "第 4 周末：晨勃频率/硬度自评提升"},
    {"weeks": [5, 8], "label": "盆底强化 + 实战巩固",
     "note": "每天累计 60–90 次（慢收＋快收混合），晨晚各一轮；感觉聚焦推进"
             "（第 6 周末插入不冲刺，第 8 周末放松状态下恢复满意硬度并巩固）"},
]

# 04·计划三：5 阶段周次表 → phases
_P3_PHASES = [
    {"weeks": [1, 1], "label": "准备",
     "note": "凯格尔打底（收 5 秒/松 5 秒 ×10 ×3）；学会腹式呼吸降兴奋；记录目前大致时长作基线"},
    {"weeks": [2, 3], "label": "兴奋控制",
     "note": "单人停-动法：自慰到 8–9 成兴奋即停，回落后再来，每次 3–4 回合再射；每周 3–4 次"},
    {"weeks": [4, 5], "label": "耐受＋挤压",
     "note": "加挤压法（临界点捏压龟头冠状沟）；放慢节奏、变换刺激强度练「耐受」"},
    {"weeks": [6, 6], "label": "仿真",
     "note": "用润滑模拟真实摩擦感，继续停-动/挤压；在更接近实战的刺激下练控制"},
    {"weeks": [7, 8], "label": "实战",
     "note": "与伴侣：感觉聚焦→插入后静止适应→慢节奏→接近临界停顿/变换体位；射精前主动收紧/放松盆底"},
]

# 04·计划四：4 阶段参数表 → phases
_P4_PHASES = [
    {"weeks": [1, 3], "label": "入门", "note": "慢肌：收 5 秒/松 5 秒 ×10；每天 3 组"},
    {"weeks": [4, 6], "label": "进阶", "note": "慢肌：收 8 秒/松 8 秒 ×10＋快收快放 ×10；每天 3 组"},
    {"weeks": [7, 9], "label": "强化", "note": "慢肌：收 10 秒/松 10 秒 ×12＋快收快放 ×15；每天 3–4 组"},
    {"weeks": [10, 12], "label": "巩固",
     "note": "收 10 秒 ×15，尝试坐/站/平躺不同体位＋快收快放 ×20；每天 3–4 组"},
]

# 04·计划五：周一~周日 7 条 + 3 分期
_P5_TEMPLATE = [
    {"weekday": 1, "sessions": [{"type": "力量 or 有氧（交替）＋睡前泡脚按摩", "pelvic": "当日凯格尔"}]},
    {"weekday": 2, "sessions": [{"type": "有氧 30–40 分钟＋10 分钟放松呼吸", "pelvic": "当日凯格尔"}]},
    {"weekday": 3, "sessions": [{"type": "力量 or 有氧（交替）＋睡前泡脚按摩", "pelvic": "当日凯格尔"}]},
    {"weekday": 4, "sessions": [{"type": "有氧 30–40 分钟＋10 分钟放松呼吸", "pelvic": "当日凯格尔"}]},
    {"weekday": 5, "sessions": [{"type": "力量 or 有氧（交替）＋睡前泡脚按摩", "pelvic": "当日凯格尔"}]},
    {"weekday": 6, "sessions": [{"type": "行为训练日（停-动/挤压 或 与伴侣感觉聚焦）", "pelvic": "当日凯格尔"}]},
    {"weekday": 7, "sessions": [{"type": "主动恢复（散步/拉伸）＋复盘记录＋食养加强"}]},
]
_P5_PHASES = [
    {"weeks": [1, 4], "label": "打基础",
     "note": "完成「计划一」全部习惯化；凯格尔入门；建立运动节奏；纠正烟酒熬夜"},
    {"weeks": [5, 8], "label": "练专项",
     "note": "按需求主攻：重硬度走计划二主线、重延时走计划三主线；凯格尔进阶；运动加量"},
    {"weeks": [9, 12], "label": "实战巩固",
     "note": "行为训练进入实战/伴侣配合阶段；凯格尔到强化期；固化放松心态；复盘整体变化"},
]

# 05·无器械综合训练：§四周表 7 条 + §六三阶段
_P05_TEMPLATE = [
    {"weekday": 1, "sessions": [{"type": "下肢/臀 无氧", "pelvic": "强化凯格尔（长收＋静蹲负荷下收）"}]},
    {"weekday": 2, "sessions": [{"type": "HIIT 有氧 20–25 分", "pelvic": "反向凯格尔（训练后放松呼吸）"}]},
    {"weekday": 3, "sessions": [{"type": "上肢＋核心 无氧", "pelvic": "基础凯格尔"}]},
    {"weekday": 4, "sessions": [{"type": "LISS 有氧（快走/慢跑/爬楼 40 分）", "pelvic": "反向凯格尔＋拉伸"}]},
    {"weekday": 5, "sessions": [{"type": "全身 无氧（循环）", "pelvic": "强化凯格尔（快肌快收快放）"}]},
    {"weekday": 6, "sessions": [{"type": "HIIT 或 户外有氧", "pelvic": "基础凯格尔"}]},
    {"weekday": 7, "sessions": [{"type": "主动恢复（拉伸/腹式呼吸/散步）", "pelvic": "反向凯格尔（深度放松）"}]},
]
_P05_PHASES = [
    {"weeks": [1, 4], "label": "入门",
     "note": "退阶动作（跪姿俯卧撑等）2–3 组 ×8–12；HIIT 20 秒练/40 秒歇 ×6–8 轮；"
             "凯格尔基础：收5放5 ×10 ×3 组＋反向 5 分呼吸"},
    {"weeks": [5, 8], "label": "进阶",
     "note": "标准动作 3–4 组 ×12–15，加保加利亚/派克；HIIT 30 秒/30 秒 ×8–10 轮；"
             "强化凯格尔：收10s×10＋快收快放×15＋静蹲负荷下收；反向＋拉伸"},
    {"weeks": [9, 12], "label": "强化整合",
     "note": "难度变式（钻石/爆发/单腿渐进）4 组、缩短组间歇；HIIT 40 秒/20 秒 ×10–12 轮"
             "或波比金字塔；强化凯格尔：收10s×12–15＋快肌×20＋负荷下；反向深度放松"},
]

# 06·减脂早晚徒手：循环计划，每天早晚两个 session，无 phases
_P06_EVENING = {
    1: {"type": "下肢/臀 力量循环", "pelvic": "强化凯格尔（静蹲负荷下）"},
    2: {"type": "HIIT 代谢循环", "pelvic": "反向凯格尔（练后放松）"},
    3: {"type": "上肢＋核心 力量循环", "pelvic": "基础凯格尔"},
    4: {"type": "HIIT 或 中低有氧（轻）", "pelvic": "反向凯格尔＋拉伸"},
    5: {"type": "全身 力量循环", "pelvic": "强化凯格尔（快肌）"},
    6: {"type": "HIIT 或 户外较长有氧", "pelvic": "基础凯格尔"},
    7: {"type": "主动恢复（拉伸/散步/呼吸）", "pelvic": "反向凯格尔（深度放松）"},
}
_P06_TEMPLATE = [
    {
        "weekday": wd,
        "sessions": [
            {"slot": "早", "type": "燃脂有氧 20–30 分（稳态 或 低冲击循环）",
             "pelvic": "收尾反向凯格尔呼吸"},
            {"slot": "晚", **_P06_EVENING[wd]},
        ],
    }
    for wd in range(1, 8)
]


def _plan_rows() -> list[dict]:
    c04 = _build_04_contents()
    md05 = _read("05_无器械综合训练计划.md")
    md06 = _read("06_减脂·早晚徒手计划.md")
    rows: list[dict] = []
    if c04:
        rows += [
            dict(name="04·计划一 养肾基础 28 天", goal="养肾基础", duration_weeks=4,
                 source_doc="04_训练计划.md#计划一", content_md=c04["计划一"],
                 weekly_template=_P1_TEMPLATE, phases=_P1_PHASES),
            dict(name="04·计划二 硬度提升", goal="硬度提升", duration_weeks=8,
                 source_doc="04_训练计划.md#计划二", content_md=c04["计划二"],
                 weekly_template=None, phases=_P2_PHASES),
            dict(name="04·计划三 延时控制", goal="延时控制", duration_weeks=8,
                 source_doc="04_训练计划.md#计划三", content_md=c04["计划三"],
                 weekly_template=None, phases=_P3_PHASES),
            dict(name="04·计划四 盆底 12 周进阶", goal="盆底专项", duration_weeks=12,
                 source_doc="04_训练计划.md#计划四", content_md=c04["计划四"],
                 weekly_template=None, phases=_P4_PHASES),
            dict(name="04·计划五 12 周综合整合", goal="综合整合", duration_weeks=12,
                 source_doc="04_训练计划.md#计划五", content_md=c04["计划五"],
                 weekly_template=_P5_TEMPLATE, phases=_P5_PHASES),
        ]
    if md05 is not None:
        rows.append(
            dict(name="05·无器械综合训练", goal="无器械综合体能", duration_weeks=12,
                 source_doc="05_无器械综合训练计划.md", content_md=md05,
                 weekly_template=_P05_TEMPLATE, phases=_P05_PHASES))
    if md06 is not None:
        rows.append(
            dict(name="06·减脂早晚徒手", goal="减脂", duration_weeks=None,
                 source_doc="06_减脂·早晚徒手计划.md", content_md=md06,
                 weekly_template=_P06_TEMPLATE, phases=None))
    return rows


def seed(db: Session) -> int:
    """幂等 seed：返回新插入条数。用 RETURNING 精确统计（rowcount 对 ON CONFLICT 可能报 -1）。"""
    inserted = 0
    for row in _plan_rows():
        new_id = db.execute(
            pg_insert(WorkoutPlan)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["name"])
            .returning(WorkoutPlan.id)
        ).scalar_one_or_none()
        if new_id is not None:
            inserted += 1
    return inserted
