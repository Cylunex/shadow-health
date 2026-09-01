"""应用配置：一切凭据来自 .env（设计文档 §7.1）。"""

from __future__ import annotations

import ipaddress
import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _secret_from_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip() if path else ""


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in os.environ.get(name, default).split(",")
        if item.strip()
    )


def _https_url(name: str) -> str:
    value = os.environ.get(name, "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute HTTPS URL")
    return value


def _optional_https_url(name: str) -> str:
    value = os.environ.get(name, "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute HTTPS URL")
    return value


def _optional_service_url(name: str) -> str:
    value = os.environ.get(name, "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not parsed.netloc or (parsed.scheme != "https" and not (loopback and parsed.scheme == "http")):
        raise ValueError(f"{name} must use HTTPS except for a loopback HTTP endpoint")
    return value


class Settings:
    def __init__(self) -> None:
        _load_dotenv()
        self.database_url: str = os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://health_app:health_dev@127.0.0.1:55433/health_dev",
        )
        self.auth_mode: str = os.environ.get("SHADOW_AUTH_MODE", "oidc").strip().lower()
        if self.auth_mode not in {"oidc", "legacy-forward"}:
            raise ValueError("SHADOW_AUTH_MODE must be oidc or legacy-forward")
        self.legacy_forward_until: datetime | None = None
        if self.auth_mode == "legacy-forward":
            raw_until = os.environ.get("SHADOW_LEGACY_FORWARD_UNTIL", "").strip()
            try:
                parsed_until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    "SHADOW_LEGACY_FORWARD_UNTIL is required as an ISO-8601 timestamp"
                ) from exc
            if parsed_until.tzinfo is None:
                raise ValueError("SHADOW_LEGACY_FORWARD_UNTIL must include a timezone")
            parsed_until = parsed_until.astimezone(UTC)
            now = datetime.now(UTC)
            if parsed_until <= now:
                raise ValueError("SHADOW_LEGACY_FORWARD_UNTIL has expired")
            if parsed_until > now + timedelta(hours=72):
                raise ValueError("legacy-forward rollback windows may not exceed 72 hours")
            self.legacy_forward_until = parsed_until
        self.sso_allowed_groups: tuple[str, ...] = _csv(
            "SHADOW_SSO_ALLOWED_GROUPS", "health-users,shadow-admins"
        )
        self.sso_entry_url: str = _optional_https_url("SHADOW_SSO_ENTRY_URL")
        self.sso_logout_url: str = _optional_https_url("SHADOW_SSO_LOGOUT_URL")
        if self.auth_mode == "legacy-forward":
            self.sso_entry_url = _https_url("SHADOW_SSO_ENTRY_URL")
            self.sso_logout_url = _https_url("SHADOW_SSO_LOGOUT_URL")
        self.oidc_issuer: str = os.environ.get("SHADOW_OIDC_ISSUER", "").strip().rstrip("/")
        self.oidc_client_id: str = os.environ.get(
            "SHADOW_OIDC_CLIENT_ID", "shadow-health"
        ).strip()
        self.oidc_client_secret_file: str = os.environ.get(
            "SHADOW_OIDC_CLIENT_SECRET_FILE", ""
        ).strip()
        self.oidc_redirect_uri: str = os.environ.get(
            "SHADOW_OIDC_REDIRECT_URI", ""
        ).strip()
        self.oidc_post_logout_redirect_uri: str = os.environ.get(
            "SHADOW_OIDC_POST_LOGOUT_REDIRECT_URI", ""
        ).strip()
        self.oidc_alternate_redirect_uri: str = os.environ.get(
            "SHADOW_OIDC_ALTERNATE_REDIRECT_URI", ""
        ).strip()
        self.oidc_alternate_post_logout_redirect_uri: str = os.environ.get(
            "SHADOW_OIDC_ALTERNATE_POST_LOGOUT_REDIRECT_URI", ""
        ).strip()
        self.oidc_required_group: str = os.environ.get(
            "SHADOW_OIDC_REQUIRED_GROUP", "health-users"
        ).strip()
        self.oidc_session_db: str = os.environ.get(
            "SHADOW_OIDC_SESSION_DB", str(BASE_DIR / "data" / "web_auth.db")
        ).strip()
        self.oidc_session_ttl_seconds: int = int(
            os.environ.get("SHADOW_OIDC_SESSION_TTL_SECONDS", "43200")
        )
        self.trusted_proxy_cidrs: tuple[str, ...] = _csv(
            "SHADOW_TRUSTED_PROXIES", "127.0.0.1/32,::1/128"
        )
        for cidr in self.trusted_proxy_cidrs:
            ipaddress.ip_network(cidr, strict=False)
        proxy_secret_file = os.environ.get("SHADOW_PROXY_AUTH_SECRET_FILE", "").strip()
        self.proxy_auth_secret: str = (
            _secret_from_file(proxy_secret_file)
            or os.environ.get("SHADOW_PROXY_AUTH_SECRET", "").strip()
        )
        if self.auth_mode == "legacy-forward" and len(self.proxy_auth_secret) < 32:
            raise ValueError("SSO requires a proxy auth secret of at least 32 characters")
        if not self.sso_allowed_groups:
            raise ValueError("SSO requires at least one allowed group")
        self.ingest_token: str = os.environ.get("INGEST_TOKEN", "")
        self.keep_mobile: str = os.environ.get("KEEP_MOBILE", "")
        self.keep_password: str = os.environ.get("KEEP_PASSWORD", "")
        # 空字符串视为未配置（.env.example 的占位空值不至于变成 Path("")）
        self.upload_dir: Path = Path(
            os.environ.get("UPLOAD_DIR") or (BASE_DIR / "uploads")
        )
        # 餐次照片目录：默认在 uploads/ 下，生产 compose 的 ./uploads 卷天然覆盖
        self.photo_dir: Path = Path(
            os.environ.get("PHOTO_DIR") or (self.upload_dir / "photos")
        )
        self.asset_base_url: str = _optional_service_url("SHADOW_ASSET_BASE_URL")
        self.asset_service_token_file: str = os.environ.get(
            "SHADOW_ASSET_SERVICE_TOKEN_FILE", ""
        ).strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
