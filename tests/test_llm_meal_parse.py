"""AI 餐照识别的 JSON 容错解析 + LLM 供应商配置解析（mock 掉 API 调用）。"""
from unittest.mock import patch

import pytest

import app.services.llm as llm


def _run(fake_response: str) -> dict:
    with patch.object(llm, "_call", return_value=fake_response):
        return llm.analyze_meal_photo(None, b"fake-image", "image/jpeg")


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
    assert r["confidence"] is None


def test_parses_confidence_and_falls_back_to_item_average():
    r = _run(
        '{"items": ['
        '{"name": "米饭", "kcal": 180, "confidence": 0.8},'
        '{"name": "青菜", "kcal": 60, "confidence": 0.6}'
        '], "note": ""}'
    )
    assert r["items"][0]["confidence"] == 0.8
    assert r["confidence"] == 0.7

    explicit = _run(
        '{"confidence": 0.92, "items": [{"name": "面条", "kcal": 300}], "note": ""}'
    )
    assert explicit["confidence"] == 0.92


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
        llm.analyze_meal_photo(None, b"x", "image/heic")


def test_oversize_image_rejected():
    with pytest.raises(llm.LLMError):
        llm.analyze_meal_photo(None, b"x" * (5 * 1024 * 1024 + 1), "image/jpeg")


# ---------- 供应商配置解析（设置页 → .env 回退口径） ----------

def test_resolve_config_defaults_to_claude(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    cfg = llm.resolve_config(None)
    assert cfg["provider"] == "claude"
    assert cfg["model"] == llm.DEFAULT_MODELS["claude"]
    assert cfg["configured"] is False


def test_resolve_config_openai_with_page_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = llm.resolve_config({
        "provider": "openai",
        "openai": {"model": "deepseek-chat", "api_key": "sk-x", "base_url": "https://api.deepseek.com/v1"},
    })
    assert cfg["provider"] == "openai"
    assert cfg["model"] == "deepseek-chat"
    assert cfg["api_key"] == "sk-x"
    assert cfg["base_url"] == "https://api.deepseek.com/v1"
    assert cfg["configured"] is True and cfg["key_from_env"] is False


def test_resolve_config_env_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    cfg = llm.resolve_config({"provider": "openai", "openai": {}})
    assert cfg["configured"] is True
    assert cfg["key_from_env"] is True
    assert cfg["api_key"] == ""  # 页面没存 Key 时交给 SDK 读环境
    assert cfg["model"] == llm.DEFAULT_MODELS["openai"]


def test_resolve_config_garbage_provider_falls_back(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    cfg = llm.resolve_config({"provider": "gemini", "claude": {"model": "claude-x"}})
    assert cfg["provider"] == "claude"
    assert cfg["model"] == "claude-x"


def test_resolve_config_model_env_fallback_claude(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "claude-legacy")
    cfg = llm.resolve_config({"provider": "claude", "claude": {}})
    assert cfg["model"] == "claude-legacy"  # 兼容旧部署的 LLM_MODEL


def test_vision_config_absent_falls_back_to_main():
    cfg = llm.resolve_vision_config(None, {
        "provider": "openai",
        "openai": {"model": "main-model", "api_key": "sk-main"},
    })
    assert cfg["fallback_to_main"] is True
    assert cfg["provider"] == "openai"
    assert cfg["model"] == "main-model"
    assert cfg["api_key"] == "sk-main"


def test_dedicated_vision_config_overrides_main():
    cfg = llm.resolve_vision_config(
        {
            "provider": "claude",
            "claude": {"model": "vision-model", "api_key": "sk-vision"},
        },
        {
            "provider": "openai",
            "openai": {"model": "main-model", "api_key": "sk-main"},
        },
    )
    assert cfg["fallback_to_main"] is False
    assert cfg["provider"] == "claude"
    assert cfg["model"] == "vision-model"
    assert cfg["api_key"] == "sk-vision"


def test_image_call_uses_vision_config():
    vision = {
        "provider": "openai", "model": "vision-model", "api_key": "sk-v",
        "base_url": "", "configured": True, "key_from_env": False,
        "fallback_to_main": False,
    }
    with (
        patch.object(llm, "get_vision_config", return_value=vision),
        patch.object(llm, "_call_openai", return_value="正常") as call,
    ):
        result = llm._call(None, "system", "user", [("image/png", "eA==")])
    assert result == "正常"
    assert call.call_args.args[0] == vision


def test_vision_trace_is_safe_and_records_config_source():
    vision = {
        "provider": "openai", "model": "vision-model", "api_key": "sk-secret",
        "base_url": "https://secret.invalid/v1", "configured": True,
        "key_from_env": False, "fallback_to_main": True,
    }
    with (
        patch.object(llm, "get_vision_config", return_value=vision),
        patch.object(llm, "now_local") as clock,
    ):
        clock.return_value.isoformat.return_value = "2026-08-03T12:00:00+08:00"
        trace = llm.vision_trace(None, "meal-photo-v2", confidence=0.8764)
    assert trace == {
        "provider": "openai",
        "model": "vision-model",
        "config_source": "main",
        "prompt_version": "meal-photo-v2",
        "analyzed_at": "2026-08-03T12:00:00+08:00",
        "confidence": 0.876,
    }
    assert "api_key" not in trace and "base_url" not in trace
