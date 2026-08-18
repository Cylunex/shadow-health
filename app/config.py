"""应用配置：一切凭据来自 .env（设计文档 §7.1）。"""

from __future__ import annotations

import ipaddress
import os
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


class Settings:
    def __init__(self) -> None:
        _load_dotenv()
        self.database_url: str = os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://health_app:health_dev@127.0.0.1:55433/health_dev",
        )
        self.sso_allowed_groups: tuple[str, ...] = _csv(
            "SHADOW_SSO_ALLOWED_GROUPS", "health-users,shadow-admins"
        )
        self.sso_entry_url: str = _https_url("SHADOW_SSO_ENTRY_URL")
        self.sso_logout_url: str = _https_url("SHADOW_SSO_LOGOUT_URL")
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
        if len(self.proxy_auth_secret) < 32:
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
