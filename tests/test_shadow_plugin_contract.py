from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).parents[1]

from app.db import get_db  # noqa: E402
from app.models import AgentMachineAudit, AgentRecordDraft  # noqa: E402
from app.routers.machine_agent import get_machine_health_service  # noqa: E402
from app.timeutil import today_local  # noqa: E402


def _platform_validator():
    candidates = (
        ROOT.parent / "shadow-platform",
        ROOT.parents[1] / "shadow-platform",
    )
    platform_root = next(
        (
            path
            for path in candidates
            if (path / "contracts" / "shadow-plugin.schema.json").is_file()
        ),
        ROOT,
    )
    if platform_root != ROOT:
        sys.path.insert(0, str(platform_root))
    from shadow_sdk.plugin_contracts import validate_plugin

    return validate_plugin, platform_root


def _application_routes(router: object) -> set[tuple[str, str]]:
    collected: set[tuple[str, str]] = set()
    for route in getattr(router, "routes", []):
        included = getattr(route, "original_router", None)
        if included is not None:
            collected.update(_application_routes(included))
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        collected.update((path, method) for method in getattr(route, "methods", set()))
    return collected


def test_shadow_plugin_contract_matches_machine_routes() -> None:
    from app.main import app

    validate_plugin, platform_root = _platform_validator()
    plugin = validate_plugin(ROOT, platform_root)
    contract = yaml.safe_load((ROOT / "contracts" / "agent.openapi.yaml").read_text("utf-8"))
    declared_routes = {
        (path, method.upper())
        for path, path_item in contract["paths"].items()
        for method in path_item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }
    actual_routes = _application_routes(app)

    assert plugin.plugin_id == "shadow-health"
    assert plugin.version == "0.1.0"
    assert declared_routes <= actual_routes
    assert {item["id"] for item in plugin.agent_manifest["capabilities"]} == {
        "health.summary.read",
        "health.trends.read",
        "health.records.draft",
    }
    assert all(
        item["risk_level"] != "L4" for item in plugin.agent_manifest["capabilities"]
    )


