"""Create hash-bound cutover and rollback plans without touching services."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from scripts.v3_pdf_namer_handoff import HandoffError, verify_manifest

from .core import CutoverError
from .mutation import (
    ATOMIC_DRILL_EXCLUDED_EVIDENCE,
    EXECUTION_PURPOSES,
    READINESS_URLS,
    REQUIRED_V2_APPLICATION_LABELS,
    V3_LABELS,
    BoundFile,
    LaunchAgent,
    _initial_launchd_state,
    v2_application_set_sha256,
    v2_initial_loaded_set_sha256,
    v2_keepalive_set_sha256,
)
from .probe import HOST_SINGLETON_LAUNCHD_LABELS

LABEL_RE = re.compile(r"^com\.magi\.[A-Za-z0-9._-]+$")
LaunchdProbe = Callable[[str], Mapping[str, Any]]


def _default_launchd_probe(label: str) -> Mapping[str, Any]:
    argv = ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"]
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        raise CutoverError(f"V2 launchd state probe timed out: {label}") from exc
    return {
        "argv": argv,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": False,
    }


def _captured_launchd_state(label: str, probe: LaunchdProbe) -> dict[str, Any]:
    receipt = dict(probe(label))
    state_match = re.search(r"(?m)^\s*state = ([^\n]+)\s*$", str(receipt.get("stdout") or ""))
    pid_match = re.search(r"(?m)^\s*pid = (\d+)\s*$", str(receipt.get("stdout") or ""))
    loaded = receipt.get("returncode") == 0
    value = {
        "loaded": loaded,
        "state": state_match.group(1).strip() if loaded and state_match else "",
        "pid": int(pid_match.group(1)) if loaded and pid_match else None,
        "launchctl_receipt": receipt,
        "launchctl_receipt_sha256": hashlib.sha256(
            (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
    }
    _initial_launchd_state(value, label=label)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path: Path, description: str) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise CutoverError(f"{description} must be an absolute non-symlink file")
    try:
        result = raw.resolve(strict=True)
    except OSError as exc:
        raise CutoverError(f"{description} is unavailable: {exc}") from exc
    if not result.is_file():
        raise CutoverError(f"{description} must be a regular file")
    return result


def _object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CutoverError(f"{description} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise CutoverError(f"{description} must be a JSON object")
    return value


def _binding(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": _sha256(path)}


def _path_identity(path: Path) -> dict[str, str]:
    canonical = path.expanduser().resolve(strict=False)
    return {
        "path": str(canonical),
        "path_sha256": hashlib.sha256(str(canonical).encode()).hexdigest(),
    }


def _handoff_root(path: Path, description: str, *, existing: bool) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise CutoverError(f"{description} must be an absolute non-symlink path")
    result = raw.resolve(strict=existing)
    if existing and not result.is_dir():
        raise CutoverError(f"{description} must be a directory")
    if not existing and result.exists() and not result.is_dir():
        raise CutoverError(f"{description} must be a directory path")
    return result


def _private_binding(path: Path, description: str) -> dict[str, str]:
    private = _regular(path, description)
    metadata = private.stat()
    if (
        stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise CutoverError(f"{description} must be owner-only 0600 with one hard link")
    return _binding(private)


def _future_output(path: Path, description: str) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.exists() or raw.is_symlink():
        raise CutoverError(f"{description} must be a new absolute non-symlink path")
    parent = raw.parent
    if not parent.is_dir() or parent.is_symlink():
        raise CutoverError(f"{description} parent must be an existing non-symlink directory")
    return raw.resolve(strict=False)


def _token_digest(path: Path) -> str:
    token = _regular(path, "authorization token")
    metadata = token.stat()
    if (
        stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise CutoverError("authorization token must be owner-only 0600 with one hard link")
    payload = token.read_bytes()
    if len(payload) > 4096:
        raise CutoverError("authorization token is too large")
    payload = payload.rstrip(b"\r\n")
    if not payload:
        raise CutoverError("authorization token is empty")
    return hashlib.sha256(payload).hexdigest()


def _v2_agents(
    values: Iterable[str],
    *,
    canonical_directory: Path,
    launchd_probe: LaunchdProbe,
) -> tuple[list[dict[str, Any]], str, str, str]:
    result: list[dict[str, Any]] = []
    labels: set[str] = set()
    for value in values:
        label, separator, raw_path = value.partition("=")
        if not separator or not LABEL_RE.fullmatch(label):
            raise CutoverError(f"invalid V2 launchagent binding: {value}")
        if label in labels or label in V3_LABELS or label in HOST_SINGLETON_LAUNCHD_LABELS:
            raise CutoverError(f"unsafe or duplicate V2 launchagent label: {label}")
        path = _regular(Path(raw_path), f"V2 launchagent {label}")
        if path.parent != canonical_directory or path.name != f"{label}.plist":
            raise CutoverError(
                f"V2 launchagent must use its canonical plist path: {label}"
            )
        try:
            with path.open("rb") as handle:
                document = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException) as exc:
            raise CutoverError(f"V2 launchagent plist is unreadable: {label}: {exc}") from exc
        if not isinstance(document, dict) or document.get("Label") != label:
            raise CutoverError(f"V2 launchagent plist label mismatch: {label}")
        labels.add(label)
        keepalive_required = document.get("KeepAlive") is True
        result.append(
            {
                "label": label,
                "plist": _binding(path),
                "initial_launchd": _captured_launchd_state(label, launchd_probe),
                "keepalive_required_running": keepalive_required,
            }
        )
    ordered = sorted(result, key=lambda row: row["label"])
    if {str(row["label"]) for row in ordered} != set(REQUIRED_V2_APPLICATION_LABELS):
        missing = sorted(set(REQUIRED_V2_APPLICATION_LABELS) - {str(row["label"]) for row in ordered})
        extra = sorted({str(row["label"]) for row in ordered} - set(REQUIRED_V2_APPLICATION_LABELS))
        raise CutoverError(
            "V2 application launchagent set is incomplete or unexpected: "
            f"missing={missing}, extra={extra}"
        )
    agents = tuple(
        LaunchAgent(
            str(row["label"]),
            BoundFile(Path(str(row["plist"]["path"])), str(row["plist"]["sha256"])),
            bool(row["initial_launchd"]["loaded"]),
            str(row["initial_launchd"]["state"]),
            bool(row["keepalive_required_running"]),
            str(row["initial_launchd"]["launchctl_receipt_sha256"]),
        )
        for row in ordered
    )
    return (
        ordered,
        v2_application_set_sha256(agents),
        v2_initial_loaded_set_sha256(agents),
        v2_keepalive_set_sha256(agents),
    )


def create_prepared_plan(
    *,
    operation: str,
    execution_purpose: str,
    output: Path,
    token_file: Path,
    gate_config: Path,
    pre_cutover_report: Path,
    deploy_prepared_marker: Path,
    release_manifest: Path,
    v2_launchagents: Iterable[str],
    v3_install_directory: Path,
    laf_dedup_sources: Iterable[Path] = (),
    laf_dedup_manifest_output: Path | None = None,
    laf_dedup_db_env_file: Path | None = None,
    pdf_namer_source: Path | None = None,
    pdf_namer_destination: Path | None = None,
    pdf_namer_manifest: Path | None = None,
    mutable_state_source_root: Path | None = None,
    mutable_state_target_shared_root: Path | None = None,
    mutable_state_dry_run_receipt: Path | None = None,
    mutable_state_prepare_receipt: Path | None = None,
    mutable_state_staging_root: Path | None = None,
    final_pre_cutover_report: Path | None = None,
    launchd_probe: LaunchdProbe = _default_launchd_probe,
) -> dict[str, Any]:
    """Persist a complete immutable-input plan and return its public identity."""

    operation_name = {
        "cutover": "v2_to_v3_cutover",
        "rollback": "v3_to_v2_rollback",
    }.get(operation)
    if operation_name is None:
        raise CutoverError(f"unsupported prepared plan operation: {operation}")
    if execution_purpose not in EXECUTION_PURPOSES:
        raise CutoverError("prepared plan execution purpose is invalid")
    dedup_sources = tuple(laf_dedup_sources)
    if operation_name == "v2_to_v3_cutover":
        if not dedup_sources or laf_dedup_manifest_output is None or laf_dedup_db_env_file is None:
            raise CutoverError(
                "cutover requires LAF dedup source, manifest output, and DB env file bindings"
            )
        if pdf_namer_source is None or pdf_namer_destination is None or pdf_namer_manifest is None:
            raise CutoverError(
                "cutover requires pdf-namer source, destination, and precopy manifest bindings"
            )
        mutable_values = (
            mutable_state_source_root,
            mutable_state_target_shared_root,
            mutable_state_dry_run_receipt,
            mutable_state_prepare_receipt,
            mutable_state_staging_root,
        )
        if any(value is not None for value in mutable_values) and not all(
            value is not None for value in mutable_values
        ):
            raise CutoverError("mutable-state handoff requires all five path bindings")
        if execution_purpose == "final_cutover" and not all(
            value is not None for value in mutable_values
        ):
            raise CutoverError(
                "final cutover requires the complete mutable-state handoff binding"
            )
    elif dedup_sources or laf_dedup_manifest_output is not None or laf_dedup_db_env_file is not None:
        raise CutoverError("rollback must not repeat the LAF dedup handoff")
    if operation_name == "v3_to_v2_rollback" and any(
        value is not None for value in (pdf_namer_source, pdf_namer_destination, pdf_namer_manifest)
    ):
        raise CutoverError("rollback must not repeat or remove the pdf-namer state handoff")
    if operation_name == "v3_to_v2_rollback" and any(
        value is not None
        for value in (
            mutable_state_source_root,
            mutable_state_target_shared_root,
            mutable_state_dry_run_receipt,
            mutable_state_prepare_receipt,
            mutable_state_staging_root,
        )
    ):
        raise CutoverError("rollback must preserve and must not repeat mutable-state handoff")
    gates = _regular(gate_config, "gate config")
    report = _regular(pre_cutover_report, "pre-cutover report")
    marker = _regular(deploy_prepared_marker, "deploy prepared marker")
    release = _regular(release_manifest, "release manifest")
    gate_digest = _sha256(gates)
    report_document = _object(report, "pre-cutover report")
    marker_document = _object(marker, "deploy prepared marker")
    release_document = _object(release, "release manifest")
    if report_document.get("gate_config_sha256") != gate_digest:
        raise CutoverError("pre-cutover report is not bound to the selected gate config")
    mutual_final_binding = (
        operation_name == "v2_to_v3_cutover"
        and execution_purpose == "final_cutover"
        and mutable_state_source_root is not None
    )
    if mutual_final_binding:
        # A final pre-cutover report must attest to the exact plan SHA, while
        # the plan must also name the report that execution will consume.  Use
        # the already-safe atomic-drill report only to authorize plan creation;
        # it can never authorize final execution.  The final report is a fresh
        # future path and becomes usable only after it binds this plan's hash.
        expected_report_purpose = "atomic_drill"
        expected_stage = "cutover_drill_26_of_28"
        expected_decision = "GO_FOR_CUTOVER_DRILL_ONLY"
        expected_excluded = list(ATOMIC_DRILL_EXCLUDED_EVIDENCE)
        expected_passed = 26
        if final_pre_cutover_report is None:
            raise CutoverError(
                "final cutover requires a fresh final pre-cutover report path"
            )
        final_report = _future_output(
            final_pre_cutover_report, "final pre-cutover report"
        )
    else:
        expected_report_purpose = execution_purpose
        expected_stage = (
            "cutover_drill_26_of_28"
            if execution_purpose == "atomic_drill"
            else "final_cutover_28_of_28"
        )
        expected_decision = (
            "GO_FOR_CUTOVER_DRILL_ONLY"
            if execution_purpose == "atomic_drill"
            else "GO"
        )
        expected_excluded = (
            list(ATOMIC_DRILL_EXCLUDED_EVIDENCE)
            if execution_purpose == "atomic_drill"
            else []
        )
        expected_passed = 26 if execution_purpose == "atomic_drill" else 28
        final_report = None
        if final_pre_cutover_report is not None:
            raise CutoverError(
                "final pre-cutover report path is valid only for a final mutable-state cutover"
            )
    if (
        report_document.get("execution_purpose") != expected_report_purpose
        or report_document.get("gate_stage") != expected_stage
        or report_document.get("decision") != expected_decision
        or report_document.get("required_evidence_count") != 28
        or report_document.get("passed_evidence_count") != expected_passed
        or report_document.get("excluded_evidence") != expected_excluded
        or not isinstance(report_document.get("release_gate_report"), dict)
    ):
        raise CutoverError("pre-cutover report stage/purpose is invalid")
    if (
        marker_document.get("release_manifest_sha256") != _sha256(release)
        or marker_document.get("release_id") != release_document.get("release_id")
    ):
        raise CutoverError("deploy marker is not bound to the selected release manifest")
    install = v3_install_directory.expanduser()
    if not install.is_absolute():
        raise CutoverError("V3 launchagent install directory must be absolute")
    install = install.resolve(strict=False)
    (
        agents,
        application_set_sha256,
        initial_loaded_set_sha256,
        keepalive_set_sha256,
    ) = _v2_agents(
        v2_launchagents,
        canonical_directory=install,
        launchd_probe=launchd_probe,
    )
    target = output.expanduser()
    if not target.is_absolute() or target.is_symlink() or target.exists():
        raise CutoverError("prepared plan output must be a new absolute non-symlink path")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "operation": operation_name,
        "execution_purpose": execution_purpose,
        "gate_config": _binding(gates),
        "pre_cutover_report": (
            _path_identity(final_report) if final_report is not None else _binding(report)
        ),
        "deploy_prepared_marker": _binding(marker),
        "release_manifest": _binding(release),
        "token_sha256": _token_digest(token_file),
        "v2_launchagents": agents,
        "v2_application_set_sha256": application_set_sha256,
        "v2_initial_loaded_set_sha256": initial_loaded_set_sha256,
        "v2_keepalive_set_sha256": keepalive_set_sha256,
        "v3_install_directory": str(install),
        "readiness_urls": sorted(READINESS_URLS),
    }
    if final_report is not None:
        payload["plan_preparation_report"] = _binding(report)
    if operation_name == "v2_to_v3_cutover":
        sources = sorted(
            {
                str(_regular(path, "LAF dedup source"))
                for path in dedup_sources
            }
        )
        if len(sources) != len(dedup_sources):
            raise CutoverError("LAF dedup source paths must be unique")
        payload["laf_dedup_handoff"] = {
            # Source contents remain mutable until V2 has stopped.  The plan
            # binds their canonical paths; the post-stop snapshot binds bytes.
            "source_paths": sources,
            "manifest_output": str(
                _future_output(laf_dedup_manifest_output, "LAF dedup manifest output")
            ),
            "db_env_file": _private_binding(laf_dedup_db_env_file, "LAF dedup DB env file"),
        }
        assert pdf_namer_source is not None
        assert pdf_namer_destination is not None
        assert pdf_namer_manifest is not None
        try:
            verified = verify_manifest(
                pdf_namer_manifest,
                source=pdf_namer_source,
                destination=pdf_namer_destination,
                allowed_statuses={"precopy_complete", "complete"},
            )
        except (HandoffError, OSError) as exc:
            raise CutoverError("pdf-namer precopy manifest is unavailable or invalid") from exc
        if verified.get("contains_business_payload") is not False:
            raise CutoverError("pdf-namer handoff evidence contains business payload")
        payload["pdf_namer_handoff"] = {
            # The manifest is intentionally path-bound rather than hash-bound:
            # post-stop final refresh atomically advances it to `complete`.
            "source": str(pdf_namer_source.expanduser().resolve(strict=True)),
            "destination": str(pdf_namer_destination.expanduser().resolve(strict=False)),
            "manifest": str(pdf_namer_manifest.expanduser().resolve(strict=True)),
        }
        if mutable_state_source_root is not None:
            assert mutable_state_target_shared_root is not None
            assert mutable_state_dry_run_receipt is not None
            assert mutable_state_prepare_receipt is not None
            assert mutable_state_staging_root is not None
            source_root = _handoff_root(
                mutable_state_source_root, "mutable-state source root", existing=True
            )
            target_root = _handoff_root(
                mutable_state_target_shared_root,
                "mutable-state target shared root",
                existing=False,
            )
            dry_receipt = _future_output(
                mutable_state_dry_run_receipt, "mutable-state dry-run receipt"
            )
            prepare_receipt = _future_output(
                mutable_state_prepare_receipt, "mutable-state prepare receipt"
            )
            staging_root = _handoff_root(
                mutable_state_staging_root, "mutable-state staging root", existing=False
            )
            distinct_paths = {
                source_root,
                target_root,
                dry_receipt,
                prepare_receipt,
                staging_root,
            }
            if staging_root.exists() or len(distinct_paths) != 5:
                raise CutoverError("mutable-state handoff paths must be fresh and distinct")
            deployment_sha256 = marker_document.get("manifest_sha256")
            if not isinstance(deployment_sha256, str) or not re.fullmatch(
                r"[0-9a-f]{64}", deployment_sha256
            ):
                raise CutoverError("mutable-state handoff requires a deployment manifest hash")
            payload["mutable_state_handoff"] = {
                "source_root": _path_identity(source_root),
                "target_shared_root": _path_identity(target_root),
                "dry_run_receipt": _path_identity(dry_receipt),
                "prepare_receipt": _path_identity(prepare_receipt),
                "staging_root": _path_identity(staging_root),
                "exact_context": {
                    "release_id": str(release_document.get("release_id")),
                    "release_manifest_sha256": _sha256(release),
                    "deployment_manifest_sha256": deployment_sha256,
                },
            }
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "schema_version": 1,
        "operation": operation_name,
        "plan": str(target),
        "plan_sha256": _sha256(target),
        "mutation_performed": False,
    }
