#!/usr/bin/env python3
"""Compile fail-closed, release-bound success-path evidence for every V2 route.

This compiler combines the dedicated actual-handler harness with successful
Flask dispatches observed in explicitly reviewed pytest cases.  A test trace
is eligible only when pytest passed, the strict offline isolation guard saw no
attempt, the HTTP outcome is below 400, and the exact test node is pinned for
the exact route-method.  Validation guards and unreviewed incidental requests
therefore cannot clear the route gate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import site
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from magi_v3.compat.gateway import RouteInventory
from scripts.v3_campaign.runner import verify_release_bundle
from scripts.v3_python_runtime_snapshot import PythonRuntimeBlocked, verify_runtime_manifest
from scripts.v3_source_contract import account_home
from scripts.v3_validation.inventory import load_and_validate_runtime_inventory
from scripts.v3_validation.paths import ROUTE_METHOD_REVIEW_PATH
from scripts.v3_validation.route_reviews import RouteMethodKey, load_route_method_reviews
from scripts.v3_validation.route_reviews import ROUTE_METHOD_REVIEW_SUPPLEMENT_PATH
from scripts.v3_validation.schema import ContractValidationError


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = Path(__file__).resolve()
PLUGIN_PATH = Path(__file__).with_name("route_success_trace_plugin.py")
PROOF_MANIFEST_PATH = Path(__file__).with_name("route-success-proof-review.json")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EXTERNAL_STORAGE_ACCESS_EVENT = "external_storage_access"
SEATBELT_EXECUTABLE = Path("/usr/bin/sandbox-exec")
SEATBELT_PROFILE_NAME = "route-external-storage.sb"
BASE_ENVIRONMENT_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "MAGI_AGENT_DIR",
    "MAGI_ALLOW_CLOUD_MODELS",
    "MAGI_ALLOW_INTERNET",
    "MAGI_DISABLE_SERVER_STARTUP_HOOKS",
    "MAGI_DISCORD_LAST_CHANNEL_FILE",
    "MAGI_ENABLE_LIVE_TESTS",
    "MAGI_EXPORTS_DIR",
    "MAGI_LINE_LAST_SENDER_FILE",
    "MAGI_METRICS_DIR",
    "MAGI_OSC_FILE_SHARE_CACHE_DIR",
    "MAGI_OSC_FILE_SHARE_STORE",
    "MAGI_ROOT_DIR",
    "MAGI_RUNTIME_DIR",
    "MAGI_SKIP_IMPORT_PROBES",
    "MAGI_V3_OFFLINE_CERTIFICATION",
    "MAGI_WEB_RESEARCH_CACHE_DIR",
    "PATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPYCACHEPREFIX",
    "PYTHONNOUSERSITE",
    "PYTHONPATH",
    "PYTHONSAFEPATH",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "TMPDIR",
)
FORMAL_RUNTIME_ENVIRONMENT_KEYS = (
    "MAGI_V3_PYTHON_RUNTIME",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256",
    "MAGI_V3_PYTHON_RUNTIME_REALPATH",
    "MAGI_V3_PYTHON_RUNTIME_SHA256",
    "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256",
    "MAGI_V3_ROUTE_CERTIFYING",
)
TRACE_ENVIRONMENT_KEYS = tuple(
    sorted(
        {
            *BASE_ENVIRONMENT_KEYS,
            "MAGI_V3_ROUTE_TRACE_FILE",
            "MAGI_V3_ROUTE_TRACE_LIVE_ROOT",
            "MAGI_V3_ROUTE_TRACE_SANDBOX",
        }
    )
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _key_dict(key: RouteMethodKey) -> dict[str, str]:
    return {
        "service": key.service,
        "rule": key.rule,
        "method": key.method,
        "endpoint": key.endpoint,
    }


def _expected_external_storage_roots() -> tuple[Path, ...]:
    home = account_home()
    return (
        Path("/Volumes"),
        home / "Library" / "CloudStorage",
        home / ".magi_mounts",
        home / "SynologyDrive",
    )


def _live_magi_root() -> Path:
    return account_home() / "Library" / "Application Support" / "MAGI"


def _live_mutable_read_roots() -> tuple[Path, ...]:
    """Mutable LIVE data denied to certification children.

    ``releases`` and ``runtimes`` are immutable, manifest-bound inputs and are
    intentionally read-only.  Keeping them readable lets the formal runtime
    execute without granting access to operational state, logs, backups, or
    credentials.  Every write below the full LIVE root remains denied.
    """

    root = _live_magi_root()
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


def _seatbelt_profile_bytes(workspace: Path) -> bytes:
    workspace = Path(os.path.abspath(workspace.expanduser()))
    clauses: list[str] = []
    protected_reads = (*_expected_external_storage_roots(), *_live_mutable_read_roots())
    for root in protected_reads:
        encoded = json.dumps(str(root), ensure_ascii=False)
        clauses.extend(
            (
                f"(deny file-read* (literal {encoded}))",
                f"(deny file-read* (subpath {encoded}))",
                f"(deny file-write* (literal {encoded}))",
                f"(deny file-write* (subpath {encoded}))",
            )
        )
    live_encoded = json.dumps(str(_live_magi_root()), ensure_ascii=False)
    clauses.extend(
        (
            f"(deny file-write* (literal {live_encoded}))",
            f"(deny file-write* (subpath {live_encoded}))",
        )
    )
    workspace_encoded = json.dumps(str(workspace), ensure_ascii=False)
    return (
        "\n".join(
            (
                "(version 1)",
                "(allow default)",
                "(deny network*)",
                "(deny file-write*)",
                '(allow file-write* (literal "/dev/null"))',
                f"(allow file-write* (literal {workspace_encoded}))",
                f"(allow file-write* (subpath {workspace_encoded}))",
                *clauses,
                "",
            )
        )
    ).encode("utf-8")


def _seatbelt_attestation(workspace: Path) -> dict[str, Any]:
    canonical_workspace = Path(os.path.abspath(workspace.expanduser())).resolve()
    return {
        "schema_version": 2,
        "authority": "macos-seatbelt",
        "sandbox_executable": str(SEATBELT_EXECUTABLE),
        "profile_sha256": hashlib.sha256(
            _seatbelt_profile_bytes(canonical_workspace)
        ).hexdigest(),
        "network_denied": True,
        "default_file_write_denied": True,
        "allowed_write_roots": [str(canonical_workspace)],
        "live_magi_root": str(_live_magi_root()),
        "live_magi_write_denied": True,
        "live_magi_mutable_read_write_denied": True,
        "live_magi_immutable_read_roots": [
            str(_live_magi_root() / "releases"),
            str(_live_magi_root() / "runtimes"),
        ],
        "external_storage_read_write_denied": True,
        "external_storage_roots": [
            str(root) for root in _expected_external_storage_roots()
        ],
        "workspace_only_write": True,
        "environment_allowlist": {
            "base": sorted(BASE_ENVIRONMENT_KEYS),
            "trace": sorted(TRACE_ENVIRONMENT_KEYS),
            "formal_base": sorted(
                {*BASE_ENVIRONMENT_KEYS, *FORMAL_RUNTIME_ENVIRONMENT_KEYS}
            ),
            "formal_trace": sorted(
                {*TRACE_ENVIRONMENT_KEYS, *FORMAL_RUNTIME_ENVIRONMENT_KEYS}
            ),
        },
        "path_overrides_inherited": False,
        "enforcement_probe_passed": True,
    }


def _attested_seatbelt_workspace(value: Any) -> Path | None:
    if not isinstance(value, dict):
        return None
    allowed = value.get("allowed_write_roots")
    if not isinstance(allowed, list) or len(allowed) != 1 or not isinstance(allowed[0], str):
        return None
    candidate = Path(allowed[0])
    if not candidate.is_absolute():
        return None
    canonical = Path(os.path.abspath(candidate.expanduser())).resolve()
    if str(canonical) != allowed[0]:
        return None
    protected = (*_expected_external_storage_roots(), _live_magi_root())
    for root in protected:
        canonical_root = Path(os.path.abspath(root.expanduser())).resolve()
        try:
            canonical.relative_to(canonical_root)
        except ValueError:
            continue
        return None
    if value != _seatbelt_attestation(canonical):
        return None
    return canonical


def _write_seatbelt_profile(workspace: Path) -> Path:
    if sys.platform != "darwin":
        raise ContractValidationError("route certification requires macOS Seatbelt")
    try:
        executable_stat = SEATBELT_EXECUTABLE.lstat()
    except OSError as exc:
        raise ContractValidationError("macOS Seatbelt executable is unavailable") from exc
    if (
        SEATBELT_EXECUTABLE.is_symlink()
        or not stat.S_ISREG(executable_stat.st_mode)
        or not os.access(SEATBELT_EXECUTABLE, os.X_OK)
    ):
        raise ContractValidationError("macOS Seatbelt executable is unsafe")

    workspace = Path(os.path.abspath(workspace.expanduser())).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    profile_path = workspace / SEATBELT_PROFILE_NAME
    expected = _seatbelt_profile_bytes(workspace)
    if profile_path.exists() or profile_path.is_symlink():
        if profile_path.is_symlink() or not profile_path.is_file():
            raise ContractValidationError("route Seatbelt profile path is unsafe")
        try:
            observed = profile_path.read_bytes()
        except OSError as exc:
            raise ContractValidationError("route Seatbelt profile is unreadable") from exc
        if observed != expected:
            raise ContractValidationError("route Seatbelt profile content drifted")
    else:
        try:
            descriptor = os.open(
                profile_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o400,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(expected)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ContractValidationError("route Seatbelt profile could not be created") from exc
    profile_path.chmod(0o400)

    protected_roots = [
        *[str(root) for root in _expected_external_storage_roots()],
        str(_live_magi_root() / "runtime"),
        str(_live_magi_root() / "state"),
    ]
    outside_probe = workspace.parent / f".{workspace.name}-seatbelt-outside-probe"
    allowed_probe = workspace / ".seatbelt-workspace-write-probe"
    for probe_path in (outside_probe, allowed_probe):
        if probe_path.exists() or probe_path.is_symlink():
            raise ContractValidationError("route Seatbelt probe path already exists")
    probe_payload = json.dumps(
        {
            "roots": protected_roots,
            "protected_writes": [
                str(_expected_external_storage_roots()[0] / "route-seatbelt-write-probe"),
                str(_live_magi_root() / "route-seatbelt-write-probe"),
            ],
            "outside_write": str(outside_probe),
            "allowed_write": str(allowed_probe),
        },
        ensure_ascii=False,
    )
    probe_code = f"""
