from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import auth


def settings(**overrides):
    values = {
        "trusted_proxy_cidrs": ("127.0.0.1/32",),
        "proxy_auth_secret": "proxy-secret-with-at-least-32-characters",
        "sso_allowed_groups": ("health-users", "shadow-admins"),
        "sso_entry_url": "https://health.example.test",
        "sso_logout_url": "https://auth.example.test/logout",
        "auth_mode": "legacy-forward",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def forward_headers(**overrides):
    values = {
        "X-Shadow-Proxy-Secret": "proxy-secret-with-at-least-32-characters",
        "Remote-User": "demo-user",
        "Remote-Groups": "health-users,shadow-admins",
        "Remote-Name": "Shadow Admin",
        "Remote-Email": "admin@example.test",
    }
    values.update(overrides)
    return values


def test_forward_identity_requires_proxy_network_secret_and_group():
    identity = auth.forward_identity(forward_headers(), "127.0.0.1", settings())

    assert identity is not None
    assert identity.username == "demo-user"
    assert identity.groups == ("health-users", "shadow-admins")
    assert auth.forward_identity(forward_headers(), "192.0.2.11", settings()) is None
    assert (
        auth.forward_identity(
            forward_headers(**{"X-Shadow-Proxy-Secret": "wrong"}),
            "127.0.0.1",
            settings(),
        )
        is None
    )
    assert (
        auth.forward_identity(
            forward_headers(**{"Remote-Groups": "stock-users"}),
            "127.0.0.1",
            settings(),
        )
        is None
    )


def test_login_endpoint_accepts_verified_forward_identity(monkeypatch):
    from app import main

    configured = settings()
    monkeypatch.setattr(main, "get_settings", lambda: configured)
    client = TestClient(
        main.app,
        client=("127.0.0.1", 50000),
        follow_redirects=False,
    )
    response = client.get("/login", headers=forward_headers())
    client.close()

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_login_endpoint_hands_direct_clients_to_platform(monkeypatch):
    from app import main

    configured = settings()
    monkeypatch.setattr(main, "get_settings", lambda: configured)
    client = TestClient(
        main.app,
        client=("127.0.0.1", 50000),
        follow_redirects=False,
    )
    response = client.get("/login")
    client.close()

    assert response.status_code == 303
    assert response.headers["location"] == "https://health.example.test"


def test_legacy_session_cookie_no_longer_authenticates():
    from app import main

    client = TestClient(main.app, follow_redirects=False)
    client.cookies.set("sh_session", "v1.legacy.invalid")
    response = client.get("/metrics")
    client.close()

    assert response.status_code == 303
    assert response.headers["location"] == "/login?return_to=%2Fmetrics"


def test_logout_uses_platform_global_logout(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "get_settings", lambda: settings())
    client = TestClient(
        main.app,
        client=("127.0.0.1", 50000),
        follow_redirects=False,
    )
    response = client.post("/logout", headers=forward_headers())
    client.close()

    assert response.status_code == 303
    assert response.headers["location"] == "https://auth.example.test/logout"


def test_healthz_does_not_require_database(monkeypatch):
    from app import main

    class BrokenEngine:
        def connect(self):
            raise RuntimeError("database down")

    monkeypatch.setattr(main, "engine", BrokenEngine())
    client = TestClient(main.app, raise_server_exceptions=False)

    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
    client.close()
