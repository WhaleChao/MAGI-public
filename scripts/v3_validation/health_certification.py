#!/usr/bin/env python3
"""Certify that 1,000 V3 health probes stay cheap and model-free.

The probe imports and calls the production ``HealthService.liveness`` method.
It does not initialize a runtime, create state, bind a socket, contact a
provider, or start a service.  Any newly imported heavy model/browser package
or any sandbox mutation fails the report closed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.dont_write_bytecode = True

SCHEMA = "magi.v3.health-probe-certification/v1"
PROBE_COUNT = 1_000
HEAVY_MODULE_ROOTS = frozenset(
    {
        "mlx",
        "torch",
        "tensorflow",
        "playwright",
        "fitz",
        "pymupdf",
        "whisper",
        "transformers",
        "selenium",
    }
)
LIVE_ROOT = (Path.home() / "Library" / "Application Support" / "MAGI").resolve()
EVIDENCE_PREFIX = "MAGI_V3_OFFLINE_EVIDENCE="


class HealthCertificationError(RuntimeError):
    """The health probe could not produce trustworthy offline evidence."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _prepare_sandbox(path: Path) -> Path:
    raw = path.expanduser()
    if raw.is_symlink():
        raise HealthCertificationError("health certification sandbox must not be a symlink")
    resolved = raw.resolve()
    if resolved in {LIVE_ROOT, REPO_ROOT} or _inside(resolved, LIVE_ROOT) or _inside(
        resolved, REPO_ROOT
    ):
        raise HealthCertificationError("health certification sandbox overlaps live/source state")
    if resolved.exists():
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise HealthCertificationError("health certification sandbox must be empty")
    else:
        resolved.mkdir(parents=True)
    return resolved


def _heavy_imports() -> set[str]:
    return {
        name
        for name in sys.modules
        if name.split(".", 1)[0].lower() in HEAVY_MODULE_ROOTS
    }


def _validation_profile(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    profile = dict(value)
    if (
        set(profile) != {"profile_id", "replay_start_local", "fault_seed"}
        or not isinstance(profile["profile_id"], str)
        or not profile["profile_id"]
        or not isinstance(profile["replay_start_local"], str)
        or not profile["replay_start_local"]
        or type(profile["fault_seed"]) is not int
    ):
        raise HealthCertificationError("health validation profile binding is invalid")
    return profile


def run_health_certification(
    sandbox: Path,
    *,
    validation_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = _prepare_sandbox(sandbox)
    profile = _validation_profile(validation_profile)
    before_entries = tuple(root.iterdir())
    before_heavy = _heavy_imports()
    # Import the production implementation inside the measured interval.  A
    # heavyweight import introduced by magi_v3.health itself must not disappear
    # into the certifier's module-import baseline.
    health_module = importlib.import_module("magi_v3.health")
    health_service = health_module.HealthService
    failures = 0
    model_probe_flags = 0
    maximum_probe_us = 0.0
    started = time.perf_counter_ns()
    for _index in range(PROBE_COUNT):
        probe_started = time.perf_counter_ns()
        report = health_service.liveness()
        maximum_probe_us = max(
            maximum_probe_us,
            (time.perf_counter_ns() - probe_started) / 1_000,
        )
        if report.status != "live" or report.ready is not True:
            failures += 1
        model_probe_flags += int(report.components.get("model_probe_performed") is True)
    duration_us = (time.perf_counter_ns() - started) / 1_000
    after_heavy = _heavy_imports()
    after_entries = tuple(root.iterdir())
    newly_loaded_heavy = sorted(after_heavy - before_heavy)
    state_mutations = sorted(path.name for path in set(after_entries) - set(before_entries))
    eligible = (
        not failures
        and not model_probe_flags
        and not newly_loaded_heavy
        and not state_mutations
    )
    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "certified" if eligible else "failed",
        "probe": "production_health_service_liveness",
        "validation_profile": profile,
        "measurements": {
            "probe_count": PROBE_COUNT,
            "successful_probes": PROBE_COUNT - failures,
            "failed_probes": failures,
            "model_imports": len(newly_loaded_heavy),
            "models_loaded": model_probe_flags,
            "model_probe_flags": model_probe_flags,
            "newly_loaded_heavy_modules": newly_loaded_heavy,
            "state_mutations": state_mutations,
            "total_duration_us": round(duration_us, 3),
            "maximum_probe_us": round(maximum_probe_us, 3),
        },
        "release_binding": {
            "certifier_script_sha256": _sha256_file(SCRIPT_PATH),
            "health_module_sha256": _sha256_file(REPO_ROOT / "magi_v3" / "health.py"),
        },
        "safety": {
            "network_access_performed": False,
            "service_start_performed": False,
            "production_port_access_performed": False,
            "launchctl_performed": False,
            "runtime_initialized": False,
        },
    }
    evidence["evidence_sha256"] = hashlib.sha256(_canonical(evidence)).hexdigest()
    return evidence


def verify_health_evidence(evidence: dict[str, Any]) -> None:
    supplied = evidence.get("evidence_sha256")
    unsigned = dict(evidence)
    unsigned.pop("evidence_sha256", None)
    if evidence.get("schema") != SCHEMA or supplied != hashlib.sha256(
        _canonical(unsigned)
    ).hexdigest():
        raise HealthCertificationError("health certification evidence identity/hash is invalid")


def campaign_evidence(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workload": "health_1000_model_free",
        "status": "passed" if report.get("status") == "certified" else "failed",
        "probe": report.get("probe"),
        "measurements": report.get("measurements"),
        "report": report,
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
    }


def _campaign_inputs() -> tuple[Path, dict[str, Any]]:
    if os.environ.get("MAGI_V3_OFFLINE_CERTIFICATION") != "1":
        raise HealthCertificationError("campaign evidence requires offline certification mode")
    temporary = os.environ.get("TMPDIR")
    profile_id = os.environ.get("MAGI_V3_VALIDATION_PROFILE_ID")
    replay_start = os.environ.get("MAGI_V3_REPLAY_START_LOCAL")
    fault_seed = os.environ.get("MAGI_V3_FAULT_SEED")
    if not temporary or not profile_id or not replay_start or fault_seed is None:
        raise HealthCertificationError("campaign health environment binding is incomplete")
    try:
        parsed_seed = int(fault_seed)
    except ValueError as exc:
        raise HealthCertificationError("campaign health fault seed is invalid") from exc
    profile = {
        "profile_id": profile_id,
        "replay_start_local": replay_start,
        "fault_seed": parsed_seed,
    }
    return Path(temporary) / "magi-v3-health-certification", profile


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--campaign-evidence", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.campaign_evidence:
            if args.sandbox is not None or args.output is not None:
                raise HealthCertificationError(
                    "campaign evidence owns its sandbox and cannot write an output path"
                )
            sandbox, profile = _campaign_inputs()
        else:
            if args.sandbox is None:
                raise HealthCertificationError("--sandbox is required")
            sandbox, profile = args.sandbox, None
        evidence = run_health_certification(
            sandbox,
            validation_profile=profile,
        )
        verify_health_evidence(evidence)
    except (HealthCertificationError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + f".tmp-{os.getpid()}")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        os.replace(temporary, output)
    if args.campaign_evidence:
        outer = json.dumps(
            campaign_evidence(evidence),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        print(EVIDENCE_PREFIX + outer)
    else:
        print(encoded)
    return 0 if evidence["status"] == "certified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
