"""Probe deployed read capabilities and emit Shadow lifecycle evidence.

The probe records only bounded status/contract facts.  Tokens, health values, and
response bodies are never written to the evidence document.
"""
from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

CAPABILITIES = {
    "health.summary.read": "/api/machine/v1/agent/profiles/{profile}/summary",
    "health.trends.read": "/api/machine/v1/agent/profiles/{profile}/trends?metric=weight_kg&days=7",
    "health.suggestions.read": "/api/machine/v1/agent/profiles/{profile}/suggestions",
}
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_id(label: str, value: str) -> str:
    if not ID_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lower-case Shadow id")
    return value


def _quality_present(capability: str, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if capability in {"health.summary.read", "health.trends.read"}:
        return isinstance(payload.get("data_quality"), dict)
    items = payload.get("items")
    return bool(
        isinstance(items, list)
        and items
        and isinstance(items[0], dict)
        and isinstance(items[0].get("data_freshness"), dict)
    )


def build_observed_evidence(
    *,
    deployment_id: str,
    build_id: str,
    instance_id: str,
    profile_id: str,
    get_json: Callable[[str], tuple[int, Any]],
    observed_at: datetime | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    _require_id("deployment_id", deployment_id)
    _require_id("instance_id", instance_id)
    _require_id("profile_id", profile_id)
    if not SHA_RE.fullmatch(build_id):
        raise ValueError("build_id must be a lowercase SHA-256")
    stamp = (observed_at or datetime.now(UTC)).astimezone(UTC)
    run = run_id or f"health-probe-{uuid.uuid4()}"
    correlation = f"health-conformance-{uuid.uuid4()}"
    records = []
    health_status, _ = get_json("/healthz")
    ready_status, _ = get_json("/readyz")
    service_ok = health_status == ready_status == 200
    for capability, route in CAPABILITIES.items():
        status, payload = get_json(route.format(profile=profile_id))
        contract_ok = status == 200 and _quality_present(capability, payload)
        passed = service_ok and contract_ok
        records.append({
            "capability_ref": f"shadow://capabilities/shadow-health/{instance_id}/{capability}",
            "stage": "observed",
            "status": "passed" if passed else "failed",
            "detail": (
                "live capability returned bounded coverage, source, and freshness evidence"
                if passed else "live capability or required data-quality envelope failed"
            ),
            "checks": [
                {
                    "name": "service-ready",
                    "category": "health",
                    "status": "passed" if service_ok else "failed",
                    "detail": f"healthz={health_status}, readyz={ready_status}",
                },
                {
                    "name": "quality-evidence",
                    "category": "contract",
                    "status": "passed" if contract_ok else "failed",
                    "detail": f"HTTP {status}; required bounded quality envelope present={contract_ok}",
                },
            ],
        })
    return {
        "version": 1,
        "protocol": "shadow.conformance-evidence.v1",
        "evidence_id": f"health-observed-{uuid.uuid4()}",
        "producer": {
            "project_id": "shadow-health",
            "component": "health-conformance-probe",
        },
        "deployment_id": deployment_id,
        "build_id": build_id,
        "observed_at": stamp.isoformat().replace("+00:00", "Z"),
        "correlation": {
            "run_id": run,
            "correlation_id": correlation,
            "trace_id": correlation,
            "request_id": f"health-probe-request-{uuid.uuid4()}",
        },
        "records": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--profile-id", default="primary")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        parser.error("token file is empty")
    base_url = args.base_url.rstrip("/")
    parsed_url = urlsplit(base_url)
    loopback_http = (
        parsed_url.scheme == "http"
        and parsed_url.hostname in {"127.0.0.1", "localhost", "::1"}
    )
    if parsed_url.scheme != "https" and not loopback_http:
        parser.error("base-url must use HTTPS except for a loopback probe")
    if parsed_url.username is not None or parsed_url.password is not None:
        parser.error("base-url must not contain credentials")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    def get_json(path: str) -> tuple[int, Any]:
        try:
            response = session.get(base_url + path, timeout=args.timeout)
        except requests.RequestException:
            return 599, None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return response.status_code, payload

    try:
        evidence = build_observed_evidence(
            deployment_id=args.deployment_id,
            build_id=args.build_id,
            instance_id=args.instance_id,
            profile_id=args.profile_id,
            get_json=get_json,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if any(record["status"] != "passed" for record in evidence["records"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
