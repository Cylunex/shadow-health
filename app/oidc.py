"""Native Shadow Identity OIDC client with server-side opaque browser sessions."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

import requests

from app.config import get_settings

SESSION_COOKIE = "__Host-shadow_health_session"
TRANSACTION_COOKIE = "__Host-shadow_health_oidc_state"
OIDC_SCOPES = "openid profile email groups"
ALLOWED_JWT_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")


class OIDCError(ValueError):
    def __init__(self, message: str, *, reason: str = "unspecified") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret_file: str
    redirect_uri: str
    post_logout_redirect_uri: str
    required_group: str
    session_db: str
    alternate_redirect_uri: str = ""
    alternate_post_logout_redirect_uri: str = ""
    session_ttl_seconds: int = 12 * 60 * 60
    transaction_ttl_seconds: int = 10 * 60
    clock_skew_seconds: int = 60
    id_token_max_age_seconds: int = 10 * 60
    metadata_ttl_seconds: int = 15 * 60

    @classmethod
    def from_settings(cls) -> OIDCConfig:
        settings = get_settings()
        return cls(
            issuer=settings.oidc_issuer,
            client_id=settings.oidc_client_id,
            client_secret_file=settings.oidc_client_secret_file,
            redirect_uri=settings.oidc_redirect_uri,
            post_logout_redirect_uri=settings.oidc_post_logout_redirect_uri,
            required_group=settings.oidc_required_group,
            session_db=settings.oidc_session_db,
            alternate_redirect_uri=settings.oidc_alternate_redirect_uri,
            alternate_post_logout_redirect_uri=(
                settings.oidc_alternate_post_logout_redirect_uri
            ),
            session_ttl_seconds=settings.oidc_session_ttl_seconds,
        )

    def validate(self) -> None:
        if not all(
            (
                self.issuer,
                self.client_id,
                self.client_secret_file,
                self.redirect_uri,
                self.post_logout_redirect_uri,
                self.required_group,
                self.session_db,
            )
        ):
            raise OIDCError("OIDC is not configured", reason="configuration")
        if self.client_id != "shadow-health":
            raise OIDCError("unexpected OIDC client", reason="configuration")
        _require_https_url("issuer", self.issuer)
        _require_https_url("redirect URI", self.redirect_uri)
        _require_https_url("post logout URI", self.post_logout_redirect_uri)
        if urlsplit(self.redirect_uri).path != "/auth/callback":
            raise OIDCError("OIDC callback path must be /auth/callback", reason="configuration")
        redirect = urlsplit(self.redirect_uri)
        post_logout = urlsplit(self.post_logout_redirect_uri)
        if (redirect.scheme, redirect.netloc) != (post_logout.scheme, post_logout.netloc):
            raise OIDCError(
                "post logout URI must use the canonical application origin",
                reason="configuration",
            )
        alternate_values = (
            self.alternate_redirect_uri,
            self.alternate_post_logout_redirect_uri,
        )
        if any(alternate_values) and not all(alternate_values):
            raise OIDCError(
                "alternate redirect and logout URIs must be configured together",
                reason="configuration",
            )
        if self.alternate_redirect_uri:
            _require_https_url("alternate redirect URI", self.alternate_redirect_uri)
            _require_https_url(
                "alternate post logout URI", self.alternate_post_logout_redirect_uri
            )
            alternate_redirect = urlsplit(self.alternate_redirect_uri)
            alternate_logout = urlsplit(self.alternate_post_logout_redirect_uri)
            if not alternate_redirect.path.endswith("/auth/callback"):
                raise OIDCError(
                    "alternate OIDC callback path must end with /auth/callback",
                    reason="configuration",
                )
            if (alternate_redirect.scheme, alternate_redirect.netloc) != (
                alternate_logout.scheme,
                alternate_logout.netloc,
            ):
                raise OIDCError(
                    "alternate logout URI must use the alternate application origin",
                    reason="configuration",
                )
        if not Path(self.client_secret_file).expanduser().is_file():
            raise OIDCError("OIDC client secret file is unavailable", reason="configuration")

    def entry_for(self, host: str, prefix: str = "") -> tuple[str, str]:
        normalized_prefix = prefix.rstrip("/") if prefix.startswith("/") else ""
        callback_path = f"{normalized_prefix}/auth/callback"
        normalized_host = host.strip().lower()
        entries = (
            (self.redirect_uri, self.post_logout_redirect_uri),
            (self.alternate_redirect_uri, self.alternate_post_logout_redirect_uri),
        )
        for redirect_uri, logout_uri in entries:
            if not redirect_uri:
                continue
            parsed = urlsplit(redirect_uri)
            if parsed.netloc.lower() == normalized_host and parsed.path == callback_path:
                return redirect_uri, logout_uri
        raise OIDCError("current browser entry is not allowed", reason="configuration")

    def require_allowed_redirect_uri(self, redirect_uri: str) -> str:
        if redirect_uri not in {self.redirect_uri, self.alternate_redirect_uri}:
            raise OIDCError("OIDC redirect URI is not allowed", reason="configuration")
        return redirect_uri


@dataclass(frozen=True, slots=True)
class BrowserIdentity:
    shadow_user_id: str
    issuer: str
    subject: str
    username: str
    display_name: str
    email: str
    groups: tuple[str, ...]

    def in_group(self, group: str) -> bool:
        return group in self.groups


@dataclass(frozen=True, slots=True)
class SessionRecord:
    identity: BrowserIdentity
    session_token: str
    expires_at: float


class SessionStore:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self._ensure_schema()
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            path = Path(self.database_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=10)
            try:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS oidc_transactions (
                      state_hash TEXT PRIMARY KEY,
                      nonce TEXT NOT NULL,
                      code_verifier TEXT NOT NULL,
                      return_to TEXT NOT NULL,
                      redirect_uri TEXT NOT NULL DEFAULT '',
                      expires_at REAL NOT NULL,
                      created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS shadow_identities (
                      shadow_user_id TEXT PRIMARY KEY,
                      issuer TEXT NOT NULL,
                      subject TEXT NOT NULL,
                      username TEXT NOT NULL,
                      display_name TEXT NOT NULL,
                      email TEXT NOT NULL,
                      groups_json TEXT NOT NULL,
                      created_at REAL NOT NULL,
                      last_seen_at REAL NOT NULL,
                      UNIQUE (issuer, subject)
                    );
                    CREATE TABLE IF NOT EXISTS browser_sessions (
                      session_hash TEXT PRIMARY KEY,
                      shadow_user_id TEXT NOT NULL,
                      groups_json TEXT NOT NULL,
                      created_at REAL NOT NULL,
                      expires_at REAL NOT NULL,
                      revoked_at REAL,
                      FOREIGN KEY (shadow_user_id) REFERENCES shadow_identities(shadow_user_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_browser_sessions_user
                      ON browser_sessions(shadow_user_id, expires_at);
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(oidc_transactions)")
                }
                if "redirect_uri" not in columns:
                    connection.execute(
                        "ALTER TABLE oidc_transactions "
                        "ADD COLUMN redirect_uri TEXT NOT NULL DEFAULT ''"
                    )
                connection.commit()
            finally:
                connection.close()
            try:
                path.chmod(0o600)
            except OSError:
                pass
            self._initialized = True

    def create_login_transaction(
        self, *, return_to: str, redirect_uri: str = "", ttl_seconds: int = 10 * 60
    ) -> tuple[str, str, str]:
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM oidc_transactions WHERE expires_at <= ?", (now,))
            connection.execute(
                """INSERT INTO oidc_transactions
                   (state_hash, nonce, code_verifier, return_to, redirect_uri,
                    expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    _digest(state), nonce, verifier, return_to, redirect_uri,
                    now + ttl_seconds, now,
                ),
            )
        return state, nonce, challenge

    def consume_login_transaction(self, state: str) -> dict[str, Any]:
        if not state or len(state) > 512:
            raise OIDCError("invalid login state", reason="state")
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT nonce, code_verifier, return_to, redirect_uri, expires_at
                   FROM oidc_transactions WHERE state_hash = ?""",
                (_digest(state),),
            ).fetchone()
            connection.execute(
                "DELETE FROM oidc_transactions WHERE state_hash = ?", (_digest(state),)
            )
            connection.commit()
        finally:
            connection.close()
        if not row or float(row["expires_at"]) <= now:
            raise OIDCError("invalid or expired login state", reason="state")
        return dict(row)

    def upsert_identity(self, claims: dict[str, Any]) -> BrowserIdentity:
        issuer = str(claims.get("iss") or "").strip()
        subject = str(claims.get("sub") or "").strip()
        if not issuer or not subject:
            raise OIDCError("verified identity is incomplete", reason="identity")
        groups = _normalize_groups(claims.get("groups"))
        username = str(claims.get("preferred_username") or subject)
        display_name = str(claims.get("name") or username)
        email = str(claims.get("email") or "")
        now = time.time()
        groups_json = json.dumps(groups, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT OR IGNORE INTO shadow_identities
                   (shadow_user_id, issuer, subject, username, display_name, email,
                    groups_json, created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()), issuer, subject, username, display_name, email,
                    groups_json, now, now,
                ),
            )
            connection.execute(
                """UPDATE shadow_identities SET username = ?, display_name = ?, email = ?,
                   groups_json = ?, last_seen_at = ? WHERE issuer = ? AND subject = ?""",
                (username, display_name, email, groups_json, now, issuer, subject),
            )
            row = connection.execute(
                """SELECT shadow_user_id FROM shadow_identities
                   WHERE issuer = ? AND subject = ?""",
                (issuer, subject),
            ).fetchone()
        return BrowserIdentity(
            shadow_user_id=str(row["shadow_user_id"]),
            issuer=issuer,
            subject=subject,
            username=username,
            display_name=display_name,
            email=email,
            groups=groups,
        )

    def create_session(self, identity: BrowserIdentity, ttl_seconds: int) -> SessionRecord:
        token = secrets.token_urlsafe(48)
        now = time.time()
        expires_at = now + ttl_seconds
        groups_json = json.dumps(identity.groups, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM browser_sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                """INSERT INTO browser_sessions
                   (session_hash, shadow_user_id, groups_json, created_at, expires_at, revoked_at)
                   VALUES (?, ?, ?, ?, ?, NULL)""",
                (_digest(token), identity.shadow_user_id, groups_json, now, expires_at),
            )
        return SessionRecord(identity, token, expires_at)

    def authenticate_session(self, token: str) -> SessionRecord | None:
        if not token or len(token) > 512:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT s.expires_at, s.groups_json, i.shadow_user_id, i.issuer,
                          i.subject, i.username, i.display_name, i.email
                   FROM browser_sessions s
                   JOIN shadow_identities i ON i.shadow_user_id = s.shadow_user_id
                   WHERE s.session_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?""",
                (_digest(token), time.time()),
            ).fetchone()
        if row is None:
            return None
        identity = BrowserIdentity(
            shadow_user_id=str(row["shadow_user_id"]),
            issuer=str(row["issuer"]),
            subject=str(row["subject"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            email=str(row["email"]),
            groups=tuple(json.loads(row["groups_json"])),
        )
        return SessionRecord(identity, token, float(row["expires_at"]))

    def revoke_session(self, token: str) -> None:
        if not token or len(token) > 512:
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE browser_sessions SET revoked_at = ? WHERE session_hash = ?",
                (time.time(), _digest(token)),
            )


class OIDCClient:
    def __init__(self, config: OIDCConfig, http: Any = requests) -> None:
        self.config = config
        self.http = http
        self._metadata: tuple[float, dict[str, Any]] | None = None
        self._jwks: tuple[float, dict[str, Any]] | None = None

    def authorization_url(
        self, *, state: str, nonce: str, challenge: str, redirect_uri: str | None = None
    ) -> str:
        redirect_uri = self.config.require_allowed_redirect_uri(
            redirect_uri or self.config.redirect_uri
        )
        params = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "scope": OIDC_SCOPES,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{self._get_metadata()['authorization_endpoint']}?{urlencode(params)}"

    def exchange_code(
        self, *, code: str, verifier: str, redirect_uri: str | None = None
    ) -> dict[str, Any]:
        redirect_uri = self.config.require_allowed_redirect_uri(
            redirect_uri or self.config.redirect_uri
        )
        if not code or len(code) > 4096:
            raise OIDCError("authorization code is missing", reason="code")
        try:
            secret = Path(self.config.client_secret_file).read_text(encoding="utf-8").strip()
            if not secret:
                raise OIDCError(
                    "OIDC client secret is unavailable", reason="configuration"
                )
            response = self.http.post(
                self._get_metadata()["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
                auth=(self.config.client_id, secret),
                timeout=15,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except OIDCError:
            raise
        except Exception as exc:
            raise OIDCError("OIDC token exchange failed", reason="token_exchange") from exc
        if not isinstance(payload, dict) or not payload.get("id_token"):
            raise OIDCError("OIDC token response is incomplete", reason="token_response")
        return payload

    def verify_id_token(self, token: str, *, nonce: str) -> dict[str, Any]:
        try:
            import jwt

            header = jwt.get_unverified_header(token)
            algorithm = str(header.get("alg") or "")
            if algorithm not in ALLOWED_JWT_ALGORITHMS:
                raise OIDCError("ID Token algorithm is not allowed", reason="algorithm")
            key_id = str(header.get("kid") or "")
            candidates = self._candidate_keys(key_id, algorithm)
            if len(candidates) != 1:
                self._jwks = None
                candidates = self._candidate_keys(key_id, algorithm)
            if len(candidates) != 1:
                raise OIDCError("ID Token signing key is unavailable", reason="signing_key")
            key = jwt.PyJWK.from_dict(candidates[0], algorithm=algorithm).key
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                issuer=self.config.issuer,
                audience=self.config.client_id,
                leeway=self.config.clock_skew_seconds,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
            )
        except OIDCError:
            raise
        except Exception as exc:
            raise OIDCError("ID Token validation failed", reason="validation") from exc
        if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise OIDCError("ID Token nonce mismatch", reason="nonce")
        if time.time() - float(claims["iat"]) > (
            self.config.id_token_max_age_seconds + self.config.clock_skew_seconds
        ):
            raise OIDCError("ID Token issued-at time is too old", reason="issued_at")
        raw_groups = claims.get("groups")
        claims["groups"] = () if raw_groups is None else _normalize_groups(raw_groups)
        return claims

    def complete_profile_claims(
        self, claims: dict[str, Any], token_response: dict[str, Any]
    ) -> dict[str, Any]:
        if claims.get("groups"):
            return claims
        access_token = str(token_response.get("access_token") or "")
        if not access_token:
            raise OIDCError("OIDC access token is unavailable", reason="userinfo")
        try:
            response = self.http.get(
                self._get_metadata()["userinfo_endpoint"],
                timeout=10,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            response.raise_for_status()
            profile = response.json()
        except Exception as exc:
            raise OIDCError("OIDC UserInfo request failed", reason="userinfo") from exc
        if not isinstance(profile, dict) or not secrets.compare_digest(
            str(profile.get("sub") or ""), str(claims.get("sub") or "")
        ):
            raise OIDCError("OIDC UserInfo subject mismatch", reason="userinfo")
        claims["groups"] = _normalize_groups(profile.get("groups"))
        for name in ("preferred_username", "name", "email"):
            if not claims.get(name) and profile.get(name):
                claims[name] = profile[name]
        return claims

    def global_logout_url(self, post_logout_redirect_uri: str | None = None) -> str:
        post_logout_redirect_uri = (
            post_logout_redirect_uri or self.config.post_logout_redirect_uri
        )
        if post_logout_redirect_uri not in {
            self.config.post_logout_redirect_uri,
            self.config.alternate_post_logout_redirect_uri,
        }:
            raise OIDCError("post logout URI is not allowed", reason="configuration")
        endpoint = self._get_metadata().get("end_session_endpoint")
        if not endpoint:
            return post_logout_redirect_uri
        return f"{endpoint}?{urlencode({'client_id': self.config.client_id, 'post_logout_redirect_uri': post_logout_redirect_uri})}"

    def _candidate_keys(self, key_id: str, algorithm: str) -> list[dict[str, Any]]:
        return [
            key
            for key in self._get_jwks().get("keys", [])
            if (not key_id or str(key.get("kid") or "") == key_id)
            and key.get("use") in (None, "", "sig")
            and key.get("alg") in (None, "", algorithm)
        ]

    def _get_metadata(self) -> dict[str, Any]:
        now = time.time()
        if self._metadata and self._metadata[0] > now:
            return self._metadata[1]
        try:
            response = self.http.get(
                f"{self.config.issuer}/.well-known/openid-configuration",
                timeout=10,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            metadata = response.json()
        except Exception as exc:
            raise OIDCError("OIDC discovery failed", reason="discovery") from exc
        if not isinstance(metadata, dict) or metadata.get("issuer") != self.config.issuer:
            raise OIDCError("OIDC discovery issuer mismatch", reason="issuer")
        for name in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint", "jwks_uri"):
            _require_https_url(name, str(metadata.get(name) or ""))
        if metadata.get("end_session_endpoint"):
            _require_https_url(
                "end_session_endpoint", str(metadata["end_session_endpoint"])
            )
        self._metadata = (now + self.config.metadata_ttl_seconds, metadata)
        return metadata

    def _get_jwks(self) -> dict[str, Any]:
        now = time.time()
        if self._jwks and self._jwks[0] > now:
            return self._jwks[1]
        try:
            response = self.http.get(
                self._get_metadata()["jwks_uri"], timeout=10, headers={"Accept": "application/json"}
            )
            response.raise_for_status()
            jwks = response.json()
        except Exception as exc:
            raise OIDCError("OIDC signing keys unavailable", reason="signing_key") from exc
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise OIDCError("OIDC signing keys are invalid", reason="signing_key")
        self._jwks = (now + self.config.metadata_ttl_seconds, jwks)
        return jwks


class OIDCService:
    def __init__(self, config: OIDCConfig, *, http: Any = requests) -> None:
        config.validate()
        self.config = config
        self.store = SessionStore(config.session_db)
        self.client = OIDCClient(config, http=http)


_service: OIDCService | None = None
_service_lock = threading.Lock()


def get_oidc_service() -> OIDCService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = OIDCService(OIDCConfig.from_settings())
    return _service


def reset_oidc_service() -> None:
    global _service
    _service = None


def sanitize_return_to(value: str | None) -> str:
    value = str(value or "/").strip()
    decoded = unquote(value)
    parsed = urlsplit(value)
    if (
        len(value) > 2048
        or not value.startswith("/")
        or value.startswith("//")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(char) < 32 for char in decoded)
        or parsed.scheme
        or parsed.netloc
    ):
        return "/"
    return value


def set_session_cookie(response: Any, record: SessionRecord) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        record.session_token,
        max_age=max(0, int(record.expires_at - time.time())),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Any) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )


def set_transaction_cookie(response: Any, state: str, ttl_seconds: int) -> None:
    response.set_cookie(
        TRANSACTION_COOKIE,
        state,
        max_age=ttl_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_transaction_cookie(response: Any) -> None:
    response.delete_cookie(
        TRANSACTION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )


def _normalize_groups(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise OIDCError("groups claim is invalid", reason="groups")
    return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))


def _require_https_url(label: str, value: str) -> None:
    parsed = urlsplit(value)
    allow_http = os.getenv("SHADOW_OIDC_ALLOW_HTTP_FOR_TESTS", "").lower() in {
        "1", "true", "yes"
    }
    if (parsed.scheme != "https" and not (allow_http and parsed.scheme == "http")) or not parsed.netloc:
        raise OIDCError(f"{label} must be an absolute HTTPS URL", reason="configuration")
    if parsed.username or parsed.password:
        raise OIDCError(f"{label} must not contain credentials", reason="configuration")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
