"""Machine-v1 MCP contract replaces the retired direct-write tool surface."""
import json
import httpx
import pytest

pytest.importorskip("mcp")
from mcp_server import server as srv


@pytest.fixture
def api(monkeypatch):
    calls = []
    real = httpx.Client
    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer dedicated-health-agent-token"
        assert "/api/machine/v1/agent/profiles/primary/" in request.url.path
        return httpx.Response(200, json={"status": "pending", "direct_domain_write": False})
    monkeypatch.setenv("HEALTH_AGENT_TOKEN", "dedicated-health-agent-token")
    monkeypatch.setenv("HEALTH_PROFILE_ID", "primary")
    monkeypatch.setattr(srv.httpx, "Client", lambda **kw: real(**kw, transport=httpx.MockTransport(handler)))
    return calls


def test_retry_preserves_caller_key_across_calls(api):
    for _ in range(2):
        result = srv.draft_record("meal", "2026-09-01", {"meal": "午餐", "name": "面"}, "stable-request-12345")
        assert result["direct_domain_write"] is False
    assert len(api) == 2  # server, not a lossy process cache, owns idempotency
    assert api[0].headers["Idempotency-Key"] == api[1].headers["Idempotency-Key"]
    assert api[0].content == api[1].content


def test_update_is_a_draft(api):
    srv.draft_meal_update(10, "2026-09-01", {"meal": "午餐", "name": "面"}, "update-request-1234")
    assert api[0].url.path.endswith("/drafts")
    assert json.loads(api[0].content)["operation"] == "update"


@pytest.mark.parametrize("key", ["", "short", "x" * 129, "../secrets"])
def test_invalid_key_does_not_send(api, key):
    with pytest.raises(srv.ApiError):
        srv.draft_record("meal", "2026-09-01", {}, key)
    assert not api


@pytest.mark.parametrize("field", ["spo2_pct", "bp_systolic", "", "weight", "SELECT *"])
def test_sensitive_or_unknown_metric_is_not_exposed(api, field):
    with pytest.raises(srv.ApiError):
        srv.query_metric_series(field)
    assert not api


def test_ingest_token_is_not_a_fallback(api, monkeypatch):
    monkeypatch.delenv("HEALTH_AGENT_TOKEN")
    monkeypatch.setenv("INGEST_TOKEN", "device-only")
    with pytest.raises(srv.ApiError, match="HEALTH_AGENT_TOKEN"):
        srv.query_today_summary("2026-09-01")
    assert not api


def test_read_routes(api):
    srv.query_today_summary("2026-09-01")
    srv.query_metric_series("steps", 7)
    srv.query_weekly_evidence("2026-09-01")
    srv.query_data_status()
    assert all(r.method == "GET" for r in api)


def test_no_credentials_sent_to_plaintext_remote(api,monkeypatch):
    monkeypatch.setattr(srv,"API_BASE","http://remote.example.test")
    with pytest.raises(srv.ApiError,match="HTTPS"):
        srv.query_today_summary("2026-09-01")
    assert not api
