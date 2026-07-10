"""体成分公式（services/miscale）：审查确认过与 openScale 同源，锁住行为防回归。"""
from app.services.miscale import compute_body_metrics


def test_male_full_profile():
    out = compute_body_metrics(71.6, 505, "male", 36.2, 175.0)
    assert out["weight_kg"] == 71.6
    assert 15 < out["body_fat_pct"] < 30
    assert 40 < out["muscle_mass_kg"] < 70
    assert 1000 < out["bmr_kcal"] < 2500
    assert 1 <= out["visceral_fat_level"] <= 50


def test_female_full_profile():
    out = compute_body_metrics(58.0, 480, "female", 52.0, 162.0)
    assert 20 < out["body_fat_pct"] < 45
    assert out["bmr_kcal"] >= 500


def test_weight_only_when_profile_incomplete():
    # 缺性别/年龄/身高 → 只回体重
    assert compute_body_metrics(70.0, 500, None, 30, 175) == {"weight_kg": 70.0}
    assert compute_body_metrics(70.0, 500, "male", None, 175) == {"weight_kg": 70.0}
    assert compute_body_metrics(70.0, 500, "male", 30, None) == {"weight_kg": 70.0}


def test_weight_only_when_impedance_invalid():
    assert compute_body_metrics(70.0, None, "male", 30, 175) == {"weight_kg": 70.0}
    assert compute_body_metrics(70.0, 0, "male", 30, 175) == {"weight_kg": 70.0}
    assert compute_body_metrics(70.0, 3000, "male", 30, 175) == {"weight_kg": 70.0}
