"""Independent Bearer authentication for the Shadow Health machine API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class MachineAPIError(Exception):
    """Stable, non-sensitive machine API failure."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


@dataclass(frozen=True, slots=True)
class MachinePrincipal:
    agent_id: str
    audiences: frozenset[str]
    scopes: frozenset[str]
    profile_grants: dict[str, frozenset[str]]


def machine_request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-ID", "").strip()
    if _REQUEST_ID_RE.fullmatch(supplied):
        return supplied
    return f"health-{uuid.uuid4().hex}"


def authenticate_machine_request(request: Request) -> MachinePrincipal:
    scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
    token = token.strip()
    if separator != " " or scheme.lower() != "bearer" or not (20 <= len(token) <= 1024):
        raise MachineAPIError(
            401,
            "invalid_token",
            "A valid machine Bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    registry, secrets_root = _load_registry()
    candidate_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    matched: MachinePrincipal | None = None
    for agent_id, raw_agent in registry["agents"].items():
        principal, hashes = _parse_agent(agent_id, raw_agent, secrets_root)
        if any(secrets.compare_digest(candidate_hash, value) for value in hashes):
            matched = principal
    if matched is None:
        raise MachineAPIError(
            401,
            "invalid_token",
            "A valid machine Bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return matched


def authorize_machine_principal(
    principal: MachinePrincipal,
    *,
    audience: str,
    scope: str,
    profile_id: str,
    grant: str,
) -> None:
    if audience not in principal.audiences:
        raise MachineAPIError(403, "audience_forbidden", "Token audience is not permitted.")
    if scope not in principal.scopes:
        raise MachineAPIError(403, "scope_forbidden", "Required scope is not granted.")
    if grant not in principal.profile_grants.get(profile_id, frozenset()):
        raise MachineAPIError(
            403,
            "resource_forbidden",
            "The requested health profile is not granted.",
        )


def _load_registry() -> tuple[dict[str, Any], Path]:
    registry_value = os.environ.get("SHADOW_HEALTH_AGENT_REGISTRY_FILE", "").strip()
    secrets_value = os.environ.get("SHADOW_HEALTH_AGENT_SECRETS_DIR", "").strip()
    if not registry_value or not secrets_value:
        raise MachineAPIError(503, "auth_unavailable", "Machine authentication is unavailable.")
    registry_path = Path(registry_value)
    secrets_root = Path(secrets_value).resolve()
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MachineAPIError(
            503, "auth_unavailable", "Machine authentication is unavailable."
        ) from exc
    if not isinstance(registry, dict) or registry.get("version") != 1:
        raise MachineAPIError(503, "auth_unavailable", "Machine authentication is unavailable.")
    if not isinstance(registry.get("agents"), dict) or not registry["agents"]:
        raise MachineAPIError(503, "auth_unavailable", "Machine authentication is unavailable.")
    return registry, secrets_root


def _parse_agent(
    agent_id: str, raw: Any, secrets_root: Path
) -> tuple[MachinePrincipal, tuple[str, ...]]:
    try:
        if not isinstance(raw, dict) or not agent_id or len(agent_id) > 80:
            raise ValueError
        audiences = _string_set(raw["audiences"])
        scopes = _string_set(raw["scopes"])
        raw_grants = raw["profile_grants"]
        raw_hash_files = raw["credential_hash_files"]
        if not isinstance(raw_grants, dict) or not isinstance(raw_hash_files, list):
            raise ValueError
        grants = {str(key): _string_set(value) for key, value in raw_grants.items()}
        hashes: list[str] = []
        for relative in raw_hash_files:
            if not isinstance(relative, str) or not relative:
                raise ValueError
            path = (secrets_root / relative).resolve()
            if not path.is_relative_to(secrets_root) or not path.is_file():
                raise ValueError
            value = path.read_text(encoding="utf-8").strip().lower()
            if not _HASH_RE.fullmatch(value):
                raise ValueError
            hashes.append(value)
        if not hashes:
            raise ValueError
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise MachineAPIError(
            503, "auth_unavailable", "Machine authentication is unavailable."
        ) from exc
    return (
        MachinePrincipal(
            agent_id=agent_id,
            audiences=audiences,
            scopes=scopes,
            profile_grants=grants,
        ),
        tuple(hashes),
    )


def _string_set(value: Any) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ValueError
    items = frozenset(item for item in value if isinstance(item, str) and item)
    if len(items) != len(value):
        raise ValueError
    return items
