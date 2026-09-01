"""Health 对 Platform Asset 的最小服务端桥接。

数据库只保存 Platform 的 AssetReference；Health 每次按稳定餐次 URI 解析引用，使用自己的
service token 申请短时读取授权并代理图片字节。不会保存 Blob、签名 URL 或其他应用 Token。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

import httpx

from app.config import Settings, get_settings

MEAL_SLUGS = {"早餐": "breakfast", "午餐": "lunch", "加餐": "snack", "晚餐": "dinner"}
SLUG_MEALS = {slug: meal for meal, slug in MEAL_SLUGS.items()}
MEAL_PHOTO_USAGE_ROLE = "meal.photo"
MAX_MEAL_PHOTO_BYTES = 15 * 1024 * 1024


class PlatformAssetError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MealAssetPhoto:
    reference_id: str
    asset_id: str
    version_id: str
    display_name: str
    content_type: str


def meal_resource_uri(day: date, meal: str) -> str:
    try:
        slug = MEAL_SLUGS[meal]
    except KeyError as exc:
        raise ValueError("无效的餐次") from exc
    return f"shadow://health/meals/{day.isoformat()}/{slug}"


def meal_from_slug(slug: str) -> str:
    try:
        return SLUG_MEALS[slug]
    except KeyError as exc:
        raise ValueError("无效的餐次路径") from exc


class PlatformAssetClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport
        if not self.settings.asset_base_url or not self.settings.asset_service_token_file:
            raise PlatformAssetError("Platform Asset 未配置")
        try:
            token = Path(self.settings.asset_service_token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PlatformAssetError("Platform Asset 凭据不可用") from exc
        if len(token) < 20:
            raise PlatformAssetError("Platform Asset 凭据不可用")
        self.client = httpx.Client(
            base_url=self.settings.asset_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        request_id = response.headers.get("x-request-id", "unknown")
        raise PlatformAssetError(
            f"Platform Asset 请求失败（{response.status_code}，request_id={request_id}）"
        )

    def resolve(self, day: date, meal: str) -> list[MealAssetPhoto]:
        response = self.client.get(
            "v1/asset-references/resolve",
            params={
                "resource_uri": meal_resource_uri(day, meal),
                "usage_role": MEAL_PHOTO_USAGE_ROLE,
            },
        )
        self._raise(response)
        payload = response.json()
        if not isinstance(payload, list):
            raise PlatformAssetError("Platform Asset 返回了无效引用列表")
        photos: list[MealAssetPhoto] = []
        for item in payload:
            try:
                reference = item["reference"]
                asset = item["asset"]
                version = asset["current_version"]
                version_id = str(item["resolved_version_id"])
                content_type = str(version["detected_mime"])
                if not content_type.startswith("image/"):
                    continue
                photos.append(MealAssetPhoto(
                    reference_id=str(reference["id"]),
                    asset_id=str(asset["id"]),
                    version_id=version_id,
                    display_name=str(asset["display_name"]),
                    content_type=content_type,
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise PlatformAssetError("Platform Asset 返回了无效引用") from exc
        return photos

    def attach(
        self, *, day: date, meal: str, asset_id: str, version_id: str
    ) -> tuple[MealAssetPhoto, bool]:
        for photo in self.resolve(day, meal):
            if photo.asset_id == asset_id and photo.version_id == version_id:
                return photo, True
        uri = meal_resource_uri(day, meal)
        response = self.client.post(
            "v1/asset-references",
            json={
                "asset_id": asset_id,
                "resource_uri": uri,
                "usage_role": MEAL_PHOTO_USAGE_ROLE,
                "reference_key": f"health:meal:{day.isoformat()}:{MEAL_SLUGS[meal]}:{asset_id}:{version_id}",
                "binding_mode": "pinned",
                "pinned_version_id": version_id,
            },
        )
        self._raise(response)
        reference_id = str(response.json().get("id", ""))
        if not reference_id:
            raise PlatformAssetError("Platform Asset 未返回引用 ID")
        for photo in self.resolve(day, meal):
            if photo.reference_id == reference_id:
                return photo, False
        raise PlatformAssetError("Platform Asset 引用创建后无法回读")

    def fetch(self, *, reference_id: str, day: date, meal: str) -> tuple[bytes, str, str]:
        photo = next(
            (item for item in self.resolve(day, meal) if item.reference_id == reference_id),
            None,
        )
        if photo is None:
            raise PlatformAssetError("餐图引用不存在")
        grant = self.client.post(
            f"v1/asset-versions/{photo.version_id}/access-grants",
            json={"operation": "inline"},
        )
        self._raise(grant)
        url = grant.json().get("url")
        if not isinstance(url, str) or not url:
            raise PlatformAssetError("Platform Asset 未返回读取授权")
        parsed = urlsplit(url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1", "testserver"}
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or (parsed.scheme != "https" and not (loopback and parsed.scheme == "http"))
        ):
            raise PlatformAssetError("Platform Asset 返回了无效读取地址")
        # 签名 URL 不携带 Health service token；即使公开入口与控制面同源也保持凭据隔离。
        with httpx.Client(timeout=15.0, transport=self.transport) as content_client:
            content = content_client.get(url)
        self._raise(content)
        content_type = content.headers.get("content-type", photo.content_type).split(";", 1)[0]
        if not content_type.startswith("image/") or len(content.content) > MAX_MEAL_PHOTO_BYTES:
            raise PlatformAssetError("Platform Asset 返回的餐图无效")
        return content.content, content_type, photo.display_name

    def release(self, *, reference_id: str, day: date, meal: str) -> None:
        if not any(item.reference_id == reference_id for item in self.resolve(day, meal)):
            raise PlatformAssetError("餐图引用不存在")
        response = self.client.delete(f"v1/asset-references/{reference_id}")
        self._raise(response)


def list_meal_photos(day: date, meal: str) -> list[MealAssetPhoto]:
    with PlatformAssetClient() as client:
        return client.resolve(day, meal)


def attach_meal_photo(
    *, day: date, meal: str, asset_id: str, version_id: str
) -> tuple[MealAssetPhoto, bool]:
    with PlatformAssetClient() as client:
        return client.attach(day=day, meal=meal, asset_id=asset_id, version_id=version_id)


def fetch_meal_photo(
    *, reference_id: str, day: date, meal: str
) -> tuple[bytes, str, str]:
    with PlatformAssetClient() as client:
        return client.fetch(reference_id=reference_id, day=day, meal=meal)


def release_meal_photo(*, reference_id: str, day: date, meal: str) -> None:
    with PlatformAssetClient() as client:
        client.release(reference_id=reference_id, day=day, meal=meal)
