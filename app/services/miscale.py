"""小米体脂秤 2（XMTZC05HM）体成分计算。

秤的 BLE 广播只给体重 + 生物阻抗，体脂率等由 App 侧公式计算。
本实现按社区逆向的小米/华米公式（openScale MiScaleLib、lolouk44/xiaomi_mi_scale、
bodymiscale 同源），输入：性别 male/female、年龄（岁）、身高 cm、体重 kg、阻抗 Ω。

注意：BIA 公式本就是估算，不同 App 间 ±2% 体脂差异正常；趋势比绝对值有意义。
"""
from __future__ import annotations

from typing import Any


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _lbm_coefficient(sex: str, age: float, height: float, weight: float, impedance: float) -> float:
    """瘦体重系数（后续各项的公共中间量）。"""
    lbm = (height * 9.058 / 100) * (height / 100)
    lbm += weight * 0.32 + 12.226
    lbm -= impedance * 0.0068
    lbm -= age * 0.0542
    return lbm


def _fat_pct(sex: str, age: float, height: float, weight: float, lbm: float) -> float:
    if sex == "female":
        const = 9.25 if age <= 49 else 7.25
    else:
        const = 0.8
    coefficient = 1.0
    if sex == "male" and weight < 61:
        coefficient = 0.98
    elif sex == "female" and weight > 60:
        coefficient = 0.96
        if height > 160:
            coefficient *= 1.03
    elif sex == "female" and weight < 50:
        coefficient = 1.02
        if height > 160:
            coefficient *= 1.03
    fat = (1.0 - ((lbm - const) * coefficient / weight)) * 100
    if fat > 63:
        fat = 75
    return _clamp(fat, 5, 75)


def _water_pct(fat_pct: float) -> float:
    water = (100 - fat_pct) * 0.7
    coefficient = 1.02 if water <= 50 else 0.98
    if water * coefficient >= 65:
        water = 75
        return _clamp(water, 35, 75)
    return _clamp(water * coefficient, 35, 75)


def _bone_mass(sex: str, lbm: float) -> float:
    base = 0.245691014 if sex == "female" else 0.18016894
    bone = (base - lbm * 0.05158) * -1
    bone += 0.1 if bone > 2.2 else -0.1
    if sex == "female" and bone > 5.1:
        bone = 8
    elif sex == "male" and bone > 5.2:
        bone = 8
    return _clamp(bone, 0.5, 8)


def _muscle_mass(sex: str, weight: float, fat_pct: float, bone: float) -> float:
    muscle = weight - fat_pct * 0.01 * weight - bone
    if sex == "female" and muscle >= 84:
        muscle = 120
    elif sex == "male" and muscle >= 93.5:
        muscle = 120
    return _clamp(muscle, 10, 120)


def _visceral_fat(sex: str, age: float, height: float, weight: float) -> float:
    """内脏脂肪等级：只依赖身高/体重/年龄（小米公式不吃阻抗）。"""
    if sex == "female":
        if weight > (13 - height * 0.5) * -1:
            subsubcalc = ((height * 1.45) + (height * 0.1158) * height) - 120
            subcalc = weight * 500 / subsubcalc
            vfal = (subcalc - 6) + age * 0.07
        else:
            subcalc = 0.691 + height * -0.0024 + height * -0.0024
            vfal = (((height * 0.027) - (subcalc * weight)) * -1) + age * 0.07 - age
    else:
        if height < weight * 1.6:
            subcalc = ((height * 0.4) - (height * (height * 0.0826))) * -1
            vfal = ((weight * 305) / (subcalc + 48)) - 2.9 + age * 0.15
        else:
            subcalc = 0.765 + height * -0.0015
            vfal = (((height * 0.143) - (weight * subcalc)) * -1) + age * 0.15 - 5.0
    return _clamp(vfal, 1, 50)


def _bmr(sex: str, age: float, height: float, weight: float) -> float:
    if sex == "female":
        bmr = 864.6 + weight * 10.2036 - height * 0.39336 - age * 6.204
        if bmr > 2996:
            bmr = 5000
    else:
        bmr = 877.8 + weight * 14.916 - height * 0.726 - age * 8.976
        if bmr > 2322:
            bmr = 5000
    return _clamp(bmr, 500, 10000)


def compute_body_metrics(
    weight_kg: float,
    impedance: float | None,
    sex: str | None,
    age: float | None,
    height_cm: float | None,
) -> dict[str, Any]:
    """体重 + 阻抗 → body_metrics 可回填字段。

    档案不全（缺性别/年龄/身高）或阻抗无效（None / 不在 0~3000Ω）时只回体重。
    返回值只含 body_metrics 中存在的列，供 autofill_fields 直接使用。
    """
    out: dict[str, Any] = {"weight_kg": round(weight_kg, 2)}
    if (
        impedance is None
        or not (0 < impedance < 3000)
        or sex not in ("male", "female")
        or age is None
        or not (5 <= age <= 120)
        or height_cm is None
        or not (50 <= height_cm <= 250)
    ):
        return out
    lbm = _lbm_coefficient(sex, age, height_cm, weight_kg, impedance)
    fat = _fat_pct(sex, age, height_cm, weight_kg, lbm)
    bone = _bone_mass(sex, lbm)
    muscle = _muscle_mass(sex, weight_kg, fat, bone)
    water_kg = _water_pct(fat) * weight_kg / 100  # body_metrics 存 kg，不存百分比
    out.update(
        {
            "body_fat_pct": round(fat, 1),
            "muscle_mass_kg": round(muscle, 2),
            "body_water_kg": round(water_kg, 2),
            "visceral_fat_level": round(_visceral_fat(sex, age, height_cm, weight_kg)),
            "bmr_kcal": round(_bmr(sex, age, height_cm, weight_kg)),
        }
    )
    return out
