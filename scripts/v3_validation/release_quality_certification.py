#!/usr/bin/env python3
"""Run release-bound pytest and golden-flow producers for gates 3 through 7."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v3_validation.golden_flows import (
    run_operational_golden_flows,
    run_osc_file_golden_flow,
)
from scripts.v3_validation.release_quality_evidence import (
    GOLDEN_DEPENDENCY_PATHS,
    SCHEMA,
    WORKLOAD,
    canonical_bytes,
    sha256_json,
    summarize_report,
)
from scripts.v3_validation.route_certification import (
    RuntimeBinding,
    _pytest_pythonpath,
    runtime_binding_from_environment,
)
from scripts.v3_validation.side_effects import SIDE_EFFECT_CLASSES, evaluate_side_effect


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "config" / "v3_release_quality_suites.json"
FIXTURE = ROOT / "tests" / "v3" / "compat" / "behavior_fixtures" / "osc-file-content.json"
EVIDENCE_PREFIX = "MAGI_V3_OFFLINE_EVIDENCE="
SEATBELT_CHILD_ENV = "MAGI_V3_RELEASE_QUALITY_SEATBELT_CHILD"
V2_COMPAT_ENV_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "MAGI_ENABLE_LIVE_TESTS",
    "MAGI_V3_OFFLINE_CERTIFICATION",
    SEATBELT_CHILD_ENV,
    "PATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "TMPDIR",
)
V2_COMPAT_CRON_ENV = (
    "MAGI_CRON_JOBS_FILE",
    "MAGI_CRON_JOBS_SHA256",
    "MAGI_CRON_JOBS_SOURCE_SHA256",
)
FORMAL_PRODUCER_STATE_PATHS = {
    "MAGI_RUNTIME_DIR": "runtime",
    "MAGI_AGENT_DIR": "agent",
    "MAGI_METRICS_DIR": "metrics",
    "MAGI_EXPORTS_DIR": "exports",
    "MAGI_MUTABLE_STATIC_DIR": "static",
    "MAGI_LOG_DIR": "logs",
    "MAGI_OSC_FILE_SHARE_STORE": "osc-file-shares.json",
    "MAGI_OSC_FILE_SHARE_CACHE_DIR": "osc-share-cache",
    "MAGI_OSC_PREVIEW_CACHE_DIR": "paperclip-preview",
    "MAGI_OSC_UPLOAD_CACHE_DIR": "paperclip-uploads",
    "MAGI_SHARED_STATE_DIR": "shared",
    "MAGI_V3_SHARED_STATE_DIR": "shared",
    "MAGI_V3_STATE_DIR": "v3-state",
}
V3_FORMAL_ENV_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "MAGI_ENABLE_LIVE_TESTS",
    "MAGI_V3_FAULT_SEED",
    "MAGI_V3_OFFLINE_CERTIFICATION",
    "MAGI_V3_PYTHON_RUNTIME",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256",
    "MAGI_V3_PYTHON_RUNTIME_REALPATH",
    "MAGI_V3_PYTHON_RUNTIME_SHA256",
    "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256",
    "MAGI_V3_RELEASE_ID",
    "MAGI_V3_RELEASE_MANIFEST",
    "MAGI_V3_RELEASE_MANIFEST_SHA256",
    "MAGI_V3_REPLAY_START_LOCAL",
    "MAGI_V3_SERVICE_MANIFEST",
    "MAGI_V3_SERVICE_MANIFEST_SHA256",
    "MAGI_V3_VALIDATION_PROFILE_ID",
    "MAGI_WEBSITE_ADMIN_SHA256",
    "MAGI_WEBSITE_ROOT",
    "PATH",
    "TMPDIR",
)
V2_COMPAT_NODE_CANDIDATES = (
    Path("/opt/homebrew/bin/node"),
    Path("/usr/local/bin/node"),
    Path("/usr/bin/node"),
)


class ReleaseQualityCertificationError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReleaseQualityCertificationError(f"JSON object required: {path}")
    return value


def _quoted(path: Path) -> str:
    return json.dumps(str(path.resolve()))


def _isolated_roots(workspace: Path) -> tuple[Path, ...]:
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    live_root = (real_home / "Library/Application Support/MAGI").resolve()
    home = Path(os.environ.get("HOME", "")).resolve(strict=True)
    temporary = Path(os.environ.get("TMPDIR", "")).resolve(strict=True)
    resolved_workspace = workspace.resolve(strict=True)
    if home == real_home or home == live_root or home.is_relative_to(live_root):
        raise ReleaseQualityCertificationError("pytest HOME is not isolated from live MAGI")
    if not resolved_workspace.is_relative_to(temporary):
        raise ReleaseQualityCertificationError("pytest workspace is outside bound TMPDIR")
    return tuple(dict.fromkeys((resolved_workspace, temporary, home)))


def _live_mutable_read_roots(real_home: Path) -> tuple[Path, ...]:
    root = real_home / "Library/Application Support/MAGI"
    return tuple(
        root / name
        for name in (
            "backups",
            "bin",
            "deployments",
            "logs",
            "metrics",
            "retired",
            "rpc-bin",
            "runtime",
            "shared",
            "state",
            "omlx_watchdog.pid",
            "omlx_watchdog_state.json",
            "oomlx_switch_aborts.jsonl",
            "probe_counter",
            "resource_governor_switch.jsonl",
        )
    )


def _seatbelt_profile(workspace: Path) -> str:
    """Deny network and non-sandbox writes for every release pytest child."""

    sandbox_exec = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or sandbox_exec != "/usr/bin/sandbox-exec":
        raise ReleaseQualityCertificationError("macOS Seatbelt is unavailable")
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    live_root = real_home / "Library/Application Support/MAGI"
    protected = (
        *_live_mutable_read_roots(real_home),
        real_home / "Library/CloudStorage",
        real_home / "Library/Keychains",
        real_home / ".ssh",
        Path("/Volumes"),
        Path("/opt/homebrew/var/mysql"),
    )
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        '(allow file-write* (literal "/dev/null"))',
        *(f"(allow file-write* (subpath {_quoted(path)}))" for path in _isolated_roots(workspace)),
        *(f"(deny file-read* (literal {_quoted(path)}))" for path in protected),
        *(f"(deny file-read* (subpath {_quoted(path)}))" for path in protected),
        f"(deny file-write* (literal {_quoted(live_root)}))",
        f"(deny file-write* (subpath {_quoted(live_root)}))",
    ]
    return "".join(rules)


def _release_files() -> tuple[dict[str, Any], dict[str, str], str]:
    manifest_path = Path(
        os.environ.get("MAGI_V3_RELEASE_MANIFEST", ROOT / "release-manifest.json")
    ).resolve(strict=True)
    manifest = _load_object(manifest_path)
    expected_manifest_sha = os.environ.get("MAGI_V3_RELEASE_MANIFEST_SHA256", "")
    observed_manifest_sha = _sha256(manifest_path)
    if expected_manifest_sha and observed_manifest_sha != expected_manifest_sha:
        raise ReleaseQualityCertificationError("release manifest SHA-256 mismatch")
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise ReleaseQualityCertificationError("release manifest file inventory is missing")
    files = {
        str(row.get("path")): str(row.get("sha256"))
        for row in rows
        if isinstance(row, dict)
    }
    return manifest, files, observed_manifest_sha


def _transcript_run(
    targets: Sequence[str],
    workspace: Path,
    *,
    cwd: Path = ROOT,
    v2_compat: bool = False,
    v3_runtime_binding: RuntimeBinding | None = None,
) -> dict[str, Any]:
    transcript = workspace / f"pytest-{hashlib.sha256(canonical_bytes(list(targets))).hexdigest()[:12]}.json"
    env = dict(os.environ)
    if v2_compat:
        env = {name: env[name] for name in V2_COMPAT_ENV_ALLOWLIST if name in env}
    elif v3_runtime_binding is not None:
        if not v3_runtime_binding.certifying:
            raise ReleaseQualityCertificationError(
                "V3 pytest requires a manifest-bound Python runtime"
            )
        env = {name: env[name] for name in V3_FORMAL_ENV_ALLOWLIST if name in env}
        sandbox = workspace / "v3-test-state"
        env.update(
            {
                "HOME": str(workspace / "home"),
                "TMPDIR": str(workspace / "tmp"),
                "PYTHONPATH": _pytest_pythonpath(v3_runtime_binding),
                "MAGI_ENABLE_LIVE_TESTS": "0",
                "MAGI_ALLOW_CLOUD_MODELS": "0",
                "MAGI_ALLOW_INTERNET": "0",
                "MAGI_DISABLE_SERVER_STARTUP_HOOKS": "1",
                "MAGI_SKIP_IMPORT_PROBES": "1",
                "MAGI_V3_ROUTE_CERTIFYING": "1",
                "MAGI_V3_EXTERNAL_WRITES_ENABLED": "0",
                "MAGI_V3_NOTIFICATIONS_ENABLED": "0",
                "MAGI_V3_SCHEDULER_ENABLED": "0",
                "MAGI_RUNTIME_DIR": str(sandbox / "runtime"),
                "MAGI_AGENT_DIR": str(sandbox / "agent"),
                "MAGI_METRICS_DIR": str(sandbox / "metrics"),
                "MAGI_EXPORTS_DIR": str(sandbox / "exports"),
                "MAGI_MUTABLE_STATIC_DIR": str(sandbox / "static"),
                "MAGI_LOG_DIR": str(sandbox / "logs"),
                "MAGI_OSC_FILE_SHARE_STORE": str(sandbox / "osc-file-shares.json"),
                "MAGI_OSC_FILE_SHARE_CACHE_DIR": str(sandbox / "osc-share-cache"),
                "MAGI_OSC_PREVIEW_CACHE_DIR": str(sandbox / "paperclip-preview"),
                "MAGI_OSC_UPLOAD_CACHE_DIR": str(sandbox / "paperclip-uploads"),
                "MAGI_SHARED_STATE_DIR": str(sandbox / "shared"),
                "MAGI_V3_SHARED_STATE_DIR": str(sandbox / "shared"),
                "MAGI_V3_STATE_DIR": str(sandbox / "state"),
            }
        )
        for directory in (Path(env["HOME"]), Path(env["TMPDIR"]), sandbox):
            directory.mkdir(parents=True, exist_ok=True)
    else:
        env["PYTHONPATH"] = _pytest_pythonpath()
    env["MAGI_V3_PYTEST_TRANSCRIPT"] = str(transcript)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    pytest_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "scripts.v3_validation.pytest_transcript_plugin",
        *targets,
    ]
    command = (
        pytest_command
        if os.environ.get(SEATBELT_CHILD_ENV) == "1"
        else [
            "/usr/bin/sandbox-exec",
            "-p",
            _seatbelt_profile(workspace),
            "--",
            *pytest_command,
        ]
    )
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    if not transcript.is_file():
        raise ReleaseQualityCertificationError(
            f"pytest transcript missing (rc={result.returncode}, stderr_sha256="
            f"{hashlib.sha256(result.stderr.encode()).hexdigest()})"
        )
    payload = _load_object(transcript)
    if result.returncode != payload.get("pytest_exitstatus"):
        raise ReleaseQualityCertificationError("pytest process/transcript exit status drifted")
    return payload


def _verified_v2_compat_mirror(
    workspace: Path,
    release_files: Mapping[str, str],
) -> Path:
    """Copy the hash-bound release snapshot into a disposable V2 layout.

    Legacy V2 regression tests intentionally exercise writable in-tree defaults.
    They cannot truthfully run from the immutable V3 root or with sealed-release
    environment variables.  The mirror contains only manifest-bound release
    files, remains inside the write/network Seatbelt, and is verified again after
    pytest so a test cannot alter functional source without invalidating evidence.
    """

    mirror = workspace / "v2-compat-root"
    mirror.mkdir(mode=0o755)
    for relative, expected_sha in sorted(release_files.items()):
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ReleaseQualityCertificationError("release manifest path is unsafe")
        source = ROOT.joinpath(*path.parts)
        destination = mirror.joinpath(*path.parts)
        if (
            source.is_symlink()
            or not source.is_file()
            or _sha256(source) != expected_sha
        ):
            raise ReleaseQualityCertificationError(
                f"release file changed before V2 compatibility mirror: {relative}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
    _verify_v2_compat_mirror(mirror, release_files)
    return mirror


def _verify_v2_compat_mirror(
    mirror: Path,
    release_files: Mapping[str, str],
) -> None:
    for relative, expected_sha in sorted(release_files.items()):
        path = PurePosixPath(relative)
        candidate = mirror.joinpath(*path.parts)
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or _sha256(candidate) != expected_sha
        ):
            raise ReleaseQualityCertificationError(
                f"V2 compatibility mirror source drifted: {relative}"
            )


def _stage_v2_compat_cron(mirror: Path) -> dict[str, str]:
    """Copy one hash/policy-bound cron snapshot into the disposable V2 root.

    The legacy suite must see ``root/cron_jobs.json`` without inheriting the
    launcher's global binding: several tests intentionally replace their own
    repository root.  Keeping the binding out of pytest also prevents those
    fixtures from accidentally reading production schedule state.
    """

    values = tuple(str(os.environ.get(name) or "").strip() for name in V2_COMPAT_CRON_ENV)
    if not all(values):
        raise ReleaseQualityCertificationError("V2 compatibility cron binding is incomplete")
    raw = Path(values[0]).expanduser()
    if (
        not raw.is_absolute()
        or raw.is_symlink()
        or not raw.is_file()
        or raw.resolve(strict=True) != raw
        or _sha256(raw) != values[1]
        or len(values[2]) != 64
    ):
        raise ReleaseQualityCertificationError("V2 compatibility cron binding is unsafe")
    policy = _load_object(ROOT / "config/v3_schedule_dispatch_policy.json")
    if policy.get("cron_jobs_sha256") != values[2]:
        raise ReleaseQualityCertificationError("V2 compatibility cron source binding mismatched")
    target = mirror / "cron_jobs.json"
    if target.exists() or target.is_symlink():
        raise ReleaseQualityCertificationError("V2 compatibility cron target already exists")
    shutil.copyfile(raw, target)
    target.chmod(0o444)
    _verify_v2_compat_cron(target, values[1])
    node_runtime = _v2_compat_node_evidence()
    return {
        "cron_jobs_sha256": values[1],
        "cron_jobs_source_sha256": values[2],
        **node_runtime,
    }


def _stage_v3_website_admin(workspace: Path) -> tuple[Path, dict[str, Any]]:
    """Copy the hash-bound Website Admin source into the test sandbox.

    Formal V3 tests must never read the mutable LIVE runtime tree.  The control
    compatibility test only needs the verified handler source plus empty
    mutable directory placeholders, so stage exactly that input and bind it to
    the deployed SHA-256 before the Seatbelt-protected pytest run.
    """

    raw_root = Path(str(os.environ.get("MAGI_WEBSITE_ROOT") or "")).expanduser()
    expected_sha = str(os.environ.get("MAGI_WEBSITE_ADMIN_SHA256") or "").strip()
    if (
        not raw_root.is_absolute()
        or raw_root.is_symlink()
        or len(expected_sha) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha)
    ):
        raise ReleaseQualityCertificationError(
            "V3 Website Admin external binding is incomplete or unsafe"
        )
    try:
        source_root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise ReleaseQualityCertificationError(
            "V3 Website Admin external root is unavailable"
        ) from exc
    source = source_root / "admin" / "admin_server.py"
    if (
        source_root.is_symlink()
        or not source_root.is_dir()
        or source.is_symlink()
        or not source.is_file()
        or _sha256(source) != expected_sha
    ):
        raise ReleaseQualityCertificationError(
            "V3 Website Admin external source is not hash-bound"
        )

    staged_root = workspace / "v3-external" / "website"
    for relative in ("admin", "data", "assets"):
        (staged_root / relative).mkdir(parents=True, exist_ok=False)
    staged_source = staged_root / "admin" / "admin_server.py"
    shutil.copyfile(source, staged_source)
    staged_source.chmod(0o444)
    if staged_source.is_symlink() or _sha256(staged_source) != expected_sha:
        raise ReleaseQualityCertificationError(
            "staged V3 Website Admin source failed verification"
        )
    return staged_root, {
        "website_admin_sha256": expected_sha,
        "staged_inside_workspace": True,
        "live_mutable_source_read_by_pytest": False,
    }


def _verify_v2_compat_cron(target: Path, expected_sha: str) -> None:
    metadata = target.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or _sha256(target) != expected_sha
    ):
        raise ReleaseQualityCertificationError("V2 compatibility cron snapshot drifted")


def _v2_compat_node_evidence() -> dict[str, str]:
    for declared in V2_COMPAT_NODE_CANDIDATES:
        try:
            resolved = declared.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode) and os.access(resolved, os.X_OK):
            return {
                "node_runtime_sha256": _sha256(resolved),
                "node_runtime_realpath_sha256": hashlib.sha256(
                    str(resolved).encode()
                ).hexdigest(),
            }
    raise ReleaseQualityCertificationError("V2 compatibility Node runtime is unavailable")


def _paths_from_manifest(manifest: Mapping[str, Any], release_files: Mapping[str, str]) -> tuple[list[str], list[str]]:
    patterns = manifest["v2_regression"]["include_globs"]
    from fnmatch import fnmatch

    v2 = sorted(path for path in release_files if any(fnmatch(path, pattern) for pattern in patterns))
    v3 = sorted({path for rows in manifest["v3_suites"].values() for path in rows})
    if not v2 or any(path not in release_files for path in [*v2, *v3]):
        raise ReleaseQualityCertificationError("quality test selection is not release-bound")
    declared_quality: set[str] = set()
    for section_name in ("quality_contract_groups", "golden_sets"):
        section = manifest.get(section_name)
        if not isinstance(section, dict) or not section:
            raise ReleaseQualityCertificationError(
                f"{section_name} selection is missing"
            )
        for rows in section.values():
            if not isinstance(rows, list) or not rows or any(
                not isinstance(path, str) for path in rows
            ):
                raise ReleaseQualityCertificationError(
                    f"{section_name} selection is invalid"
                )
            declared_quality.update(rows)
    if not declared_quality <= set(v2):
        missing = sorted(declared_quality - set(v2))
        raise ReleaseQualityCertificationError(
            f"quality contract tests are absent from the release: {missing}"
        )
    return v2, v3


def _side_effect_snapshot() -> dict[str, Any]:
    def project(effect: str, *, phase: str = "offline_replay", sandboxed: bool = False) -> dict[str, bool]:
        decision = evaluate_side_effect(
            effect,
            phase=phase,
            sandboxed=sandboxed,
            allow_sandbox_writes=sandboxed,
        )
        return {"allowed": decision.allowed, "execute": decision.execute}

    return {
        "offline": {effect: project(effect) for effect in sorted(SIDE_EFFECT_CLASSES)},
        "isolated_live_default": {
            effect: project(effect, phase="isolated_live_validation")
            for effect in sorted(SIDE_EFFECT_CLASSES)
        },
        "isolated_live_explicit_sandbox": {
            effect: project(effect, phase="isolated_live_validation", sandboxed=True)
            for effect in ("local_draft", "reversible_write", "external_commit", "destructive")
        },
    }


def run_certification(workspace: Path) -> dict[str, Any]:
    if os.environ.get("MAGI_V3_OFFLINE_CERTIFICATION") != "1":
        raise ReleaseQualityCertificationError("offline certification guard is required")
    manifest_payload, release_files, release_manifest_sha = _release_files()
    expected_runtime_sha = os.environ.get("MAGI_V3_PYTHON_RUNTIME_SHA256", "")
    runtime = Path(sys.executable)
    runtime_realpath = runtime.resolve(strict=True)
    observed_runtime_sha = _sha256(runtime)
    if (
        len(expected_runtime_sha) != 64
        or observed_runtime_sha != expected_runtime_sha
        or _sha256(runtime_realpath) != expected_runtime_sha
        or runtime_realpath
        != Path(os.environ.get("MAGI_V3_PYTHON_RUNTIME_REALPATH", "")).resolve(strict=True)
    ):
        raise ReleaseQualityCertificationError(
            "certifier is not executing in the hash-bound release Python runtime"
        )
    previous_route_certifying = os.environ.get("MAGI_V3_ROUTE_CERTIFYING")
    os.environ["MAGI_V3_ROUTE_CERTIFYING"] = "1"
    try:
        v3_runtime_binding = runtime_binding_from_environment(runtime)
    except Exception as exc:
        raise ReleaseQualityCertificationError(
            f"formal V3 pytest runtime binding is invalid: {exc}"
        ) from exc
    finally:
        if previous_route_certifying is None:
            os.environ.pop("MAGI_V3_ROUTE_CERTIFYING", None)
        else:
            os.environ["MAGI_V3_ROUTE_CERTIFYING"] = previous_route_certifying
    if not v3_runtime_binding.certifying:
        raise ReleaseQualityCertificationError(
            "formal V3 pytest runtime binding is not certifying"
        )
    suite_manifest = _load_object(MANIFEST_PATH)
    dependency_hashes = {
        path: release_files.get(path) for path in GOLDEN_DEPENDENCY_PATHS
    }
    if any(
        digest is None or _sha256(ROOT / path) != digest
        for path, digest in dependency_hashes.items()
    ):
        raise ReleaseQualityCertificationError(
            "golden flow dependency differs from the release manifest"
        )
    v2_targets, v3_targets = _paths_from_manifest(suite_manifest, release_files)
    workspace.mkdir(parents=True, exist_ok=False)
    v2_root = _verified_v2_compat_mirror(workspace, release_files)
    v2_compat_inputs = _stage_v2_compat_cron(v2_root)
    v2_transcript = _transcript_run(
        v2_targets,
        workspace,
        cwd=v2_root,
        v2_compat=True,
    )
    _verify_v2_compat_mirror(v2_root, release_files)
    _verify_v2_compat_cron(v2_root / "cron_jobs.json", v2_compat_inputs["cron_jobs_sha256"])
    staged_website, v3_external_inputs = _stage_v3_website_admin(workspace)
    previous_website_root = os.environ.get("MAGI_WEBSITE_ROOT")
    os.environ["MAGI_WEBSITE_ROOT"] = str(staged_website)
    try:
        v3_transcript = _transcript_run(
            v3_targets,
            workspace,
            v3_runtime_binding=v3_runtime_binding,
        )
    finally:
        if previous_website_root is None:
            os.environ.pop("MAGI_WEBSITE_ROOT", None)
        else:
            os.environ["MAGI_WEBSITE_ROOT"] = previous_website_root
    flow_root = workspace / "golden-flows"
    flows = [
        run_osc_file_golden_flow(FIXTURE, flow_root / "osc-preview"),
        run_operational_golden_flows(flow_root / "operations"),
    ]
    observed_test_paths = sorted(
        {
            nodeid.split("::", 1)[0]
            for transcript in (v2_transcript, v3_transcript)
            for nodeid in transcript["collected_nodeids"]
        }
    )
    source_paths = {
        "certifier_script_sha256": "scripts/v3_validation/release_quality_certification.py",
        "evidence_module_sha256": "scripts/v3_validation/release_quality_evidence.py",
        "pytest_plugin_sha256": "scripts/v3_validation/pytest_transcript_plugin.py",
        "suite_manifest_sha256": "config/v3_release_quality_suites.json",
        "golden_flows_sha256": "scripts/v3_validation/golden_flows.py",
        "side_effects_sha256": "scripts/v3_validation/side_effects.py",
    }
    runtime_sha = expected_runtime_sha
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "certified",
        "workload": WORKLOAD,
        "validation_profile": {
            "profile_id": os.environ.get("MAGI_V3_VALIDATION_PROFILE_ID"),
            "replay_start_local": os.environ.get("MAGI_V3_REPLAY_START_LOCAL"),
            "fault_seed": int(os.environ.get("MAGI_V3_FAULT_SEED", "0")),
        },
        "release_binding": {
            "release_id": manifest_payload.get("release_id"),
            "release_manifest_sha256": release_manifest_sha,
            "python_runtime_sha256": runtime_sha,
            "python_runtime_observed_sha256": observed_runtime_sha,
            **{field: release_files.get(path) for field, path in source_paths.items()},
        },
        "test_source_sha256": {path: release_files.get(path) for path in observed_test_paths},
        "golden_dependency_sha256": dependency_hashes,
        "pytest_runs": {
            "v2_regression": v2_transcript,
            "v3_suites": v3_transcript,
        },
        "v2_compat_inputs": v2_compat_inputs,
        "v3_external_inputs": v3_external_inputs,
        "golden_flows": flows,
        "side_effect_snapshot": _side_effect_snapshot(),
        "safety": {
            "live_state_accessed": False,
            "production_service_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "external_writes": False,
            "network_denied_by_seatbelt": True,
            "writes_restricted_to_sandbox": True,
            "pytest_home_isolated": True,
        },
    }
    report["evidence_sha256"] = sha256_json(report)
    metrics = summarize_report(
        report,
        manifest=suite_manifest,
        release_files=release_files,
        python_runtime_sha256=runtime_sha,
        expected_profile=report["validation_profile"],
        expected_release_id=str(manifest_payload.get("release_id") or ""),
        expected_release_manifest_sha256=release_manifest_sha,
    )
    report["metrics"] = metrics
    report.pop("evidence_sha256")
    report["evidence_sha256"] = sha256_json(report)
    return report


def campaign_evidence(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workload": WORKLOAD,
        "status": "passed",
        "measurements": dict(report["metrics"]),
        "report": dict(report),
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
    }


def _run_seatbelt_child() -> int:
    """Re-exec the complete producer under one inherited Seatbelt profile."""

    temporary = Path(os.environ.get("TMPDIR", "")).resolve(strict=True)
    env = dict(os.environ)
    env[SEATBELT_CHILD_ENV] = "1"
    with tempfile.TemporaryDirectory(
        prefix="magi-v3-producer-state-", dir=temporary
    ) as producer_state_value:
        producer_state = Path(producer_state_value)
        staged_website, _staged_website_receipt = _stage_v3_website_admin(
            producer_state
        )
        env.update(
            {
                name: str(producer_state / relative)
                for name, relative in FORMAL_PRODUCER_STATE_PATHS.items()
            }
        )
        env["MAGI_WEBSITE_ROOT"] = str(staged_website)
        result = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-p",
                _seatbelt_profile(temporary),
                "--",
                sys.executable,
                str(Path(__file__).resolve(strict=True)),
                "--campaign-evidence",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=3590,
            check=False,
        )
    if result.returncode == 0:
        print(result.stdout.strip())
        return 0
    child_error = "unavailable"
    for line in reversed(result.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        error = payload.get("error") if isinstance(payload, dict) else None
        if payload.get("ok") is False and isinstance(error, str) and error.strip():
            child_error = error.strip()[:1000]
            break
    print(
        json.dumps(
            {
                "ok": False,
                "error": "seatbelt release quality child failed",
                "child_error": child_error,
                "returncode": result.returncode,
                "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-evidence", action="store_true")
    args = parser.parse_args(argv)
    if not args.campaign_evidence:
        print(json.dumps({"ok": False, "error": "--campaign-evidence is required"}, sort_keys=True))
        return 2
    if os.environ.get(SEATBELT_CHILD_ENV) != "1":
        try:
            return _run_seatbelt_child()
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return 2
    try:
        temporary_root = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
        with tempfile.TemporaryDirectory(prefix="magi-v3-release-quality-", dir=temporary_root) as temporary:
            report = run_certification(Path(temporary) / "work")
        payload = campaign_evidence(report)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 2
    print(EVIDENCE_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
