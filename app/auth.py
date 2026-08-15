"""本地登录与 Shadow Forward Auth 身份校验。

- .env 存 scrypt 哈希，不存明文：python -m app.auth hash <密码> 生成
- 本地登录签发 HMAC 会话，重启和多 worker 场景仍可验证
- 公网 SSO 身份头必须同时来自可信代理并携带独立代理密钥
- 登录限速：同 IP 连续失败 5 次锁 60 秒
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import secrets
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

SESSION_COOKIE = "sh_session"
SESSION_MAX_AGE = 30 * 24 * 3600

# ip -> (连续失败次数, 最近失败时间)
_login_failures: dict[str, tuple[int, float]] = {}
_fallback_session_secret = secrets.token_urlsafe(48)

LOCK_THRESHOLD = 5
LOCK_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ForwardIdentity:
    username: str
    display_name: str
    email: str
    groups: tuple[str, ...]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return (
        "scrypt$"
        + base64.b64encode(salt).decode()
        + "$"
        + base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
        )
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError, binascii.Error):
        return False


def is_locked(ip: str) -> bool:
    count, last = _login_failures.get(ip, (0, 0.0))
    return count >= LOCK_THRESHOLD and (time.time() - last) < LOCK_SECONDS


def record_failure(ip: str) -> None:
    count, _ = _login_failures.get(ip, (0, 0.0))
    _login_failures[ip] = (count + 1, time.time())


def clear_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


def _session_key(secret: str | None = None) -> bytes:
    if secret is None:
        from app.config import get_settings

        secret = get_settings().session_secret
    return (secret or _fallback_session_secret).encode("utf-8")


def create_session(secret: str | None = None, *, now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else now
    payload = f"v1.{issued_at}.{secrets.token_urlsafe(24)}"
    signature = hmac.new(
        _session_key(secret), payload.encode("ascii"), hashlib.sha256
    ).digest()
    encoded = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{payload}.{encoded}"


def session_valid(
    token: str | None, secret: str | None = None, *, now: int | None = None
) -> bool:
    if not token:
        return False
    try:
        version, timestamp, nonce, supplied = token.split(".", 3)
        issued_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if version != "v1" or not nonce or issued_at > current + 60:
        return False
    if current - issued_at > SESSION_MAX_AGE:
        return False
    payload = f"{version}.{timestamp}.{nonce}"
    expected = (
        base64.urlsafe_b64encode(
            hmac.new(
                _session_key(secret), payload.encode("ascii"), hashlib.sha256
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return secrets.compare_digest(supplied, expected)


def destroy_session(token: str | None) -> None:
    # HMAC 会话无服务端状态；调用方删除 Cookie 即完成本站退出。
    del token


def is_trusted_proxy(client_host: str, cidrs: tuple[str, ...]) -> bool:
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)


def forward_identity(
    headers: Mapping[str, str], client_host: str, settings: Settings
) -> ForwardIdentity | None:
    if settings.auth_mode == "local" or not is_trusted_proxy(
        client_host, settings.trusted_proxy_cidrs
    ):
        return None
    supplied_secret = headers.get("X-Shadow-Proxy-Secret", "")
    if not supplied_secret or not secrets.compare_digest(
        supplied_secret, settings.proxy_auth_secret
    ):
        return None
    username = _safe_header(headers.get("Remote-User", ""), 255)
    if not username:
        return None
    raw_groups = headers.get("Remote-Groups", "")
    groups = tuple(
        item
        for item in (_safe_header(value, 128) for value in raw_groups.split(","))
        if item
    )
    if settings.sso_allowed_groups and not set(groups).intersection(
        settings.sso_allowed_groups
    ):
        return None
    return ForwardIdentity(
        username=username,
        display_name=_safe_header(headers.get("Remote-Name", ""), 255) or username,
        email=_safe_header(headers.get("Remote-Email", ""), 320),
        groups=groups,
    )


def _safe_header(value: str, limit: int) -> str:
    value = value.strip()
    if not value or len(value) > limit or "\r" in value or "\n" in value:
        return ""
    return value


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "hash":
        print(hash_password(sys.argv[2]))
    else:
        print("用法: python -m app.auth hash <密码>", file=sys.stderr)
        sys.exit(1)