@pytest.fixture()
def machine_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tokens = {
        "health-helper": "health-helper-test-token-0001",
        "summary-only": "summary-only-test-token-0002",
        "ungranted": "ungranted-test-token-000003",
        "wrong-audience": "wrong-audience-test-token-04",
    }
    secrets_root = tmp_path / "agent-secrets"
    agents: dict[str, object] = {}
    for agent_id, token in tokens.items():
        digest = secrets_root / "agents" / agent_id / "current-token.sha256"
        digest.parent.mkdir(parents=True, exist_ok=True)
        digest.write_text(hashlib.sha256(token.encode()).hexdigest(), encoding="utf-8")
        scopes = ["health.summary.read", "health.trends.read", "health.records.draft"]
        grants = {"primary": ["summary:read", "trends:read", "drafts:create"]}
        audiences = ["health"]
        if agent_id == "summary-only":
            scopes = ["health.summary.read"]
        elif agent_id == "ungranted":
            grants = {"another-profile": ["summary:read", "trends:read", "drafts:create"]}
        elif agent_id == "wrong-audience":
            audiences = ["travel"]
        agents[agent_id] = {
            "audiences": audiences,
            "scopes": scopes,
            "profile_grants": grants,
            "credential_hash_files": [f"agents/{agent_id}/current-token.sha256"],
        }
    registry = tmp_path / "agents.json"
    registry.write_text(json.dumps({"version": 1, "agents": agents}), encoding="utf-8")
    monkeypatch.setenv("SHADOW_HEALTH_AGENT_REGISTRY_FILE", str(registry))
    monkeypatch.setenv("SHADOW_HEALTH_AGENT_SECRETS_DIR", str(secrets_root))
    monkeypatch.setenv("INGEST_TOKEN", tokens["wrong-audience"])

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def attach_health_schema(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS health")

    AgentRecordDraft.__table__.create(engine)
    AgentMachineAudit.__table__.create(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE health.diet_logs "
            "(log_date DATE, kcal NUMERIC, protein_g NUMERIC)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE health.workout_logs "
            "(log_date DATE, duration_min INTEGER)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE health.daily_activity (log_date DATE, steps INTEGER)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE health.body_metrics "
            "(log_date DATE, weight_kg NUMERIC, sleep_hours NUMERIC, mood_score INTEGER)"
        )
        today = today_local().isoformat()
        connection.exec_driver_sql(
            "INSERT INTO health.diet_logs VALUES (?, ?, ?)",
            (today, 820, 48),
        )
        connection.exec_driver_sql(
            "INSERT INTO health.workout_logs VALUES (?, ?)",
            (today, 35),
        )
        connection.exec_driver_sql(
            "INSERT INTO health.daily_activity VALUES (?, ?)",
            (today, 6800),
        )
        connection.exec_driver_sql(
            "INSERT INTO health.body_metrics VALUES (?, ?, ?, ?)",
            (today, 70.5, 7.2, 8),
        )
        connection.exec_driver_sql(
            "INSERT INTO health.body_metrics VALUES (date(?, '-6 day'), ?, ?, ?)",
            (today, 71.2, 6.8, 7),
        )

    local_session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db() -> Iterator[Session]:
        session = local_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    from app.main import app

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides.pop(get_machine_health_service, None)
    client = TestClient(app)
    try:
        yield client, tokens, local_session
    finally:
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_machine_endpoints_return_bounded_summary_and_trend(machine_api) -> None:
    client, tokens, _session = machine_api
    headers = _bearer(tokens["health-helper"])

    summary = client.get("/api/machine/v1/agent/profiles/primary/summary", headers=headers)
    trend = client.get(
        "/api/machine/v1/agent/profiles/primary/trends?metric=weight_kg&days=7",
        headers=headers,
    )

    assert summary.status_code == trend.status_code == 200
    assert summary.json()["indicators"]["steps"] == 6800
    assert "不构成诊断或治疗建议" in summary.json()["summary"]
    assert "bp_systolic" not in json.dumps(summary.json())
    assert trend.json()["data_points"] == 2
    assert "series" not in trend.json()
    assert "不构成诊断或治疗建议" in trend.json()["summary"]


def test_machine_api_rejects_legacy_token_scope_and_resource_grant(machine_api) -> None:
    client, tokens, session_factory = machine_api

    missing = client.get("/api/machine/v1/agent/profiles/primary/summary")
    legacy = client.get(
        "/api/machine/v1/agent/profiles/primary/summary",
        headers={"X-Ingest-Token": tokens["health-helper"]},
    )
    wrong_audience = client.get(
        "/api/machine/v1/agent/profiles/primary/summary",
        headers=_bearer(tokens["wrong-audience"]),
    )
    missing_scope = client.get(
        "/api/machine/v1/agent/profiles/primary/trends?metric=steps",
        headers=_bearer(tokens["summary-only"]),
    )
    missing_grant = client.get(
        "/api/machine/v1/agent/profiles/primary/summary",
        headers=_bearer(tokens["ungranted"]),
    )

    assert missing.status_code == legacy.status_code == 401
    assert wrong_audience.json()["error"]["code"] == "audience_forbidden"
    assert missing_scope.json()["error"]["code"] == "scope_forbidden"
    assert missing_grant.json()["error"]["code"] == "resource_forbidden"
    with session_factory() as session:
        denied = session.scalar(
            select(func.count()).select_from(AgentMachineAudit).where(
                AgentMachineAudit.outcome == "denied"
            )
        )
    assert denied == 3


def test_draft_endpoint_is_pending_resource_granted_and_idempotent(machine_api) -> None:
    client, tokens, session_factory = machine_api
    headers = {
        **_bearer(tokens["health-helper"]),
        "Idempotency-Key": "shadow-dsh-test-draft-0001",
        "X-Request-ID": "shadow-dsh-test-call-0001",
    }
    payload = {
        "record_type": "metric",
        "effective_date": today_local().isoformat(),
        "fields": {"sleep_hours": 7, "mood_score": 8},
    }

    created = client.post(
        "/api/machine/v1/agent/profiles/primary/drafts", headers=headers, json=payload
    )
    replayed = client.post(
        "/api/machine/v1/agent/profiles/primary/drafts", headers=headers, json=payload
    )
    conflict = client.post(
        "/api/machine/v1/agent/profiles/primary/drafts",
        headers=headers,
        json={**payload, "fields": {"sleep_hours": 8}},
    )
    denied = client.post(
        "/api/machine/v1/agent/profiles/primary/drafts",
        headers={
            **_bearer(tokens["ungranted"]),
            "Idempotency-Key": "shadow-dsh-test-draft-0002",
        },
        json=payload,
    )

    assert created.status_code == replayed.status_code == 201
    assert created.json()["draft_id"] == replayed.json()["draft_id"]
    assert created.json()["status"] == "pending"
    assert created.json()["direct_domain_write"] is False
    assert replayed.json()["replayed"] is True
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert denied.status_code == 403
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentRecordDraft)) == 1
        audits = session.scalars(select(AgentMachineAudit)).all()
    assert all(event.detail_code != json.dumps(payload) for event in audits)
