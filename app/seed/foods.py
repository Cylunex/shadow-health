"""foods seed：读 foods_base.csv upsert + 内置 TCM 标签 dict 按 name 打 tcm_tags（设计文档 §5.3）。

幂等：INSERT ... ON CONFLICT (name) DO NOTHING，人工改过的内容不回滚。
"""
from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Food

CSV_PATH = Path(__file__).parent / "data" / "foods_base.csv"

# §5.3 受控词表（严格）：已剔除 02 标注"不建议"的麻雀；滋阴+填精 同一集合打双标签。
TCM_TAG_SETS: dict[str, set[str]] = {
    "温阳": {"韭菜", "核桃", "羊肉", "虾", "海参", "泥鳅", "肉桂", "生姜"},
    "滋阴": {"黑芝麻", "黑豆", "桑椹", "枸杞", "山药", "栗子", "牡蛎", "鸭肉", "甲鱼", "银耳"},
    "填精": {"黑芝麻", "黑豆", "桑椹", "枸杞", "山药", "栗子", "牡蛎", "鸭肉", "甲鱼", "银耳"},
    "固精": {"芡实", "莲子", "山药", "白果", "金樱子"},
    "平补": {"枸杞", "山药", "黑芝麻", "核桃", "黑豆", "桑椹"},
    "黑色入肾": {"黑豆", "黑芝麻", "黑米", "黑木耳", "桑椹"},
    "补锌": {"南瓜子", "牡蛎", "贝类", "瘦牛肉"},
    "护血管": {"深海鱼", "番茄", "坚果"},
}
# 输出顺序与 §3.3 词表一致
TAG_ORDER = ("温阳", "滋阴", "填精", "固精", "平补", "黑色入肾", "补锌", "护血管")

NOTES: dict[str, str] = {
    "白果": "有小毒，须煮熟且少量食用",
}


def _tags_for(name: str) -> list[str] | None:
    tags = [t for t in TAG_ORDER if name in TCM_TAG_SETS[t]]
    return tags or None


def _num(value: str | None) -> str | None:
    v = (value or "").strip()
    return v or None


def seed(db: Session) -> int:
    rows: list[dict] = []
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            name = (r.get("name") or "").strip()
            if not name:
                continue
            rows.append(
                {
                    "name": name,
                    "category": (r.get("category") or "").strip() or None,
                    "kcal_per_100g": _num(r.get("kcal_per_100g")),
                    "protein_g": _num(r.get("protein_g")),
                    "fat_g": _num(r.get("fat_g")),
                    "carb_g": _num(r.get("carb_g")),
                    "tcm_tags": _tags_for(name),
                    "notes": NOTES.get(name),
                }
            )
    if not rows:
        return 0

    # 自检：TCM 词表引用的食材名必须都在 CSV 中，避免标签落空
    csv_names = {row["name"] for row in rows}
    referenced = {n for names in TCM_TAG_SETS.values() for n in names}
    missing = sorted(referenced - csv_names)
    if missing:
        raise ValueError(f"foods_base.csv 缺少 §5.3 TCM 食材: {missing}")

    stmt = (
        insert(Food)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["name"])
        .returning(Food.id)
    )
    inserted = db.execute(stmt).fetchall()
    return len(inserted)
