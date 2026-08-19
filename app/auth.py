"""浏览器身份入口；默认使用原生 OIDC，保留显式 Forward Auth 回滚模式。

Android、BLE、Agent 和 MCP 的 Bearer 鉴权由各自路由处理，不经过这里。
"""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

    from app.config import Settings


@dataclass(frozen=True, slots=True)
class ForwardIdentity:
    username: str
    display_name: str
    email: str
    groups: tuple[str, ...]


def is_trusted_proxy(client_host: str, cidrs: tuple[str, ...]) -> bool:
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)


def forward_identity(
    headers: Mapping[str, str], client_host: str, settings: Settings
) -> ForwardIdentity | None:
    if not is_trusted_proxy(client_host, settings.trusted_proxy_cidrs):
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
    if not set(groups).intersection(settings.sso_allowed_groups):
        return None
    return ForwardIdentity(
        username=username,
        display_name=_safe_header(headers.get("Remote-Name", ""), 255) or username,
        email=_safe_header(headers.get("Remote-Email", ""), 320),
        groups=groups,
    )


def browser_identity(request: Request, settings: Settings):
    if settings.auth_mode == "legacy-forward":
        client_host = request.client.host if request.client else ""
        return forward_identity(request.headers, client_host, settings)
    from app.oidc import SESSION_COOKIE, OIDCError, get_oidc_service

    try:
        service = get_oidc_service()
    except OIDCError:
        return None
    record = service.store.authenticate_session(request.cookies.get(SESSION_COOKIE, ""))
    if record is None or not record.identity.in_group(service.config.required_group):
        return None
    request.state.browser_identity = record.identity
    return record.identity


def _safe_header(value: str, limit: int) -> str:
    value = value.strip()
    if not value or len(value) > limit or "\r" in value or "\n" in value:
        return ""
    return value
