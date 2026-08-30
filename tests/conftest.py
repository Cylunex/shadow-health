"""测试公共环境：项目路径与可信 Platform 身份头。"""
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_PROXY_SECRET = "test-proxy-secret-with-at-least-32-characters"
os.environ["SHADOW_PROXY_AUTH_SECRET"] = TEST_PROXY_SECRET
os.environ["SHADOW_AUTH_MODE"] = "legacy-forward"
os.environ["SHADOW_LEGACY_FORWARD_UNTIL"] = (
    datetime.now(UTC) + timedelta(hours=24)
).isoformat()
os.environ["SHADOW_TRUSTED_PROXIES"] = "127.0.0.1/32"
os.environ["SHADOW_SSO_ALLOWED_GROUPS"] = "health-users,shadow-admins"
os.environ["SHADOW_SSO_ENTRY_URL"] = "https://health.example.test"
os.environ["SHADOW_SSO_LOGOUT_URL"] = "https://auth.example.test/logout"


@pytest.fixture(scope="session")
def sso_headers() -> dict[str, str]:
    return {
        "X-Shadow-Proxy-Secret": TEST_PROXY_SECRET,
        "Remote-User": "test-user",
        "Remote-Groups": "health-users",
        "Remote-Name": "Test User",
        "Remote-Email": "test@example.test",
    }