import errno
import json
import os
import socket
import sys

payload = json.loads({probe_payload!r})
denied = {{errno.EPERM, errno.EACCES}}
results = []
for path in payload["roots"]:
    try:
        os.stat(path)
    except OSError as exc:
        results.append(exc.errno in denied)
    else:
        results.append(False)
for path in [*payload["protected_writes"], payload["outside_write"]]:
    try:
        open(path, "xb").close()
    except OSError as exc:
        results.append(exc.errno in denied)
    else:
        results.append(False)
with open(payload["allowed_write"], "xb") as handle:
    handle.write(b"workspace-only")
os.unlink(payload["allowed_write"])
with open("/dev/null", "wb") as handle:
    handle.write(b"dev-null")
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.connect(("127.0.0.1", 9))
except OSError as exc:
    results.append(exc.errno in denied)
else:
    results.append(False)
finally:
    sock.close()
print("MAGI_V3_SEATBELT_OK" if all(results) else "MAGI_V3_SEATBELT_FAILED")
sys.exit(0 if all(results) else 3)
"""
    probe = subprocess.run(
        [
            str(SEATBELT_EXECUTABLE),
            "-f",
            str(profile_path),
            "/usr/bin/python3",
            "-I",
            "-S",
            "-c",
            probe_code,
        ],
        cwd=workspace,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(workspace),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": "/dev/null",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "MAGI_V3_SEATBELT_OK":
        raise ContractValidationError(
            "macOS Seatbelt external-storage denial probe failed: "
            + (probe.stderr.strip() or probe.stdout.strip() or str(probe.returncode))
        )
    return profile_path


def _seatbelt_command(profile_path: Path, command: Sequence[str]) -> list[str]:
    return [
        str(SEATBELT_EXECUTABLE),
        "-f",
        str(profile_path),
        *command,
    ]


def _diagnostic_pythonpath_roots() -> tuple[Path, ...]:
    roots = [REPO_ROOT.resolve(strict=True)]
    for module_name in ("pytest", "jsonschema"):
        spec = importlib.util.find_spec(module_name)
        if spec is None or not spec.origin:
            raise ContractValidationError(
                f"route source diagnostic requires {module_name}"
            )
        module_root = Path(spec.origin).resolve(strict=True).parent.parent
        if module_root not in roots:
            roots.append(module_root)
    return tuple(roots)


@dataclass(frozen=True, slots=True)
class RuntimeBinding:
    certifying: bool
    mode: str
    python_runtime: str | None
    python_runtime_realpath: str | None
    python_runtime_sha256: str | None
    runtime_manifest: str | None
    runtime_manifest_sha256: str | None
    runtime_tree_sha256: str | None
    runtime_root: str | None
    base_runtime_root: str | None
    pythonpath_roots: tuple[Path, ...]

    def as_dict(self) -> dict[str, Any]:
        user_python_root = account_home() / "Library" / "Python"
        user_site_included = any(
            root == user_python_root or user_python_root in root.parents
            for root in self.pythonpath_roots
        )
        return {
            "certifying": self.certifying,
            "mode": self.mode,
            "python_runtime": self.python_runtime,
            "python_runtime_realpath": self.python_runtime_realpath,
            "python_runtime_sha256": self.python_runtime_sha256,
            "runtime_manifest": self.runtime_manifest,
            "runtime_manifest_sha256": self.runtime_manifest_sha256,
            "runtime_tree_sha256": self.runtime_tree_sha256,
            "runtime_root": self.runtime_root,
            "base_runtime_root": self.base_runtime_root,
            "pythonpath_roots": [str(root) for root in self.pythonpath_roots],
            "user_site_included": user_site_included,
            "parent_sys_path_inherited": False,
            "site_processing_disabled": True,
        }


def _manifest_site_packages(payload: Mapping[str, Any]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for root_key, rows_key in (
        ("runtime_root", "directories"),
        ("base_runtime_root", "base_directories"),
    ):
        root_text = payload.get(root_key)
        rows = payload.get(rows_key)
        if not isinstance(root_text, str) or not Path(root_text).is_absolute() or not isinstance(rows, list):
            raise ContractValidationError("route runtime manifest site-packages binding is invalid")
        root = Path(root_text).resolve(strict=True)
        for row in rows:
            relative = row.get("path") if isinstance(row, dict) else None
            if not isinstance(relative, str):
                raise ContractValidationError("route runtime manifest directory row is invalid")
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts or pure.name != "site-packages":
                continue
            candidate = (root / Path(*pure.parts)).resolve(strict=True)
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ContractValidationError("route runtime site-packages escapes manifest root") from exc
            if not candidate.is_dir() or candidate in roots:
                continue
            user_site = Path(site.getusersitepackages()).expanduser().resolve(strict=False)
            user_python_root = account_home() / "Library" / "Python"
            if (
                candidate == user_site
                or candidate == user_python_root
                or user_python_root in candidate.parents
            ):
                raise ContractValidationError("formal route runtime cannot include user site-packages")
            roots.append(candidate)
    if not roots:
        raise ContractValidationError("route runtime manifest contains no site-packages")
    return tuple(sorted(roots, key=str))


def runtime_binding_from_environment(python: Path) -> RuntimeBinding:
    keys = {
        "python_runtime": "MAGI_V3_PYTHON_RUNTIME",
        "python_runtime_realpath": "MAGI_V3_PYTHON_RUNTIME_REALPATH",
        "python_runtime_sha256": "MAGI_V3_PYTHON_RUNTIME_SHA256",
        "runtime_manifest": "MAGI_V3_PYTHON_RUNTIME_MANIFEST",
        "runtime_manifest_sha256": "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256",
        "runtime_tree_sha256": "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256",
    }
    values = {name: os.environ.get(env, "").strip() for name, env in keys.items()}
    if os.environ.get("MAGI_V3_ROUTE_CERTIFYING") != "1":
        return RuntimeBinding(
            False, "source_diagnostic", None, None, None, None, None, None, None, None,
            _diagnostic_pythonpath_roots(),
        )
    if any(not value for value in values.values()) or any(
        not SHA256_RE.fullmatch(values[name])
        for name in ("python_runtime_sha256", "runtime_manifest_sha256", "runtime_tree_sha256")
    ):
        raise ContractValidationError("formal route runtime binding is incomplete")
    declared = Path(values["python_runtime"])
    realpath = Path(values["python_runtime_realpath"])
    manifest = Path(values["runtime_manifest"])
    if not all(path.is_absolute() for path in (declared, realpath, manifest)):
        raise ContractValidationError("formal route runtime paths must be absolute")
    try:
        if manifest.is_symlink() or _sha256(manifest) != values["runtime_manifest_sha256"]:
            raise ContractValidationError("formal route runtime manifest SHA-256 drifted")
        report = verify_runtime_manifest(
            manifest,
            expected_tree_sha256=values["runtime_tree_sha256"],
            expected_python_runtime=declared,
            expected_python_realpath=realpath,
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        observed_python = python.resolve(strict=True)
    except (OSError, UnicodeError, json.JSONDecodeError, PythonRuntimeBlocked) as exc:
        raise ContractValidationError(f"formal route runtime verification failed: {exc}") from exc
    if (
        observed_python != realpath.resolve(strict=True)
        or payload.get("python_runtime_sha256") != values["python_runtime_sha256"]
        or report.get("tree_sha256") != values["runtime_tree_sha256"]
    ):
        raise ContractValidationError("formal route Python executable or tree binding drifted")
    site_roots = _manifest_site_packages(payload)
    return RuntimeBinding(
        True,
        "formal_manifest_bound",
        str(declared),
        str(realpath),
        values["python_runtime_sha256"],
        str(manifest),
        values["runtime_manifest_sha256"],
        values["runtime_tree_sha256"],
        str(Path(payload["runtime_root"]).resolve(strict=True)),
        str(Path(payload["base_runtime_root"]).resolve(strict=True)),
        (REPO_ROOT.resolve(strict=True), *site_roots),
    )


def _pytest_pythonpath(runtime_binding: RuntimeBinding | None = None) -> str:
    binding = runtime_binding or RuntimeBinding(
        False, "source_diagnostic", None, None, None, None, None, None, None, None,
        _diagnostic_pythonpath_roots(),
    )
    return os.pathsep.join(str(root) for root in binding.pythonpath_roots)


def _child_environment(
    workspace: Path,
    *,
    runtime_binding: RuntimeBinding | None = None,
    trace_path: Path | None = None,
) -> dict[str, str]:
    """Build a sealed child environment without inheriting the parent process."""

    workspace = Path(os.path.abspath(workspace.expanduser())).resolve()
    home = workspace / "home"
    temporary = workspace / "tmp"
    runtime = workspace / "runtime"
    for directory in (home, temporary, runtime):
        directory.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": "/dev/null",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONPATH": _pytest_pythonpath(runtime_binding),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "MAGI_ENABLE_LIVE_TESTS": "0",
        "MAGI_ALLOW_INTERNET": "0",
        "MAGI_ALLOW_CLOUD_MODELS": "0",
        "MAGI_RUNTIME_DIR": str(runtime),
        "MAGI_WEB_RESEARCH_CACHE_DIR": str(runtime / "cache" / "web_search"),
        "MAGI_ROOT_DIR": str(workspace / "magi-root"),
        "MAGI_AGENT_DIR": str(workspace / "agent"),
        "MAGI_METRICS_DIR": str(workspace / "metrics"),
        "MAGI_EXPORTS_DIR": str(workspace / "exports"),
        "MAGI_OSC_FILE_SHARE_STORE": str(workspace / "osc-file-shares.json"),
        "MAGI_OSC_FILE_SHARE_CACHE_DIR": str(workspace / "osc-share-cache"),
        "MAGI_LINE_LAST_SENDER_FILE": str(workspace / "line-last.json"),
        "MAGI_DISCORD_LAST_CHANNEL_FILE": str(workspace / "discord-last.json"),
        "MAGI_DISABLE_SERVER_STARTUP_HOOKS": "1",
        "MAGI_SKIP_IMPORT_PROBES": "1",
        "MAGI_V3_OFFLINE_CERTIFICATION": "1",
    }
    formal_keys: set[str] = set()
    if runtime_binding is not None and runtime_binding.certifying:
        formal_values = {
            "MAGI_V3_ROUTE_CERTIFYING": "1",
            "MAGI_V3_PYTHON_RUNTIME": runtime_binding.python_runtime,
            "MAGI_V3_PYTHON_RUNTIME_REALPATH": runtime_binding.python_runtime_realpath,
            "MAGI_V3_PYTHON_RUNTIME_SHA256": runtime_binding.python_runtime_sha256,
            "MAGI_V3_PYTHON_RUNTIME_MANIFEST": runtime_binding.runtime_manifest,
            "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256": runtime_binding.runtime_manifest_sha256,
            "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256": runtime_binding.runtime_tree_sha256,
        }
        if any(not isinstance(value, str) or not value for value in formal_values.values()):
            raise ContractValidationError(
                "verified formal RuntimeBinding is incomplete for child execution"
            )
        for name in (
            "MAGI_V3_PYTHON_RUNTIME_SHA256",
            "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256",
            "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256",
        ):
            if not SHA256_RE.fullmatch(str(formal_values[name])):
                raise ContractValidationError(
                    "verified formal RuntimeBinding contains an invalid SHA-256"
                )
        environment.update({name: str(value) for name, value in formal_values.items()})
        formal_keys = set(FORMAL_RUNTIME_ENVIRONMENT_KEYS)
    if trace_path is not None:
        environment.update(
            {
                "MAGI_V3_ROUTE_TRACE_FILE": str(trace_path),
                "MAGI_V3_ROUTE_TRACE_SANDBOX": str(workspace),
                "MAGI_V3_ROUTE_TRACE_LIVE_ROOT": str(_live_magi_root()),
            }
        )
        expected_keys = set(TRACE_ENVIRONMENT_KEYS) | formal_keys
    else:
        expected_keys = set(BASE_ENVIRONMENT_KEYS) | formal_keys
    if set(environment) != expected_keys:
        raise ContractValidationError("route certification child environment allowlist drifted")
    return environment


@dataclass(frozen=True, slots=True)
class ReleaseBinding:
    release_id: str
    release_sha: str
    release_manifest: Path
    release_manifest_sha256: str
    release_commit: str

    def as_dict(self) -> dict[str, str]:
        return {
            "release_id": self.release_id,
            "release_sha": self.release_sha,
            "release_manifest": str(self.release_manifest),
            "release_manifest_sha256": self.release_manifest_sha256,
            "release_commit": self.release_commit,
        }


def release_binding_from_environment() -> ReleaseBinding:
    if os.environ.get("MAGI_V3_OFFLINE_CERTIFICATION") != "1":
        raise ContractValidationError("route certification requires MAGI_V3_OFFLINE_CERTIFICATION=1")
    manifest_value = os.environ.get("MAGI_V3_RELEASE_MANIFEST", "").strip()
    expected_id = os.environ.get("MAGI_V3_RELEASE_ID", "").strip()
    expected_manifest_sha = os.environ.get(
        "MAGI_V3_RELEASE_MANIFEST_SHA256", ""
    ).strip()
    if not manifest_value or not expected_id or not SHA256_RE.fullmatch(expected_manifest_sha):
        raise ContractValidationError("route certification release environment is incomplete")
    manifest_path = Path(manifest_value).expanduser().resolve(strict=True)
    if manifest_path.parent != REPO_ROOT.resolve(strict=True):
        raise ContractValidationError("route certification is not executing from the bound release root")
    if _sha256(manifest_path) != expected_manifest_sha:
        raise ContractValidationError("route certification manifest SHA-256 binding mismatch")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError("route certification release manifest is invalid") from exc
    release_sha = str(manifest.get("source_snapshot_sha256") or "")
    if not SHA256_RE.fullmatch(release_sha):
        raise ContractValidationError("route certification release source SHA-256 is invalid")
    bundle = verify_release_bundle(REPO_ROOT, release_sha)
    if bundle.release_id != expected_id or bundle.manifest_sha256 != expected_manifest_sha:
        raise ContractValidationError("route certification immutable release identity drifted")
    return ReleaseBinding(
        release_id=bundle.release_id,
        release_sha=bundle.source_snapshot_sha256,
        release_manifest=manifest_path,
        release_manifest_sha256=bundle.manifest_sha256,
        release_commit=bundle.commit,
    )


def load_proof_reviews(path: Path = PROOF_MANIFEST_PATH) -> dict[RouteMethodKey, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError("route success proof-review manifest is invalid") from exc
    inventory = RouteInventory.load()
    inventory_fingerprint = load_and_validate_runtime_inventory()["fingerprint"]
    if payload.get("schema_version") != 1:
        raise ContractValidationError("route success proof-review schema_version must be 1")
    if payload.get("inventory_fingerprint") != inventory_fingerprint:
        raise ContractValidationError("route success proof-review inventory binding drifted")
    rows = payload.get("proofs")
    if not isinstance(rows, list):
        raise ContractValidationError("route success proof-review proofs must be an array")
    result: dict[RouteMethodKey, str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ContractValidationError(f"route success proof-review {index} is invalid")
        key = RouteMethodKey(
            str(row.get("service") or ""),
            str(row.get("rule") or ""),
            str(row.get("method") or "").upper(),
            str(row.get("endpoint") or ""),
        )
        nodeid = str(row.get("test_nodeid") or "")
        if key in result or not nodeid.startswith("tests/") or "::" not in nodeid:
            raise ContractValidationError(f"route success proof-review {index} is not explicit")
        result[key] = nodeid
    inventory_keys = {
        RouteMethodKey(route.service, route.rule, method, route.endpoint)
        for route in inventory.routes
        for method in route.methods
    }
    unknown = sorted(set(result) - inventory_keys)
    if unknown:
        raise ContractValidationError(f"route success proof-review contains unknown methods: {unknown[:3]}")
    return result


def load_trace_targets(path: Path = PROOF_MANIFEST_PATH) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError("route success proof-review manifest is invalid") from exc
    targets = payload.get("test_targets")
    if not isinstance(targets, list) or not targets:
        raise ContractValidationError("route success proof-review test_targets must be non-empty")
    normalized = tuple(str(value) for value in targets)
    if len(set(normalized)) != len(normalized) or any(
        not value.startswith("tests/")
        or not (REPO_ROOT / value.split("::", 1)[0]).is_file()
        for value in normalized
    ):
        raise ContractValidationError("route success proof-review test target is invalid")
    return normalized


def _run_trace(
    workspace: Path,
    *,
    python: Path,
    nodeids: Sequence[str],
    runtime_binding: RuntimeBinding,
    seatbelt_profile: Path,
) -> dict[str, Any]:
    seatbelt = _seatbelt_attestation(seatbelt_profile.parent)
    if not nodeids:
        return {
            "schema_version": 1,
            "pytest_exit_status": 0,
            "observations": [],
            "isolation_attempts": {},
            "isolation_attempt_details": [],
            "external_storage_roots": [
                str(root) for root in _expected_external_storage_roots()
            ],
            "external_storage_access_attempts": 0,
            "seatbelt": seatbelt,
        }
    trace_path = workspace / "route-success-trace.json"
    environment = _child_environment(
        workspace, runtime_binding=runtime_binding, trace_path=trace_path
    )
    result = subprocess.run(
        _seatbelt_command(
            seatbelt_profile,
            [
                str(python),
                "-S",
                "-m",
                "pytest",
                "-q",
                "-p",
                "scripts.v3_validation.route_success_trace_plugin",
                "-p",
                "no:cacheprovider",
                *nodeids,
            ],
        ),
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if result.returncode != 0 or not trace_path.is_file():
        raise ContractValidationError(
            "route success proof tests failed: "
            + (result.stderr.strip() or result.stdout.strip() or str(result.returncode))
        )
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    if payload.get("pytest_exit_status") != 0:
        raise ContractValidationError("route success proof trace did not complete successfully")
    payload["seatbelt"] = seatbelt
    return payload


def _run_base_replay(
    workspace: Path,
    *,
    python: Path,
    runtime_binding: RuntimeBinding,
    seatbelt_profile: Path,
) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    environment = _child_environment(workspace, runtime_binding=runtime_binding)
    result = subprocess.run(
        _seatbelt_command(
            seatbelt_profile,
            [
                str(python),
                "-S",
                str(Path(__file__).with_name("actual_route_replay.py")),
                "--workspace",
                str(workspace),
            ],
        ),
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if result.returncode not in {0, 2}:
        raise ContractValidationError(
            "Seatbelt actual-handler replay failed: "
            + (result.stderr.strip() or result.stdout.strip() or str(result.returncode))
        )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "Seatbelt actual-handler replay emitted invalid JSON"
        ) from exc
    if not isinstance(payload, dict) or payload.get("execution_passed") is not True:
        raise ContractValidationError("Seatbelt actual-handler replay did not execute safely")
    safety = payload.get("safety")
    if not isinstance(safety, dict):
        raise ContractValidationError("actual-handler replay safety evidence is missing")
    safety["seatbelt"] = _seatbelt_attestation(seatbelt_profile.parent)
    return payload


def compile_route_evidence(
    base: Mapping[str, Any],
    trace: Mapping[str, Any],
    proof_reviews: Mapping[RouteMethodKey, str],
    binding: ReleaseBinding,
    runtime_binding: RuntimeBinding | None = None,
) -> dict[str, Any]:
    runtime_binding = runtime_binding or RuntimeBinding(
        False,
        "source_diagnostic",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        _diagnostic_pythonpath_roots(),
    )
    inventory = RouteInventory.load()
    inventory_fingerprint = load_and_validate_runtime_inventory()["fingerprint"]
    reviews = load_route_method_reviews(expected_inventory_fingerprint=inventory_fingerprint)
    expected_external_storage_roots = [
        str(root) for root in _expected_external_storage_roots()
    ]
    trace_seatbelt = trace.get("seatbelt")
    base_safety = base.get("safety")
    base_seatbelt = (
        base_safety.get("seatbelt") if isinstance(base_safety, dict) else None
    )
    attested_workspace = _attested_seatbelt_workspace(trace_seatbelt)
    seatbelt_attested = (
        attested_workspace is not None
        and base_seatbelt == trace_seatbelt
        and _attested_seatbelt_workspace(base_seatbelt) == attested_workspace
    )
    trace_isolation_attempts = trace.get("isolation_attempts")
    trace_external_attempts = trace.get("external_storage_access_attempts")
    trace_external_roots = trace.get("external_storage_roots")
    trace_external_counter = (
        trace_isolation_attempts.get(EXTERNAL_STORAGE_ACCESS_EVENT, 0)
        if isinstance(trace_isolation_attempts, dict)
        else None
    )
    trace_counters_zero = (
        isinstance(trace_isolation_attempts, dict)
        and all(
            isinstance(name, str) and type(value) is int and value == 0
            for name, value in trace_isolation_attempts.items()
        )
    )
    trace_external_storage_attested = (
        type(trace_external_attempts) is int
        and trace_external_attempts == 0
        and type(trace_external_counter) is int
        and trace_external_counter == 0
        and trace_external_roots == expected_external_storage_roots
        and trace_counters_zero
        and seatbelt_attested
    )
    base_external_attempts = (
        base_safety.get("external_storage_access_attempts")
        if isinstance(base_safety, dict)
        else None
    )
    base_external_roots = (
        base_safety.get("external_storage_roots")
        if isinstance(base_safety, dict)
        else None
    )
    base_isolation_attempts = (
        base_safety.get("isolation_attempts")
        if isinstance(base_safety, dict)
        else None
    )
    base_counters_zero = (
        isinstance(base_isolation_attempts, dict)
        and all(
            isinstance(name, str) and type(value) is int and value == 0
            for name, value in base_isolation_attempts.items()
        )
    )
    base_external_storage_attested = (
        isinstance(base_safety, dict)
        and type(base_external_attempts) is int
        and base_external_attempts == 0
        and base_external_roots == expected_external_storage_roots
        and base_counters_zero
        and seatbelt_attested
        and base_safety.get("nas_accessed") is False
    )
    external_storage_attested = (
        trace_external_storage_attested and base_external_storage_attested
    )
    inventory_keys = {
        RouteMethodKey(route.service, route.rule, method, route.endpoint)
        for route in inventory.routes
        for method in route.methods
    }
    accepted: dict[RouteMethodKey, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    unsafe_nodeids = set(trace.get("unsafe_test_nodeids") or ())
    for row in trace.get("observations", ()):
        candidates = [
            key
            for key in inventory_keys
            if key.rule == row.get("rule")
            and key.method == row.get("method")
            and key.endpoint == row.get("endpoint")
        ]
        if len(candidates) != 1:
            rejected.append({"reason": "inventory_identity_mismatch", "observation": row})
            continue
        key = candidates[0]
        nodeid = str(row.get("test_nodeid") or "")
        status = row.get("status")
        if nodeid in unsafe_nodeids:
            rejected.append({"reason": "test_attempted_isolation_boundary", **_key_dict(key), "test_nodeid": nodeid})
            continue
        assertion_lines = row.get("success_assertion_lines")
        if not isinstance(assertion_lines, list) or not assertion_lines:
            rejected.append({"reason": "success_status_not_asserted", **_key_dict(key), "test_nodeid": nodeid})
            continue
        pinned_nodeid = proof_reviews.get(key)
        if pinned_nodeid is None:
            rejected.append({"reason": "success_proof_not_pinned", **_key_dict(key), "test_nodeid": nodeid})
            continue
        if pinned_nodeid != nodeid:
            rejected.append({"reason": "pinned_test_nodeid_mismatch", **_key_dict(key), "test_nodeid": nodeid})
            continue
        if key not in reviews:
            rejected.append({"reason": "route_method_not_side_effect_reviewed", **_key_dict(key)})
            continue
        if not isinstance(status, int) or not 200 <= status < 400:
            rejected.append({"reason": "not_success_status", **_key_dict(key), "status": status})
            continue
        if str(row.get("location_path") or "").rstrip("/") in {"/login", "/auth/login"}:
            rejected.append({"reason": "authentication_redirect", **_key_dict(key)})
            continue
        accepted.setdefault(key, dict(row))

    dispositions = [dict(row) for row in base.get("route_method_dispositions", ())]
    promoted = 0
    for row in dispositions:
        key = RouteMethodKey(row["service"], row["rule"], row["method"], row["endpoint"])
        if row.get("disposition") == "actual_handler_passed" or key not in accepted:
            continue
        proof = accepted[key]
        row.update(
            {
                "disposition": "actual_handler_passed",
                "reason_code": "REVIEWED_PYTEST_REPRESENTATIVE_SUCCESS_PATH",
                "reviewed": True,
                "side_effect_class": reviews[key].side_effect_class,
                "branch_class": "representative_success_path",
                "handler_dispatch_passed": True,
                "representative_success_path_passed": True,
                "evidence_sha256": hashlib.sha256(_canonical(proof)).hexdigest(),
                "test_nodeid": proof["test_nodeid"],
            }
        )
        promoted += 1

    passed_methods = sum(row.get("disposition") == "actual_handler_passed" for row in dispositions)
    by_route: dict[tuple[str, str, str], list[bool]] = {}
    for row in dispositions:
        by_route.setdefault((row["service"], row["rule"], row["endpoint"]), []).append(
            row.get("disposition") == "actual_handler_passed"
        )
    passed_routes = sum(all(values) for values in by_route.values())
    complete = passed_methods == len(inventory_keys) and passed_routes == len(inventory.routes)
    base_safe_execution = isinstance(base_safety, dict) and base_safety.get(
        "safe_execution"
    ) is True
    certification_safe = base_safe_execution and external_storage_attested
    workload_passed = (
        complete and base.get("execution_passed") is True and certification_safe
    )
    formally_passed = workload_passed and runtime_binding.certifying
    report = {
        "schema_version": 1,
        "workload": "346_route_contract_replay",
        "status": "passed" if formally_passed else (
            "diagnostic_passed" if workload_passed else "failed"
        ),
        "certifying": runtime_binding.certifying,
        "diagnostic_passed": workload_passed and not runtime_binding.certifying,
        "runtime_binding": runtime_binding.as_dict(),
        **binding.as_dict(),
        "inventory_fingerprint": inventory_fingerprint,
        "inventory_counts": inventory.counts,
        "pinned_route_methods": len(inventory_keys),
        "representative_success_path_passed": passed_methods,
        "remaining_route_methods": len(inventory_keys) - passed_methods,
        "pinned_routes": len(inventory.routes),
        "fully_replayed_routes": passed_routes,
        "remaining_routes": len(inventory.routes) - passed_routes,
        "measurements": {
            "validation_profile_id": os.environ.get(
                "MAGI_V3_VALIDATION_PROFILE_ID", "unprofiled"
            ),
            "pinned_routes": len(inventory.routes),
            "fully_replayed_routes": passed_routes,
            "remaining_routes": len(inventory.routes) - passed_routes,
            "pinned_route_methods": len(inventory_keys),
            "representative_success_path_passed": passed_methods,
            "remaining_route_methods": len(inventory_keys) - passed_methods,
        },
        "trace_promotions": promoted,
        "trace_rejections": rejected,
        "route_method_dispositions": dispositions,
        "safety": {
            "offline": True,
            "production_service_started": False,
            "production_database_accessed": False,
            "nas_accessed": not external_storage_attested,
            "external_storage_roots": expected_external_storage_roots,
            "external_storage_access_attempts": (
                base_external_attempts + trace_external_attempts
                if type(base_external_attempts) is int
                and type(trace_external_attempts) is int
                else -1
            ),
            "trace_isolation_attempts": (
                trace_isolation_attempts
                if isinstance(trace_isolation_attempts, dict)
                else {"invalid_trace_isolation_attempts": -1}
            ),
            "base_isolation_attempts": (
                base_isolation_attempts
                if isinstance(base_isolation_attempts, dict)
                else {"invalid_base_isolation_attempts": -1}
            ),
            "seatbelt": (
                trace_seatbelt if seatbelt_attested else {"attestation_invalid": True}
            ),
            "trace_external_storage_attested": trace_external_storage_attested,
            "base_external_storage_attested": base_external_storage_attested,
            "external_storage_attested": external_storage_attested,
            "base_safe_execution": base_safe_execution,
        },
        "source_binding": {
            "compiler_sha256": _sha256(SCRIPT_PATH),
            "actual_route_replay_sha256": _sha256(
                Path(__file__).with_name("actual_route_replay.py")
            ),
            "trace_plugin_sha256": _sha256(PLUGIN_PATH),
            "proof_review_manifest_sha256": _sha256(PROOF_MANIFEST_PATH),
            "primary_side_effect_review_sha256": _sha256(
                Path(ROUTE_METHOD_REVIEW_PATH)
            ),
            "supplemental_side_effect_review_sha256": _sha256(
                ROUTE_METHOD_REVIEW_SUPPLEMENT_PATH
            ),
            "base_evidence_sha256": str(base.get("evidence_sha256") or ""),
        },
        "blockers": {
            "ROUTE_REPLAY_NOT_IMPLEMENTED": {
                "retained": not complete,
                "remaining_routes": len(inventory.routes) - passed_routes,
                "remaining_route_methods": len(inventory_keys) - passed_methods,
                "reason": (
                    "all pinned route-methods have reviewed representative-success evidence"
                    if complete
                    else "one or more pinned route-methods lack reviewed representative-success evidence"
                ),
            }
        },
        "coverage_complete": complete,
        "passed": formally_passed,
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
    }
    report["evidence_sha256"] = hashlib.sha256(_canonical(report)).hexdigest()
    return report


def run_route_certification(workspace: Path, *, python: Path) -> dict[str, Any]:
    binding = release_binding_from_environment()
    runtime_binding = runtime_binding_from_environment(python)
    proof_reviews = load_proof_reviews()
    trace_targets = load_trace_targets()
    workspace = workspace.expanduser().resolve()
    live_root = (account_home() / "Library" / "Application Support" / "MAGI").resolve()
    try:
        workspace.relative_to(live_root)
    except ValueError:
        pass
    else:
        raise ContractValidationError("route certification workspace overlaps live MAGI state")
    workspace.mkdir(parents=True, exist_ok=True)
    seatbelt_profile = _write_seatbelt_profile(workspace)
    base = _run_base_replay(
        workspace / "actual-handler",
        python=python,
        runtime_binding=runtime_binding,
        seatbelt_profile=seatbelt_profile,
    )
    trace = _run_trace(
        workspace / "proof-tests",
        python=python,
        nodeids=trace_targets,
        runtime_binding=runtime_binding,
        seatbelt_profile=seatbelt_profile,
    )
    return compile_route_evidence(
        base, trace, proof_reviews, binding, runtime_binding=runtime_binding
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        python = Path(os.path.abspath(args.python.expanduser()))
        if not python.is_file():
            raise ContractValidationError("route certification Python executable is missing")
        workspace = args.workspace
        if workspace is None:
            state_dir = os.environ.get("MAGI_V3_STATE_DIR", "").strip()
            if not state_dir:
                raise ContractValidationError(
                    "route certification requires --workspace or MAGI_V3_STATE_DIR"
                )
            profile = os.environ.get("MAGI_V3_VALIDATION_PROFILE_ID", "unprofiled")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", profile):
                raise ContractValidationError("route certification validation profile is invalid")
            workspace = Path(state_dir) / "route-certification" / profile
        report = run_route_certification(workspace, python=python)
    except Exception as exc:
        print(
            json.dumps(
                {"passed": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    print("MAGI_V3_OFFLINE_EVIDENCE=" + encoded)
    return 0 if report["passed"] or report.get("diagnostic_passed") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
