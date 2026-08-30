"""Hash-bound, fail-closed offline validation campaign runner for a V3 release."""

from __future__ import annotations

import argparse
import base64
from magi_v3 import fcntl_compat as fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from scripts.v3_python_runtime_snapshot import (
    PythonRuntimeBlocked,
    verify_runtime_manifest,
)
from scripts.v3_source_contract import account_home
from scripts.v3_validation.g8_isolated_smb import (
    G8SMBBlocked,
    verify_report as verify_g8_smb_report,
)

LIVE_ROOT = Path.home() / "Library" / "Application Support" / "MAGI"
APPLICATION_SUPPORT = Path.home() / "Library" / "Application Support"
MANIFEST_NAME = "release-manifest.json"
COMPLETION_MARKER = "RELEASE_COMPLETE.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
ROUTE_BASE_ENVIRONMENT_KEYS = (
    "HOME", "LANG", "LC_ALL", "MAGI_AGENT_DIR", "MAGI_ALLOW_CLOUD_MODELS",
    "MAGI_ALLOW_INTERNET", "MAGI_DISABLE_SERVER_STARTUP_HOOKS",
    "MAGI_DISCORD_LAST_CHANNEL_FILE", "MAGI_ENABLE_LIVE_TESTS", "MAGI_EXPORTS_DIR",
    "MAGI_LINE_LAST_SENDER_FILE", "MAGI_METRICS_DIR", "MAGI_OSC_FILE_SHARE_CACHE_DIR",
    "MAGI_OSC_FILE_SHARE_STORE", "MAGI_ROOT_DIR", "MAGI_RUNTIME_DIR",
    "MAGI_SKIP_IMPORT_PROBES", "MAGI_V3_OFFLINE_CERTIFICATION",
    "MAGI_WEB_RESEARCH_CACHE_DIR", "PATH", "PYTHONDONTWRITEBYTECODE",
    "PYTHONPYCACHEPREFIX",
    "PYTHONNOUSERSITE", "PYTHONPATH", "PYTHONSAFEPATH",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "TMPDIR",
)
ROUTE_TRACE_ENVIRONMENT_KEYS = tuple(sorted({
    *ROUTE_BASE_ENVIRONMENT_KEYS,
    "MAGI_V3_ROUTE_TRACE_FILE",
    "MAGI_V3_ROUTE_TRACE_LIVE_ROOT",
    "MAGI_V3_ROUTE_TRACE_SANDBOX",
}))
ROUTE_FORMAL_RUNTIME_ENVIRONMENT_KEYS = (
    "MAGI_V3_PYTHON_RUNTIME",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256",
    "MAGI_V3_PYTHON_RUNTIME_REALPATH",
    "MAGI_V3_PYTHON_RUNTIME_SHA256",
    "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256",
    "MAGI_V3_ROUTE_CERTIFYING",
)


def _route_external_storage_roots() -> list[str]:
    home = account_home()
    return [
        str(Path("/Volumes")),
        str(home / "Library" / "CloudStorage"),
        str(home / ".magi_mounts"),
        str(home / "SynologyDrive"),
    ]


def _route_live_root() -> Path:
    return account_home() / "Library" / "Application Support" / "MAGI"


def _route_seatbelt_profile(workspace: Path) -> bytes:
    from scripts.v3_validation.route_certification import _seatbelt_profile_bytes

    return _seatbelt_profile_bytes(workspace)


def _route_seatbelt_attestation(workspace: Path) -> dict[str, Any]:
    from scripts.v3_validation.route_certification import _seatbelt_attestation

    return _seatbelt_attestation(workspace)


def _route_attested_seatbelt_workspace(
    value: Any, expected_profile: dict[str, Any] | None
) -> Path | None:
    if not isinstance(value, dict):
        return None
    roots = value.get("allowed_write_roots")
    if not isinstance(roots, list) or len(roots) != 1 or not isinstance(roots[0], str):
        return None
    candidate = Path(roots[0])
    if not candidate.is_absolute():
        return None
    canonical = Path(os.path.abspath(candidate.expanduser())).resolve()
    if str(canonical) != roots[0] or value != _route_seatbelt_attestation(canonical):
        return None
    profile_id = expected_profile.get("profile_id") if isinstance(expected_profile, dict) else None
    if profile_id is not None and (
        canonical.name != profile_id or canonical.parent.name != "route-certification"
    ):
        return None
    for root in (*_route_external_storage_roots(), str(_route_live_root())):
        try:
            canonical.relative_to(Path(root).resolve())
        except ValueError:
            continue
        return None
    return canonical


def _route_runtime_site_packages(payload: dict[str, Any]) -> list[str]:
    roots: list[str] = []
    for root_key, rows_key in (
        ("runtime_root", "directories"),
        ("base_runtime_root", "base_directories"),
    ):
        root_text = payload.get(root_key)
        rows = payload.get(rows_key)
        if not isinstance(root_text, str) or not Path(root_text).is_absolute() or not isinstance(rows, list):
            raise CampaignSafetyError("route runtime manifest roots are invalid")
        root = Path(root_text).resolve(strict=True)
        for row in rows:
            relative = row.get("path") if isinstance(row, dict) else None
            if not isinstance(relative, str):
                raise CampaignSafetyError("route runtime manifest directory is invalid")
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts or pure.name != "site-packages":
                continue
            candidate = (root / Path(*pure.parts)).resolve(strict=True)
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise CampaignSafetyError("route runtime site-packages escapes manifest") from exc
            if candidate.is_dir() and str(candidate) not in roots:
                roots.append(str(candidate))
    if not roots:
        raise CampaignSafetyError("route runtime manifest has no site-packages")
    return sorted(roots)


def _route_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_route_runtime_binding(
    value: Any,
    *,
    expected_release: ReleaseBundle | None,
    expected_binding: dict[str, Any] | None,
) -> bool:
    required = {
        "certifying", "mode", "python_runtime", "python_runtime_realpath",
        "python_runtime_sha256", "runtime_manifest", "runtime_manifest_sha256",
        "runtime_tree_sha256", "runtime_root", "base_runtime_root",
        "pythonpath_roots", "user_site_included", "parent_sys_path_inherited",
        "site_processing_disabled",
    }
    if not isinstance(value, dict) or set(value) != required:
        return False
    if (
        value.get("certifying") is not True
        or value.get("mode") != "formal_manifest_bound"
        or value.get("user_site_included") is not False
        or value.get("parent_sys_path_inherited") is not False
        or value.get("site_processing_disabled") is not True
        or any(not SHA256_RE.fullmatch(str(value.get(key) or "")) for key in (
            "python_runtime_sha256", "runtime_manifest_sha256", "runtime_tree_sha256"
        ))
    ):
        return False
    roots = value.get("pythonpath_roots")
    if not isinstance(roots, list) or len(roots) < 2 or len(set(roots)) != len(roots):
        return False
    try:
        canonical = [str(Path(root).resolve()) for root in roots if isinstance(root, str) and Path(root).is_absolute()]
        runtime_root = Path(str(value["runtime_root"])).resolve()
        base_root = Path(str(value["base_runtime_root"])).resolve()
    except (OSError, TypeError):
        return False
    if canonical != roots or len(canonical) != len(roots):
        return False
    if expected_release is not None and roots[0] != str(expected_release.root):
        return False
    user_python = account_home() / "Library" / "Python"
    for root_text in roots[1:]:
        root = Path(root_text)
        if root.name != "site-packages":
            return False
        if root == user_python or user_python in root.parents:
            return False
        if not any(_route_inside(root, allowed) for allowed in (runtime_root, base_root)):
            return False
    return expected_binding is None or value == expected_binding

_PYTEST = ("-m", "pytest", "-q", "-p", "no:cacheprovider")
EVIDENCE_PREFIX = "MAGI_V3_OFFLINE_EVIDENCE="
# These are exact, release-relative test targets. Configuration may select only
# these names; neither configuration nor CLI input can supply executable argv.
OFFLINE_COMMANDS: dict[str, tuple[str, ...]] = {
    "346_route_contract_replay": (
        "scripts/v3_validation/route_certification.py",
    ),
    "seven_day_schedule_10x_arrival_2x_duration_replay": (
        "scripts/v3_validation/schedule_capacity_certification.py",
        "--campaign-evidence",
    ),
    "golden_business_flows": (
        "scripts/v3_validation/release_quality_certification.py",
        "--campaign-evidence",
    ),
    "fault_injection": (
        *_PYTEST,
        "-s",
        "-k",
        "test_bounded_fault_matrix_with_realism_audit_emits_recovery_duplicate_and_loss_evidence",
        "tests/v3/test_fault_realism.py",
        "tests/v3/test_campaign_offline_probes.py",
    ),
    "fault_recovery_certification": (
        "scripts/v3_validation/fault_certification.py",
        "--campaign-evidence",
    ),
    "hundred_cycle_worker_reap_soak": (
        "-m",
        "pytest",
        "-q",
        "-s",
        "-p",
        "no:cacheprovider",
        "-k",
        "test_hundred_cycle_worker_reap_soak_emits_measured_evidence",
        "tests/v3/test_campaign_offline_probes.py",
    ),
    "health_1000_model_free": (
        "scripts/v3_validation/health_certification.py",
        "--campaign-evidence",
    ),
}
FORBIDDEN_EXECUTABLES = frozenset(
    {"launchctl", "systemctl", "service", "kill", "killall", "pkill", "reboot", "shutdown"}
)
FORBIDDEN_PORTS = frozenset(
    {5002, 5003, 5014, 50052, 5102, 5103, 8188, 8080, 8081, 8082, 8083, 8088, 8090}
)


class CampaignSafetyError(RuntimeError):
    """Raised whenever campaign evidence can no longer be trusted."""


@dataclass(frozen=True, slots=True)
class CampaignContext:
    campaign_id: str
    release_sha: str
    hardware_id: str
    gate_config_sha256: str

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.campaign_id or ""):
            raise CampaignSafetyError("invalid campaign_id")
        if not SHA256_RE.fullmatch(self.release_sha):
            raise CampaignSafetyError(
                "release_sha must be the lowercase 64-character source_snapshot_sha256"
            )
        if not self.hardware_id or len(self.hardware_id) > 256:
            raise CampaignSafetyError("invalid hardware_id")
        if not SHA256_RE.fullmatch(self.gate_config_sha256):
            raise CampaignSafetyError("gate_config_sha256 must be lowercase SHA-256")

    def to_dict(self) -> dict[str, str]:
        return {
            "campaign_id": self.campaign_id,
            "release_sha": self.release_sha,
            "hardware_id": self.hardware_id,
            "gate_config_sha256": self.gate_config_sha256,
        }


@dataclass(frozen=True, slots=True)
class ReleaseBundle:
    root: Path
    release_id: str
    commit: str
    source_snapshot_sha256: str
    manifest_sha256: str
    files: tuple[tuple[str, str, int, int], ...]

    def binding(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "release_commit": self.commit,
            "release_manifest_sha256": self.manifest_sha256,
            "release_source_file_count": len(self.files),
        }


CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _failure_diagnostics(stdout: str, stderr: str) -> dict[str, Any]:
    """Preserve bounded structured errors without retaining raw workload logs."""

    for line in reversed(stdout.splitlines()):
        candidate = line[line.find("{") :] if "{" in line else ""
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("ok") is not False:
            continue
        diagnostic: dict[str, Any] = {
            "schema": "magi.v3.workload-failure-diagnostic/v1",
            "source": "structured_stdout",
        }
        for key in ("error", "child_error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                diagnostic[key] = "".join(
                    character if character >= " " or character in "\t" else "?"
                    for character in value.strip()
                )[:8000]
        if type(payload.get("returncode")) is int:
            diagnostic["child_returncode"] = payload["returncode"]
        for key in ("stdout_sha256", "stderr_sha256"):
            value = payload.get(key)
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                diagnostic[f"child_{key}"] = value
        return diagnostic
    return {
        "schema": "magi.v3.workload-failure-diagnostic/v1",
        "source": "digest_only",
        "stdout_bytes": len(stdout.encode()),
        "stderr_bytes": len(stderr.encode()),
    }


