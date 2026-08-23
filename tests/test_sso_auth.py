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


def test_ip_prefixed_lan_entry_bypasses_browser_login(monkeypatch):
    from app import main

    configured = settings(auth_mode="oidc")
    monkeypatch.setattr(main, "get_settings", lambda: configured)
    headers = {
        "Host": "192.0.2.21:55080",
        "X-Forwarded-Prefix": "/shealth",
        "X-Shadow-Lan-Bypass": "1",
    }
    client = TestClient(
        main.app,
        base_url="http://192.0.2.21:55080",
        client=("127.0.0.1", 50000),
        follow_redirects=False,
    )
    response = client.get("/login", headers=headers)
    client.close()

    assert response.status_code == 303
    assert response.headers["location"] == "/shealth/"


def test_lan_marker_does_not_bypass_for_a_domain_host():
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("health.example.test", 80),
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [
                (b"host", b"health.example.test"),
                (b"x-forwarded-prefix", b"/shealth"),
                (b"x-shadow-lan-bypass", b"1"),
            ],
        }
    )
    assert auth.is_lan_bypass(request) is False


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
