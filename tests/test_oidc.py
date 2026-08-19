from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import main, oidc


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHTTP:
    issuer = "https://auth.example.test"

    def get(self, url, **_kwargs):
        if url.endswith("/.well-known/openid-configuration"):
            return FakeResponse(
                {
                    "issuer": self.issuer,
                    "authorization_endpoint": self.issuer + "/authorize",
                    "token_endpoint": self.issuer + "/token",
                    "userinfo_endpoint": self.issuer + "/userinfo",
                    "jwks_uri": self.issuer + "/jwks.json",
                    "end_session_endpoint": self.issuer + "/logout",
                }
            )
        raise AssertionError(url)


class SigningHTTP(FakeHTTP):
    def __init__(self, jwk):
        self.jwk = jwk

    def get(self, url, **kwargs):
        if url.endswith("/jwks.json"):
            return FakeResponse({"keys": [self.jwk]})
        return super().get(url, **kwargs)


def config(tmp_path: Path) -> oidc.OIDCConfig:
    secret = tmp_path / "client-secret"
    secret.write_text("health-client-secret", encoding="utf-8")
    return oidc.OIDCConfig(
        issuer="https://auth.example.test",
        client_id="shadow-health",
        client_secret_file=str(secret),
        redirect_uri="https://health.example.test/auth/callback",
        post_logout_redirect_uri="https://health.example.test/",
        required_group="health-users",
        session_db=str(tmp_path / "sessions.db"),
    )


def test_login_transaction_is_one_time_and_pkce_is_s256(tmp_path):
    service = oidc.OIDCService(config(tmp_path), http=FakeHTTP())
    state, nonce, challenge = service.store.create_login_transaction(return_to="/metrics")
    transaction = service.store.consume_login_transaction(state)

    assert transaction["nonce"] == nonce
    expected = oidc._b64url(
        hashlib.sha256(transaction["code_verifier"].encode("ascii")).digest()
    )
    assert challenge == expected
    with pytest.raises(oidc.OIDCError, match="invalid or expired"):
        service.store.consume_login_transaction(state)

    query = parse_qs(
        urlsplit(
            service.client.authorization_url(
                state="state", nonce="nonce", challenge="challenge"
            )
        ).query
    )
    assert query["client_id"] == ["shadow-health"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://health.example.test/auth/callback"]


def test_identity_session_is_opaque_stable_and_revocable(tmp_path):
    store = oidc.SessionStore(str(tmp_path / "sessions.db"))
    claims = {
        "iss": "https://auth.example.test",
        "sub": "subject-1",
        "preferred_username": "demo",
        "name": "Demo User",
        "email": "demo@example.test",
        "groups": ["health-users"],
    }
    first = store.upsert_identity(claims)
    second = store.upsert_identity({**claims, "name": "Updated User"})
    assert first.shadow_user_id == second.shadow_user_id

    session = store.create_session(second, 300)
    assert "." not in session.session_token
    assert store.authenticate_session(session.session_token).identity.display_name == "Updated User"
    store.revoke_session(session.session_token)
    assert store.authenticate_session(session.session_token) is None


def test_id_token_requires_signature_issuer_audience_and_nonce(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "health-key", "alg": "RS256", "use": "sig"})
    client = oidc.OIDCClient(config(tmp_path), http=SigningHTTP(public_jwk))
    now = int(time.time())
    claims = {
        "iss": "https://auth.example.test",
        "sub": "subject-1",
        "aud": "shadow-health",
        "iat": now,
        "exp": now + 300,
        "nonce": "expected-nonce",
        "groups": ["health-users"],
    }
    token = jwt.encode(
        claims, private_key, algorithm="RS256", headers={"kid": "health-key"}
    )
    assert client.verify_id_token(token, nonce="expected-nonce")["sub"] == "subject-1"
    with pytest.raises(oidc.OIDCError, match="nonce mismatch"):
        client.verify_id_token(token, nonce="wrong-nonce")
    wrong_audience = jwt.encode(
        {**claims, "aud": "shadow-stock"},
        private_key,
        algorithm="RS256",
        headers={"kid": "health-key"},
    )
    with pytest.raises(oidc.OIDCError, match="validation failed"):
        client.verify_id_token(wrong_audience, nonce="expected-nonce")


def test_oidc_login_route_uses_native_client(tmp_path, monkeypatch):
    service = oidc.OIDCService(config(tmp_path), http=FakeHTTP())
    settings = SimpleNamespace(auth_mode="oidc")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_oidc_service", lambda: service)
    monkeypatch.setattr(main.auth, "browser_identity", lambda *_: None)

    client = TestClient(main.app, follow_redirects=False)
    response = client.get("/login?return_to=%2Fmetrics")
    client.close()

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://auth.example.test/authorize?")
    assert oidc.TRANSACTION_COOKIE in response.cookies


def test_callback_rejects_state_not_bound_to_browser(tmp_path, monkeypatch):
    service = oidc.OIDCService(config(tmp_path), http=FakeHTTP())
    state, _, _ = service.store.create_login_transaction(return_to="/")
    monkeypatch.setattr(main, "get_oidc_service", lambda: service)

    client = TestClient(main.app, follow_redirects=False)
    response = client.get(f"/auth/callback?state={state}&code=unused")
    client.close()

    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_return_to_rejects_external_and_control_character_values():
    assert oidc.sanitize_return_to("/metrics?range=7") == "/metrics?range=7"
    assert oidc.sanitize_return_to("https://evil.example/") == "/"
    assert oidc.sanitize_return_to("//evil.example/") == "/"
    assert oidc.sanitize_return_to("/%0aevil") == "/"
