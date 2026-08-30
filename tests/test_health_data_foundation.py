from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.routers.ingest import (
    HealthConnectNormalizationError,
    _external_id,
    _existing_effective_date,
    _extract_heart_rate_samples,
    _failure_reason,
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


def test_heart_rate_samples_are_strictly_parsed_and_split_by_record_timezone() -> None:
    samples = _extract_heart_rate_samples({
        "recordType": "HeartRateRecord",
        "startZoneOffset": "+08:00",
        "samples": [
            {"time": "2026-08-30T15:59:00Z", "beatsPerMinute": 58},
            {"time": "2026-08-30T16:01:00Z", "beatsPerMinute": {"value": 82}},
        ],
    })
    assert [(sample[1].isoformat(), sample[2]) for sample in samples] == [
        ("2026-08-30", 58),
        ("2026-08-31", 82),
    ]


@pytest.mark.parametrize(
    ("record", "code"),
    [
        ({"recordType": "HeartRateRecord", "samples": []}, "heart_rate_samples_empty"),
        (
            {
                "recordType": "HeartRateRecord",
                "samples": [{"time": "2026-08-30T01:00:00Z", "beatsPerMinute": 999}],
            },
            "heart_rate_value_invalid",
        ),
        (
            {"recordType": "HeartRateRecord", "samples": [{"beatsPerMinute": 70}]},
            "heart_rate_time_missing",
        ),
    ],
)
def test_heart_rate_never_silently_discards_malformed_samples(
    record: dict, code: str
) -> None:
    with pytest.raises(HealthConnectNormalizationError) as captured:
        _extract_heart_rate_samples(record)
    reason, detail = _failure_reason(captured.value)
    assert reason == code
    assert detail.startswith(f"{code}:")


def test_heart_rate_single_value_bridge_shape_remains_supported() -> None:
    samples = _extract_heart_rate_samples({
        "type": "heart_rate",
        "time": 1_788_051_600_000,
        "heart_rate": "72",
    })
    assert len(samples) == 1
    assert samples[0][2] == 72


def test_health_connect_android_contract_requires_stable_identity_and_raw_hr_samples() -> None:
    contract = json.loads(
        (Path(__file__).parents[1] / "contracts" / "health-connect-ingest.schema.json")
        .read_text(encoding="utf-8")
    )
    record = contract["properties"]["records"]["items"]
    assert record["required"] == ["recordType", "metadata"]
    assert record["properties"]["metadata"]["required"] == [
        "clientRecordId", "clientRecordVersion"
    ]
    heart = record["allOf"][0]["then"]
    assert "samples" in heart["required"]
    assert heart["properties"]["samples"]["items"]["required"] == [
        "time", "beatsPerMinute"
    ]


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
