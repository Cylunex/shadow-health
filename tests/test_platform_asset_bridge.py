"""Health 独立凭据访问 Platform Asset 餐图的控制面与数据面边界。"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.config import _optional_service_url
from app.services.platform_assets import PlatformAssetClient, meal_resource_uri

DAY = date(2026, 9, 1)
ASSET_ID = "10000000-0000-4000-8000-000000000001"
VERSION_ID = "20000000-0000-4000-8000-000000000002"
REFERENCE_ID = "30000000-0000-4000-8000-000000000003"


def test_asset_service_url_allows_loopback_http_but_rejects_remote_http(monkeypatch) -> None:
    monkeypatch.setenv("TEST_ASSET_URL", "http://127.0.0.1:8400")
    assert _optional_service_url("TEST_ASSET_URL") == "http://127.0.0.1:8400"
    monkeypatch.setenv("TEST_ASSET_URL", "http://assets.example.test")
    with pytest.raises(ValueError, match="HTTPS"):
        _optional_service_url("TEST_ASSET_URL")


def _resolved() -> dict:
    return {
        "reference": {
            "id": REFERENCE_ID,
            "asset_id": ASSET_ID,
            "app_id": "health",
            "resource_uri": meal_resource_uri(DAY, "午餐"),
            "usage_role": "meal.photo",
        },
        "asset": {
            "id": ASSET_ID,
            "display_name": "午餐照片.png",
            "current_version": {"detected_mime": "image/png"},
        },
        "resolved_version_id": VERSION_ID,
    }


def test_health_token_resolves_fetches_and_releases_without_leaking_to_signed_url(tmp_path) -> None:
    token_file = tmp_path / "health-asset-token"
    token_file.write_text("health-private-service-token-0001", encoding="utf-8")
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/asset-references/resolve"):
            assert request.headers["authorization"] == "Bearer health-private-service-token-0001"
            return httpx.Response(200, json=[_resolved()])
        if request.url.path.endswith("/access-grants"):
            assert request.headers["authorization"] == "Bearer health-private-service-token-0001"
            return httpx.Response(200, json={
                "url": "http://testserver/v1/asset-content/signed?token=short-lived"
            })
        if request.url.path.endswith("/asset-content/signed"):
            assert "authorization" not in request.headers
            return httpx.Response(200, content=b"PNG", headers={"Content-Type": "image/png"})
        if request.method == "DELETE":
            assert request.headers["authorization"] == "Bearer health-private-service-token-0001"
            return httpx.Response(200, json={"id": REFERENCE_ID, "state": "released"})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    settings = SimpleNamespace(
        asset_base_url="http://127.0.0.1:8400",
        asset_service_token_file=str(token_file),
    )
    with PlatformAssetClient(
        settings, transport=httpx.MockTransport(handler)
    ) as client:
        photos = client.resolve(DAY, "午餐")
        content, content_type, name = client.fetch(
            reference_id=REFERENCE_ID, day=DAY, meal="午餐"
        )
        client.release(reference_id=REFERENCE_ID, day=DAY, meal="午餐")

    assert photos[0].asset_id == ASSET_ID
    assert (content, content_type, name) == (b"PNG", "image/png", "午餐照片.png")
    assert any(path.endswith("asset-content/signed") and auth is None for _, path, auth in seen)


def test_attach_is_idempotent_and_uses_stable_health_uri(tmp_path) -> None:
    token_file = tmp_path / "health-asset-token"
    token_file.write_text("health-private-service-token-0001", encoding="utf-8")
    resolutions = 0
    created_body: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal resolutions, created_body
        if request.url.path.endswith("/asset-references/resolve"):
            resolutions += 1
            return httpx.Response(200, json=[] if resolutions == 1 else [_resolved()])
        if request.url.path.endswith("/asset-references") and request.method == "POST":
            created_body = json.loads(request.content)
            return httpx.Response(201, json={"id": REFERENCE_ID})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    settings = SimpleNamespace(
        asset_base_url="http://127.0.0.1:8400",
        asset_service_token_file=str(token_file),
    )
    with PlatformAssetClient(settings, transport=httpx.MockTransport(handler)) as client:
        photo, replayed = client.attach(
            day=DAY, meal="午餐", asset_id=ASSET_ID, version_id=VERSION_ID
        )

    assert photo.reference_id == REFERENCE_ID and replayed is False
    assert created_body is not None
    assert created_body["resource_uri"] == "shadow://health/meals/2026-09-01/lunch"
    assert created_body["usage_role"] == "meal.photo"
    assert created_body["binding_mode"] == "pinned"
