"""Validate a Health isolated-restore drill through the Shadow Platform contract."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


REQUIRED_HEALTH_CHECKS = {
    ("schema-current", "contract"),
    ("record-identity-unique", "data"),
    ("health-ready", "health"),
}


def _platform_root(repo_root: Path) -> Path:
    configured = os.environ.get("SHADOW_PLATFORM_ROOT", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend([repo_root.parent / "shadow-platform", repo_root.parents[1] / "shadow-platform"])
    for candidate in candidates:
        if (candidate / "contracts" / "shadow-restore-drill.schema.json").is_file():
            return candidate.resolve()
    raise RuntimeError("shadow-platform checkout not found; set SHADOW_PLATFORM_ROOT")


def _require_health_checks(drill: dict) -> None:
    passed = {
        (item.get("name"), item.get("category"))
        for item in drill.get("checks", [])
        if item.get("status") == "passed"
    }
    missing = REQUIRED_HEALTH_CHECKS - passed
    if missing:
        labels = ", ".join(f"{name}/{category}" for name, category in sorted(missing))
        raise ValueError(f"Health restore drill is missing required passed checks: {labels}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--drill", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    try:
        platform_root = _platform_root(repo_root)
        sys.path.insert(0, str(platform_root))
        from scripts.activate_shadow_profile import verify_release
        from shadow_sdk.conformance import load_json_object, restore_drill_to_evidence

        release = args.release_dir.resolve()
        verify_release(release)
        status = load_json_object(release / "shadow-capability-status.json", label="capability status")
        drill = load_json_object(args.drill.resolve(), label="Health restore drill")
        _require_health_checks(drill)
        evidence = restore_drill_to_evidence(
            args.drill.resolve(), status, platform_root=platform_root
        )
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
