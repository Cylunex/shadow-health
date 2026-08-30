from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.routers.ingest import (
    _external_id,
    _existing_effective_date,
    _parse_sync_boundaries,
    _payload_hash,
    _record_action,
    _record_provenance,
    _record_version,
)
from scripts.platform_evidence import build_observed_evidence
from scripts.verify_restore import _require_health_checks


def test_health_connect_identity_version_and_provenance_are_stable() -> None:
    record = {
        "metadata": {
            "clientRecordId": "watch-steps-20260830-1",
            "clientRecordVersion": 7,
            "dataOrigin": {"packageName": "com.example.health"},
            "device": {"manufacturer": "Example", "model": "Watch", "type": 2},
            "recordingMethod": 1,
        },
        "startTime": "2026-08-30T01:00:00Z",
        "count": 123,
    }
    assert _external_id(record) == "watch-steps-20260830-1"
    assert _record_version(record) == 7
    assert len(_payload_hash(record)) == 64
    assert _payload_hash(record) == _payload_hash(dict(reversed(list(record.items()))))
    provenance = _record_provenance(record)
    assert provenance["channel"] == "health_connect"
    assert provenance["origin_package"] == "com.example.health"
    assert provenance["device"]["model"] == "Watch"


def test_health_connect_sync_boundaries_are_per_type_and_bounded() -> None:
    parsed = _parse_sync_boundaries({"sync": [
        {"record_type": "steps", "cursor": "steps-2", "permission": "granted", "source_fingerprint": "apps-v1"},
        {"record_type": "sleep", "permission": "revoked"},
    ]})
    assert [item["record_type"] for item in parsed] == ["steps", "sleep"]
    assert parsed[0]["cursor"] == "steps-2"
    assert parsed[1]["permission"] == "revoked"
    with pytest.raises(ValueError):
        _parse_sync_boundaries({"sync": [
            {"record_type": "steps"}, {"record_type": "steps"},
        ]})
    with pytest.raises(ValueError):
        _parse_sync_boundaries({"sync": {"record_type": "steps", "cursor": "x" * 2049}})


def test_record_version_resolution_is_idempotent_and_quarantines_conflicts() -> None:
    common = {"prior_version": 3, "prior_hash": "a", "prior_status": "parsed"}
    assert _record_action(**common, incoming_version=4, incoming_hash="b") == "update"
    assert _record_action(**common, incoming_version=2, incoming_hash="a") == "skip"
    assert _record_action(**common, incoming_version=3, incoming_hash="a") == "skip"
    assert _record_action(**common, incoming_version=3, incoming_hash="b") == "conflict"
    assert _record_action(
        prior_version=3, prior_hash="a", prior_status="failed",
        incoming_version=3, incoming_hash="a",
    ) == "retry"


def test_existing_rows_without_normalized_snapshot_keep_their_old_day() -> None:
    row = SimpleNamespace(
        normalized=None,
        raw={
            "recordType": "StepsRecord",
            "startTime": "2026-08-29T03:00:00Z",
            "count": 100,
        },
    )
    assert _existing_effective_date("steps", row).isoformat() == "2026-08-29"


def test_observed_evidence_requires_quality_envelopes_without_copying_values() -> None:
    def fake_get(path: str):
        if path in {"/healthz", "/readyz"}:
            return 200, None
        if path.endswith("/suggestions"):
            return 200, {"items": [{"data_freshness": {"missing_ratio": 0.5}}]}
        return 200, {"data_quality": {"coverage_ratio": 0.5}, "sensitive_value": 72.3}

    evidence = build_observed_evidence(
        deployment_id="health-prod",
        build_id="a" * 64,
        instance_id="health-main",
        profile_id="primary",
        get_json=fake_get,
        observed_at=datetime(2026, 8, 30, tzinfo=UTC),
        run_id="health-test-run",
    )
    assert all(record["status"] == "passed" for record in evidence["records"])
    encoded = str(evidence)
    assert "72.3" not in encoded
    assert "shadow.conformance-evidence.v1" in encoded


def test_restore_entry_requires_health_specific_contract_data_and_health_checks() -> None:
    _require_health_checks({"checks": [
        {"name": "schema-current", "category": "contract", "status": "passed"},
        {"name": "record-identity-unique", "category": "data", "status": "passed"},
        {"name": "health-ready", "category": "health", "status": "passed"},
    ]})
    with pytest.raises(ValueError):
        _require_health_checks({"checks": [
            {"name": "health-ready", "category": "health", "status": "passed"},
        ]})
