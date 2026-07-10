"""AI 餐照识别的 JSON 容错解析（services/llm.analyze_meal_photo，mock 掉 API 调用）。"""
from unittest.mock import patch

import pytest

import app.services.llm as llm


def _run(fake_response: str) -> dict:
    with patch.object(llm, "_call", return_value=fake_response):
        return llm.analyze_meal_photo(b"fake-image", "image/jpeg")


def test_parses_items_with_noise_and_filters_bad_entries():
    fake = (
        '前置噪声 {"items": ['
        '{"name": "西兰花炒鸡胸", "amount_g": 220, "kcal": 260, "protein_g": 34, "fat_g": 8, "carb_g": 12},'
        '{"name": "米饭", "amount_g": 150, "kcal": 174, "protein_g": 4},'
        '{"bad": 1}, {"name": "", "kcal": 100}'
        '], "note": "按一人份估算"} 尾巴'
    )
    r = _run(fake)
    assert len(r["items"]) == 2
    assert r["items"][0]["fat_g"] == 8 and r["items"][0]["carb_g"] == 12
    assert r["items"][1]["fat_g"] is None  # 缺项容错为 None
    assert r["note"] == "按一人份估算"


def test_out_of_range_values_become_none():
    fake = '{"items": [{"name": "怪东西", "kcal": 999999, "protein_g": -5}], "note": ""}'
    r = _run(fake)
    assert r["items"][0]["kcal"] is None
    assert r["items"][0]["protein_g"] is None


def test_no_json_raises_llmerror():
    with pytest.raises(llm.LLMError):
        _run("模型这次没按格式返回")


def test_unsupported_media_type_rejected():
    with pytest.raises(llm.LLMError):
        llm.analyze_meal_photo(b"x", "image/heic")


def test_oversize_image_rejected():
    with pytest.raises(llm.LLMError):
        llm.analyze_meal_photo(b"x" * (5 * 1024 * 1024 + 1), "image/jpeg")
