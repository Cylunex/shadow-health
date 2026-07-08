"""recipes seed：7 方药膳（设计文档 §5.4，源 02_食疗药膳与药酒.md §三）。

幂等：INSERT ... ON CONFLICT (name) DO NOTHING，人工改过的内容不回滚。
契约：effect_tags 受控词表 {平补,温阳,滋阴,填精,固精,健脾,强腰}；
contraindications = 方子专属禁忌（如有）+ 统一体质提示（02§三 搭配原则）。
"""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Recipe

SOURCE = "02_食疗药膳与药酒.md §三"

# 统一体质提示（02§三 搭配原则），append 到每方 contraindications 末尾
BODY_HINT = (
    "体质提示：阳虚畏寒者可加温性食材（羊肉、韭菜、肉桂、生姜）；"
    "阴虚火旺者加滋润食材（银耳、桑椹、鸭肉、海参）；"
    "湿热体质（口苦、舌苔黄腻、前列腺炎）应先清利再进补，不宜急补。"
)

RECIPES: list[dict] = [
    {
        "name": "枸杞山药排骨(或羊肉)汤",
        "ingredients": "枸杞15g、山药200g、排骨（或羊肉）300g、生姜3片、红枣3枚",
        "method": "排骨（或羊肉）焯水去沫，与姜片、红枣加水炖约1小时，下山药块再炖20分钟，出锅前5分钟加枸杞，少盐调味。",
        "effects": "平补肝肾，气血双调，阴虚阳虚都较安全。",
        "effect_tags": ["平补"],
        "extra_contra": None,
    },
    {
        "name": "核桃栗子炖鸡",
        "ingredients": "核桃仁30g、栗子仁100g、鸡半只（约500g）、生姜3片",
        "method": "鸡块焯水，与姜片同炖40分钟，加入栗子、核桃再炖30分钟至软糯，少盐调味。",
        "effects": "温肾健脾，适合腰膝酸软、畏寒怕冷者。",
        "effect_tags": ["温阳", "健脾"],
        "extra_contra": None,
    },
    {
        "name": "韭菜炒虾仁(或炒核桃)",
        "ingredients": "韭菜200g、虾仁150g（或核桃仁50g）、生姜少许",
        "method": "热锅少油爆香姜末，大火快炒虾仁至变色，下韭菜段翻炒断生即出锅。",
        "effects": "温阳助性，适合阳虚畏寒者。",
        "effect_tags": ["温阳"],
        "extra_contra": "阴虚火旺者少吃",
    },
    {
        "name": "黑豆黑芝麻核桃糊",
        "ingredients": "黑豆50g、黑芝麻30g、核桃仁30g（可加黑米20g）",
        "method": "黑豆提前浸泡，全部材料入破壁机/豆浆机打糊煮沸即可，可加少量蜂蜜调味。",
        "effects": "填精养发，'黑色入肾'，适合作日常早餐。",
        "effect_tags": ["填精"],
        "extra_contra": None,
    },
    {
        "name": "海参枸杞瘦肉汤",
        "ingredients": "泡发海参2只、枸杞10g、瘦猪肉100g、生姜2片",
        "method": "瘦肉切片焯水，与姜片加水煮20分钟，下海参再煮10分钟，出锅前加枸杞，少盐调味。",
        "effects": "滋阴益精，性质温和。",
        "effect_tags": ["滋阴", "填精"],
        "extra_contra": None,
    },
    {
        "name": "芡实莲子山药粥",
        "ingredients": "芡实20g、莲子20g、山药100g、大米50g",
        "method": "芡实、莲子提前浸泡，与大米同煮成粥，山药切块后下，煮至软糯。",
        "effects": "固涩食养，针对遗精早泄、脾肾两虚。",
        "effect_tags": ["固精", "健脾"],
        "extra_contra": None,
    },
    {
        "name": "杜仲(或巴戟天)煲猪腰",
        "ingredients": "杜仲15g（或巴戟天10g）、猪腰1对、生姜3片",
        "method": "猪腰剖开去臊腺、切花刀后焯水；与药材、姜片加水煲40分钟，少盐调味。",
        "effects": "补肾强腰，传统'以形补形'。",
        "effect_tags": ["温阳", "强腰"],
        "extra_contra": "含药材偏温补，建议咨询中医师",
    },
]


def seed(db: Session) -> int:
    rows = []
    for r in RECIPES:
        contra = f"{r['extra_contra']}；{BODY_HINT}" if r["extra_contra"] else BODY_HINT
        rows.append(
            {
                "name": r["name"],
                "ingredients": r["ingredients"],
                "method": r["method"],
                "effects": r["effects"],
                "effect_tags": r["effect_tags"],
                "contraindications": contra,
                "source": SOURCE,
            }
        )
    stmt = (
        insert(Recipe)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["name"])
        .returning(Recipe.id)
    )
    inserted = db.execute(stmt).fetchall()
    return len(inserted)
