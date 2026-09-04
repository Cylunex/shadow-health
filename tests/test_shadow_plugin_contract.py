from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).parents[1]

from app.db import get_db  # noqa: E402
from app.models import AgentMachineAudit, AgentRecordDraft, SyncState  # noqa: E402
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
    assert plugin.version == "0.2.0"
    assert declared_routes <= actual_routes
    assert {item["id"] for item in plugin.agent_manifest["capabilities"]} == {
        "health.summary.read",
        "health.trends.read",
        "health.suggestions.read",
        "health.records.draft",
        "health.records.write",
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
        scopes = [
            "health.summary.read",
            "health.trends.read",
            "health.suggestions.read",
            "health.records.draft",
            "health.records.write",
        ]
        grants = {
            "primary": ["summary:read", "trends:read", "suggestions:read", "drafts:create", "records:write"]
        }
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
    SyncState.__table__.create(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE health.diet_logs "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, log_date DATE NOT NULL, meal TEXT, "
            "food_id INTEGER, free_text TEXT, amount_g NUMERIC, kcal NUMERIC, "
            "protein_g NUMERIC, fat_g NUMERIC, carb_g NUMERIC, notes TEXT, provenance JSON, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE health.workout_logs "
            "(log_date DATE, duration_min INTEGER, calories INTEGER, source TEXT DEFAULT 'manual', "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE health.daily_activity "
            "(log_date DATE, steps INTEGER, active_kcal NUMERIC, hr_min INTEGER, source TEXT DEFAULT 'manual', "
            "field_sources JSON DEFAULT '{}', "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE health.body_metrics "
            "(log_date DATE, weight_kg NUMERIC, sleep_hours NUMERIC, mood_score INTEGER, "
            "autofilled JSON DEFAULT '{}', updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)"
        )
        today = today_local().isoformat()
        connection.exec_driver_sql(
            "INSERT INTO health.diet_logs (log_date, kcal, protein_g) VALUES (?, ?, ?)",
            (today, 820, 48),
        )
        connection.exec_driver_sql(
            "INSERT INTO health.workout_logs (log_date, duration_min, calories) VALUES (?, ?, ?)",
            (today, 35, 240),
        )
        connection.exec_driver_sql(
            "INSERT INTO health.daily_activity (log_date, steps, hr_min, field_sources) "
            "VALUES (?, ?, ?, ?)",
            (today, 6800, 62, json.dumps({"steps": "manual", "hr_min": "health_connect"})),
        )
        connection.exec_driver_sql(
            "INSERT INTO health.body_metrics "
            "(log_date, weight_kg, sleep_hours, mood_score) VALUES (?, ?, ?, ?)",
            (today, 70.5, 7.2, 8),
        )
        connection.exec_driver_sql(
            "INSERT INTO health.body_metrics "
            "(log_date, weight_kg, sleep_hours, mood_score) "
            "VALUES (date(?, '-6 day'), ?, ?, ?)",
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
    heart_rate = client.get(
        "/api/machine/v1/agent/profiles/primary/trends?metric=heart_rate&days=7",
        headers=headers,
    )

    assert summary.status_code == trend.status_code == heart_rate.status_code == 200
    assert summary.json()["indicators"]["steps"] == 6800
    assert summary.json()["indicators"]["active_kcal"] == 240
    assert summary.json()["data_quality"]["indicators"]["active_kcal"]["sources"] == [
        "workout_log_fallback"
    ]
    assert summary.json()["data_quality"]["coverage_ratio"] > 0
    assert summary.json()["data_quality"]["sources"]
    assert "不构成诊断或治疗建议" in summary.json()["summary"]
    assert "bp_systolic" not in json.dumps(summary.json())
    assert trend.json()["data_points"] == 2
    assert trend.json()["statistics"]["average"] is None
    assert trend.json()["statistics"]["change"] is None
    assert trend.json()["data_quality"]["sufficient_for_trend"] is False
    assert "series" not in trend.json()
    assert "不构成诊断或治疗建议" in trend.json()["summary"]
    assert heart_rate.json()["data_quality"]["sources"] == ["health_connect"]


def test_weekly_suggestion_is_explainable_bounded_and_stable(machine_api) -> None:
    client, tokens, _session = machine_api
    headers = _bearer(tokens["health-helper"])

    first = client.get(
        "/api/machine/v1/agent/profiles/primary/suggestions", headers=headers
    )
    second = client.get(
        "/api/machine/v1/agent/profiles/primary/suggestions", headers=headers
    )

    assert first.status_code == second.status_code == 200
    item = first.json()["items"][0]
    assert item["protocol"] == "shadow.suggestion.v1"
    assert item["suggestion_id"] == second.json()["items"][0]["suggestion_id"]
    assert item["evidence_refs"] == [
        (
            f"shadow://health/profiles/primary/weekly-reviews/"
            f"{today_local().isocalendar().year}-W{today_local().isocalendar().week:02d}"
        )
    ]
    assert "近 7 天" in item["reason"]
    assert "不构成诊断或治疗建议" in item["reason"]
    assert 0 <= item["data_freshness"]["missing_ratio"] <= 1
    assert item["data_freshness"]["coverage_days"]["steps"] >= 1
    assert item["data_freshness"]["sources"]


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


def test_machine_meal_asset_attach_readback_and_scope_denial(
    machine_api, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import machine_agent
    from app.services.platform_assets import MealAssetPhoto

    client, tokens, _ = machine_api
    day = today_local()
    asset_id = "10000000-0000-4000-8000-000000000001"
    version_id = "20000000-0000-4000-8000-000000000002"
    photo = MealAssetPhoto(
        reference_id="30000000-0000-4000-8000-000000000003",
        asset_id=asset_id,
        version_id=version_id,
        display_name="测试午餐.png",
        content_type="image/png",
    )
    monkeypatch.setattr(
        machine_agent.platform_assets, "attach_meal_photo", lambda **_: (photo, False)
    )
    monkeypatch.setattr(
        machine_agent.platform_assets, "list_meal_photos", lambda *_: [photo]
    )
    attached = client.post(
        "/api/machine/v1/agent/profiles/primary/meal-asset-photos",
        headers=_bearer(tokens["health-helper"]),
        json={
            "effective_date": day.isoformat(),
            "meal": "午餐",
            "asset_id": asset_id,
            "version_id": version_id,
        },
    )
    assert attached.status_code == 201, attached.text
    assert attached.json()["resource_uri"].endswith(f"/{day.isoformat()}/lunch")
    readback = client.get(
        "/api/machine/v1/agent/profiles/primary/meal-asset-photos",
        headers=_bearer(tokens["health-helper"]),
        params={"date": day.isoformat(), "meal": "午餐"},
    )
    assert readback.status_code == 200
    assert readback.json()["items"][0]["asset_id"] == asset_id
    denied = client.post(
        "/api/machine/v1/agent/profiles/primary/meal-asset-photos",
        headers=_bearer(tokens["summary-only"]),
        json={
            "effective_date": day.isoformat(),
            "meal": "午餐",
            "asset_id": asset_id,
            "version_id": version_id,
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "scope_forbidden"


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


def test_nexus_review_commit_materializes_meal_and_is_idempotent(machine_api) -> None:
    client, tokens, session_factory = machine_api
    headers = {
        **_bearer(tokens["health-helper"]),
        "Idempotency-Key": "shadow-nexus-meal-review-0001",
    }
    payload = {
        "record_type": "meal",
        "effective_date": today_local().isoformat(),
        "fields": {
            "meal": "午餐",
            "name": "单人麻辣烫",
            "amount_g": 570,
            "kcal": 679,
            "protein_g": 44.7,
        },
    }
    created = client.post(
        "/api/machine/v1/agent/profiles/primary/drafts", headers=headers, json=payload
    )
    draft_id = created.json()["draft_id"]
    committed = client.post(
        f"/api/machine/v1/agent/profiles/primary/drafts/{draft_id}/commit",
        headers=_bearer(tokens["health-helper"]),
        json={},
    )
    replayed = client.post(
        f"/api/machine/v1/agent/profiles/primary/drafts/{draft_id}/commit",
        headers=_bearer(tokens["health-helper"]),
        json={},
    )

    assert committed.status_code == replayed.status_code == 200
    assert committed.json()["status"] == "applied"
    assert committed.json()["resource_uri"].startswith("shadow://health/diet/")
    assert replayed.json()["resource_uri"] == committed.json()["resource_uri"]
    assert replayed.json()["replayed"] is True
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT meal, free_text, kcal, protein_g, provenance "
                "FROM health.diet_logs WHERE free_text = '单人麻辣烫'"
            )
        ).one()
        draft = session.get(AgentRecordDraft, draft_id)
    assert row.meal == "午餐"
    assert float(row.kcal) == 679
    assert float(row.protein_g) == 44.7
    assert draft is not None and draft.status == "applied"


def test_nexus_review_commit_materializes_each_meal_item_with_all_macros(machine_api) -> None:
    client, tokens, session_factory = machine_api
    headers = {
        **_bearer(tokens["health-helper"]),
        "Idempotency-Key": "shadow-nexus-meal-items-0001",
    }
    payload = {
        "record_type": "meal",
        "effective_date": today_local().isoformat(),
        "fields": {
            "meal": "午餐",
            "name": "食堂快餐（一荤两素）",
            "amount_g": 500,
            "kcal": 671,
            "protein_g": 30,
            "fat_g": 35,
            "carb_g": 60.4,
            "items": [
                {"name": "白米饭", "amount_g": 160, "kcal": 186, "carb_g": 41.4, "protein_g": 4.2, "fat_g": 0.5},
                {"name": "手撕包菜", "amount_g": 110, "kcal": 85, "carb_g": 5.5, "protein_g": 1.8, "fat_g": 6.5},
                {"name": "辣椒炒香干", "amount_g": 140, "kcal": 140, "carb_g": 6, "protein_g": 8, "fat_g": 9.5},
                {"name": "香酥炸鸡块", "amount_g": 90, "kcal": 260, "carb_g": 7.5, "protein_g": 16, "fat_g": 18.5},
            ],
        },
    }
    created = client.post(
        "/api/machine/v1/agent/profiles/primary/drafts", headers=headers, json=payload
    )
    assert created.status_code == 201
    draft_id = created.json()["draft_id"]

    pending = client.get(
        "/api/machine/v1/agent/profiles/primary/drafts",
        headers=_bearer(tokens["health-helper"]),
    )
    assert pending.status_code == 200
    assert pending.json()["items"][0]["fields"]["items"][2]["name"] == "辣椒炒香干"

    committed = client.post(
        f"/api/machine/v1/agent/profiles/primary/drafts/{draft_id}/commit",
        headers=_bearer(tokens["health-helper"]),
        json={},
    )
    replayed = client.post(
        f"/api/machine/v1/agent/profiles/primary/drafts/{draft_id}/commit",
        headers=_bearer(tokens["health-helper"]),
        json={},
    )
    assert committed.status_code == replayed.status_code == 200
    assert committed.json()["resource_uri"] == f"shadow://health/diet/batches/{draft_id}"
    assert replayed.json()["resource_uri"] == committed.json()["resource_uri"]
    assert replayed.json()["replayed"] is True

    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT id, free_text, amount_g, kcal, protein_g, fat_g, carb_g, provenance "
                "FROM health.diet_logs WHERE free_text IN "
                "('白米饭', '手撕包菜', '辣椒炒香干', '香酥炸鸡块') ORDER BY id"
            )
        ).all()
        draft = session.get(AgentRecordDraft, draft_id)
    assert [row.free_text for row in rows] == ["白米饭", "手撕包菜", "辣椒炒香干", "香酥炸鸡块"]
    assert sum(float(row.kcal) for row in rows) == 671
    assert sum(float(row.carb_g) for row in rows) == 60.4
    assert json.loads(rows[3].provenance)["meal_name"] == "食堂快餐（一荤两素）"
    assert draft is not None and draft.payload["_result_ids"] == [row.id for row in rows]


def test_meal_item_validation_rejects_unknown_or_oversized_detail(machine_api) -> None:
    client, tokens, _ = machine_api
    headers = {
        **_bearer(tokens["health-helper"]),
        "Idempotency-Key": "shadow-nexus-invalid-items-001",
    }
    base = {
        "record_type": "meal",
        "effective_date": today_local().isoformat(),
        "fields": {"meal": "午餐", "name": "套餐"},
    }
    unknown = client.post(
        "/api/machine/v1/agent/profiles/primary/drafts",
        headers=headers,
        json={**base, "fields": {**base["fields"], "items": [{"name": "米饭", "price": 2}]}},
    )
    too_many = client.post(
        "/api/machine/v1/agent/profiles/primary/drafts",
        headers={**headers, "Idempotency-Key": "shadow-nexus-invalid-items-002"},
        json={**base, "fields": {**base["fields"], "items": [{"name": "米饭"}] * 51}},
    )
    assert unknown.status_code == too_many.status_code == 400
    assert unknown.json()["error"]["code"] == "invalid_draft"


def test_pending_health_drafts_can_be_federated_and_rejected_from_nexus(machine_api) -> None:
    client, tokens, session_factory = machine_api
    headers = {
        **_bearer(tokens["health-helper"]),
        "Idempotency-Key": "shadow-nexus-federated-health-0001",
    }
    payload = {
        "record_type": "meal",
        "effective_date": today_local().isoformat(),
        "fields": {"meal": "午餐", "name": "待审核午餐", "kcal": 520},
    }
    created = client.post(
        "/api/machine/v1/agent/profiles/primary/drafts", headers=headers, json=payload
    )
    draft_id = created.json()["draft_id"]
    pending = client.get(
        "/api/machine/v1/agent/profiles/primary/drafts",
        headers=_bearer(tokens["health-helper"]),
    )
    rejected = client.post(
        f"/api/machine/v1/agent/profiles/primary/drafts/{draft_id}/reject",
        headers=_bearer(tokens["health-helper"]),
        json={},
    )
    replayed = client.post(
        f"/api/machine/v1/agent/profiles/primary/drafts/{draft_id}/reject",
        headers=_bearer(tokens["health-helper"]),
        json={},
    )
    remaining = client.get(
        "/api/machine/v1/agent/profiles/primary/drafts",
        headers=_bearer(tokens["health-helper"]),
    )

    assert pending.status_code == 200
    assert len(pending.json()["items"]) == 1
    assert pending.json()["items"][0]["resource_uri"] == created.json()["resource_uri"]
    assert pending.json()["items"][0]["fields"] == payload["fields"]
    assert rejected.status_code == replayed.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert replayed.json()["replayed"] is True
    assert remaining.json()["items"] == []
    with session_factory() as session:
        draft = session.get(AgentRecordDraft, draft_id)
    assert draft is not None and draft.status == "rejected"


def test_standard_nexus_review_protocol_creates_lists_and_commits(machine_api) -> None:
    client, tokens, session_factory = machine_api
    headers = {
        **_bearer(tokens["health-helper"]),
        "Idempotency-Key": "standard-health-review-0001",
    }
    created = client.post(
        "/api/machine/v1/agent/nexus/reviews?profile_id=primary",
        headers=headers,
        json={
            "intent": "health.meal",
            "summary": "午餐",
            "fields": {
                "recordType": "meal",
                "effectiveDate": today_local().isoformat(),
                "meal": "午餐",
                "mealName": "测试套餐",
                "kcal": 520,
                "mealItemsJson": json.dumps([
                    {"name": "测试米饭", "kcal": 220, "notes": "实际吃了约八成"},
                    {"name": "测试鸡肉", "kcal": 300, "notes": "去皮后估算"},
                ], ensure_ascii=False),
            },
        },
    )
    assert created.status_code == 201, created.text
    review = created.json()
    assert review["protocol"] == "shadow.review.v1"
    assert review["domain"] == "health"
    assert review["state"] == "pending"
    assert review["fields"]["mealName"] == "测试套餐"
    projected_items = json.loads(review["fields"]["mealItemsJson"])
    assert projected_items[0]["notes"] == "实际吃了约八成"

    listed = client.get(
        "/api/machine/v1/agent/nexus/reviews?profile_id=primary",
        headers=_bearer(tokens["health-helper"]),
    )
    assert listed.status_code == 200, listed.text
    assert [item["review_id"] for item in listed.json()["items"]] == [review["review_id"]]

    committed = client.post(
        f"/api/machine/v1/agent/nexus/reviews/{review['review_id']}/commit?profile_id=primary",
        headers=_bearer(tokens["health-helper"]),
        json={},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["state"] == "committed"
    assert committed.json()["receipt"].startswith("shadow://health/diet/batches/")
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT free_text, notes FROM health.diet_logs "
                "WHERE free_text IN ('测试米饭', '测试鸡肉') ORDER BY id"
            )
        ).all()
    assert [(row.free_text, row.notes) for row in rows] == [
        ("测试米饭", "实际吃了约八成"),
        ("测试鸡肉", "去皮后估算"),
    ]