def _structured_workload_evidence(
    workload: str,
    stdout: str,
    expected_profile: dict[str, Any] | None = None,
    expected_release: ReleaseBundle | None = None,
    expected_python_runtime_sha256: str | None = None,
    expected_route_runtime_binding: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    structured_workloads = {
        "346_route_contract_replay",
        "seven_day_schedule_10x_arrival_2x_duration_replay",
        "golden_business_flows",
        "fault_injection",
        "fault_recovery_certification",
        "hundred_cycle_worker_reap_soak",
        "health_1000_model_free",
    }
    if workload not in structured_workloads:
        return None
    # Pytest's quiet progress renderer may leave earlier test dots on the same
    # physical line as a later ``print`` call (for example
    # ``................MAGI_V3_OFFLINE_EVIDENCE=...``).  The prefix remains
    # the trust boundary; accept it once anywhere on a line while still
    # rejecting zero or multiple records below.
    encoded: list[str] = []
    for line in stdout.splitlines():
        occurrences = line.count(EVIDENCE_PREFIX)
        if occurrences:
            if occurrences != 1:
                raise CampaignSafetyError(
                    f"{workload} must emit exactly one structured evidence record"
                )
            encoded.append(line.split(EVIDENCE_PREFIX, 1)[1])
    if len(encoded) != 1:
        raise CampaignSafetyError(f"{workload} must emit exactly one structured evidence record")
    try:
        evidence = json.loads(encoded[0], object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise CampaignSafetyError("hundred-cycle soak evidence is invalid JSON") from exc
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema_version") != 1
        or evidence.get("workload") != workload
        or evidence.get("status") != "passed"
    ):
        raise CampaignSafetyError(f"{workload} evidence identity is invalid")
    for field in (
        "service_start_performed",
        "production_port_access_performed",
        "launchctl_performed",
    ):
        if evidence.get(field) is not False:
            raise CampaignSafetyError(f"{workload} safety attestation failed: {field}")
    if (
        workload != "seven_day_schedule_10x_arrival_2x_duration_replay"
        and evidence.get("network_access_performed") is not False
    ):
        raise CampaignSafetyError(
            f"{workload} safety attestation failed: network_access_performed"
        )
    measurements = evidence.get("measurements")
    if not isinstance(measurements, dict):
        raise CampaignSafetyError(f"{workload} measurements are missing")
    if workload == "346_route_contract_replay":
        _validate_route_certification_evidence(
            evidence,
            expected_profile=expected_profile,
            expected_release=expected_release,
            expected_runtime_binding=expected_route_runtime_binding,
        )
        return evidence
    if workload == "health_1000_model_free":
        _validate_health_certification_evidence(
            evidence,
            expected_profile=expected_profile,
            expected_release=expected_release,
        )
        return evidence
    if workload == "golden_business_flows":
        _validate_release_quality_certification_evidence(
            evidence,
            expected_profile=expected_profile,
            expected_release=expected_release,
            expected_python_runtime_sha256=expected_python_runtime_sha256,
        )
        return evidence
    if workload == "fault_recovery_certification":
        _validate_fault_certification_evidence(
            evidence,
            expected_profile=expected_profile,
            expected_release=expected_release,
        )
        return evidence
    if workload == "matched_v2_v3_performance":
        _validate_resource_performance_partial(
            evidence,
            expected_profile=expected_profile,
            expected_release=expected_release,
            expected_python_runtime_sha256=expected_python_runtime_sha256,
        )
        report = evidence["report"]["performance_report"]
        proof = report["equivalence_proof"]
        handler_identities = proof.get("handler_identities")
        expected_modes = {"v2_actual_livez_wsgi", "v3_native_gateway_livez_wsgi"}
        if (
            not isinstance(handler_identities, dict)
            or set(handler_identities) != expected_modes
            or any(not isinstance(row, dict) for row in handler_identities.values())
            or handler_identities["v2_actual_livez_wsgi"].get("implementation")
            != "production_v2"
            or handler_identities["v3_native_gateway_livez_wsgi"].get("implementation")
            != "native_v3"
            or any(
                not SHA256_RE.fullmatch(str(row.get("source_sha256") or ""))
                for row in handler_identities.values()
                if isinstance(row, dict)
            )
            or len(
                {
                    row.get("source_sha256")
                    for row in handler_identities.values()
                    if isinstance(row, dict)
                }
            )
            != 2
        ):
            raise CampaignSafetyError("matched performance handler identity proof failed")
        coverage = report.get("claim_coverage")
        if (
            not isinstance(coverage, dict)
            or coverage.get("production_v2_handler") is not True
            or coverage.get("native_v3_handler") is not True
            or coverage.get("native_v3_gateway_probe") is not True
            or coverage.get("production_business_workload") is not False
            or coverage.get("representative_synthetic_business_corpus_defined") is not True
            or coverage.get("matched_native_business_handler") is not True
            or coverage.get("synthetic_business_workload") is not True
            or coverage.get("synthetic_business_get_measured") is not True
            or coverage.get("synthetic_business_post_measured") is not True
            or coverage.get("rss_evidence") is not True
            or coverage.get("file_descriptor_evidence") is not True
            or coverage.get("same_host_sequential_not_concurrent") is not True
            or coverage.get("release_gateway_thresholds_applied") is not True
        ):
            raise CampaignSafetyError("matched performance claim coverage is invalid")
        threshold_evaluation = report.get("gateway_threshold_evaluation")
        checks = threshold_evaluation.get("checks") if isinstance(threshold_evaluation, dict) else None
        expected_checks = {
            "gateway_livez_p95_us": 10_000.0,
            "gateway_added_overhead_p95_us": 5_000.0,
            "gateway_added_overhead_p99_us": 10_000.0,
        }
        if (
            not isinstance(threshold_evaluation, dict)
            or threshold_evaluation.get("passed") is not True
            or threshold_evaluation.get("aggregation")
            != "median_of_three_isolated_run_percentiles"
            or not isinstance(checks, dict)
            or set(checks) != set(expected_checks)
        ):
            raise CampaignSafetyError("matched performance gateway thresholds failed")
        for name, maximum in expected_checks.items():
            row = checks[name]
            observed = row.get("observed_us") if isinstance(row, dict) else None
            if (
                not isinstance(row, dict)
                or row.get("passed") is not True
                or row.get("maximum_us") != maximum
                or isinstance(observed, bool)
                or not isinstance(observed, (int, float))
                or not 0 <= observed <= maximum
            ):
                raise CampaignSafetyError(f"matched performance threshold is invalid: {name}")
        sequence = report.get("sequential_process_proof")
        children = sequence.get("children") if isinstance(sequence, dict) else None
        expected_order = [
            "v2_actual_livez_wsgi",
            "v3_native_gateway_livez_wsgi",
            "v3_native_gateway_livez_wsgi",
            "v2_actual_livez_wsgi",
            "v2_actual_livez_wsgi",
            "v3_native_gateway_livez_wsgi",
            "v2_actual_osc_cases_wsgi",
            "v3_native_osc_cases_wsgi",
            "v3_native_osc_cases_wsgi",
            "v2_actual_osc_cases_wsgi",
            "v2_actual_osc_cases_wsgi",
            "v3_native_osc_cases_wsgi",
        ]
        if (
            not isinstance(sequence, dict)
            or sequence.get("maximum_simultaneous_benchmark_children") != 1
            or sequence.get("blocking_subprocess_run_used") is not True
            or not isinstance(children, list)
            or len(children) != 12
            or any(not isinstance(row, dict) for row in children)
            or [row.get("mode") for row in children] != expected_order
            or [row.get("ordinal") for row in children] != list(range(12))
            or len({row.get("pid") for row in children}) != 12
            or len({row.get("parent_pid") for row in children}) != 1
        ):
            raise CampaignSafetyError("matched performance sequence proof is invalid")
        runs = report.get("runs")
        if (
            not isinstance(runs, dict)
            or set(runs) != {"v2_actual_livez_wsgi", "v3_native_gateway_livez_wsgi"}
            or any(not isinstance(rows, list) or len(rows) != 3 for rows in runs.values())
        ):
            raise CampaignSafetyError("matched performance isolated runs are invalid")
        required_run_safety = {
            "listener_started": False,
            "production_service_imported": False,
            "production_handler_module_imported": True,
            "network_connections_blocked": True,
            "live_state_accessed": False,
            "external_writes": False,
            "production_state_writes": False,
            "production_port_accessed": False,
            "nas_accessed": False,
            "launchctl_invoked": False,
        }
        for mode, rows in runs.items():
            if any(
                row.get("mode") != mode
                or row.get("workload") != "native_gateway_livez"
                or row.get("correctness_passed") is not True
                or not isinstance(row.get("runtime"), dict)
                or not SHA256_RE.fullmatch(
                    str(row["runtime"].get("executable_sha256") or "")
                )
                or not SHA256_RE.fullmatch(str(row.get("request_plan_sha256") or ""))
                or not isinstance(row.get("latency"), dict)
                or any(
                    isinstance(row["latency"].get(metric), bool)
                    or not isinstance(row["latency"].get(metric), (int, float))
                    or row["latency"][metric] <= 0
                    for metric in ("p50", "p95", "p99")
                )
                or not isinstance(row.get("memory"), dict)
                or any(
                    type(row["memory"].get(metric)) is not int
                    for metric in ("rss_before_bytes", "rss_after_bytes", "rss_growth_bytes")
                )
                or not isinstance(row.get("file_descriptors"), dict)
                or type(row["file_descriptors"].get("before")) is not int
                or type(row["file_descriptors"].get("after")) is not int
                or row["file_descriptors"].get("drift") != 0
                or not isinstance(row.get("safety"), dict)
                or any(row["safety"].get(key) != value for key, value in required_run_safety.items())
                for row in rows
            ):
                raise CampaignSafetyError(f"matched performance run safety failed: {mode}")
        runtime_hashes = {
            row["runtime"]["executable_sha256"] for rows in runs.values() for row in rows
        }
        request_plan_hashes = {
            row["request_plan_sha256"] for rows in runs.values() for row in rows
        }
        if len(runtime_hashes) != 1 or len(request_plan_hashes) != 1:
            raise CampaignSafetyError("matched performance runtime or request-plan binding drifted")
        gate = report.get("gate")
        if (
            not isinstance(gate, dict)
            or gate.get("blocker_code") != "MATCHED_PRODUCTION_PERFORMANCE_NOT_IMPLEMENTED"
            or gate.get("eligible_to_clear_full_v2_v3_performance_blocker") is not False
            or gate.get("decision") != "blocker_retained"
            or gate.get("thresholds_applied") is not True
            or gate.get("threshold_scope") != "gateway_livez_native_v2_v3_only"
        ):
            raise CampaignSafetyError("matched production performance blocker must remain explicit")
        corpus = report.get("representative_business_corpus")
        gap = corpus.get("architecture_gap") if isinstance(corpus, dict) else None
        if (
            not isinstance(corpus, dict)
            or corpus.get("synthetic_only") is not True
            or corpus.get("production_state_accessed") is not False
            or corpus.get("request_count") != 2
            or corpus.get("matched_measurement_status")
            != "matched_synthetic_get_and_post_measured"
            or corpus.get("measured_methods") != ["GET", "POST"]
            or corpus.get("unmeasured_methods") != []
            or not isinstance(corpus.get("v3_native_handler"), dict)
            or corpus["v3_native_handler"].get("composed_in_service_manifest") is not False
            or not SHA256_RE.fullmatch(
                str(corpus["v3_native_handler"].get("source_sha256") or "")
            )
            or not isinstance(corpus.get("database_corpus"), dict)
            or corpus["database_corpus"].get("backend") != "sqlite_memory"
            or corpus["database_corpus"].get("row_count") != 32
            or corpus["database_corpus"].get("production_state_accessed") is not False
            or not SHA256_RE.fullmatch(str(corpus["database_corpus"].get("sha256") or ""))
            or not isinstance(gap, dict)
            or gap.get("code") != "NATIVE_OSC_CASES_NOT_COMPOSED_IN_SERVICE_MANIFEST"
            or gap.get("factory_kind") != "v2_compatibility"
            or gap.get("gateway_application_factories")
            != {
                "main_http": "magi_v3.compat:create_main_app",
                "tools_http": "magi_v3.compat:create_tools_app",
            }
            or not SHA256_RE.fullmatch(str(corpus.get("request_plan_sha256") or ""))
            or not SHA256_RE.fullmatch(str(gap.get("manifest_sha256") or ""))
        ):
            raise CampaignSafetyError("matched performance business architecture gap is invalid")
        business = report.get("synthetic_business_benchmark")
        business_runs = business.get("runs") if isinstance(business, dict) else None
        business_sequence = (
            business.get("sequential_process_proof") if isinstance(business, dict) else None
        )
        business_children = (
            business_sequence.get("children")
            if isinstance(business_sequence, dict)
            else None
        )
        business_modes = {"v2_actual_osc_cases_wsgi", "v3_native_osc_cases_wsgi"}
        expected_business_order = expected_order[6:]
        business_handlers = (
            business.get("handler_identities") if isinstance(business, dict) else None
        )
        business_comparison = (
            business.get("comparison") if isinstance(business, dict) else None
        )
        isolation_contract = (
            business.get("isolation_contract") if isinstance(business, dict) else None
        )
        if (
            not isinstance(business, dict)
            or business.get("schema_version") != 1
            or business.get("workload") != "synthetic_osc_cases"
            or business.get("synthetic_only") is not True
            or business.get("production_business_workload") is not False
            or business.get("release_thresholds_applied") is not False
            or business.get("parameters")
            != {"warmup": 100, "iterations": 1000, "repeats": 3}
            or business.get("measured_methods") != ["GET", "POST"]
            or business.get("unmeasured_methods") != []
            or business.get("same_python_runtime") is not True
            or business.get("same_request_plan") is not True
            or business.get("same_route_identity") is not True
            or business.get("response_projection_equivalent") is not True
            or not isinstance(isolation_contract, dict)
            or isolation_contract.get("actual_v2_blueprint_view_executed") is not True
            or isolation_contract.get("actual_native_wsgi_and_service_executed") is not True
            or isolation_contract.get("v2_database_override")
            != "_osc_exec_bounded_disposable_in_memory_sqlite"
            or isolation_contract.get("v2_manual_schema_guard") != "pre_satisfied_no_ddl"
            or isolation_contract.get("v2_settings_lookup")
            != "forbidden_rows_have_explicit_lawyer"
            or isolation_contract.get("network") != "socket_connect_blocked"
            or not isinstance(business_sequence, dict)
            or business_sequence.get("maximum_simultaneous_benchmark_children") != 1
            or business_sequence.get("blocking_subprocess_run_used") is not True
            or not isinstance(business_children, list)
            or [row.get("mode") for row in business_children] != expected_business_order
            or [row.get("ordinal") for row in business_children] != list(range(6, 12))
            or len({row.get("pid") for row in business_children}) != 6
            or len({row.get("parent_pid") for row in business_children}) != 1
            or not isinstance(business_runs, dict)
            or set(business_runs) != business_modes
            or any(not isinstance(rows, list) or len(rows) != 3 for rows in business_runs.values())
            or not isinstance(business_handlers, dict)
            or set(business_handlers) != business_modes
            or business_handlers["v2_actual_osc_cases_wsgi"].get("implementation")
            != "production_v2"
            or business_handlers["v3_native_osc_cases_wsgi"].get("implementation")
            != "native_v3"
            or any(
                not SHA256_RE.fullmatch(str(row.get("source_sha256") or ""))
                for row in business_handlers.values()
                if isinstance(row, dict)
            )
            or not isinstance(business_comparison, dict)
            or any(
                isinstance(business_comparison.get(metric), bool)
                or not isinstance(business_comparison.get(metric), (int, float))
                or business_comparison[metric] <= 0
                for metric in (
                    "latency_p50_v2_us",
                    "latency_p50_native_v3_us",
                    "latency_p95_v2_us",
                    "latency_p95_native_v3_us",
                    "latency_p99_v2_us",
                    "latency_p99_native_v3_us",
                    "cold_start_v2_us",
                    "cold_start_native_v3_us",
                )
            )
            or business_comparison.get("fd_drift_v2") != 0
            or business_comparison.get("fd_drift_native_v3") != 0
            or not isinstance(business_comparison.get("rss_growth_v2_bytes"), (int, float))
            or not isinstance(
                business_comparison.get("rss_growth_native_v3_bytes"), (int, float)
            )
        ):
            raise CampaignSafetyError("matched synthetic OSC business evidence is invalid")
        business_runtime_hashes: set[str] = set()
        business_transcripts: dict[str, dict[str, Any]] = {}
        for mode, rows in business_runs.items():
            expected_handler_import = mode == "v2_actual_osc_cases_wsgi"
            for row in rows:
                safety = row.get("safety")
                corpus_binding = row.get("synthetic_corpus")
                if (
                    row.get("mode") != mode
                    or row.get("workload") != "synthetic_osc_cases"
                    or row.get("correctness_passed") is not True
                    or row.get("response_sequence_sha256")
                    != row.get("expected_response_sequence_sha256")
                    or not isinstance(row.get("cold_start"), dict)
                    or not isinstance(row["cold_start"].get("latency_us"), (int, float))
                    or row["cold_start"]["latency_us"] <= 0
                    or not isinstance(row.get("latency"), dict)
                    or any(
                        not isinstance(row["latency"].get(metric), (int, float))
                        or isinstance(row["latency"].get(metric), bool)
                        or row["latency"][metric] <= 0
                        for metric in ("p50", "p95", "p99")
                    )
                    or not isinstance(row.get("file_descriptors"), dict)
                    or row["file_descriptors"].get("drift") != 0
                    or not isinstance(row.get("memory"), dict)
                    or any(
                        type(row["memory"].get(metric)) is not int
                        for metric in (
                            "rss_before_bytes",
                            "rss_after_bytes",
                            "rss_growth_bytes",
                        )
                    )
                    or not isinstance(safety, dict)
                    or safety.get("production_handler_module_imported")
                    is not expected_handler_import
                    or any(
                        safety.get(key) is not False
                        for key in (
                            "listener_started",
                            "production_service_imported",
                            "live_state_accessed",
                            "external_writes",
                            "production_state_writes",
                            "production_port_accessed",
                            "nas_accessed",
                            "launchctl_invoked",
                        )
                    )
                    or safety.get("network_connections_blocked") is not True
                    or not isinstance(corpus_binding, dict)
                    or corpus_binding.get("database") != "sqlite_memory_disposable"
                    or corpus_binding.get("row_count") != 32
                    or corpus_binding.get("read_only") is not False
                    or corpus_binding.get("disposable") is not True
                    or corpus_binding.get("measured_methods") != ["GET", "POST"]
                    or corpus_binding.get("unmeasured_methods") != []
                    or corpus_binding.get("opposite_handler_module_imported") is not False
                    or not SHA256_RE.fullmatch(
                        str(corpus_binding.get("corpus_sha256") or "")
                    )
                ):
                    raise CampaignSafetyError(
                        f"matched synthetic OSC run safety failed: {mode}"
                    )
                transcript = corpus_binding.get("side_effect_transcript")
                expected_event_counts = {
                    "begin": 550,
                    "insert": 0,
                    "update": 550,
                    "commit": 550,
                    "rollback": 0,
                }
                if (
                    not isinstance(transcript, dict)
                    or transcript.get("database") != "sqlite_memory_disposable"
                    or transcript.get("post_transaction_count") != 550
                    or transcript.get("transaction_event_counts") != expected_event_counts
                    or transcript.get("balanced_transactions") is not True
                    or transcript.get("external_writes") is not False
                    or transcript.get("production_state_accessed") is not False
                    or transcript.get("nas_accessed") is not False
                    or not isinstance(transcript.get("target_state"), dict)
                    or transcript["target_state"].get("id") != "synthetic-case-000"
                    or transcript["target_state"].get("case_number") != "2026-0001"
                    or transcript["target_state"].get("notes")
                    != "production-shaped-post-fixture"
                    or not SHA256_RE.fullmatch(
                        str(transcript.get("target_state_sha256") or "")
                    )
                    or not SHA256_RE.fullmatch(
                        str(transcript.get("post_transaction_transcript_sha256") or "")
                    )
                ):
                    raise CampaignSafetyError(
                        f"matched synthetic OSC POST transcript failed: {mode}"
                    )
                if mode in business_transcripts and transcript != business_transcripts[mode]:
                    raise CampaignSafetyError(
                        f"matched synthetic OSC POST transcript drifted: {mode}"
                    )
                business_transcripts[mode] = transcript
                business_runtime_hashes.add(row["runtime"]["executable_sha256"])
        if len(business_runtime_hashes) != 1:
            raise CampaignSafetyError("matched synthetic OSC runtime binding drifted")
        summary_transcripts = business.get("side_effect_transcript")
        if (
            not isinstance(summary_transcripts, dict)
            or set(summary_transcripts) != business_modes
            or summary_transcripts != business_transcripts
            or len(
                {
                    json.dumps(row, ensure_ascii=False, sort_keys=True)
                    for row in summary_transcripts.values()
                }
            )
            != 1
        ):
            raise CampaignSafetyError("matched synthetic OSC POST side effects differ")
        supplied_hash = report.get("evidence_sha256")
        unhashed_report = dict(report)
        unhashed_report.pop("evidence_sha256", None)
        observed_hash = hashlib.sha256(
            json.dumps(
                unhashed_report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if not isinstance(supplied_hash, str) or supplied_hash != observed_hash:
            raise CampaignSafetyError("matched performance evidence hash is invalid")
        if evidence.get("live_state_access_performed") is not False:
            raise CampaignSafetyError("matched performance live-state safety attestation failed")
        return evidence
    if workload == "seven_day_schedule_10x_arrival_2x_duration_replay":
        if isinstance(evidence.get("report"), dict):
            _validate_schedule_capacity_campaign_evidence(
                evidence,
                expected_profile=expected_profile,
                expected_release=expected_release,
            )
            return evidence
        if evidence.get("probe") != "measured_schedule_ledger_replay":
            raise CampaignSafetyError("schedule replay evidence probe is invalid")
        exact = {
            "arrival_multiplier": 10,
            "duration_multiplier": 2.0,
            "duration_basis": "measured_ledger_lifecycle_p95",
            "virtual_duration_seconds": 604800,
            "governor_light_slots": 2,
            "governor_heavy_slots": 1,
            "duplicate_jobs": 0,
            "lost_jobs": 0,
            "latest_start_misses": 0,
            "deadline_misses": 0,
            "journal_mode_wal": True,
            "integrity_check_ok": True,
            "reopen_ping_ok": True,
        }
        if any(measurements.get(key) != value for key, value in exact.items()):
            raise CampaignSafetyError("schedule replay thresholds failed")
        cron_definition_count = measurements.get("cron_definitions")
        enabled_cron_count = measurements.get("enabled_cron_definitions")
        if (
            type(cron_definition_count) is not int
            or type(enabled_cron_count) is not int
            or cron_definition_count <= 0
            or enabled_cron_count <= 0
            or enabled_cron_count > cron_definition_count
        ):
            raise CampaignSafetyError("schedule replay cron-definition totals are invalid")
        if expected_profile is not None and (
            measurements.get("validation_profile_id") != expected_profile.get("profile_id")
            or measurements.get("replay_start_local")
            != expected_profile.get("replay_start_local")
        ):
            raise CampaignSafetyError("schedule replay validation profile binding failed")
        integer_fields = (
            "base_seven_day_arrivals",
            "replayed_arrivals",
            "persisted_jobs",
            "recovered_jobs",
        )
        if any(type(measurements.get(key)) is not int for key in integer_fields):
            raise CampaignSafetyError("schedule replay counts are invalid")
        base = measurements["base_seven_day_arrivals"]
        replayed = measurements["replayed_arrivals"]
        if base <= 0 or replayed != base * 10:
            raise CampaignSafetyError("schedule replay arrival amplification is invalid")
        if measurements["persisted_jobs"] != replayed or measurements["recovered_jobs"] != replayed:
            raise CampaignSafetyError("schedule replay persistence/recovery counts failed")
        numeric_positive = (
            "wall_duration_seconds",
            "acceleration_factor",
            "calibrated_light_p95_ms",
            "calibrated_maintenance_p95_ms",
        )
        for field in numeric_positive:
            value = measurements.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise CampaignSafetyError(f"schedule replay measurement is invalid: {field}")
        queue_delay = measurements.get("max_queue_delay_ms")
        if isinstance(queue_delay, bool) or not isinstance(queue_delay, (int, float)) or queue_delay < 0:
            raise CampaignSafetyError("schedule replay queue delay is invalid")
        if not SHA256_RE.fullmatch(str(measurements.get("cron_jobs_sha256") or "")):
            raise CampaignSafetyError("schedule replay cron binding is invalid")
        if evidence.get("live_state_access_performed") is not False:
            raise CampaignSafetyError("schedule replay live-state safety attestation failed")
        realism = measurements.get("realism_audit")
        if (
            not isinstance(realism, dict)
            or realism.get("schema_version") != 1
            or realism.get("workload") != "production_duration_and_representative_job_body_sandbox"
            or realism.get("status") != "incomplete"
            or realism.get("completion_claimed") is not False
        ):
            raise CampaignSafetyError("schedule realism evidence identity is invalid")
        realism_measurements = realism.get("measurements")
        if not isinstance(realism_measurements, dict):
            raise CampaignSafetyError("schedule realism coverage thresholds failed")
        allowlisted_count = realism_measurements.get("representative_bodies_allowlisted")
        passed_body_count = realism_measurements.get("representative_bodies_passed")
        body_gap_count = realism_measurements.get("representative_body_gap_jobs")
        if (
            realism_measurements.get("cron_definitions") != cron_definition_count
            or realism_measurements.get("enabled_cron_definitions") != enabled_cron_count
            or type(allowlisted_count) is not int
            or type(passed_body_count) is not int
            or type(body_gap_count) is not int
            or allowlisted_count <= 0
            or passed_body_count != allowlisted_count
            or body_gap_count <= 0
            or allowlisted_count + body_gap_count != enabled_cron_count
            or realism_measurements.get("all_allowlisted_bodies_passed") is not True
        ):
            raise CampaignSafetyError("schedule realism dynamic coverage totals failed")
        if type(realism_measurements.get("production_duration_percentile_available")) is not bool:
            raise CampaignSafetyError("schedule realism percentile availability is invalid")
        observed_jobs = realism_measurements.get("production_duration_observations")
        duration_gap_jobs = realism_measurements.get("production_duration_gap_jobs")
        if (
            type(observed_jobs) is not int
            or type(duration_gap_jobs) is not int
            or observed_jobs < 0
            or duration_gap_jobs < 0
            or observed_jobs + duration_gap_jobs != enabled_cron_count
        ):
            raise CampaignSafetyError("schedule realism duration coverage regressed")
        if realism_measurements.get("cron_jobs_sha256") != measurements.get("cron_jobs_sha256"):
            raise CampaignSafetyError("schedule realism cron binding differs from replay")
        for field in (
            "network_access_performed",
            "production_database_access_performed",
            "nas_access_performed",
            "live_service_access_performed",
            "production_state_write_performed",
        ):
            if realism.get(field) is not False:
                raise CampaignSafetyError(f"schedule realism safety attestation failed: {field}")
        if realism.get("sandbox_writes_only") is not True:
            raise CampaignSafetyError("schedule realism sandbox-write attestation failed")
        body_results = realism.get("body_results")
        body_result_ids = {
            str(row.get("job_id") or "")
            for row in body_results
            if isinstance(row, dict)
        } if isinstance(body_results, list) else set()
        if (
            not isinstance(body_results, list)
            or len(body_results) != allowlisted_count
            or any(not isinstance(row, dict) for row in body_results)
            or "" in body_result_ids
            or len(body_result_ids) != allowlisted_count
            or any(
                row.get("status") != "passed"
                or row.get("executed") is not True
                or row.get("semantic_success") is not True
                or row.get("adapter_mode") != "real_entrypoint_dry_run_v1"
                or row.get("adapter_dry_run") is not True
                or row.get("network_denied_by_seatbelt") is not True
                or row.get("notifications_disabled") is not True
                or not SHA256_RE.fullmatch(str(row.get("sandbox_profile_sha256") or ""))
                or not SHA256_RE.fullmatch(
                    str(row.get("adapter_fixture_manifest_sha256") or "")
                )
                for row in body_results
            )
        ):
            raise CampaignSafetyError("schedule realism adapter evidence is invalid")
        gaps = realism.get("gaps")
        representative_gap_ids = {
            str(row.get("job_id") or "")
            for row in gaps
            if isinstance(row, dict)
            and row.get("gap_type") == "representative_job_body"
        } if isinstance(gaps, list) else set()
        if (
            not isinstance(gaps, list)
            or any(not isinstance(row, dict) for row in gaps)
            or "" in representative_gap_ids
            or len(representative_gap_ids) != body_gap_count
            or body_result_ids & representative_gap_ids
            or len(body_result_ids | representative_gap_ids) != enabled_cron_count
        ):
            raise CampaignSafetyError("schedule realism gap evidence is invalid")
        return evidence
    if workload == "fault_injection":
        if evidence.get("probe") != "bounded_offline_fault_matrix":
            raise CampaignSafetyError("fault campaign evidence probe is invalid")
        exact = {
            "faults_requested": 6,
            "faults_completed": 6,
            "faults_passed": 6,
            "duplicate_total": 0,
            "loss_total": 0,
        }
        if any(measurements.get(key) != value for key, value in exact.items()):
            raise CampaignSafetyError("fault campaign aggregate thresholds failed")
        matrix = measurements.get("matrix")
        expected_faults = {
            "sqlite_wal_concurrent_reopen",
            "sqlite_bounded_disk_full",
            "atomic_fsync_failure",
            "worker_crash",
            "worker_timeout",
            "notification_storm_dlq",
        }
        if (
            not isinstance(matrix, list)
            or len(matrix) != 6
            or any(not isinstance(row, dict) for row in matrix)
            or {row.get("fault") for row in matrix} != expected_faults
        ):
            raise CampaignSafetyError("fault campaign matrix identity is invalid")
        for row in matrix:
            if row.get("status") != "passed" or row.get("duplicate") != 0 or row.get("loss") != 0:
                raise CampaignSafetyError(f"fault campaign row failed: {row.get('fault')}")
            recovery = row.get("recovery_ms")
            if isinstance(recovery, bool) or not isinstance(recovery, (int, float)) or recovery < 0:
                raise CampaignSafetyError("fault campaign recovery measurement is invalid")
        realism = measurements.get("realism_audit")
        if (
            not isinstance(realism, dict)
            or realism.get("schema_version") != 1
            or realism.get("workload") != "fault_injection_realism_audit"
            or realism.get("probe") != "owned_sqlite_wal_sigkill_commit_window_sweep"
            or realism.get("status") != "passed_partial_evidence"
        ):
            raise CampaignSafetyError("fault campaign realism evidence identity is invalid")
        realism_measurements = realism.get("measurements")
        realism_exact = {
            "cycles_requested": 12,
            "cycles_completed": 12,
            "acknowledged_commits_lost": 0,
            "partially_visible_transactions": 0,
            "final_job_rows": 12,
            "final_unique_jobs": 12,
            "final_payload_rows": 384,
            "duplicate_jobs": 0,
            "lost_jobs_after_recovery": 0,
            "integrity_check": "ok",
            "journal_mode": "wal",
            "synchronous": "FULL",
        }
        if not isinstance(realism_measurements, dict) or any(
            realism_measurements.get(key) != value for key, value in realism_exact.items()
        ):
            raise CampaignSafetyError("fault campaign realism thresholds failed")
        instruction = realism_measurements.get("transaction_instruction_boundary_sweep")
        expected_stages = [
            "READY",
            "BEGIN",
            "JOB_INSERT",
            *(f"PAYLOAD_{index:02d}" for index in range(32)),
            "COMMIT_STARTED",
            "COMMIT_ACK",
        ]
        instruction_exact = {
            "stages_requested": 37,
            "stages_completed": 37,
            "stage_markers": expected_stages,
            "acknowledged_commits_lost": 0,
            "partially_visible_transactions": 0,
            "final_job_rows": 37,
            "final_unique_jobs": 37,
            "final_payload_rows": 1184,
            "duplicate_jobs": 0,
            "lost_jobs_after_recovery": 0,
            "integrity_check": "ok",
        }
        if not isinstance(instruction, dict) or any(
            instruction.get(key) != value for key, value in instruction_exact.items()
        ):
            raise CampaignSafetyError("fault instruction-boundary sweep thresholds failed")
        instruction_cycles = instruction.get("cycles")
        if (
            not isinstance(instruction_cycles, list)
            or len(instruction_cycles) != 37
            or any(not isinstance(row, dict) for row in instruction_cycles)
            or [row.get("target_stage") for row in instruction_cycles] != expected_stages
            or any(
                row.get("signal") != "SIGKILL"
                or row.get("final_job_rows") != 1
                or row.get("final_payload_rows") != 32
                or row.get("integrity_check") != "ok"
                for row in instruction_cycles
            )
        ):
            raise CampaignSafetyError("fault instruction-boundary cycle evidence failed")
        time_offsets = realism_measurements.get("bounded_time_offset_sigkill_sweep")
        expected_offsets = [0, 50, 250, 1_000, 5_000, 20_000]
        time_offset_exact = {
            "offsets_requested": 6,
            "offsets_completed": 6,
            "scheduled_offsets_us": expected_offsets,
            "acknowledged_commits_lost": 0,
            "partially_visible_transactions": 0,
            "final_job_rows": 6,
            "final_unique_jobs": 6,
            "final_payload_rows": 192,
            "duplicate_jobs": 0,
            "lost_jobs_after_recovery": 0,
            "integrity_check": "ok",
        }
        if not isinstance(time_offsets, dict) or any(
            time_offsets.get(key) != value for key, value in time_offset_exact.items()
        ):
            raise CampaignSafetyError("fault bounded time-offset SIGKILL thresholds failed")
        time_offset_cycles = time_offsets.get("cycles")
        if (
            not isinstance(time_offset_cycles, list)
            or len(time_offset_cycles) != 6
            or any(not isinstance(row, dict) for row in time_offset_cycles)
            or [row.get("scheduled_kill_offset_us") for row in time_offset_cycles]
            != expected_offsets
            or any(
                row.get("signal") != "SIGKILL"
                or row.get("final_job_rows") != 1
                or row.get("final_payload_rows") != 32
                or row.get("integrity_check") != "ok"
                for row in time_offset_cycles
            )
        ):
            raise CampaignSafetyError("fault bounded time-offset cycle evidence failed")
        vfs_fsync = realism_measurements.get("sqlite_vfs_fsync_io_error")
        if (
            not isinstance(vfs_fsync, dict)
            or vfs_fsync.get("status") != "passed"
            or vfs_fsync.get("injection_boundary") != "custom SQLite VFS xSync"
            or vfs_fsync.get("injected_error") != "SQLITE_IOERR_FSYNC"
            or vfs_fsync.get("injected_file_role") != "wal"
            or vfs_fsync.get("commit_rc") != 1034
            or vfs_fsync.get("extended_rc") != 1034
            or vfs_fsync.get("expected_extended_rc") != 1034
            or vfs_fsync.get("injected") != 1
            or isinstance(vfs_fsync.get("sync_calls_after_arm"), bool)
            or not isinstance(vfs_fsync.get("sync_calls_after_arm"), int)
            or vfs_fsync["sync_calls_after_arm"] < 1
            or vfs_fsync.get("baseline_rows") != 1
            or vfs_fsync.get("partial_rows") != 0
            or vfs_fsync.get("recovery_rc") != 0
            or vfs_fsync.get("final_rows") != 2
            or vfs_fsync.get("integrity_ok") != 1
            or vfs_fsync.get("journal_mode") != "wal"
            or vfs_fsync.get("synchronous") != "FULL"
            or vfs_fsync.get("power_loss_simulated") is not False
            or not SHA256_RE.fullmatch(str(vfs_fsync.get("source_sha256") or ""))
            or not SHA256_RE.fullmatch(str(vfs_fsync.get("executable_sha256") or ""))
        ):
            raise CampaignSafetyError("fault custom SQLite VFS fsync evidence failed")
        machine_offsets = realism_measurements.get("machine_instruction_offset_sigkill")
        if (
            not isinstance(machine_offsets, dict)
            or machine_offsets.get("status") != "blocked"
            or machine_offsets.get("logical_transaction_boundary_sweep_substituted") is not False
        ):
            raise CampaignSafetyError("fault instruction-offset blocker evidence is invalid")
        apfs = realism_measurements.get("apfs_sparse_image")
        if (
            not isinstance(apfs, dict)
            or apfs.get("status") != "passed"
            or apfs.get("filesystem") != "apfs"
            or apfs.get("image_type") != "sparsebundle"
            or apfs.get("image_capacity_bytes") != 33_554_432
            or apfs.get("recovery_reserve_bytes") != 4_194_304
            or apfs.get("filesystem_enospc_observed") is not True
            or apfs.get("filesystem_enospc_operation") not in {"write", "fsync"}
            or apfs.get("sqlite_full_observed") is not True
            or apfs.get("sqlite_error_code") != 13
            or apfs.get("sqlite_error_name") != "SQLITE_FULL"
            or apfs.get("committed_rows_preserved") != 1
            or apfs.get("partial_rows_visible") != 0
            or apfs.get("final_jobs") != 2
            or apfs.get("integrity_check") != "ok"
        ):
            raise CampaignSafetyError("fault campaign APFS sandbox evidence failed")
        filler_bytes = apfs.get("filler_bytes_before_enospc")
        if (
            isinstance(filler_bytes, bool)
            or not isinstance(filler_bytes, int)
            or not 0 < filler_bytes < apfs["image_capacity_bytes"]
        ):
            raise CampaignSafetyError("fault campaign APFS fill measurement is invalid")
        expected_coverage = {
            "owned_process_sigkill_at_commit_boundary": True,
            "owned_process_sigkill_at_bounded_time_offsets": True,
            "sqlite_wal_full_synchronous_sigkill": True,
            "sqlite_wal_reopen_and_integrity_check": True,
            "idempotent_recovery_from_known_input_plan": True,
            "all_logical_transaction_boundaries_sigkill": True,
            "sandbox_apfs_sparse_image_enospc": True,
            "physical_apfs_enospc": False,
            "physical_power_interruption": False,
            "custom_sqlite_vfs_power_loss": False,
            "sqlite_vfs_fsync_io_error_injection": True,
            "arbitrary_instruction_offset_sigkill": False,
        }
        if realism.get("coverage") != expected_coverage:
            raise CampaignSafetyError("fault campaign realism coverage attestation failed")
        blocker = realism.get("blocker")
        if (
            not isinstance(blocker, dict)
            or blocker.get("code") != "FAULT_CAMPAIGN_REALISM_INCOMPLETE"
            or blocker.get("eligible_to_clear") is not False
            or blocker.get("decision") != "blocker_retained"
        ):
            raise CampaignSafetyError("fault campaign realism blocker must remain explicit")
        realism_safety = realism.get("safety")
        required_safety = {
            "live_magi_state_accessed": False,
            "production_service_imported": False,
            "listener_started": False,
            "network_api_invoked": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "signals_sent_only_to_owned_children": True,
            "owned_custom_vfs_compiled_and_executed": True,
            "compiler_network_access": False,
            "owned_disk_image_attach_performed": True,
            "owned_disk_image_detached_and_removed": True,
        }
        if not isinstance(realism_safety, dict) or any(
            realism_safety.get(key) != value for key, value in required_safety.items()
        ):
            raise CampaignSafetyError("fault campaign realism safety attestation failed")
        supplied_hash = realism.get("evidence_sha256")
        unhashed_realism = dict(realism)
        unhashed_realism.pop("evidence_sha256", None)
        observed_hash = hashlib.sha256(
            json.dumps(
                unhashed_realism,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if not isinstance(supplied_hash, str) or supplied_hash != observed_hash:
            raise CampaignSafetyError("fault campaign realism evidence hash is invalid")
        if evidence.get("live_state_access_performed") is not False:
            raise CampaignSafetyError("fault campaign live-state safety attestation failed")
        return evidence
    if workload == "ime_candidate_window_pressure_probe":
        if evidence.get("probe") != "native_mcbopomofo_candidate_window_pressure":
            raise CampaignSafetyError("IME pressure evidence probe is invalid")
        exact = {
            "cycles_requested": 3,
            "cycles_completed": 3,
            "candidate_windows_detected": 3,
            "candidate_window_failures": 0,
            "pressure_allocated_mb": 256,
            "text_services_healthy": True,
        }
        if any(measurements.get(key) != value for key, value in exact.items()):
            raise CampaignSafetyError("IME pressure completion thresholds failed")
        for field in ("candidate_latency_p95_ms", "candidate_latency_max_ms"):
            value = measurements.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 2000:
                raise CampaignSafetyError(f"IME pressure latency is invalid: {field}")
        if not SHA256_RE.fullmatch(str(measurements.get("input_source_id_sha256") or "")):
            raise CampaignSafetyError("IME pressure source binding is invalid")
        if (
            evidence.get("external_write_performed") is not False
            or evidence.get("live_magi_state_access_performed") is not False
            or evidence.get("temporary_native_ui_performed") is not True
            or evidence.get("unsaved_document_cleanup_performed") is not True
            or evidence.get("unsaved_documents_remaining") != 0
        ):
            raise CampaignSafetyError("IME pressure cleanup/safety attestation failed")
        return evidence
    if evidence.get("probe") != "owned_process_group_reap_soak":
        raise CampaignSafetyError("hundred-cycle soak evidence probe is invalid")
    exact = {
        "cycles_requested": 100,
        "cycles_completed": 100,
        "process_groups_gone": 100,
        "active_workers_after": 0,
        "governor_slots_after": 0,
    }
    if any(measurements.get(key) != value for key, value in exact.items()):
        raise CampaignSafetyError("hundred-cycle soak completion/reap measurements failed")
    integer_fields = ("fd_count_before", "fd_count_after", "fd_peak", "fd_drift")
    if any(type(measurements.get(key)) is not int for key in integer_fields):
        raise CampaignSafetyError("hundred-cycle soak FD measurements are invalid")
    if measurements["fd_count_before"] < 0 or measurements["fd_count_after"] < 0:
        raise CampaignSafetyError("hundred-cycle soak FD counts are invalid")
    if measurements["fd_peak"] < max(
        measurements["fd_count_before"], measurements["fd_count_after"]
    ):
        raise CampaignSafetyError("hundred-cycle soak peak FD measurement is inconsistent")
    if measurements["fd_drift"] != (
        measurements["fd_count_after"] - measurements["fd_count_before"]
    ) or measurements["fd_drift"] > 2:
        raise CampaignSafetyError("hundred-cycle soak detected unacceptable FD drift")
    duration = measurements.get("total_worker_duration_sec")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
        raise CampaignSafetyError("hundred-cycle soak duration measurement is invalid")
    return evidence


def _validate_schedule_capacity_campaign_evidence(
    evidence: dict[str, Any],
    *,
    expected_profile: dict[str, Any] | None,
    expected_release: ReleaseBundle | None,
) -> None:
    """Recompute the schedule/body claims from the two raw hashed reports."""

    from scripts.v3_validation.schedule_capacity_certification import (
        ScheduleCapacityError,
        verify_schedule_capacity_evidence,
    )

    report = evidence.get("report")
    body = evidence.get("body_evidence")
    if not isinstance(report, dict) or not isinstance(body, dict):
        raise CampaignSafetyError("schedule capacity/body raw evidence is missing")
    try:
        verify_schedule_capacity_evidence(report)
    except ScheduleCapacityError as exc:
        raise CampaignSafetyError(f"schedule capacity report failed verification: {exc}") from exc
    unsigned_body = dict(body)
    supplied_body_sha = unsigned_body.pop("evidence_sha256", None)
    observed_body_sha = hashlib.sha256(
        json.dumps(
            unsigned_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if supplied_body_sha != observed_body_sha:
        raise CampaignSafetyError("schedule body evidence hash is invalid")

    profile_id = str(report.get("validation_profile_id") or "")
    if not profile_id or (
        expected_profile is not None
        and profile_id != str(expected_profile.get("profile_id") or "")
    ):
        raise CampaignSafetyError("schedule validation profile binding failed")
    gate = report.get("gate")
    if (
        report.get("status") != "certified"
        or not isinstance(gate, dict)
        or gate.get("eligible_to_clear_schedule_realism_blocker") is not True
        or gate.get("decision") != "clear"
        or gate.get("blocking_reasons") != []
    ):
        raise CampaignSafetyError("schedule capacity report is not certifying")

    body_measurements = body.get("measurements")
    entries = body.get("registry_entries")
    results = body.get("body_results")
    expected_enabled_jobs = (
        body_measurements.get("enabled_jobs")
        if isinstance(body_measurements, dict)
        else None
    )
    if (
        body.get("status") != "passed"
        or body.get("completion_claimed") is not True
        or not isinstance(body_measurements, dict)
        or type(expected_enabled_jobs) is not int
        or expected_enabled_jobs <= 0
        or body_measurements.get("safe_adapter_coverage_jobs") != expected_enabled_jobs
        or body_measurements.get("blocked_jobs") != 0
        or body_measurements.get("body_jobs_passed") != expected_enabled_jobs
        or body_measurements.get("all_safe_bodies_passed") is not True
        or body_measurements.get("registry_complete_for_enabled_jobs") is not True
        or not isinstance(entries, list)
        or len(entries) != expected_enabled_jobs
        or any(
            not isinstance(row, dict)
            or row.get("classification") != "safe_adapter"
            or row.get("blockers") != []
            for row in entries
        )
        or not isinstance(results, list)
        or len(results) != expected_enabled_jobs
        or len({str(row.get("job_id") or "") for row in results if isinstance(row, dict)})
        != expected_enabled_jobs
        or any(
            not isinstance(row, dict)
            or row.get("status") != "passed"
            or row.get("semantic_success") is not True
            or row.get("successful_samples") != 3
            or row.get("duration_sample_count") != 3
            or row.get("network_denied_by_seatbelt") is not True
            or row.get("notifications_disabled") is not True
            for row in results
        )
        or body.get("sandbox_escape_probes", {}).get("status") != "passed"
    ):
        raise CampaignSafetyError("schedule real-body coverage is incomplete or inconsistent")

    layers = report.get("layers")
    control = layers.get("control_plane") if isinstance(layers, dict) else None
    business = layers.get("business_body_plane") if isinstance(layers, dict) else None
    capacity = control.get("measurements") if isinstance(control, dict) else None
    duration = business.get("duration_evidence") if isinstance(business, dict) else None
    body_summary = business.get("body_evidence") if isinstance(business, dict) else None
    deadlines = business.get("deadline_measurements") if isinstance(business, dict) else None
    if (
        not isinstance(control, dict)
        or control.get("status") != "passed"
        or not isinstance(capacity, dict)
        or capacity.get("delivery_multiplier") != 10
        or capacity.get("duration_multiplier") != 2.0
        or capacity.get("all_deliveries_accounted") is not True
        or capacity.get("all_distinct_occurrences_accounted") is not True
        or capacity.get("same_job_concurrency_violations") != 0
        or capacity.get("loss_sensitive_coalesced_occurrences") != 0
        or capacity.get("coalescing_safety_passed") is not True
        or not isinstance(capacity.get("coalescing_policy"), dict)
        or capacity.get("coalescing_policy", {}).get("queue_all_non_durable")
        is not True
        or capacity.get("coalescing_policy", {}).get("pending_occurrences_per_job")
        != capacity.get("pending_per_job_limit")
        or not isinstance(capacity.get("pending_per_job_limit"), int)
        or capacity.get("pending_per_job_limit") < 2
        or not isinstance(capacity.get("durable_backlog_coalescing_job_ids"), list)
        or any(
            not isinstance(job_id, str) or not job_id
            for job_id in capacity.get("durable_backlog_coalescing_job_ids", [])
        )
        or capacity.get("coalesced_distinct_occurrences")
        != capacity.get("durable_backlog_coalesced_occurrences")
        or capacity.get("latest_start_misses") != 0
        or capacity.get("deadline_misses") != 0
        or capacity.get("global_worker_cap") != 4
        or not isinstance(business, dict)
        or business.get("status") != "passed"
        or not isinstance(duration, dict)
        or duration.get("enabled_jobs") != expected_enabled_jobs
        or duration.get("p95_jobs") != expected_enabled_jobs
        or duration.get("sparse_fallback_jobs") != 0
        or duration.get("missing_jobs") != 0
        or duration.get("certifying_p95_coverage") is not True
        or not isinstance(body_summary, dict)
        or body_summary.get("enabled_jobs") != expected_enabled_jobs
        or body_summary.get("jobs_with_three_successful_real_body_samples") != expected_enabled_jobs
        or body_summary.get("jobs_missing_real_body_adapter") != 0
        or body_summary.get("body_adapter_coverage_complete") is not True
        or body_summary.get("registry_evidence_sha256") != supplied_body_sha
        or deadlines
        != {
            "latest_start_misses": 0,
            "deadline_misses": 0,
            "max_queue_delay_seconds": capacity.get("max_queue_delay_seconds"),
        }
    ):
        raise CampaignSafetyError("schedule replay/capacity thresholds failed")

    body_network = body.get("network_access_performed")
    safety = report.get("safety")
    if (
        type(body_network) is not bool
        or evidence.get("network_access_performed") is not body_network
        or evidence.get("external_network_access_performed") is not False
        or body.get("external_network_access_performed") is not False
        or body.get("production_database_access_performed") is not False
        or body.get("nas_access_performed") is not False
        or body.get("production_state_write_performed") is not False
        or evidence.get("live_state_access_performed") is not False
        or not isinstance(safety, dict)
        or safety.get("body_network_access_performed") is not body_network
        or safety.get("body_external_network_access_performed") is not False
        or safety.get("body_nas_access_performed") is not False
        or safety.get("body_production_database_access_performed") is not False
        or safety.get("live_state_accessed") is not False
        or safety.get("production_service_started") is not False
        or safety.get("production_port_accessed") is not False
        or safety.get("launchctl_invoked") is not False
        or safety.get("sandbox_writes_only") is not True
    ):
        raise CampaignSafetyError("schedule offline safety attestation failed")

    report_binding = report.get("release_binding")
    body_binding = body.get("release_binding")
    if not isinstance(report_binding, dict) or not isinstance(body_binding, dict):
        raise CampaignSafetyError("schedule release binding is missing")
    if report_binding.get("real_job_body_evidence_sha256") != supplied_body_sha:
        raise CampaignSafetyError("schedule report/body evidence binding failed")
    if (
        report_binding.get("release_id") != body_binding.get("release_id")
        or report_binding.get("release_manifest_sha256")
        != body_binding.get("release_manifest_sha256")
        or report_binding.get("cron_jobs_sha256") != body_binding.get("cron_jobs_sha256")
    ):
        raise CampaignSafetyError("schedule raw report release bindings differ")
    outer_measurements = evidence.get("measurements")
    if outer_measurements != {
        "validation_profile_id": profile_id,
        "enabled_jobs": expected_enabled_jobs,
        "covered_jobs": expected_enabled_jobs,
        "passed_jobs": expected_enabled_jobs,
        "blocked_jobs": 0,
        "latest_start_misses": 0,
        "deadline_misses": 0,
    }:
        raise CampaignSafetyError("schedule campaign summary differs from raw reports")
    if expected_release is None:
        return
    release_files = {path: digest for path, digest, _size, _mode in expected_release.files}
    expected_report_binding = {
        "release_id": expected_release.release_id,
        "release_manifest_sha256": expected_release.manifest_sha256,
        "dispatch_policy_sha256": release_files.get(
            "config/v3_schedule_dispatch_policy.json"
        ),
        "certifier_script_sha256": release_files.get(
            "scripts/v3_validation/schedule_capacity_certification.py"
        ),
        "real_job_body_registry_script_sha256": release_files.get(
            "scripts/v3_validation/schedule_body_registry.py"
        ),
        "real_job_body_registry_sha256": release_files.get(
            "config/v3_schedule_body_adapter_registry.json"
        ),
        "duration_baseline_sha256": release_files.get(
            "config/v3_schedule_realism_baseline.json"
        ),
    }
    if any(report_binding.get(key) != value for key, value in expected_report_binding.items()):
        raise CampaignSafetyError("schedule capacity source files are not release-bound")
    if (
        body_binding.get("release_id") != expected_release.release_id
        or body_binding.get("release_manifest_sha256") != expected_release.manifest_sha256
        or body_binding.get("registry_sha256")
        != release_files.get("config/v3_schedule_body_adapter_registry.json")
        or body_binding.get("inherited_baseline_sha256")
        != release_files.get("config/v3_schedule_realism_baseline.json")
    ):
        raise CampaignSafetyError("schedule body source files are not release-bound")

def _validate_resource_performance_partial(
    evidence: dict[str, Any],
    *,
    expected_profile: dict[str, Any] | None,
    expected_release: ReleaseBundle | None,
    expected_python_runtime_sha256: str | None,
) -> None:
    """Accept only a hash-bound incomplete report; never promote its gaps."""

    from scripts.v3_validation.resource_performance_evidence import (
        ResourcePerformanceEvidenceError,
        summarize_report,
    )

    if evidence.get("probe") != "release_bound_resource_performance_partial":
        raise CampaignSafetyError("resource/performance partial probe is invalid")
    report = evidence.get("report")
    if not isinstance(report, dict):
        raise CampaignSafetyError("resource/performance inner report is missing")
    if expected_release is None:
        return
    release_files = {
        path: digest for path, digest, _size, _mode in expected_release.files
    }
    runtime_sha = expected_python_runtime_sha256 or str(
        report.get("release_binding", {}).get("python_runtime_sha256") or ""
    )
    if not SHA256_RE.fullmatch(runtime_sha):
        raise CampaignSafetyError("resource/performance runtime binding is invalid")
    try:
        metrics = summarize_report(
            report,
            release_files=release_files,
            python_runtime_sha256=runtime_sha,
            expected_profile=expected_profile,
            expected_release_id=expected_release.release_id,
            expected_release_manifest_sha256=expected_release.manifest_sha256,
        )
    except ResourcePerformanceEvidenceError as exc:
        raise CampaignSafetyError(
            f"resource/performance partial certification failed: {exc}"
        ) from exc
    if evidence.get("measurements") != metrics or report.get("metrics") != metrics:
        raise CampaignSafetyError(
            "resource/performance metrics differ from raw-observation recomputation"
        )


def _validate_release_quality_certification_evidence(
    evidence: dict[str, Any],
    *,
    expected_profile: dict[str, Any] | None,
    expected_release: ReleaseBundle | None,
    expected_python_runtime_sha256: str | None,
) -> None:
    """Recompute from pytest node outcomes and golden snapshots, never pass booleans."""

    from scripts.v3_validation.release_quality_evidence import (
        ReleaseQualityEvidenceError,
        summarize_report,
    )

    report = evidence.get("report")
    if not isinstance(report, dict):
        raise CampaignSafetyError("release quality inner report is missing")
    safety = report.get("safety")
    if (
        not isinstance(safety, dict)
        or any(
            safety.get(field) is not False
            for field in (
                "live_state_accessed",
                "production_service_started",
                "production_port_accessed",
                "launchctl_invoked",
                "external_writes",
            )
        )
    ):
        raise CampaignSafetyError("release quality safety report failed")
    if expected_release is None:
        return
    release_files = {
        path: digest for path, digest, _size, _mode in expected_release.files
    }
    suite_path = expected_release.root / "config/v3_release_quality_suites.json"
    if release_files.get("config/v3_release_quality_suites.json") != _sha256(suite_path):
        raise CampaignSafetyError("release quality suite manifest is not release-bound")
    try:
        suite_manifest = json.loads(suite_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignSafetyError("release quality suite manifest is unreadable") from exc
    runtime_sha = expected_python_runtime_sha256 or str(
        report.get("release_binding", {}).get("python_runtime_sha256") or ""
    )
    if not SHA256_RE.fullmatch(runtime_sha):
        raise CampaignSafetyError("release quality Python runtime binding is invalid")
    try:
        metrics = summarize_report(
            report,
            manifest=suite_manifest,
            release_files=release_files,
            python_runtime_sha256=runtime_sha,
            expected_profile=expected_profile,
            expected_release_id=expected_release.release_id,
            expected_release_manifest_sha256=expected_release.manifest_sha256,
        )
    except ReleaseQualityEvidenceError as exc:
        raise CampaignSafetyError(f"release quality certification failed: {exc}") from exc
    if evidence.get("measurements") != metrics or report.get("metrics") != metrics:
        raise CampaignSafetyError("release quality metrics differ from transcript recomputation")


def _validate_health_certification_evidence(
    evidence: dict[str, Any],
    *,
    expected_profile: dict[str, Any] | None,
    expected_release: ReleaseBundle | None,
) -> None:
    """Validate the hashed inner report, never the generic outer assertions."""

    report = evidence.get("report")
    if not isinstance(report, dict):
        raise CampaignSafetyError("health certification inner report is missing")
    supplied_hash = report.get("evidence_sha256")
    unhashed = dict(report)
    unhashed.pop("evidence_sha256", None)
    observed_hash = hashlib.sha256(
        json.dumps(
            unhashed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        report.get("schema") != "magi.v3.health-probe-certification/v1"
        or report.get("status") != "certified"
        or report.get("probe") != "production_health_service_liveness"
        or supplied_hash != observed_hash
    ):
        raise CampaignSafetyError("health certification inner report identity/hash is invalid")
    profile = report.get("validation_profile")
    if expected_profile is not None and profile != expected_profile:
        raise CampaignSafetyError("health certification validation profile binding drifted")
    measurements = report.get("measurements")
    exact = {
        "probe_count": 1_000,
        "successful_probes": 1_000,
        "failed_probes": 0,
        "model_imports": 0,
        "models_loaded": 0,
        "model_probe_flags": 0,
        "newly_loaded_heavy_modules": [],
        "state_mutations": [],
    }
    if (
        not isinstance(measurements, dict)
        or any(measurements.get(key) != value for key, value in exact.items())
        or evidence.get("measurements") != measurements
        or evidence.get("probe") != report.get("probe")
    ):
        raise CampaignSafetyError(
            "health certification is not 1000/1000 model-free and read-only"
        )
    for field in ("total_duration_us", "maximum_probe_us"):
        value = measurements.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise CampaignSafetyError(f"health certification timing is invalid: {field}")
    safety = report.get("safety")
    if not isinstance(safety, dict) or any(
        safety.get(field) is not False
        for field in (
            "network_access_performed",
            "service_start_performed",
            "production_port_access_performed",
            "launchctl_performed",
            "runtime_initialized",
        )
    ):
        raise CampaignSafetyError("health certification inner safety report failed")
    if expected_release is None:
        return
    release_files = {path: digest for path, digest, _size, _mode in expected_release.files}
    source_binding = report.get("release_binding")
    if not isinstance(source_binding, dict) or source_binding != {
        "certifier_script_sha256": release_files.get(
            "scripts/v3_validation/health_certification.py"
        ),
        "health_module_sha256": release_files.get("magi_v3/health.py"),
    }:
        raise CampaignSafetyError("health certification source files are not release-bound")


def _validate_fault_certification_evidence(
    evidence: dict[str, Any],
    *,
    expected_profile: dict[str, Any] | None,
    expected_release: ReleaseBundle | None,
) -> None:
    """Validate the hashed offline controlled-restart fault report."""

    report = evidence.get("report")
    if not isinstance(report, dict):
        raise CampaignSafetyError("fault certification inner report is missing")
    supplied_hash = report.get("evidence_sha256")
    unhashed = dict(report)
    unhashed.pop("evidence_sha256", None)
    observed_hash = hashlib.sha256(
        json.dumps(
            unhashed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        report.get("schema") != "magi.v3.fault-certification/v2"
        or report.get("status") != "certified_controlled_restart_fault_layer"
        or supplied_hash != observed_hash
        or (
            expected_profile is not None
            and report.get("validation_profile") != expected_profile
        )
    ):
        raise CampaignSafetyError("fault certification inner identity/profile/hash is invalid")
    from scripts.v3_validation.fault_certification import build_fault_stimulus_plan

    stimulus_plan = report.get("stimulus_plan")
    if (
        not isinstance(stimulus_plan, dict)
        or stimulus_plan != build_fault_stimulus_plan(report.get("validation_profile"))
    ):
        raise CampaignSafetyError("fault certification stimulus plan is invalid")
    decision = report.get("decision")
    if (
        not isinstance(decision, dict)
        or decision.get("blocker_code")
        != "FAULT_CAMPAIGN_CONTROLLED_RESTART_DEFERRED"
        or decision.get("required_evidence_id")
        != "sqlite_wal_disk_full_fsync_faults_passed"
        or decision.get("eligible_to_clear_fault_campaign_realism_blocker") is not True
        or decision.get("software_equivalent_layer_certified") is not True
        or decision.get("transaction_stage_sigkill_certified") is not True
        or decision.get("external_device_disconnect_required") is not False
        or decision.get("physical_power_cut_required") is not False
        or decision.get("controlled_cold_restart_required_at_cutover") is not True
        or decision.get("hard_gate_blocked") is not False
    ):
        raise CampaignSafetyError("fault certification hard-gate decision is invalid")
    residual = report.get("residual_risk")
    if (
        not isinstance(residual, dict)
        or residual.get("accepted_by_equivalent_layer") is not True
        or residual.get("hard_gate_blocking") is not False
        or residual.get("deferred_gate")
        != "atomic_release_switch_and_cold_rollback_drill_passed"
        or residual.get("required_before_final_replacement")
        != [
            "controlled cold restart with boot-session change",
            "V2 readiness and single-owner restoration after restart",
        ]
    ):
        raise CampaignSafetyError("fault certification controlled-restart deferral is invalid")
    measurements = report.get("measurements")
    if not isinstance(measurements, dict) or evidence.get("measurements") != measurements:
        raise CampaignSafetyError("fault certification measurements are missing or drifted")
    _validate_fault_equivalent_measurements(measurements, stimulus_plan)
    safety = report.get("safety")
    if (
        not isinstance(safety, dict)
        or safety.get("live_magi_state_accessed") is not False
        or safety.get("live_business_database_accessed") is not False
        or safety.get("production_service_started") is not False
        or safety.get("production_port_accessed") is not False
        or safety.get("launchctl_invoked") is not False
        or safety.get("network_accessed") is not False
        or safety.get("signals_sent_only_to_owned_children") is not True
        or safety.get("apfs_mount_was_disposable_sparse_image") is not True
        or safety.get("apfs_image_detached_and_removed") is not True
        or not SHA256_RE.fullmatch(str(safety.get("sandbox_path_sha256") or ""))
    ):
        raise CampaignSafetyError("fault certification safety proof is invalid")
    binding = report.get("release_binding")
    if not isinstance(binding, dict):
        raise CampaignSafetyError("fault certification source binding is missing")
    if (
        not SHA256_RE.fullmatch(str(binding.get("python_executable_sha256") or ""))
        or not isinstance(binding.get("mach_helper"), dict)
        or any(
            not SHA256_RE.fullmatch(str(binding["mach_helper"].get(field) or ""))
            for field in ("source_sha256", "executable_sha256")
        )
    ):
        raise CampaignSafetyError("fault certification runtime/helper binding is invalid")
    if expected_release is None:
        return
    release_files = {path: digest for path, digest, _size, _mode in expected_release.files}
    if (
        binding.get("certifier_script_sha256")
        != release_files.get("scripts/v3_validation/fault_certification.py")
        or binding.get("fault_probe_script_sha256")
        != release_files.get("scripts/v3_validation/fault_realism.py")
    ):
        raise CampaignSafetyError("fault certification source files are not release-bound")


def _validate_fault_equivalent_measurements(
    measurements: dict[str, Any], stimulus_plan: dict[str, Any]
) -> None:
    apfs = measurements.get("apfs_enospc")
    if (
        not isinstance(apfs, dict)
        or apfs.get("status") != "passed"
        or apfs.get("filesystem") != "apfs"
        or apfs.get("image_type") != "sparsebundle"
        or apfs.get("image_capacity_bytes") != 33_554_432
        or apfs.get("recovery_reserve_bytes") != 4_194_304
        or apfs.get("sqlite_overhead_reserve_bytes") != 1_048_576
        or apfs.get("sqlite_full_attempt_isolated_to_owned_child") is not True
        or apfs.get("sqlite_recovery_isolated_to_owned_child") is not True
        or apfs.get("fault_filler_removed_before_recovery") is not True
        or apfs.get("filesystem_enospc_observed") is not True
        or apfs.get("filesystem_enospc_operation") not in {"write", "fsync"}
        or apfs.get("sqlite_full_observed") is not True
        or apfs.get("sqlite_error_code") != 13
        or apfs.get("sqlite_error_name") != "SQLITE_FULL"
        or apfs.get("committed_rows_preserved") != 1
        or apfs.get("partial_rows_visible") != 0
        or apfs.get("final_jobs") != 2
        or apfs.get("integrity_check") != "ok"
    ):
        raise CampaignSafetyError("fault APFS ENOSPC inner measurement failed")
    vfs = measurements.get("sqlite_wal_fsync_io_error")
    if (
        not isinstance(vfs, dict)
        or vfs.get("status") != "passed"
        or vfs.get("injection_boundary") != "custom SQLite VFS xSync"
        or vfs.get("injected_error") != "SQLITE_IOERR_FSYNC"
        or vfs.get("injected_file_role") != "wal"
        or vfs.get("commit_rc") != 1034
        or vfs.get("extended_rc") != 1034
        or vfs.get("expected_extended_rc") != 1034
        or vfs.get("injected") != 1
        or type(vfs.get("sync_calls_after_arm")) is not int
        or vfs["sync_calls_after_arm"] < 1
        or vfs.get("baseline_rows") != 1
        or vfs.get("partial_rows") != 0
        or vfs.get("recovery_rc") != 0
        or vfs.get("final_rows") != 2
        or vfs.get("integrity_ok") != 1
        or vfs.get("journal_mode") != "wal"
        or vfs.get("synchronous") != "FULL"
        or vfs.get("power_loss_simulated") is not False
    ):
        raise CampaignSafetyError("fault SQLite WAL xSync inner measurement failed")
    expected_stages = [
        "READY",
        "BEGIN",
        "JOB_INSERT",
        *(f"PAYLOAD_{index:02d}" for index in range(32)),
        "COMMIT_STARTED",
        "COMMIT_ACK",
    ]
    logical = measurements.get("logical_transaction_boundary_sweep")
    if (
        not isinstance(logical, dict)
        or logical.get("stages_requested") != 37
        or logical.get("stages_completed") != 37
        or logical.get("stage_markers") != expected_stages
        or logical.get("acknowledged_commits_lost") != 0
        or logical.get("partially_visible_transactions") != 0
        or logical.get("final_job_rows") != 37
        or logical.get("final_unique_jobs") != 37
        or logical.get("final_payload_rows") != 1_184
        or logical.get("duplicate_jobs") != 0
        or logical.get("lost_jobs_after_recovery") != 0
        or logical.get("integrity_check") != "ok"
        or not isinstance(logical.get("cycles"), list)
        or any(not isinstance(row, dict) for row in logical["cycles"])
        or [row.get("target_stage") for row in logical["cycles"]] != expected_stages
        or any(
            row.get("signal") != "SIGKILL"
            or row.get("final_job_rows") != 1
            or row.get("final_payload_rows") != 32
            or row.get("integrity_check") != "ok"
            for row in logical["cycles"]
        )
    ):
        raise CampaignSafetyError("fault logical transaction sweep failed")
    mach = measurements.get("mach_clock_sigkill")
    offsets = stimulus_plan.get("mach_kill_offsets_us")
    cycles = mach.get("cycles") if isinstance(mach, dict) else None
    if (
        not isinstance(offsets, list)
        or len(offsets) != 6
        or any(type(offset) is not int or not 0 <= offset <= 20_000 for offset in offsets)
        or len(set(offsets)) != 6
        or not isinstance(mach, dict)
        or mach.get("clock") != "mach_absolute_time"
        or mach.get("wait") != "mach_wait_until"
        or mach.get("offsets_us") != offsets
        or mach.get("cycles_completed") != 6
        or mach.get("acknowledged_commits_lost") != 0
        or mach.get("partially_visible_transactions") != 0
        or mach.get("duplicate_jobs") != 0
        or mach.get("lost_jobs_after_recovery") != 0
        or mach.get("final_job_rows") != 6
        or mach.get("final_payload_rows") != 192
        or mach.get("integrity_check") != "ok"
        or not isinstance(cycles, list)
        or len(cycles) != 6
    ):
        raise CampaignSafetyError("fault Mach-clock aggregate measurement failed")
    for index, row in enumerate(cycles):
        timing = row.get("timing") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or not isinstance(timing, dict)
            or timing.get("clock") != "mach_absolute_time"
            or timing.get("wait") != "mach_wait_until"
            or timing.get("signal") != "SIGKILL"
            or timing.get("scheduled_delay_ns") != offsets[index] * 1_000
            or type(timing.get("target_pid")) is not int
            or timing["target_pid"] <= 1
            or row.get("final_job_rows") != 1
            or row.get("final_payload_rows") != 32
            or row.get("integrity_check") != "ok"
        ):
            raise CampaignSafetyError("fault Mach-clock cycle measurement failed")


def _validate_route_certification_evidence(
    evidence: dict[str, Any],
    *,
    expected_profile: dict[str, Any] | None,
    expected_release: ReleaseBundle | None,
    expected_runtime_binding: dict[str, Any] | None = None,
) -> None:
    """Accept only complete, immutable-release-bound strict handler proof."""

    measurements = evidence.get("measurements")
    if not isinstance(measurements, dict):
        raise CampaignSafetyError("route certification measurements are missing")
    exact_measurements = {
        "pinned_routes": 347,
        "fully_replayed_routes": 347,
        "remaining_routes": 0,
        "pinned_route_methods": 431,
        "representative_success_path_passed": 431,
        "remaining_route_methods": 0,
    }
    if any(measurements.get(key) != value for key, value in exact_measurements.items()):
        raise CampaignSafetyError("route certification is not strict 431/431 success")
    if expected_profile is not None and measurements.get(
        "validation_profile_id"
    ) != expected_profile.get("profile_id"):
        raise CampaignSafetyError("route certification validation profile binding drifted")
    for key, value in exact_measurements.items():
        if evidence.get(key) != value:
            raise CampaignSafetyError(f"route certification top-level count drifted: {key}")
    if evidence.get("coverage_complete") is not True or evidence.get("passed") is not True:
        raise CampaignSafetyError("route certification completion claim is absent")
    if evidence.get("certifying") is not True or evidence.get("diagnostic_passed") is not False:
        raise CampaignSafetyError("route certification is non-certifying diagnostic evidence")
    if not _validate_route_runtime_binding(
        evidence.get("runtime_binding"),
        expected_release=expected_release,
        expected_binding=expected_runtime_binding,
    ):
        raise CampaignSafetyError("route certification runtime binding is invalid")
    blocker = evidence.get("blockers", {}).get("ROUTE_REPLAY_NOT_IMPLEMENTED")
    if (
        not isinstance(blocker, dict)
        or blocker.get("retained") is not False
        or blocker.get("remaining_routes") != 0
        or blocker.get("remaining_route_methods") != 0
    ):
        raise CampaignSafetyError("route replay blocker remains retained")
    safety = evidence.get("safety")
    expected_external_storage_roots = _route_external_storage_roots()
    trace_isolation_attempts = (
        safety.get("trace_isolation_attempts")
        if isinstance(safety, dict)
        else None
    )
    external_storage_attempts = (
        safety.get("external_storage_access_attempts")
        if isinstance(safety, dict)
        else None
    )
    base_isolation_attempts = (
        safety.get("base_isolation_attempts")
        if isinstance(safety, dict)
        else None
    )
    all_trace_counters_zero = (
        isinstance(trace_isolation_attempts, dict)
        and all(
            isinstance(name, str) and type(value) is int and value == 0
            for name, value in trace_isolation_attempts.items()
        )
    )
    all_base_counters_zero = (
        isinstance(base_isolation_attempts, dict)
        and all(
            isinstance(name, str) and type(value) is int and value == 0
            for name, value in base_isolation_attempts.items()
        )
    )
    seatbelt_workspace = _route_attested_seatbelt_workspace(
        safety.get("seatbelt") if isinstance(safety, dict) else None,
        expected_profile,
    )
    if (
        not isinstance(safety, dict)
        or safety.get("offline") is not True
        or safety.get("production_service_started") is not False
        or safety.get("production_database_accessed") is not False
        or safety.get("nas_accessed") is not False
        or safety.get("external_storage_attested") is not True
        or safety.get("trace_external_storage_attested") is not True
        or safety.get("base_external_storage_attested") is not True
        or safety.get("external_storage_roots") != expected_external_storage_roots
        or seatbelt_workspace is None
        or type(external_storage_attempts) is not int
        or external_storage_attempts != 0
        or not all_trace_counters_zero
        or not all_base_counters_zero
        or safety.get("base_safe_execution") is not True
    ):
        raise CampaignSafetyError("route certification safety proof is invalid")
    rows = evidence.get("route_method_dispositions")
    if not isinstance(rows, list) or len(rows) != 431:
        raise CampaignSafetyError("route certification does not contain 431 dispositions")
    observed: set[tuple[str, str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CampaignSafetyError("route certification disposition is invalid")
        key = (
            str(row.get("service") or ""),
            str(row.get("rule") or ""),
            str(row.get("method") or "").upper(),
            str(row.get("endpoint") or ""),
        )
        if key in observed:
            raise CampaignSafetyError("route certification disposition is duplicated")
        observed.add(key)
        if (
            row.get("disposition") != "actual_handler_passed"
            or row.get("handler_dispatch_passed") is not True
            or row.get("representative_success_path_passed") is not True
        ):
            raise CampaignSafetyError("validation guard cannot certify a route method")
    supplied_hash = evidence.get("evidence_sha256")
    unhashed = dict(evidence)
    unhashed.pop("evidence_sha256", None)
    observed_hash = hashlib.sha256(
        json.dumps(
            unhashed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if supplied_hash != observed_hash:
        raise CampaignSafetyError("route certification evidence hash is invalid")
    if expected_release is None:
        return
    expected_binding = {
        "release_id": expected_release.release_id,
        "release_sha": expected_release.source_snapshot_sha256,
        "release_manifest": str(expected_release.root / MANIFEST_NAME),
        "release_manifest_sha256": expected_release.manifest_sha256,
        "release_commit": expected_release.commit,
    }
    if any(evidence.get(key) != value for key, value in expected_binding.items()):
        raise CampaignSafetyError("route certification release binding drifted")
    route_payload, _route_bytes = _read_json_regular(
        expected_release.root
        / "docs"
        / "architecture"
        / "v3"
        / "generated"
        / "v2_runtime_routes.json",
        "route certification runtime inventory",
    )
    normalized: list[dict[str, Any]] = []
    expected_methods: set[tuple[str, str, str, str]] = set()
    services = route_payload.get("services")
    if not isinstance(services, dict):
        raise CampaignSafetyError("route certification runtime inventory is invalid")
    for service, routes in services.items():
        if service not in {"5002", "5003"} or not isinstance(routes, list):
            raise CampaignSafetyError("route certification runtime inventory service is invalid")
        for route in routes:
            if not isinstance(route, dict):
                raise CampaignSafetyError("route certification runtime route is invalid")
            methods = sorted({str(method).upper() for method in route.get("methods", ())})
            rule = str(route.get("rule") or "")
            endpoint = str(route.get("endpoint") or "")
            normalized.append(
                {"service": service, "rule": rule, "methods": methods, "endpoint": endpoint}
            )
            expected_methods.update((service, rule, method, endpoint) for method in methods)
    normalized.sort(
        key=lambda row: (row["service"], row["rule"], row["methods"], row["endpoint"])
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        len(normalized) != 347
        or len(expected_methods) != 431
        or observed != expected_methods
        or evidence.get("inventory_fingerprint") != fingerprint
        or evidence.get("inventory_counts") != route_payload.get("counts")
    ):
        raise CampaignSafetyError("route certification is not bound to the exact route inventory")
    release_files = {path: digest for path, digest, _size, _mode in expected_release.files}
    source_binding = evidence.get("source_binding")
    expected_sources = {
        "compiler_sha256": "scripts/v3_validation/route_certification.py",
        "actual_route_replay_sha256": "scripts/v3_validation/actual_route_replay.py",
        "trace_plugin_sha256": "scripts/v3_validation/route_success_trace_plugin.py",
        "proof_review_manifest_sha256": "scripts/v3_validation/route-success-proof-review.json",
        "primary_side_effect_review_sha256": "scripts/v3_validation/route-method-review.json",
        "supplemental_side_effect_review_sha256": (
            "scripts/v3_validation/route-method-review-supplement.json"
        ),
    }
    if not isinstance(source_binding, dict) or any(
        source_binding.get(role) != release_files.get(path)
        for role, path in expected_sources.items()
    ) or not SHA256_RE.fullmatch(str(source_binding.get("base_evidence_sha256") or "")):
        raise CampaignSafetyError("route certification source files are not release-bound")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CampaignSafetyError(f"duplicate JSON key is forbidden: {key}")
        value[key] = item
    return value


def _read_json_regular(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or path.is_symlink():
            raise CampaignSafetyError(f"{label} must be a regular, non-symlink file")
        data = path.read_bytes()
        after = path.lstat()
    except (OSError, CampaignSafetyError) as exc:
        if isinstance(exc, CampaignSafetyError):
            raise
        raise CampaignSafetyError(f"{label} unreadable: {exc}") from exc
    signature = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        stat.S_IMODE(value.st_mode),
    )
    if signature(before) != signature(after):
        raise CampaignSafetyError(f"{label} changed while being read")
    try:
        value = json.loads(data, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise CampaignSafetyError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CampaignSafetyError(f"{label} must contain a JSON object")
    return value, data


def _manifest_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CampaignSafetyError("release manifest contains an unsafe file path")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise CampaignSafetyError("release manifest contains an unsafe file path")
    return parsed.as_posix()


def _scan_release_files(root: Path, relative: Path = Path()) -> dict[str, Path]:
    directory = root / relative
    try:
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
    except OSError as exc:
        raise CampaignSafetyError(f"release directory unreadable: {relative.as_posix() or '.'}") from exc
    files: dict[str, Path] = {}
    for child in children:
        child_relative = relative / child.name
        child_path = Path(child.path)
        if child.is_symlink():
            raise CampaignSafetyError(
                f"symlinks are forbidden in release bundle: {child_relative.as_posix()}"
            )
        if child.is_dir(follow_symlinks=False):
            files.update(_scan_release_files(root, child_relative))
        elif child.is_file(follow_symlinks=False):
            files[child_relative.as_posix()] = child_path
        else:
            raise CampaignSafetyError(
                f"special files are forbidden in release bundle: {child_relative.as_posix()}"
            )
    return files


def _verified_file(path: Path, root: Path, expected: tuple[str, int, int], relative: str) -> None:
    expected_hash, expected_size, expected_mode = expected
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        actual_hash = _sha256(path)
        after = path.lstat()
    except (OSError, ValueError) as exc:
        raise CampaignSafetyError(f"release file is missing or escapes bundle: {relative}") from exc
    signature = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        stat.S_IMODE(value.st_mode),
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or signature(before) != signature(after)
        or before.st_size != expected_size
        or stat.S_IMODE(before.st_mode) != expected_mode
        or actual_hash != expected_hash
    ):
        raise CampaignSafetyError(f"release file hash/size/mode mismatch: {relative}")


def verify_release_bundle(release_root: Path, expected_release_sha: str) -> ReleaseBundle:
    """Verify marker, manifest, exact file set, and every listed file hash."""

    raw_root = release_root.expanduser()
    if raw_root.is_symlink():
        raise CampaignSafetyError("release_root must not be a symlink")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise CampaignSafetyError(f"release_root is unavailable: {exc}") from exc
    if not root.is_dir():
        raise CampaignSafetyError("release_root must be a directory")
    try:
        root.relative_to(APPLICATION_SUPPORT.resolve())
    except ValueError:
        pass
    else:
        raise CampaignSafetyError("release_root must not be inside Application Support")

    marker, _marker_bytes = _read_json_regular(root / COMPLETION_MARKER, "release marker")
    manifest, manifest_bytes = _read_json_regular(root / MANIFEST_NAME, "release manifest")
    if marker.get("schema_version") != 1 or manifest.get("schema_version") != 1:
        raise CampaignSafetyError("unsupported release marker/manifest schema")
    if marker.get("manifest") != MANIFEST_NAME:
        raise CampaignSafetyError("release marker does not bind the canonical manifest")
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if marker.get("manifest_sha256") != manifest_hash:
        raise CampaignSafetyError("release manifest SHA-256 does not match completion marker")
    source_sha = manifest.get("source_snapshot_sha256")
    if (
        not isinstance(source_sha, str)
        or not SHA256_RE.fullmatch(source_sha)
        or marker.get("source_snapshot_sha256") != source_sha
        or expected_release_sha != source_sha
    ):
        raise CampaignSafetyError(
            "release_sha must exactly match marker/manifest source_snapshot_sha256"
        )
    release_id = manifest.get("release_id")
    commit = manifest.get("commit")
    if (
        not isinstance(release_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", release_id)
        or marker.get("release_id") != release_id
        or not isinstance(commit, str)
        or not COMMIT_RE.fullmatch(commit)
        or marker.get("commit") != commit
        or manifest.get("immutable") is not True
    ):
        raise CampaignSafetyError("release marker/manifest identity binding is invalid")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise CampaignSafetyError("release manifest file inventory is empty")
    entries: list[tuple[str, str, int, int]] = []
    for item in raw_files:
        if not isinstance(item, dict):
            raise CampaignSafetyError("release manifest contains an invalid file entry")
        relative = _manifest_path(item.get("path"))
        if relative in {MANIFEST_NAME, COMPLETION_MARKER}:
            raise CampaignSafetyError("release manifest must not inventory its control files")
        digest = item.get("sha256")
        size = item.get("size")
        mode_text = item.get("mode")
        if (
            not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(mode_text, str)
            or not re.fullmatch(r"[0-7]{4}", mode_text)
            or int(mode_text, 8) not in {0o444, 0o555}
        ):
            raise CampaignSafetyError(f"release manifest entry metadata is invalid: {relative}")
        entries.append((relative, digest, size, int(mode_text, 8)))
    paths = [entry[0] for entry in entries]
    if len(paths) != len(set(paths)):
        raise CampaignSafetyError("release manifest contains duplicate file paths")
    if paths != sorted(paths):
        raise CampaignSafetyError("release manifest file inventory must be canonically sorted")
    if manifest.get("source_file_count") != len(entries) or marker.get("source_file_count") != len(
        entries
    ):
        raise CampaignSafetyError("release source file count binding is invalid")
    snapshot_payload = [
        {"path": path, "sha256": digest, "size": size, "mode": f"{mode:04o}"}
        for path, digest, size, mode in entries
    ]
    snapshot_hash = hashlib.sha256(
        json.dumps(snapshot_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if snapshot_hash != source_sha:
        raise CampaignSafetyError("release source snapshot digest does not match manifest inventory")

    actual = _scan_release_files(root)
    expected_paths = set(paths) | {MANIFEST_NAME, COMPLETION_MARKER}
    if set(actual) != expected_paths:
        missing = sorted(expected_paths - set(actual))
        extra = sorted(set(actual) - expected_paths)
        raise CampaignSafetyError(
            f"release file inventory mismatch: missing={missing[:3]}, extra={extra[:3]}"
        )
    by_path = {path: (digest, size, mode) for path, digest, size, mode in entries}
    for relative in sorted(by_path):
        _verified_file(actual[relative], root, by_path[relative], relative)

    # Close the marker/manifest read-to-verify race as far as a pathname-based
    # verifier can: both control files must still be the exact bytes accepted.
    _final_marker, final_marker_bytes = _read_json_regular(
        root / COMPLETION_MARKER, "release marker"
    )
    _final_manifest, final_manifest_bytes = _read_json_regular(
        root / MANIFEST_NAME, "release manifest"
    )
    if final_marker_bytes != _marker_bytes or final_manifest_bytes != manifest_bytes:
        raise CampaignSafetyError("release marker/manifest changed during verification")

    required = {
        "bin/magi-v3-python",
        "config/v3_validation_campaign.json",
        "config/v3_cutover_gates.json",
        "magi_v3/health.py",
        "scripts/v3_validation/fault_realism.py",
        "scripts/v3_validation/health_certification.py",
        *(
            argument
            for command in OFFLINE_COMMANDS.values()
            for argument in command
            if argument.startswith(("tests/", "scripts/"))
        ),
    }
    missing_required = sorted(required - set(by_path))
    if missing_required:
        raise CampaignSafetyError(
            f"release is missing allowlisted offline campaign file: {missing_required[0]}"
        )
    launcher_mode = by_path["bin/magi-v3-python"][2]
    if launcher_mode & 0o111 == 0:
        raise CampaignSafetyError("release Python launcher is not executable")
    return ReleaseBundle(
        root=root,
        release_id=release_id,
        commit=commit,
        source_snapshot_sha256=source_sha,
        manifest_sha256=manifest_hash,
        files=tuple(entries),
    )


def _safe_state_dir(path: Path, release_root: Path) -> Path:
    raw = path.expanduser()
    if raw.exists() and raw.is_symlink():
        raise CampaignSafetyError("state directory must not be a symlink")
    candidate = raw.resolve()
    for forbidden, message in (
        (LIVE_ROOT.resolve(), "state directory must not be inside the live MAGI runtime"),
        (release_root, "state directory must be outside the immutable release bundle"),
    ):
        try:
            candidate.relative_to(forbidden)
        except ValueError:
            pass
        else:
            raise CampaignSafetyError(message)
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _load_config(path: Path) -> dict[str, Any]:
    value, _data = _read_json_regular(path, "campaign config")
    if value.get("schema_version") != 1:
        raise CampaignSafetyError("unsupported campaign config")
    if value.get("maximum_simultaneously_active_magi_releases") != 1:
        raise CampaignSafetyError("campaign must require single-active release")
    if value.get("production_release") != "v3":
        raise CampaignSafetyError("campaign production release must be V3")
    if value.get("timezone") != "Asia/Taipei":
        raise CampaignSafetyError("certifying campaign timezone must be Asia/Taipei")
    offline = value.get("offline_campaign")
    if not isinstance(offline, dict) or not isinstance(offline.get("workloads"), list):
        raise CampaignSafetyError("offline campaign workloads missing")
    if (
        offline.get("validation_strategy")
        != "targeted_v3_once_with_production_observation"
    ):
        raise CampaignSafetyError(
            "offline campaign must use the targeted V3 promotion strategy"
        )
    maximum_hours = offline.get("maximum_completion_hours")
    if (
        not isinstance(maximum_hours, int)
        or isinstance(maximum_hours, bool)
        or not 1 <= maximum_hours <= 24
    ):
        raise CampaignSafetyError("offline campaign must complete within 1 to 24 hours")
    required = offline.get("minimum_consecutive_days")
    if not isinstance(required, int) or isinstance(required, bool) or required != 1:
        raise CampaignSafetyError("accelerated offline campaign must require one validation day")
    required_passes = offline.get("required_independent_passes")
    if (
        not isinstance(required_passes, int)
        or isinstance(required_passes, bool)
        or required_passes != 1
    ):
        raise CampaignSafetyError(
            "targeted V3 promotion must run exactly one independent pass"
        )
    profiles = offline.get("validation_pass_profiles")
    if not isinstance(profiles, list) or len(profiles) != required_passes:
        raise CampaignSafetyError("each accelerated validation pass requires one profile")
    retired_workloads = {
        "matched_v2_v3_performance",
        "ime_candidate_window_pressure_probe",
    }
    if retired_workloads & set(offline["workloads"]):
        raise CampaignSafetyError(
            "retired V2/IME diagnostics cannot be promotion workloads"
        )
    profile_ids: set[str] = set()
    profile_starts: set[str] = set()
    fault_seeds: set[int] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            raise CampaignSafetyError("validation pass profile must be an object")
        profile_id = profile.get("profile_id")
        replay_start = profile.get("replay_start_local")
        fault_seed = profile.get("fault_seed")
        if (
            not isinstance(profile_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_]{2,63}", profile_id) is None
            or not isinstance(replay_start, str)
            or not isinstance(fault_seed, int)
            or isinstance(fault_seed, bool)
        ):
            raise CampaignSafetyError("validation pass profile fields are invalid")
        try:
            parsed_start = datetime.fromisoformat(replay_start)
        except ValueError as exc:
            raise CampaignSafetyError("validation profile start is not ISO-8601") from exc
        if parsed_start.tzinfo is None or parsed_start.astimezone(ZoneInfo("Asia/Taipei")).utcoffset() != timedelta(hours=8):
            raise CampaignSafetyError("validation profile start must bind Asia/Taipei time")
        if parsed_start.second or parsed_start.microsecond:
            raise CampaignSafetyError("validation profile start must be minute-aligned")
        profile_ids.add(profile_id)
        profile_starts.add(replay_start)
        fault_seeds.add(fault_seed)
    if len(profile_ids) != required_passes or len(profile_starts) != required_passes or len(fault_seeds) != required_passes:
        raise CampaignSafetyError("validation pass profiles must have unique ids, starts, and seeds")
    harness = offline.get("harness_certification")
    if not isinstance(harness, dict) or harness.get("status") not in {"blocked", "certified"}:
        raise CampaignSafetyError("offline harness certification status is missing")
    blockers = harness.get("arming_blockers")
    if not isinstance(blockers, list) or any(not isinstance(item, dict) for item in blockers):
        raise CampaignSafetyError("offline harness arming blockers must be machine-readable objects")
    armed = value.get("armed") is True
    if armed and (
        value.get("campaign_state") != "certifying_offline"
        or harness.get("status") != "certified"
        or blockers
    ):
        raise CampaignSafetyError("armed campaign requires a certified, blocker-free offline harness")
    if not armed and value.get("campaign_state") not in {"ready_unarmed", "certifying_offline"}:
        raise CampaignSafetyError("unarmed campaign must be ready_unarmed or certifying_offline")
    live = value.get("isolated_live_validation")
    if not isinstance(live, dict) or live.get("external_writes_enabled") is not False:
        raise CampaignSafetyError("live validation safety declaration missing")
    return value


class CampaignRunner:
    def __init__(
        self,
        *,
        release_root: Path,
        state_dir: Path,
        context: CampaignContext,
        command_runner: CommandRunner | None = None,
        clock: Callable[[], datetime] | None = None,
        python_runtime: Path | None = None,
        python_runtime_sha256: str | None = None,
        python_runtime_realpath: Path | None = None,
        python_runtime_manifest: Path | None = None,
        python_runtime_manifest_sha256: str | None = None,
        python_runtime_tree_sha256: str | None = None,
        cron_jobs_file: Path | None = None,
        cron_jobs_sha256: str | None = None,
        cron_jobs_source_file: Path | None = None,
        cron_jobs_source_sha256: str | None = None,
        g8_smb_report: Path | None = None,
        g8_smb_report_sha256: str | None = None,
        website_root: Path | None = None,
        website_admin_sha256: str | None = None,
    ) -> None:
        context.validate()
        # Full release verification intentionally precedes all mutable state creation.
        self.bundle = verify_release_bundle(release_root, context.release_sha)
        self.release_root = self.bundle.root
        self.context = context
        self.config_path = self.release_root / "config" / "v3_validation_campaign.json"
        self.gate_config_path = self.release_root / "config" / "v3_cutover_gates.json"
        self.config = _load_config(self.config_path)
        self.campaign_config_sha256 = _sha256(self.config_path)
        if _sha256(self.gate_config_path) != context.gate_config_sha256:
            raise CampaignSafetyError("gate config SHA-256 does not match campaign context")
        self.g8_smb_report = g8_smb_report
        self.g8_smb_report_sha256 = g8_smb_report_sha256
        self._g8_smb_binding = self._verify_g8_smb_binding()
        self.website_root = website_root or (
            Path(os.environ["MAGI_WEBSITE_ROOT"])
            if os.environ.get("MAGI_WEBSITE_ROOT")
            else None
        )
        self.website_admin_sha256 = website_admin_sha256 or os.environ.get(
            "MAGI_WEBSITE_ADMIN_SHA256"
        )
        self._website_admin_binding = self._verify_website_admin_binding(
            required=command_runner is None
        )
        self.state_dir = _safe_state_dir(state_dir, self.release_root)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timezone = ZoneInfo("Asia/Taipei")
        self.db_path = self.state_dir / "campaign.sqlite3"
        self.artifact_dir = self.state_dir / "artifacts"
        self.artifact_dir.mkdir(exist_ok=True)
        self._injected_command_runner = command_runner
        # Populated only by the real release-launcher path immediately before
        # a route workload executes.  Unit fixtures may retain a certifiable
        # context while replacing _run_command with a non-executing evidence
        # producer; those fixtures must not be forced to manufacture a full
        # site-packages runtime manifest.
        self._last_route_runtime_binding: dict[str, Any] | None = None
        self.python_runtime = python_runtime or (
            Path(os.environ["MAGI_V3_PYTHON_RUNTIME"])
            if os.environ.get("MAGI_V3_PYTHON_RUNTIME")
            else None
        )
        self.python_runtime_sha256 = python_runtime_sha256 or os.environ.get(
            "MAGI_V3_PYTHON_RUNTIME_SHA256"
        )
        self.python_runtime_realpath = python_runtime_realpath or (
            Path(os.environ["MAGI_V3_PYTHON_RUNTIME_REALPATH"])
            if os.environ.get("MAGI_V3_PYTHON_RUNTIME_REALPATH")
            else None
        )
        self.python_runtime_manifest = python_runtime_manifest or (
            Path(os.environ["MAGI_V3_PYTHON_RUNTIME_MANIFEST"])
            if os.environ.get("MAGI_V3_PYTHON_RUNTIME_MANIFEST")
            else None
        )
        self.python_runtime_manifest_sha256 = (
            python_runtime_manifest_sha256
            or os.environ.get("MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256")
        )
        self.python_runtime_tree_sha256 = (
            python_runtime_tree_sha256
            or os.environ.get("MAGI_V3_PYTHON_RUNTIME_TREE_SHA256")
        )
        self.cron_jobs_file = cron_jobs_file or (
            Path(os.environ["MAGI_CRON_JOBS_FILE"])
            if os.environ.get("MAGI_CRON_JOBS_FILE")
            else None
        )
        self.cron_jobs_sha256 = cron_jobs_sha256 or os.environ.get("MAGI_CRON_JOBS_SHA256")
        self.cron_jobs_source_file = cron_jobs_source_file or (
            Path(os.environ["MAGI_CRON_JOBS_SOURCE_FILE"])
            if os.environ.get("MAGI_CRON_JOBS_SOURCE_FILE")
            else None
        )
        self.cron_jobs_source_sha256 = (
            cron_jobs_source_sha256
            or os.environ.get("MAGI_CRON_JOBS_SOURCE_SHA256")
            or self.cron_jobs_sha256
        )
        if command_runner is None:
            self._verify_python_runtime()
        self._initialize()

    @property
    def execution_certifiable(self) -> bool:
        return self._injected_command_runner is None

    def _execution_binding(self) -> dict[str, Any]:
        return {
            "execution_backend": (
                "release_launcher" if self.execution_certifiable else "injected_noncertifying"
            ),
            "python_runtime_path": (
                str(self.python_runtime) if self.execution_certifiable else None
            ),
            "python_runtime_sha256": (
                self.python_runtime_sha256 if self.execution_certifiable else None
            ),
            "python_runtime_realpath": (
                str(self.python_runtime_realpath) if self.execution_certifiable else None
            ),
            "python_runtime_manifest": (
                str(self.python_runtime_manifest) if self.execution_certifiable else None
            ),
            "python_runtime_manifest_sha256": (
                self.python_runtime_manifest_sha256 if self.execution_certifiable else None
            ),
            "python_runtime_tree_sha256": (
                self.python_runtime_tree_sha256 if self.execution_certifiable else None
            ),
            "cron_jobs_file": (
                str(self.cron_jobs_file) if self.execution_certifiable else None
            ),
            "cron_jobs_sha256": (
                self.cron_jobs_sha256 if self.execution_certifiable else None
            ),
            "cron_jobs_source_file": (
                str(self.cron_jobs_source_file) if self.execution_certifiable else None
            ),
            "cron_jobs_source_sha256": (
                self.cron_jobs_source_sha256 if self.execution_certifiable else None
            ),
            **self._g8_smb_binding,
            **self._website_admin_binding,
        }

    def _verify_website_admin_binding(self, *, required: bool) -> dict[str, Any]:
        root = self.website_root
        expected = self.website_admin_sha256
        if root is None and expected is None and not required:
            return {"website_root": None, "website_admin_sha256": None}
        if root is None or not SHA256_RE.fullmatch(expected or ""):
            raise CampaignSafetyError(
                "external Website Admin requires an absolute root and SHA-256"
            )
        raw = root.expanduser()
        admin = raw / "admin" / "admin_server.py"
        try:
            resolved = raw.resolve(strict=True)
            root_metadata = raw.lstat()
            admin_resolved = admin.resolve(strict=True)
            admin_metadata = admin.lstat()
        except OSError as exc:
            raise CampaignSafetyError(
                f"external Website Admin source is unavailable: {exc}"
            ) from exc
        if (
            not raw.is_absolute()
            or resolved != raw
            or not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or admin_resolved != admin
            or not stat.S_ISREG(admin_metadata.st_mode)
            or stat.S_ISLNK(admin_metadata.st_mode)
            or admin_metadata.st_nlink != 1
            or resolved == self.release_root
            or resolved.is_relative_to(self.release_root)
            or resolved == LIVE_ROOT
            or resolved.is_relative_to(LIVE_ROOT)
            or _sha256(admin_resolved) != expected
        ):
            raise CampaignSafetyError(
                "external Website Admin must be canonical, hash-bound, and outside release/LIVE state"
            )
        if self.g8_smb_report is not None:
            try:
                g8_report = json.loads(self.g8_smb_report.read_text(encoding="utf-8"))
                ownership = json.loads(
                    base64.b64decode(
                        g8_report["raw_artifacts_b64"]["ownership_manifest"],
                        validate=True,
                    )
                )
                external = ownership["external_inputs"]
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CampaignSafetyError(
                    f"G8 ownership Website Admin binding is unavailable: {exc}"
                ) from exc
            if (
                not isinstance(external, dict)
                or external.get("website_root") != str(resolved)
                or external.get("website_admin_sha256") != expected
            ):
                raise CampaignSafetyError(
                    "external Website Admin differs from the G8 ownership composition"
                )
        return {
            "website_root": str(resolved),
            "website_admin_sha256": expected,
        }

    def _verify_g8_smb_binding(self) -> dict[str, Any]:
        path = self.g8_smb_report
        expected = self.g8_smb_report_sha256
        if path is None and expected is None:
            return {
                "g8_smb_report": None,
                "g8_smb_report_sha256": None,
                "g8_smb_evidence_sha256": None,
                "g8_smb_plan_semantic_sha256": None,
                "g8_matched_performance_sha256": None,
            }
        if path is None or not SHA256_RE.fullmatch(expected or ""):
            raise CampaignSafetyError("G8 SMB report requires an absolute path and SHA-256")
        raw = path.expanduser()
        try:
            resolved = raw.resolve(strict=True)
            metadata = raw.lstat()
        except OSError as exc:
            raise CampaignSafetyError(f"G8 SMB report is unavailable: {exc}") from exc
        if (
            not raw.is_absolute()
            or resolved != raw
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or resolved == self.release_root
            or resolved.is_relative_to(self.release_root)
            or resolved == LIVE_ROOT
            or resolved.is_relative_to(LIVE_ROOT)
            or _sha256(resolved) != expected
        ):
            raise CampaignSafetyError(
                "G8 SMB report must be canonical, hash-bound, outside release/LIVE state"
            )
        try:
            value = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("not an object")
            verify_g8_smb_report(
                value,
                expected_release_id=self.bundle.release_id,
                expected_release_manifest_sha256=self.bundle.manifest_sha256,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, G8SMBBlocked) as exc:
            raise CampaignSafetyError(f"G8 SMB report failed raw verification: {exc}") from exc
        evidence_sha = value.get("evidence_sha256")
        semantic_sha = value.get("plan", {}).get("plan_sha256")
        matched_sha = value.get("plan", {}).get("matched_performance_report", {}).get(
            "evidence_sha256"
        )
        if not SHA256_RE.fullmatch(str(evidence_sha or "")) or not SHA256_RE.fullmatch(
            str(semantic_sha or "")
        ) or not SHA256_RE.fullmatch(str(matched_sha or "")):
            raise CampaignSafetyError("G8 SMB report authority hashes are missing")
        return {
            "g8_smb_report": str(resolved),
            "g8_smb_report_sha256": expected,
            "g8_smb_evidence_sha256": evidence_sha,
            "g8_smb_plan_semantic_sha256": semantic_sha,
            "g8_matched_performance_sha256": matched_sha,
        }

    @property
    def _harness(self) -> dict[str, Any]:
        return self.config["offline_campaign"]["harness_certification"]

    @property
    def harness_certified(self) -> bool:
        return self._harness["status"] == "certified" and not self._harness["arming_blockers"]

    @property
    def armed(self) -> bool:
        return self.config.get("armed") is True

    def _verify_python_runtime(self) -> None:
        runtime = self.python_runtime
        runtime_realpath = self.python_runtime_realpath
        runtime_manifest = self.python_runtime_manifest
        runtime_manifest_sha256 = self.python_runtime_manifest_sha256
        runtime_tree_sha256 = self.python_runtime_tree_sha256
        cron_jobs = self.cron_jobs_file
        cron_jobs_sha256 = self.cron_jobs_sha256
        cron_jobs_source = self.cron_jobs_source_file
        cron_jobs_source_sha256 = self.cron_jobs_source_sha256
        expected = self.python_runtime_sha256
        if (
            runtime is None
            or not runtime.is_absolute()
            or runtime_realpath is None
            or not runtime_realpath.is_absolute()
            or runtime_manifest is None
            or not runtime_manifest.is_absolute()
            or cron_jobs is None
            or not cron_jobs.is_absolute()
            or cron_jobs_source is None
            or not cron_jobs_source.is_absolute()
            or any(
                not SHA256_RE.fullmatch(value or "")
                for value in (
                    expected,
                    runtime_manifest_sha256,
                    runtime_tree_sha256,
                    cron_jobs_sha256,
                    cron_jobs_source_sha256,
                )
            )
        ):
            raise CampaignSafetyError(
                "absolute hash-bound Python tree and cron definitions are required"
            )
        try:
            before = runtime.lstat()
            observed_realpath = runtime.resolve(strict=True)
            real_before = runtime_realpath.lstat()
            actual = _sha256(runtime_realpath)
            manifest_before = runtime_manifest.lstat()
            manifest_hash = _sha256(runtime_manifest)
            manifest_after = runtime_manifest.lstat()
            cron_before = cron_jobs.lstat()
            cron_hash = _sha256(cron_jobs)
            cron_after = cron_jobs.lstat()
            cron_source_before = cron_jobs_source.lstat()
            cron_source_hash = _sha256(cron_jobs_source)
            cron_source_after = cron_jobs_source.lstat()
            after = runtime.lstat()
        except OSError as exc:
            raise CampaignSafetyError(f"bound Python runtime is unreadable: {exc}") from exc
        if (
            not (stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode))
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or stat.S_IMODE(before.st_mode) != stat.S_IMODE(after.st_mode)
            or stat.S_IMODE(before.st_mode) & 0o111 == 0
            or observed_realpath != runtime_realpath
            or runtime_realpath.is_symlink()
            or not stat.S_ISREG(real_before.st_mode)
            or actual != expected
        ):
            raise CampaignSafetyError("bound Python runtime is missing, mutable, or hash-mismatched")
        signature = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            stat.S_IMODE(value.st_mode),
        )
        if (
            runtime_manifest.is_symlink()
            or not stat.S_ISREG(manifest_before.st_mode)
            or signature(manifest_before) != signature(manifest_after)
            or manifest_hash != runtime_manifest_sha256
            or cron_jobs.is_symlink()
            or not stat.S_ISREG(cron_before.st_mode)
            or signature(cron_before) != signature(cron_after)
            or cron_hash != cron_jobs_sha256
            or cron_jobs_source.is_symlink()
            or not stat.S_ISREG(cron_source_before.st_mode)
            or signature(cron_source_before) != signature(cron_source_after)
            or cron_source_hash != cron_jobs_source_sha256
        ):
            raise CampaignSafetyError("runtime manifest or cron binding drift detected")
        try:
            manifest_payload = json.loads(runtime_manifest.read_text(encoding="utf-8"))
            runtime_report = verify_runtime_manifest(runtime_manifest)
            cron_payload = json.loads(cron_jobs.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, PythonRuntimeBlocked) as exc:
            raise CampaignSafetyError(f"runtime tree or cron verification failed: {exc}") from exc
        if (
            not isinstance(manifest_payload, dict)
            or manifest_payload.get("python_runtime") != str(runtime)
            or manifest_payload.get("python_runtime_realpath") != str(runtime_realpath)
            or manifest_payload.get("python_runtime_sha256") != expected
            or runtime_report.get("tree_sha256") != runtime_tree_sha256
            or not isinstance(cron_payload, list)
        ):
            raise CampaignSafetyError("runtime tree or cron identity binding mismatch")
        try:
            probe = subprocess.run(
                [
                    str(runtime),
                    "-B",
                    "-X",
                    "pycache_prefix=/dev/null",
                    "-I",
                    "-S",
                    "-c",
                    "import sys;print('MAGI_V3_PYTHON_OK:'+str(sys.version_info.major))",
                ],
                cwd=self.release_root,
                env={
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "HOME": str(self.state_dir),
                    "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPYCACHEPREFIX": "/dev/null",
                },
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CampaignSafetyError(f"bound Python runtime probe failed: {exc}") from exc
        if probe.returncode != 0 or probe.stdout.strip() != "MAGI_V3_PYTHON_OK:3":
            raise CampaignSafetyError("bound runtime did not identify as a Python 3 interpreter")

    def _route_runtime_binding(self) -> dict[str, Any]:
        self._verify_python_runtime()
        assert self.python_runtime is not None
        assert self.python_runtime_realpath is not None
        assert self.python_runtime_manifest is not None
        payload = json.loads(self.python_runtime_manifest.read_text(encoding="utf-8"))
        site_roots = _route_runtime_site_packages(payload)
        return {
            "certifying": True,
            "mode": "formal_manifest_bound",
            "python_runtime": str(self.python_runtime),
            "python_runtime_realpath": str(self.python_runtime_realpath),
            "python_runtime_sha256": self.python_runtime_sha256,
            "runtime_manifest": str(self.python_runtime_manifest),
            "runtime_manifest_sha256": self.python_runtime_manifest_sha256,
            "runtime_tree_sha256": self.python_runtime_tree_sha256,
            "runtime_root": str(Path(payload["runtime_root"]).resolve(strict=True)),
            "base_runtime_root": str(Path(payload["base_runtime_root"]).resolve(strict=True)),
            "pythonpath_roots": [str(self.release_root), *site_roots],
            "user_site_included": False,
            "parent_sys_path_inherited": False,
            "site_processing_disabled": True,
        }

    def _run_command(
        self,
        argv: Sequence[str],
        cwd: Path,
        validation_profile: dict[str, Any] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if self._injected_command_runner is not None:
            return self._injected_command_runner(argv, cwd)
        self._verify_python_runtime()
        self._last_route_runtime_binding = None
        if tuple(argv[1:]) == OFFLINE_COMMANDS["346_route_contract_replay"]:
            self._last_route_runtime_binding = self._route_runtime_binding()
        isolated_home = self.state_dir / "offline-home"
        canonical_runtime = (
            isolated_home
            / "Library"
            / "Application Support"
            / "MAGI"
            / "runtime"
            / "MAGI_v3"
        )
        workload_state = canonical_runtime / "state" / "offline-campaign"
        external_json = canonical_runtime / "shared" / "external"
        temporary = self.state_dir / "tmp"
        for directory in (
            isolated_home,
            canonical_runtime,
            workload_state,
            external_json,
            temporary,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(isolated_home),
            "TMPDIR": str(temporary),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": "/dev/null",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "MAGI_V3_OFFLINE_CERTIFICATION": "1",
            "MAGI_ENABLE_LIVE_TESTS": "0",
            "MAGI_V3_STATE_DIR": str(workload_state),
            "MAGI_V3_SHARED_STATE_DIR": str(canonical_runtime / "shared"),
            "MAGI_JSON_DIR": str(external_json),
            "MAGI_V3_RELEASE_ID": self.bundle.release_id,
            "MAGI_V3_RELEASE_MANIFEST": str(self.release_root / MANIFEST_NAME),
            "MAGI_V3_RELEASE_MANIFEST_SHA256": self.bundle.manifest_sha256,
            "MAGI_V3_PYTHON_RUNTIME": str(self.python_runtime),
            "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(self.python_runtime_realpath),
            "MAGI_V3_PYTHON_RUNTIME_SHA256": str(self.python_runtime_sha256),
            "MAGI_V3_PYTHON_RUNTIME_MANIFEST": str(self.python_runtime_manifest),
            "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256": str(
                self.python_runtime_manifest_sha256
            ),
            "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256": str(self.python_runtime_tree_sha256),
            "MAGI_V3_ROUTE_CERTIFYING": "1",
            "MAGI_CRON_JOBS_FILE": str(self.cron_jobs_file),
            "MAGI_CRON_JOBS_SHA256": str(self.cron_jobs_sha256),
            "MAGI_CRON_JOBS_SOURCE_FILE": str(self.cron_jobs_source_file),
            "MAGI_CRON_JOBS_SOURCE_SHA256": str(self.cron_jobs_source_sha256),
        }
        if tuple(argv[1:]) == OFFLINE_COMMANDS[
            "seven_day_schedule_10x_arrival_2x_duration_replay"
        ]:
            env["MAGI_V3_SCHEDULE_BODY_CACHE"] = str(
                temporary / "schedule-body-cache.json"
            )
        if validation_profile is not None:
            env.update(
                {
                    "MAGI_V3_VALIDATION_PROFILE_ID": str(
                        validation_profile["profile_id"]
                    ),
                    "MAGI_V3_REPLAY_START_LOCAL": str(
                        validation_profile["replay_start_local"]
                    ),
                    "MAGI_V3_FAULT_SEED": str(validation_profile["fault_seed"]),
                }
            )
        if (
            len(argv) == 3
            and Path(argv[1]).resolve(strict=False)
            == self.release_root
            / "scripts/v3_validation/release_quality_certification.py"
            and argv[2] == "--campaign-evidence"
        ):
            env.update(
                {
                    "MAGI_WEBSITE_ROOT": str(
                        self._website_admin_binding["website_root"]
                    ),
                    "MAGI_WEBSITE_ADMIN_SHA256": str(
                        self._website_admin_binding["website_admin_sha256"]
                    ),
                }
            )
        if (
            len(argv) == 3
            and Path(argv[1]).resolve(strict=False)
            == self.release_root
            / "scripts/v3_validation/resource_performance_certification.py"
            and argv[2] == "--campaign-evidence"
            and self._g8_smb_binding["g8_smb_report"] is not None
        ):
            env.update(
                {
                    "MAGI_V3_G8_SMB_REPORT": str(
                        self._g8_smb_binding["g8_smb_report"]
                    ),
                    "MAGI_V3_G8_SMB_REPORT_SHA256": str(
                        self._g8_smb_binding["g8_smb_report_sha256"]
                    ),
                }
            )
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )

    def _verify_bundle_stable(self) -> None:
        current = verify_release_bundle(self.release_root, self.context.release_sha)
        if current != self.bundle:
            raise CampaignSafetyError("release bundle binding changed during campaign")
        if _sha256(self.config_path) != self.campaign_config_sha256:
            raise CampaignSafetyError("campaign config changed during campaign")
        if _sha256(self.gate_config_path) != self.context.gate_config_sha256:
            raise CampaignSafetyError("gate config changed during campaign")
        if self._verify_g8_smb_binding() != self._g8_smb_binding:
            raise CampaignSafetyError("G8 SMB report binding changed during campaign")
        if self._verify_website_admin_binding(
            required=self.execution_certifiable
        ) != self._website_admin_binding:
            raise CampaignSafetyError(
                "external Website Admin binding changed during campaign"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS context (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS days (
                    local_date TEXT PRIMARY KEY, status TEXT NOT NULL,
                    artifact_path TEXT, artifact_sha256 TEXT, observed_at TEXT NOT NULL,
                    CHECK(status IN ('running','offline_passed','offline_failed'))
                );
                """
            )
            payload = json.dumps(
                {
                    **self.context.to_dict(),
                    **self.bundle.binding(),
                    "campaign_config_sha256": self.campaign_config_sha256,
                    **self._execution_binding(),
                },
                sort_keys=True,
            )
            row = db.execute("SELECT payload FROM context WHERE singleton=1").fetchone()
            if row is None:
                db.execute("INSERT INTO context(singleton,payload) VALUES(1,?)", (payload,))
            elif row["payload"] != payload:
                raise CampaignSafetyError(
                    "ledger context does not match campaign/release/hardware/gate binding"
                )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise CampaignSafetyError("campaign clock must be timezone-aware")
        return value.astimezone(self.timezone)

    def _artifact_binding(self) -> dict[str, Any]:
        return {
            **self.context.to_dict(),
            **self.bundle.binding(),
            "campaign_config_sha256": self.campaign_config_sha256,
            **self._execution_binding(),
        }

    def _days(self) -> list[sqlite3.Row]:
        with self._connect() as db:
            rows = list(db.execute("SELECT * FROM days ORDER BY local_date"))
        for row in rows:
            if row["status"] == "running":
                continue
            relative = Path(str(row["artifact_path"] or ""))
            if relative.is_absolute() or ".." in relative.parts:
                raise CampaignSafetyError("ledger artifact path escapes state directory")
            raw_artifact = self.state_dir / relative
            if raw_artifact.is_symlink():
                raise CampaignSafetyError("ledger artifact must not be a symlink")
            artifact = raw_artifact.resolve()
            try:
                artifact.relative_to(self.state_dir)
            except ValueError as exc:
                raise CampaignSafetyError("ledger artifact path escapes state directory") from exc
            if not artifact.is_file() or artifact.is_symlink() or _sha256(artifact) != row[
                "artifact_sha256"
            ]:
                raise CampaignSafetyError(
                    f"ledger artifact missing or SHA-256 mismatch: {row['local_date']}"
                )
            try:
                payload = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CampaignSafetyError(f"ledger artifact unreadable: {row['local_date']}") from exc
            if (
                any(payload.get(key) != value for key, value in self._artifact_binding().items())
                or payload.get("local_date") != row["local_date"]
                or payload.get("status") != row["status"]
            ):
                raise CampaignSafetyError(f"ledger artifact binding mismatch: {row['local_date']}")
        return rows

    @contextmanager
    def _exclusive_run(self):
        lock_path = self.state_dir / "campaign.lock"
        with lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CampaignSafetyError("another campaign run holds the ledger lock") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _claim(self, local_date: str, observed_at: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO days(local_date,status,artifact_path,artifact_sha256,observed_at) "
                "VALUES(?,'running',NULL,NULL,?)",
                (local_date, observed_at),
            )

    def _record(
        self, local_date: str, status: str, artifact: Path, digest: str, observed_at: str
    ) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE days SET status=?,artifact_path=?,artifact_sha256=?,observed_at=? "
                "WHERE local_date=? AND status='running'",
                (status, str(artifact.relative_to(self.state_dir)), digest, observed_at, local_date),
            )
            if db.execute("SELECT changes()").fetchone()[0] != 1:
                raise CampaignSafetyError("campaign day was not exclusively claimed")

    def _write_artifact(self, artifact: Path, evidence: dict[str, Any]) -> str:
        temporary = artifact.with_suffix(".json.tmp")
        data = _canonical_bytes(evidence)
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, artifact)
            directory_fd = os.open(self.artifact_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return hashlib.sha256(data).hexdigest()

    def _write_report(self, report: dict[str, Any]) -> None:
        """Atomically publish the latest machine-readable campaign decision."""

        target = self.state_dir / "campaign-report.json"
        if target.is_symlink():
            raise CampaignSafetyError("campaign report target must not be a symlink")
        temporary = self.state_dir / (
            f".campaign-report.json.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        )
        data = _canonical_bytes(report)
        replaced = False
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            replaced = True
            directory_fd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            if replaced:
                target.unlink(missing_ok=True)
                directory_fd = os.open(self.state_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def _invalidate_report(self) -> None:
        """Remove any previous decision before starting a new validation pass."""

        target = self.state_dir / "campaign-report.json"
        if target.is_symlink():
            raise CampaignSafetyError("campaign report target must not be a symlink")
        if target.exists():
            target.unlink()
            directory_fd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _fail_closed_report(self, reason: str, error: Exception | None = None) -> dict[str, Any]:
        report = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).astimezone(self.timezone).isoformat(),
            **self._artifact_binding(),
            "decision": "NO_GO",
            "decision_scope": "offline_campaign_only",
            "fail_closed": True,
            "evidence_class": "immutable_release_offline_campaign",
            "certifying": False,
            "armed": self.armed,
            "harness_certified": self.harness_certified,
            "offline_complete": False,
            "passed_days": [],
            "artifacts": [],
            "required_consecutive_taipei_days": self.config["offline_campaign"][
                "minimum_consecutive_days"
            ],
            "required_independent_passes": self.config["offline_campaign"][
                "required_independent_passes"
            ],
            "ran_today": False,
            "no_go_reasons": [reason],
            "arming_blockers": self._harness["arming_blockers"],
            "live_execution_performed": False,
            "cutover_execution_performed": False,
            "release_gate_evidence_created": False,
        }
        if error is not None:
            report["safety_error"] = f"{type(error).__name__}: {error}"
        return report

    @staticmethod
    def _dates_are_consecutive(values: list[str]) -> bool:
        parsed = [date.fromisoformat(value) for value in values]
        return all(current == previous + timedelta(days=1) for previous, current in zip(parsed, parsed[1:]))

    def _report(self, *, ran_today: bool, reasons: list[str]) -> dict[str, Any]:
        rows = self._days()
        passed = [row["local_date"] for row in rows if row["status"] == "offline_passed"]
        required = self.config["offline_campaign"]["minimum_consecutive_days"]
        sequence_passed = (
            len(rows) >= required
            and all(row["status"] == "offline_passed" for row in rows)
            and self._dates_are_consecutive([row["local_date"] for row in rows])
        )
        complete = (
            sequence_passed
            and self.armed
            and self.harness_certified
            and self.execution_certifiable
        )
        if not self.armed:
            reasons.append("campaign_unarmed")
        if not self.harness_certified:
            reasons.append("harness_not_certified")
        if not self.execution_certifiable:
            reasons.append("execution_backend_not_certifiable")
        if not sequence_passed:
            reasons.append("accelerated_validation_day_not_complete")
        decision = "GO" if complete else "NO_GO"
        return {
            "schema_version": 1,
            "generated_at": self._now().isoformat(),
            **self._artifact_binding(),
            "decision": decision,
            "decision_scope": "offline_campaign_only",
            "fail_closed": decision != "GO",
            "evidence_class": "immutable_release_offline_campaign",
            "certifying": self.armed and self.harness_certified and self.execution_certifiable,
            "armed": self.armed,
            "harness_certified": self.harness_certified,
            "offline_complete": complete,
            "passed_days": passed,
            "artifacts": [
                {
                    "local_date": row["local_date"],
                    "status": row["status"],
                    "path": row["artifact_path"],
                    "sha256": row["artifact_sha256"],
                }
                for row in rows
                if row["artifact_path"] and row["artifact_sha256"]
            ],
            "required_consecutive_taipei_days": required,
            "required_independent_passes": self.config["offline_campaign"][
                "required_independent_passes"
            ],
            "ran_today": ran_today,
            "no_go_reasons": [] if complete else list(dict.fromkeys(reasons)),
            "arming_blockers": self._harness["arming_blockers"],
            "live_execution_performed": False,
            "cutover_execution_performed": False,
            "release_gate_evidence_created": False,
        }

    def _command_for(self, workload: str) -> tuple[str, ...]:
        suffix = OFFLINE_COMMANDS[workload]
        expanded: list[str] = []
        for argument in suffix:
            if not argument.startswith(("tests/", "scripts/")):
                expanded.append(argument)
                continue
            target = (self.release_root / argument).resolve(strict=True)
            try:
                target.relative_to(self.release_root)
            except ValueError as exc:
                raise CampaignSafetyError("offline test target escapes release bundle") from exc
            expanded.append(str(target))
        return (
            str(self.release_root / "bin" / "magi-v3-python"),
            *expanded,
        )

    def _validate_argv(self, workload: str, argv: Sequence[str]) -> None:
        if tuple(argv) != self._command_for(workload):
            raise CampaignSafetyError(f"command does not match release offline allowlist: {workload}")
        executable = Path(argv[0]).resolve(strict=True)
        targets = [
            Path(argument).resolve(strict=True)
            for argument in argv[1:]
            if Path(argument).is_absolute()
        ]
        for path in (executable, *targets):
            try:
                path.relative_to(self.release_root)
            except ValueError as exc:
                raise CampaignSafetyError("offline command path escapes release bundle") from exc
        if executable.name in FORBIDDEN_EXECUTABLES:
            raise CampaignSafetyError("service mutation command forbidden")
        text = " ".join(argv)
        if any(re.search(rf"(?<!\d){port}(?!\d)", text) for port in FORBIDDEN_PORTS):
            raise CampaignSafetyError("production/service port argument forbidden")

    def run_today(self) -> dict[str, Any]:
        with self._exclusive_run():
            self._invalidate_report()
            self._write_report(self._fail_closed_report("validation_in_progress"))
            try:
                report = self._run_today_locked()
                self._write_report(report)
                return report
            except Exception as exc:
                self._write_report(self._fail_closed_report("campaign_safety_error", exc))
                raise

    def _run_today_locked(self) -> dict[str, Any]:
        self._verify_bundle_stable()
        now = self._now()
        started_monotonic = time.monotonic()
        maximum_elapsed_seconds = (
            self.config["offline_campaign"]["maximum_completion_hours"] * 3600
        )
        today = now.date().isoformat()
        rows = self._days()
        if rows:
            required = self.config["offline_campaign"]["minimum_consecutive_days"]
            if (
                len(rows) >= required
                and all(row["status"] == "offline_passed" for row in rows)
                and self._dates_are_consecutive([row["local_date"] for row in rows])
            ):
                return self._report(ran_today=False, reasons=["offline_campaign_already_complete"])
            last = rows[-1]
            if today < last["local_date"]:
                raise CampaignSafetyError("campaign clock moved before the last ledger date")
            if today == last["local_date"]:
                reasons = ["daily_run_already_recorded"]
                if last["status"] != "offline_passed":
                    reasons.append("offline_day_failed")
                return self._report(ran_today=False, reasons=reasons)
            expected = (date.fromisoformat(last["local_date"]) + timedelta(days=1)).isoformat()
            if today != expected:
                artifact = self.artifact_dir / f"day-{today}.json"
                evidence = {
                    "schema_version": 1,
                    **self._artifact_binding(),
                    "evidence_class": "immutable_release_offline_campaign",
                    "certifying": (
                        self.armed and self.harness_certified and self.execution_certifiable
                    ),
                    "release_gate_eligible": False,
                    "local_date": today,
                    "status": "offline_failed",
                    "reason": f"date_gap_after_{last['local_date']}",
                    "workloads": [],
                    "live_execution_performed": False,
                }
                self._claim(today, now.isoformat())
                digest = self._write_artifact(artifact, evidence)
                self._record(today, "offline_failed", artifact, digest, now.isoformat())
                return self._report(ran_today=False, reasons=["campaign_date_interrupted"])
            if any(row["status"] != "offline_passed" for row in rows):
                return self._report(ran_today=False, reasons=["prior_offline_day_failed"])

        configured = self.config["offline_campaign"]["workloads"]
        if (
            len(configured) != len(set(configured))
            or any(not isinstance(item, str) or item not in OFFLINE_COMMANDS for item in configured)
        ):
            raise CampaignSafetyError(
                "campaign contains duplicate or non-allowlisted offline workload"
            )
        self._claim(today, now.isoformat())
        outcomes: list[dict[str, Any]] = []
        status = "offline_passed"
        release_error: str | None = None
        required_passes = self.config["offline_campaign"]["required_independent_passes"]
        validation_profiles = self.config["offline_campaign"]["validation_pass_profiles"]
        completed_passes = 0
        stop_campaign = False
        for validation_pass in range(1, required_passes + 1):
            validation_profile = validation_profiles[validation_pass - 1]
            pass_failed = False
            for index, workload in enumerate(configured):
                if time.monotonic() - started_monotonic > maximum_elapsed_seconds:
                    status = "offline_failed"
                    pass_failed = True
                    stop_campaign = True
                    outcomes.append(
                        {
                            "validation_pass": validation_pass,
                            "validation_profile": validation_profile,
                            "workload": workload,
                            "status": "offline_failed",
                            "reason": "accelerated_campaign_exceeded_24h_window",
                        }
                    )
                    break
                argv = self._command_for(workload)
                self._validate_argv(workload, argv)
                try:
                    self._last_route_runtime_binding = None
                    result = self._run_command(
                        argv, self.release_root, validation_profile
                    )
                    returncode = int(result.returncode)
                    stdout = result.stdout if isinstance(result.stdout, str) else ""
                    stderr = result.stderr if isinstance(result.stderr, str) else ""
                except Exception as exc:
                    returncode, stdout, stderr = -1, "", f"{type(exc).__name__}: {exc}"
                structured_evidence: dict[str, Any] | None = None
                structured_rejection_reason: str | None = None
                if returncode == 0:
                    try:
                        structured_evidence = _structured_workload_evidence(
                            workload,
                            stdout,
                            validation_profile if self.execution_certifiable else None,
                            self.bundle,
                            self.python_runtime_sha256 if self.execution_certifiable else None,
                            self._last_route_runtime_binding
                            if self.execution_certifiable
                            and workload == "346_route_contract_replay"
                            else None,
                        )
                    except CampaignSafetyError as exc:
                        returncode = 65
                        structured_rejection_reason = str(exc)
                        stderr = f"{stderr}\nstructured evidence rejected: {exc}".strip()
                outcome = {
                    "validation_pass": validation_pass,
                    "validation_profile": validation_profile,
                    "workload": workload,
                    "argv": list(argv),
                    "cwd": str(self.release_root),
                    "returncode": returncode,
                    "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
                    "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
                    "status": "offline_passed" if returncode == 0 else "offline_failed",
                }
                if workload == "matched_v2_v3_performance":
                    outcome["g8_smb_binding"] = dict(self._g8_smb_binding)
                if structured_evidence is not None:
                    outcome["structured_evidence"] = structured_evidence
                    if workload == "346_route_contract_replay":
                        route_report = self.artifact_dir / (
                            f"route-certification-{today}-pass-{validation_pass}.json"
                        )
                        route_report_sha256 = self._write_artifact(
                            route_report, structured_evidence
                        )
                        outcome["structured_evidence_artifact"] = {
                            "path": str(route_report.relative_to(self.state_dir)),
                            "sha256": route_report_sha256,
                        }
                    elif workload == "seven_day_schedule_10x_arrival_2x_duration_replay":
                        capacity_report = self.artifact_dir / (
                            f"schedule-capacity-{today}-pass-{validation_pass}.json"
                        )
                        capacity_sha256 = self._write_artifact(
                            capacity_report, structured_evidence["report"]
                        )
                        outcome["inner_report_artifact"] = {
                            "path": str(capacity_report.relative_to(self.state_dir)),
                            "sha256": capacity_sha256,
                        }
                        body_report = self.artifact_dir / (
                            f"schedule-body-{today}-pass-{validation_pass}.json"
                        )
                        body_sha256 = self._write_artifact(
                            body_report, structured_evidence["body_evidence"]
                        )
                        outcome["body_evidence_artifact"] = {
                            "path": str(body_report.relative_to(self.state_dir)),
                            "sha256": body_sha256,
                        }
                    elif workload == "health_1000_model_free":
                        health_report = self.artifact_dir / (
                            f"health-certification-{today}-pass-{validation_pass}.json"
                        )
                        health_report_sha256 = self._write_artifact(
                            health_report, structured_evidence["report"]
                        )
                        outcome["inner_report_artifact"] = {
                            "path": str(health_report.relative_to(self.state_dir)),
                            "sha256": health_report_sha256,
                        }
                    elif workload == "fault_recovery_certification":
                        fault_report = self.artifact_dir / (
                            f"fault-certification-{today}-pass-{validation_pass}.json"
                        )
                        fault_report_sha256 = self._write_artifact(
                            fault_report, structured_evidence["report"]
                        )
                        outcome["inner_report_artifact"] = {
                            "path": str(fault_report.relative_to(self.state_dir)),
                            "sha256": fault_report_sha256,
                        }
                    elif workload == "golden_business_flows":
                        quality_report = self.artifact_dir / (
                            f"release-quality-{today}-pass-{validation_pass}.json"
                        )
                        quality_report_sha256 = self._write_artifact(
                            quality_report, structured_evidence["report"]
                        )
                        outcome["inner_report_artifact"] = {
                            "path": str(quality_report.relative_to(self.state_dir)),
                            "sha256": quality_report_sha256,
                        }
                    elif workload == "matched_v2_v3_performance":
                        partial_report = self.artifact_dir / (
                            f"resource-performance-{today}-pass-{validation_pass}.json"
                        )
                        partial_report_sha256 = self._write_artifact(
                            partial_report, structured_evidence["report"]
                        )
                        outcome["inner_report_artifact"] = {
                            "path": str(partial_report.relative_to(self.state_dir)),
                            "sha256": partial_report_sha256,
                        }
                if returncode != 0:
                    outcome["failure_category"] = (
                        "structured_evidence_rejected"
                        if structured_rejection_reason is not None
                        else "workload_process_failed"
                    )
                    outcome["failure_diagnostic"] = _failure_diagnostics(
                        stdout, stderr
                    )
                if structured_rejection_reason is not None:
                    outcome["failure_reason"] = structured_rejection_reason
                outcomes.append(outcome)
                try:
                    self._verify_bundle_stable()
                    if self._injected_command_runner is None:
                        self._verify_python_runtime()
                except CampaignSafetyError as exc:
                    release_error = str(exc)
                    status = "offline_failed"
                    pass_failed = True
                    stop_campaign = True
                    outcomes.append(
                        {
                            "validation_pass": validation_pass,
                            "workload": "release_bundle_postcheck",
                            "status": "offline_failed",
                            "reason": release_error,
                        }
                    )
                    outcomes.extend(
                        {
                            "validation_pass": validation_pass,
                            "workload": item,
                            "status": "skipped",
                            "reason": "release_changed_during_campaign",
                        }
                        for item in configured[index + 1 :]
                    )
                    break
                if returncode != 0:
                    status = "offline_failed"
                    pass_failed = True
                    stop_campaign = True
                    outcomes.extend(
                        {
                            "validation_pass": validation_pass,
                            "workload": item,
                            "status": "skipped",
                            "reason": "prior_workload_failed",
                        }
                        for item in configured[index + 1 :]
                    )
                    break
            if not pass_failed:
                completed_passes += 1
            if stop_campaign:
                break

        if release_error is None:
            try:
                self._verify_bundle_stable()
                if self._injected_command_runner is None:
                    self._verify_python_runtime()
            except CampaignSafetyError as exc:
                release_error = str(exc)
                status = "offline_failed"
                outcomes.append(
                    {
                        "workload": "release_bundle_postcheck",
                        "status": "offline_failed",
                        "reason": release_error,
                    }
                )

        artifact = self.artifact_dir / f"day-{today}.json"
        completed_at = self._now()
        elapsed_seconds = time.monotonic() - started_monotonic
        if elapsed_seconds > maximum_elapsed_seconds:
            status = "offline_failed"
        evidence = {
            "schema_version": 1,
            **self._artifact_binding(),
            "evidence_class": "immutable_release_offline_campaign",
            "certifying": self.armed and self.harness_certified and self.execution_certifiable,
            "release_gate_eligible": (
                status == "offline_passed"
                and self.armed
                and self.harness_certified
                and self.execution_certifiable
            ),
            "generated_at": completed_at.isoformat(),
            "started_at": now.isoformat(),
            "completed_at": completed_at.isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 6),
            "local_date": today,
            "status": status,
            "validation_strategy": self.config["offline_campaign"]["validation_strategy"],
            "maximum_completion_hours": self.config["offline_campaign"][
                "maximum_completion_hours"
            ],
            "required_independent_passes": required_passes,
            "completed_independent_passes": completed_passes,
            "workloads": outcomes,
            "live_execution_performed": False,
        }
        if release_error:
            evidence["release_integrity_error"] = release_error
        digest = self._write_artifact(artifact, evidence)
        self._record(today, status, artifact, digest, now.isoformat())
        reasons = [] if status == "offline_passed" else ["offline_day_failed_or_skipped"]
        if release_error:
            reasons.append("release_changed_during_campaign")
        return self._report(ran_today=True, reasons=reasons)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config-sha256", required=True)
    parser.add_argument("--python-runtime", type=Path)
    parser.add_argument("--python-runtime-sha256")
    parser.add_argument("--python-runtime-realpath", type=Path)
    parser.add_argument("--python-runtime-manifest", type=Path)
    parser.add_argument("--python-runtime-manifest-sha256")
    parser.add_argument("--python-runtime-tree-sha256")
    parser.add_argument("--cron-jobs-file", type=Path)
    parser.add_argument("--cron-jobs-sha256")
    parser.add_argument("--cron-jobs-source-file", type=Path)
    parser.add_argument("--cron-jobs-source-sha256")
    parser.add_argument("--g8-smb-report", type=Path)
    parser.add_argument("--g8-smb-report-sha256")
    parser.add_argument("--website-root", type=Path)
    parser.add_argument("--website-admin-sha256")
    args = parser.parse_args(argv)
    try:
        report = CampaignRunner(
            release_root=args.release_root,
            state_dir=args.state_dir,
            context=CampaignContext(
                args.campaign_id,
                args.release_sha,
                args.hardware_id,
                args.gate_config_sha256,
            ),
            python_runtime=args.python_runtime,
            python_runtime_sha256=args.python_runtime_sha256,
            python_runtime_realpath=args.python_runtime_realpath,
            python_runtime_manifest=args.python_runtime_manifest,
            python_runtime_manifest_sha256=args.python_runtime_manifest_sha256,
            python_runtime_tree_sha256=args.python_runtime_tree_sha256,
            cron_jobs_file=args.cron_jobs_file,
            cron_jobs_sha256=args.cron_jobs_sha256,
            cron_jobs_source_file=args.cron_jobs_source_file,
            cron_jobs_source_sha256=args.cron_jobs_source_sha256,
            g8_smb_report=args.g8_smb_report,
            g8_smb_report_sha256=args.g8_smb_report_sha256,
            website_root=args.website_root,
            website_admin_sha256=args.website_admin_sha256,
        ).run_today()
    except Exception as exc:
        report = {"decision": "NO_GO", "fail_closed": True, "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("decision") == "GO" else 2


if __name__ == "__main__":
    raise SystemExit(main())
