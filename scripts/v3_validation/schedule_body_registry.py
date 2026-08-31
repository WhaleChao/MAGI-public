#!/usr/bin/env python3
"""Execute release-bound real schedule bodies in disposable macOS fixtures.

The registry is deliberately incomplete: every enabled cron definition is
resolved either to a reviewed real-body adapter or to one or more explicit
blockers.  A reviewed adapter executes the actual entrypoint three times; help
output, dispatcher timing, sleeps, and stand-in handlers are not measurements.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.dont_write_bytecode = True

from scripts.v3_campaign.offline_probes import OfflineProbeError, bound_cron_jobs
from scripts.v3_campaign.schedule_realism import (
    BASELINE_PATH,
    MIN_SUCCESSFUL_SAMPLES,
    _execute_body_samples,
    _logical_definition_sha256,
)
from magi_v3.cron_macros import resolve_exact_cron_macro
from magi_v3.external_inputs import (
    ExternalInputError,
    NAMED_MUTABLE_STATE_BINDINGS,
    load_bound_cron_jobs,
)
from scripts.v3_validation.schedule_product_fixture_matrix import (
    populate_product_fixture,
)
from scripts.v3_validation.schedule_nonstorage_fixture_matrix import (
    populate_nonstorage_fixture,
)
from scripts.v3_validation.schedule_sample_evidence import (
    CONTRACT_DIAGNOSTIC_SCHEMA,
    SYSTEM_DIAGNOSTIC_KIND,
    SYSTEM_DIAGNOSTIC_JOB_IDS,
    SYSTEM_DIAGNOSTIC_RESOURCE_WARNING_CODES,
    build_sample_evidence,
    canonical_sha256 as _sample_evidence_sha256,
)
from skills.ops.cron_command_identity import canonical_command_tokens


REGISTRY_PATH = Path("config/v3_schedule_body_adapter_registry.json")
SCHEMA = "magi.v3.schedule-body-adapter-registry/v1"
ADAPTER_MODE = "real_entrypoint_fixture_v1"
LIVE_ROOT = (Path.home() / "Library" / "Application Support" / "MAGI").resolve()
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DIAGNOSTIC_STREAM_LIMIT = 128 * 1024
TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")
OSC_EVENTS_FIXTURE_HORIZON_DAYS = 30
TRUSTED_DEPENDENCY_ROOTS = (
    Path("/usr/bin"),
    Path("/bin"),
    Path("/opt/homebrew"),
    Path("/usr/local"),
)


class ScheduleBodyRegistryError(RuntimeError):
    """The adapter registry or its evidence failed closed."""


def _trusted_dependency_executable(*names: str) -> str:
    """Resolve a local fixture dependency without widening the sealed PATH."""

    candidates: list[Path] = []
    for name in names:
        located = shutil.which(name)
        if located:
            candidates.append(Path(located))
        candidates.extend(
            (root if root.name == "bin" else root / "bin") / name
            for root in TRUSTED_DEPENDENCY_ROOTS
        )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
            continue
        if not any(_is_relative_to(resolved, root) for root in TRUSTED_DEPENDENCY_ROOTS):
            continue
        return str(resolved)
    raise ScheduleBodyRegistryError(
        f"trusted schedule dependency is unavailable: {names[0]}"
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cron_policy_source_sha256(source_root: Path) -> str:
    policy_path = source_root / "config/v3_schedule_dispatch_policy.json"
    try:
        policy = json.loads(
            _stable_regular_bytes(policy_path, label="cron dispatch policy").decode("utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScheduleBodyRegistryError(f"cron dispatch policy is unreadable: {exc}") from exc
    source_sha = policy.get("cron_jobs_sha256") if isinstance(policy, dict) else None
    if not isinstance(source_sha, str) or not HEX64.fullmatch(source_sha):
        raise ScheduleBodyRegistryError("cron dispatch policy source binding is invalid")
    return source_sha


def _stable_regular_bytes(path: Path, *, label: str) -> bytes:
    try:
        declared = path.lstat()
    except OSError as exc:
        raise ScheduleBodyRegistryError(f"{label} is unavailable: {exc}") from exc
    if stat.S_ISLNK(declared.st_mode) or not stat.S_ISREG(declared.st_mode):
        raise ScheduleBodyRegistryError(f"{label} must be a non-symlink regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ScheduleBodyRegistryError(f"{label} is unavailable: {exc}") from exc
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_nlink")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or any(
            getattr(declared, field) != getattr(before, field) for field in fields
        ):
            raise ScheduleBodyRegistryError(f"{label} changed before it was read")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
    except OSError as exc:
        raise ScheduleBodyRegistryError(f"{label} is unreadable: {exc}") from exc
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if (
        any(getattr(before, field) != getattr(after, field) for field in fields)
        or any(getattr(after, field) != getattr(current, field) for field in fields)
        or stat.S_ISLNK(current.st_mode)
        or len(payload) != before.st_size
    ):
        raise ScheduleBodyRegistryError(f"{label} changed while being read")
    return payload


def _bound_cron_bytes(source_root: Path) -> bytes:
    """Read only the launcher-bound candidate snapshot (or source fixture in tests)."""

    try:
        binding = load_bound_cron_jobs(source_root, missing_source_default=False)
    except ExternalInputError as exc:
        raise ScheduleBodyRegistryError(f"bound cron fixture is invalid: {exc}") from exc
    payload = _stable_regular_bytes(binding.path, label="bound cron fixture")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != binding.sha256:
        raise ScheduleBodyRegistryError("bound cron fixture changed after binding verification")
    if binding.source_sha256 != _cron_policy_source_sha256(source_root):
        raise ScheduleBodyRegistryError("bound cron fixture source/policy binding mismatched")
    try:
        rows = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ScheduleBodyRegistryError(f"bound cron fixture is invalid JSON: {exc}") from exc
    if not isinstance(rows, list) or not rows:
        raise ScheduleBodyRegistryError("bound cron fixture must be a non-empty list")
    return payload


def _source_bound_cron_jobs(source_root: Path) -> tuple[list[dict[str, Any]], str]:
    """Load the candidate source schedule when production paths are rebased.

    A deployed snapshot legitimately contains release/runtime paths while the
    candidate source snapshot contains source-tree paths.  Body adapters must
    resolve against the latter; capacity simulation still consumes the former.
    Keep both inputs hash-bound and require the source snapshot to be the
    declared policy source.
    """

    declared = str(os.environ.get("MAGI_CRON_JOBS_SOURCE_FILE") or "").strip()
    if not declared:
        jobs, digest = bound_cron_jobs(source_root)
        return jobs, digest
    path = Path(declared).expanduser()
    expected = str(os.environ.get("MAGI_CRON_JOBS_SOURCE_SHA256") or "").strip()
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not HEX64.fullmatch(expected)
        or not path.is_file()
    ):
        raise ScheduleBodyRegistryError("cron source snapshot binding is unsafe")
    payload = _stable_regular_bytes(path, label="cron source snapshot")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected:
        raise ScheduleBodyRegistryError("cron source snapshot SHA-256 binding mismatch")
    try:
        rows = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ScheduleBodyRegistryError(f"cron source snapshot is invalid JSON: {exc}") from exc
    if (
        not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
        or any(not str(row.get("id") or "").strip() for row in rows)
        or len({str(row.get("id") or "").strip() for row in rows}) != len(rows)
    ):
        raise ScheduleBodyRegistryError("cron source snapshot must contain unique job ids")
    return [dict(row) for row in rows], digest


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _fixture_shared_root(
    fixture_root: Path, environment: Mapping[str, str]
) -> Path:
    """Infer one canonical shared root from an adapter's existing leaf paths."""

    fixture = fixture_root.resolve()
    declared = str(
        environment.get("MAGI_V3_SHARED_STATE_DIR")
        or environment.get("MAGI_SHARED_STATE_DIR")
        or ""
    ).strip()
    candidates: set[Path] = set()
    if declared:
        raw = Path(declared).expanduser()
        if not raw.is_absolute() or raw.is_symlink():
            raise ScheduleBodyRegistryError(
                "fixture shared-state binding must be absolute and non-symlinked"
            )
        candidates.add(raw.resolve(strict=False))
    for name, leaf in (
        ("MAGI_AGENT_DIR", "agent"),
        ("MAGI_RUNTIME_DIR", "runtime"),
        ("MAGI_MUTABLE_STATIC_DIR", "static"),
        ("MAGI_EXPORTS_DIR", "exports"),
    ):
        value = str(environment.get(name) or "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute() or path.name != leaf:
            raise ScheduleBodyRegistryError(
                f"{name} is not a canonical fixture {leaf} binding"
            )
        candidates.add(path.resolve(strict=False).parent)
    if not candidates:
        candidates.add(fixture)
    if len(candidates) != 1:
        raise ScheduleBodyRegistryError(
            "fixture mutable directories do not share one canonical root"
        )
    shared = next(iter(candidates))
    if shared != fixture and not shared.is_relative_to(fixture):
        raise ScheduleBodyRegistryError(
            "fixture shared-state binding escapes the disposable fixture"
        )
    if shared.is_symlink():
        raise ScheduleBodyRegistryError("fixture shared-state binding is symlinked")
    return shared


def _bind_fixture_v3_shared_state(
    fixture_root: Path,
    environment: dict[str, str],
    *,
    source_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    """Install the production-shaped mutable path contract inside one fixture.

    The real sealed release remains fail-closed.  This helper only supplies the
    complete contract to the already disposable, Seatbelt-confined fixture so
    source-layout defaults cannot accidentally decide where a sample writes.
    """

    fixture = fixture_root.resolve()
    shared = _fixture_shared_root(fixture, environment)
    directory_bindings = {
        "MAGI_V3_SHARED_STATE_DIR": shared,
        "MAGI_SHARED_STATE_DIR": shared,
        "MAGI_AGENT_DIR": shared / "agent",
        "MAGI_DATA_DIR": shared / "agent",
        "MAGI_RUNTIME_DIR": shared / "runtime",
        "MAGI_MUTABLE_STATIC_DIR": shared / "static",
        "MAGI_EXPORTS_DIR": shared / "exports",
    }
    declared_file_review_state = str(
        environment.get("MAGI_FILE_REVIEW_STATE_DIR") or ""
    ).strip()
    file_review_state = (
        Path(declared_file_review_state).expanduser()
        if declared_file_review_state
        else shared / "file-review"
    )
    optional_directory_bindings = {
        "MAGI_METRICS_DIR": shared / "metrics",
        "MAGI_AUTOPILOT_RUNS_DIR": shared / "autopilot-runs",
        "MAGI_PDF_NAMER_STATE_DIR": shared / "pdf-namer",
        "FAISS_INDEX_DIR": shared / "memory" / "index_cache",
        "MAGI_FILE_REVIEW_STATE_DIR": file_review_state,
        # The worker and its parent must observe the same terminal-state tree.
        # In particular, an adapter may intentionally bind FileReview state to
        # <FIXTURE>/state rather than the shared-root default.
        "MAGI_FILE_REVIEW_BG_JOB_DIR": file_review_state / "bg-jobs",
        "MAGI_EEFILE_DOWNLOAD_FOLDER": file_review_state / "downloads",
        "MAGI_LAF_ORCHESTRATOR_STATE_DIR": shared / "agent" / "laf-orchestrator",
    }
    named_bindings = {
        env_name: shared / relative
        for env_name, (_manifest_name, relative) in NAMED_MUTABLE_STATE_BINDINGS.items()
    }
    additional_file_bindings = {
        "MAGI_FILE_REVIEW_PENDING_PATH": (
            shared / "agent" / "file-review" / "review_submit_pending.json"
        ),
        "MAGI_LAF_GMAIL_STATE_PATH": shared / "static" / "laf_gmail_monitor_state.json",
        "MAGI_LAF_GMAIL_MONITOR_STATE": shared / "static" / "laf_gmail_monitor_state.json",
        "MAGI_LAF_GMAIL_PENDING_PATH": shared / "runtime" / "laf_gmail_dispatch_pending.json",
        "MAGI_BRAIN_SQLITE_PATH": shared / "agent" / "magi_brain.db",
    }
    environment.update({name: str(path) for name, path in directory_bindings.items()})
    for name, path in {
        **named_bindings,
        **optional_directory_bindings,
        **additional_file_bindings,
    }.items():
        environment.setdefault(name, str(path))

    # setdefault deliberately preserves adapter-specific fixture paths.  Check
    # those retained values just as strictly as generated defaults so ambient
    # production paths, relative paths, and symlinks cannot escape Seatbelt's
    # disposable fixture through an explicit optional/named binding.
    resolved_bindings: dict[str, str] = {}
    for name in (*directory_bindings, *optional_directory_bindings):
        raw = str(environment.get(name) or "").strip()
        path = Path(raw).expanduser() if raw else Path()
        if not raw or not path.is_absolute() or path.is_symlink():
            raise ScheduleBodyRegistryError(
                f"fixture mutable directory binding {name} is not canonical"
            )
        unresolved = path.resolve(strict=False)
        if not unresolved.is_relative_to(fixture):
            raise ScheduleBodyRegistryError(
                f"fixture mutable directory binding {name} escaped the fixture"
            )
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
        if not path.is_dir() or not resolved.is_relative_to(fixture):
            raise ScheduleBodyRegistryError(
                f"fixture mutable directory binding {name} escaped the fixture"
            )
        environment[name] = str(resolved)
        resolved_bindings[name] = resolved.relative_to(fixture).as_posix() or "."
    for name in (*named_bindings, *additional_file_bindings):
        raw = str(environment.get(name) or "").strip()
        path = Path(raw).expanduser() if raw else Path()
        if not raw or not path.is_absolute() or path.is_symlink():
            raise ScheduleBodyRegistryError(
                f"fixture named mutable binding {name} is not canonical"
            )
        unresolved_parent = path.parent.resolve(strict=False)
        unresolved = path.resolve(strict=False)
        if (
            not unresolved_parent.is_relative_to(fixture)
            or not unresolved.is_relative_to(fixture)
        ):
            raise ScheduleBodyRegistryError(
                f"fixture named mutable binding {name} escaped the fixture"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = path.parent.resolve(strict=True)
        resolved = path.resolve(strict=False)
        if (
            not resolved_parent.is_relative_to(fixture)
            or not resolved.is_relative_to(fixture)
            or (path.exists() and not path.is_file())
        ):
            raise ScheduleBodyRegistryError(
                f"fixture named mutable binding {name} escaped the fixture"
            )
        environment[name] = str(resolved)
        resolved_bindings[name] = resolved.relative_to(fixture).as_posix()
    config_value = str(
        environment.get("MAGI_CONFIG_PATH")
        or environment.get("MAGI_LAF_CONFIG_FILE")
        or ""
    ).strip()
    config_path = (
        Path(config_value).expanduser()
        if config_value
        else fixture / "magi-root" / "json" / "config.json"
    )
    external_bindings: list[str] = []
    if config_path.exists():
        if (
            not config_path.is_absolute()
            or config_path.is_symlink()
            or not config_path.resolve().is_relative_to(fixture)
        ):
            raise ScheduleBodyRegistryError(
                "fixture LAF config must be a regular file inside the fixture"
            )
        config_payload = _stable_regular_bytes(config_path, label="fixture LAF config")
        config_sha = hashlib.sha256(config_payload).hexdigest()
        environment.update(
            {
                "MAGI_CONFIG_PATH": str(config_path.resolve()),
                "MAGI_CONFIG_SHA256": config_sha,
                "MAGI_LAF_CONFIG_FILE": str(config_path.resolve()),
                "MAGI_LAF_CONFIG_SHA256": config_sha,
            }
        )
        external_bindings.append("laf_config")

    fixture_cron = str(environment.get("MAGI_CRON_JOBS_FILE") or "").strip()
    if not fixture_cron:
        cron_path = shared / "runtime-inputs" / "cron_jobs.json"
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        cron_path.write_bytes(_bound_cron_bytes(source_root))
        environment["MAGI_CRON_JOBS_FILE"] = str(cron_path)
        external_bindings.append("cron_snapshot")
    fixture_magi_root_value = str(environment.get("MAGI_ROOT_DIR") or "").strip()
    if fixture_magi_root_value:
        fixture_magi_root = Path(fixture_magi_root_value).expanduser().resolve(
            strict=False
        )
        if fixture_magi_root == fixture or fixture_magi_root.is_relative_to(fixture):
            policy_target = (
                fixture_magi_root / "config" / "v3_schedule_dispatch_policy.json"
            )
            if not policy_target.exists():
                policy_target.parent.mkdir(parents=True, exist_ok=True)
                policy_target.write_bytes(
                    _stable_regular_bytes(
                        source_root / "config" / "v3_schedule_dispatch_policy.json",
                        label="source cron dispatch policy",
                    )
                )
                external_bindings.append("cron_dispatch_policy")
    receipt = {
        "schema": "magi.v3.schedule-fixture-bindings/v1",
        "shared_relative": shared.relative_to(fixture).as_posix() or ".",
        "directory_bindings": sorted(directory_bindings),
        "optional_directory_bindings": sorted(optional_directory_bindings),
        "named_mutable_bindings": sorted(named_bindings),
        "additional_file_bindings": sorted(additional_file_bindings),
        "external_bindings": sorted(external_bindings),
        "resolved_bindings": dict(sorted(resolved_bindings.items())),
    }
    receipt["sha256"] = _sha256(receipt)
    return receipt


def _diagnostic_text(text: str, *, source_root: Path, sample_root: Path) -> str:
    """Bound and de-identify subprocess output kept with disposable evidence."""

    normalized = str(text or "").replace(str(sample_root), "<SAMPLE_ROOT>")
    normalized = normalized.replace(str(source_root), "<SOURCE_ROOT>")
    normalized = normalized.replace(str(Path.home()), "<HOME>")
    encoded = normalized.encode("utf-8", errors="replace")
    if len(encoded) <= DIAGNOSTIC_STREAM_LIMIT:
        return normalized
    clipped = encoded[:DIAGNOSTIC_STREAM_LIMIT].decode("utf-8", errors="replace")
    return clipped + "\n<TRUNCATED>\n"


def _write_execution_diagnostic(
    sample_root: Path,
    *,
    source_root: Path,
    job_id: str,
    returncode: int | None,
    stdout: str,
    stderr: str,
    semantic_success: bool,
    dependency_evidence: Mapping[str, Any] | None,
    fixture_binding_receipt: Mapping[str, Any],
) -> dict[str, str]:
    """Persist bounded execution detail while exported evidence carries hashes."""

    relative = Path("diagnostics") / "execution.json"
    target = sample_root / relative
    target.parent.mkdir(parents=True, exist_ok=False)
    payload = {
        "schema": "magi.v3.schedule-execution-diagnostic/v1",
        "job_id": job_id,
        "returncode": returncode,
        "semantic_success": semantic_success,
        "fixture_binding_sha256": fixture_binding_receipt["sha256"],
        "dependency_evidence": dict(dependency_evidence or {}),
        "stdout": _diagnostic_text(
            stdout, source_root=source_root, sample_root=sample_root
        ),
        "stderr": _diagnostic_text(
            stderr, source_root=source_root, sample_root=sample_root
        ),
    }
    target.write_bytes(_canonical_json(payload) + b"\n")
    return {
        "diagnostic_evidence_relative_path": relative.as_posix(),
        "diagnostic_evidence_sha256": _sha256_file(target),
        "fixture_binding_sha256": str(fixture_binding_receipt["sha256"]),
    }


def _owned_empty_workdir(path: Path) -> Path:
    if path.is_symlink():
        raise ScheduleBodyRegistryError("registry workdir must not be a symlink")
    resolved = path.expanduser().resolve()
    if resolved == REPO_ROOT or _is_relative_to(resolved, REPO_ROOT):
        raise ScheduleBodyRegistryError("registry workdir must be outside the source tree")
    if resolved == LIVE_ROOT or _is_relative_to(resolved, LIVE_ROOT):
        raise ScheduleBodyRegistryError("registry workdir must be outside live MAGI state")
    if resolved.exists():
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise ScheduleBodyRegistryError("registry workdir must be an empty directory")
    else:
        resolved.mkdir(parents=True)
    (resolved / ".magi-v3-schedule-body-registry-workdir").write_text(
        "owned disposable schedule body evidence\n", encoding="utf-8"
    )
    return resolved


def _load_registry(source_root: Path, jobs: list[dict[str, Any]], cron_sha: str) -> dict[str, Any]:
    path = source_root / REGISTRY_PATH
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScheduleBodyRegistryError(f"adapter registry unreadable: {exc}") from exc
    if not isinstance(registry, dict) or registry.get("schema_version") != 1:
        raise ScheduleBodyRegistryError("adapter registry schema_version must be 1")
    if registry.get("status") != "incomplete":
        raise ScheduleBodyRegistryError("adapter registry must not claim completion")
    binding = registry.get("release_binding")
    if not isinstance(binding, dict):
        raise ScheduleBodyRegistryError("adapter registry release binding is malformed")
    source_cron_sha = binding.get("cron_jobs_source_sha256")
    if not isinstance(source_cron_sha, str) or not HEX64.fullmatch(source_cron_sha):
        raise ScheduleBodyRegistryError("adapter registry source cron binding is malformed")
    logical_sha = _logical_definition_sha256(jobs)
    if binding.get("logical_definition_sha256") != logical_sha:
        raise ScheduleBodyRegistryError("adapter registry logical cron binding drifted")
    baseline = source_root / BASELINE_PATH
    if binding.get("inherited_baseline_sha256") != _sha256_file(baseline):
        raise ScheduleBodyRegistryError("inherited adapter baseline binding drifted")
    return registry


def _parse_command(job: Mapping[str, Any]) -> list[str]:
    try:
        tokens = shlex.split(str(job.get("command") or ""), posix=True)
    except ValueError as exc:
        raise ScheduleBodyRegistryError(
            f"cron command is not parseable for {job.get('id')}: {exc}"
        ) from exc
    if not tokens:
        raise ScheduleBodyRegistryError(f"cron command is empty for {job.get('id')}")
    return tokens


def _relative_entrypoint(source_root: Path, token: str) -> str:
    if token.startswith("<MAGI_ROOT>/"):
        return token.removeprefix("<MAGI_ROOT>/")
    path = Path(token)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(source_root.resolve()).as_posix()
        except ValueError:
            return str(path)
    return path.as_posix()


def actual_entrypoint(source_root: Path, job: Mapping[str, Any]) -> tuple[str, str]:
    tokens = _parse_command(job)
    if tokens[0].startswith("@MAGI") or str(job.get("command") or "").startswith("@MAGI"):
        prompt = str(job.get("command") or "").removeprefix("@MAGI").strip()
        reviewed = resolve_exact_cron_macro(prompt)
        if reviewed is not None:
            return reviewed.entrypoint, "reviewed_cron_macro"
        return "@MAGI", "orchestrator_macro"
    canonical = canonical_command_tokens(str(job.get("command") or ""))
    candidates = [token for token in canonical if token.endswith((".py", ".sh"))]
    if not candidates:
        return tokens[0], "executable"
    entrypoint = _relative_entrypoint(source_root, candidates[-1])
    suffix = Path(entrypoint).suffix.lstrip(".") or "executable"
    return entrypoint, suffix


def _blocker_reasons(job: Mapping[str, Any], entrypoint: str) -> list[str]:
    command = str(job.get("command") or "").lower()
    reasons: list[str] = []
    explicit = {
        # These generic dispatcher/benchmark names hide provider, storage, or
        # production-state dependencies that are visible only inside the body.
        "job_benchmark_pdf_namer": {
            "LIVE_MODEL_OR_SERVICE_REQUIRED", "NAS_OR_EXTERNAL_STORAGE_REQUIRED",
        },
        "job_file_review_check": {
            "EXTERNAL_PROVIDER_OR_LOGIN_REQUIRED", "NAS_OR_EXTERNAL_STORAGE_REQUIRED",
            "PRODUCTION_MUTATION_OR_IRREVERSIBLE_SIDE_EFFECT",
        },
        "job_laf_condition_draft": {
            "EXTERNAL_PROVIDER_OR_LOGIN_REQUIRED",
            "PRODUCTION_MUTATION_OR_IRREVERSIBLE_SIDE_EFFECT",
        },
        "job_laf_nightly_audit": {
            "EXTERNAL_PROVIDER_OR_LOGIN_REQUIRED", "NAS_OR_EXTERNAL_STORAGE_REQUIRED",
            "PRODUCTION_DATABASE_OR_INDEX_REQUIRED",
            "PRODUCTION_MUTATION_OR_IRREVERSIBLE_SIDE_EFFECT",
        },
        "job_laf_pending_scan": {
            "PRODUCTION_DATABASE_OR_INDEX_REQUIRED",
            "PRODUCTION_MUTATION_OR_IRREVERSIBLE_SIDE_EFFECT",
        },
        "job_nightly_autopilot": {
            "EXTERNAL_PROVIDER_OR_LOGIN_REQUIRED", "LIVE_MODEL_OR_SERVICE_REQUIRED",
            "NAS_OR_EXTERNAL_STORAGE_REQUIRED", "PRODUCTION_DATABASE_OR_INDEX_REQUIRED",
            "PRODUCTION_MUTATION_OR_IRREVERSIBLE_SIDE_EFFECT",
        },
        "job_nightly_regression": {"LIVE_MODEL_OR_SERVICE_REQUIRED"},
        "job_obsidian_ingest": {
            "NAS_OR_EXTERNAL_STORAGE_REQUIRED", "PRODUCTION_DATABASE_OR_INDEX_REQUIRED",
            "PRODUCTION_MUTATION_OR_IRREVERSIBLE_SIDE_EFFECT",
        },
        "job_pdf_namer_nightly": {
            "LIVE_MODEL_OR_SERVICE_REQUIRED", "NAS_OR_EXTERNAL_STORAGE_REQUIRED",
        },
        "job_translator_ape_regression": {"LIVE_MODEL_OR_SERVICE_REQUIRED"},
        "pdfnamer_docling_layout": {
            "LIVE_MODEL_OR_SERVICE_REQUIRED", "NAS_OR_EXTERNAL_STORAGE_REQUIRED",
            "PRODUCTION_MUTATION_OR_IRREVERSIBLE_SIDE_EFFECT",
        },
    }
    reasons.extend(explicit.get(str(job.get("id") or ""), set()))
    if command.startswith("@magi"):
        reasons.append("ORCHESTRATOR_MACRO_REQUIRES_LIVE_AGENT")
    if any(
        token in command
        for token in (
            "gmail", "google", "drive", "portal", "judicial", "worldmonitor",
            "token_health", "calendar", "transcript_sync", "research-brief",
            "market-briefing", "judgment-collector",
        )
    ):
        reasons.append("EXTERNAL_PROVIDER_OR_LOGIN_REQUIRED")
    if any(
        token in command
        for token in ("nas_", "synology", ".magi_mounts", "slow_archive", "drive_case_sync")
    ):
        reasons.append("NAS_OR_EXTERNAL_STORAGE_REQUIRED")
    if any(
        token in command
        for token in ("live", "omlx", "model", "translation", "external_chat", "distill")
    ):
        reasons.append("LIVE_MODEL_OR_SERVICE_REQUIRED")
    if any(
        token in command
        for token in (
            "accounting", "cortex", "vector", "index_cases", "reindex",
            "purge_persona", "insight", "magi_self_repair_guardian",
        )
    ):
        reasons.append("PRODUCTION_DATABASE_OR_INDEX_REQUIRED")
    if any(
        token in command
        for token in (
            "--apply", "--commit", "repair-safe", "cleanup", "purge", "sync",
            "backup", "switch_model", "auto_skill_import",
        )
    ):
        reasons.append("PRODUCTION_MUTATION_OR_IRREVERSIBLE_SIDE_EFFECT")
    if not reasons:
        reasons.append("LOCAL_BODY_NOT_YET_REVIEWED_FOR_FIXTURE_ISOLATION")
    if entrypoint.startswith("/"):
        reasons.append("ENTRYPOINT_OUTSIDE_RELEASE_SOURCE_TREE")
    return sorted(set(reasons))


def _validate_dependency(job_id: str, dependency: Any) -> None:
    if not isinstance(dependency, Mapping):
        raise ScheduleBodyRegistryError(f"adapter {job_id} dependency is malformed")
    kind = dependency.get("kind")
    if kind == "localhost_http_and_disposable_mariadb":
        http = dependency.get("http")
        database = dependency.get("database")
        if (
            not isinstance(http, Mapping)
            or http.get("kind") != "localhost_http"
            or not isinstance(database, Mapping)
            or database.get("kind") != "disposable_mariadb"
        ):
            raise ScheduleBodyRegistryError(
                f"adapter {job_id} composite dependency is malformed"
            )
        _validate_dependency(job_id, http)
        _validate_dependency(job_id, database)
        return
    if kind not in {"localhost_http", "disposable_mariadb"}:
        raise ScheduleBodyRegistryError(f"adapter {job_id} dependency is malformed")
    expected = dependency.get("expected_requests")
    if not isinstance(expected, Mapping) or not expected:
        raise ScheduleBodyRegistryError(f"adapter {job_id} dependency transcript is incomplete")
    if kind == "localhost_http":
        routes = dependency.get("routes")
        if not isinstance(routes, Mapping) or not routes:
            raise ScheduleBodyRegistryError(f"adapter {job_id} HTTP dependency is incomplete")
        if not set(expected).issubset(routes):
            raise ScheduleBodyRegistryError(
                f"adapter {job_id} expects an undefined dependency route"
            )
        for path, response in routes.items():
            if not str(path).startswith("/") or not isinstance(response, Mapping):
                raise ScheduleBodyRegistryError(
                    f"adapter {job_id} dependency route is malformed"
                )
            status = response.get("status", 200)
            if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status < 600:
                raise ScheduleBodyRegistryError(
                    f"adapter {job_id} dependency status is invalid"
                )
    elif not isinstance(dependency.get("seed_sql"), list) or not dependency.get("seed_sql"):
        raise ScheduleBodyRegistryError(f"adapter {job_id} MariaDB seed is missing")
    postconditions = dependency.get("postconditions", [])
    if not isinstance(postconditions, list) or any(
        not isinstance(item, Mapping)
        or not str(item.get("sql") or "").strip()
        or "equals" not in item
        for item in postconditions
    ):
        raise ScheduleBodyRegistryError(
            f"adapter {job_id} dependency postcondition is malformed"
        )
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 1
        for count in expected.values()
    ):
        raise ScheduleBodyRegistryError(
            f"adapter {job_id} dependency request count is invalid"
        )


def _validate_new_adapter(adapter: Mapping[str, Any]) -> None:
    job_id = str(adapter.get("job_id") or "")
    argv = adapter.get("argv")
    env = adapter.get("environment")
    if not job_id or not isinstance(argv, list) or not argv or not isinstance(env, dict):
        raise ScheduleBodyRegistryError("new adapter is malformed")
    lowered = [str(value).lower() for value in argv]
    if any(value in {"--help", "-h"} for value in lowered):
        raise ScheduleBodyRegistryError(f"adapter {job_id} uses forbidden help output")
    joined = " ".join(lowered)
    if "dispatcher" in joined or "time.sleep" in joined or "fake_handler" in joined:
        raise ScheduleBodyRegistryError(f"adapter {job_id} is not a real body adapter")
    if "-c" in argv:
        raise ScheduleBodyRegistryError(f"adapter {job_id} may not use inline stand-in code")
    if not str(adapter.get("production_entrypoint") or "").endswith((".py", ".sh")):
        raise ScheduleBodyRegistryError(f"adapter {job_id} lacks a real entrypoint")
    if not str(adapter.get("fixture_kind") or ""):
        raise ScheduleBodyRegistryError(f"adapter {job_id} lacks a fixture kind")
    if not isinstance(adapter.get("success_contract"), dict):
        raise ScheduleBodyRegistryError(f"adapter {job_id} lacks a success contract")
    success_contract = adapter["success_contract"]
    system_diagnostic_job = job_id in SYSTEM_DIAGNOSTIC_JOB_IDS
    system_diagnostic_contract = (
        success_contract.get("type") == SYSTEM_DIAGNOSTIC_KIND
    )
    if system_diagnostic_job != system_diagnostic_contract:
        raise ScheduleBodyRegistryError(
            f"adapter {job_id} has an invalid system diagnostic contract type"
        )
    if system_diagnostic_contract:
        if (
            success_contract.get("warning_allowlist")
            != sorted(SYSTEM_DIAGNOSTIC_RESOURCE_WARNING_CODES)
        ):
            raise ScheduleBodyRegistryError(
                f"adapter {job_id} has an invalid system diagnostic warning allowlist"
            )
    dependency = adapter.get("dependency")
    if dependency is not None:
        _validate_dependency(job_id, dependency)


def resolve_registry(
    source_root: Path,
    registry: Mapping[str, Any],
    jobs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    enabled = {str(job.get("id") or ""): job for job in jobs if job.get("enabled") is True}
    baseline = json.loads((source_root / BASELINE_PATH).read_text(encoding="utf-8"))
    legacy_list = baseline.get("representative_body_allowlist")
    if not isinstance(legacy_list, list):
        raise ScheduleBodyRegistryError("inherited adapter allowlist is malformed")
    legacy = {
        str(item.get("job_id") or ""): item
        for item in legacy_list
        if isinstance(item, dict)
    }
    raw_new = registry.get("new_safe_adapters")
    if not isinstance(raw_new, list):
        raise ScheduleBodyRegistryError("new safe adapter list is malformed")
    new: dict[str, Mapping[str, Any]] = {}
    for raw in raw_new:
        if not isinstance(raw, dict):
            raise ScheduleBodyRegistryError("new safe adapter must be an object")
        _validate_new_adapter(raw)
        job_id = str(raw["job_id"])
        if job_id in new or job_id in legacy:
            raise ScheduleBodyRegistryError(f"duplicate safe adapter: {job_id}")
        new[job_id] = raw
    safe_ids = set(legacy) | set(new)
    if not safe_ids.issubset(enabled):
        raise ScheduleBodyRegistryError("safe adapter references a disabled or missing job")

    entries: list[dict[str, Any]] = []
    for job_id in sorted(enabled):
        job = enabled[job_id]
        entrypoint, entrypoint_kind = actual_entrypoint(source_root, job)
        command_sha = hashlib.sha256(str(job.get("command") or "").encode()).hexdigest()
        if job_id in legacy:
            expected = str(legacy[job_id].get("entrypoint") or "")
            if entrypoint != expected:
                raise ScheduleBodyRegistryError(f"inherited entrypoint drifted for {job_id}")
            classification = "safe_adapter"
            runner = "inherited_real_entrypoint_dry_run_v1"
            blockers: list[str] = []
        elif job_id in new:
            expected = str(new[job_id].get("production_entrypoint") or "")
            if entrypoint != expected:
                raise ScheduleBodyRegistryError(f"new adapter entrypoint drifted for {job_id}")
            classification = "safe_adapter"
            runner = ADAPTER_MODE
            blockers = []
        else:
            classification = "blocked"
            runner = "none"
            blockers = _blocker_reasons(job, entrypoint)
        entries.append(
            {
                "job_id": job_id,
                "classification": classification,
                "runner": runner,
                "actual_entrypoint": entrypoint,
                "entrypoint_kind": entrypoint_kind,
                "production_command_sha256": command_sha,
                "blockers": blockers,
            }
        )
    if len(entries) != len(enabled) or {row["job_id"] for row in entries} != set(enabled):
        raise ScheduleBodyRegistryError("registry completeness invariant failed")
    if any((row["classification"] == "blocked") != bool(row["blockers"]) for row in entries):
        raise ScheduleBodyRegistryError("registry blocker invariant failed")
    return entries, legacy, new


def _quoted(path: Path) -> str:
    return json.dumps(str(path.resolve()))


def _seatbelt_profile(
    source_root: Path,
    allowed_root: Path,
    *,
    allowed_localhost_ports: Sequence[int] = (),
) -> str:
    denied_subpaths = (
        source_root / ".runtime",
        source_root / ".agent",
        source_root / "閱卷下載",
        Path("/Volumes"),
        Path.home() / ".magi_mounts",
        Path.home() / "Library" / "CloudStorage",
        Path.home() / "Library" / "Keychains",
        Path.home() / ".ssh",
        Path("/opt/homebrew/var/mysql"),
    )
    denied_literals = (source_root / ".env", source_root / "config" / "credentials.json")
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        '(allow file-write* (literal "/dev/null"))',
        f"(allow file-write* (subpath {_quoted(allowed_root)}))",
    ]
    for port in allowed_localhost_ports:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ScheduleBodyRegistryError("Seatbelt localhost port is invalid")
        rules.append(f'(allow network-outbound (remote ip "localhost:{port}"))')
    rules.extend(f"(deny file-read* (subpath {_quoted(path)}))" for path in denied_subpaths)
    rules.extend(f"(deny file-read* (literal {_quoted(path)}))" for path in denied_literals)
    return "".join(rules)


def _expand(
    value: str,
    *,
    source_root: Path,
    fixture_root: Path,
    python: Path,
    mock_port: int | None = None,
    database_port: int | None = None,
) -> str:
    expanded = (
        value.replace("<ROOT>", str(source_root))
        .replace("<FIXTURE>", str(fixture_root))
        .replace("<PYTHON>", str(python))
    )
    if "<MOCK_PORT>" in expanded:
        if mock_port is None:
            raise ScheduleBodyRegistryError("adapter references a missing localhost dependency")
        expanded = expanded.replace("<MOCK_PORT>", str(mock_port))
    if "<DB_PORT>" in expanded:
        if database_port is None:
            raise ScheduleBodyRegistryError(
                "adapter references a missing disposable database dependency"
            )
        expanded = expanded.replace("<DB_PORT>", str(database_port))
    return expanded


def _expand_dependency_spec(
    value: Any,
    *,
    source_root: Path,
    fixture_root: Path,
    python: Path,
) -> Any:
    """Bind fixture paths inside disposable dependency seed/postcondition data."""

    if isinstance(value, Mapping):
        return {
            str(key): _expand_dependency_spec(
                item,
                source_root=source_root,
                fixture_root=fixture_root,
                python=python,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _expand_dependency_spec(
                item,
                source_root=source_root,
                fixture_root=fixture_root,
                python=python,
            )
            for item in value
        ]
    if isinstance(value, str):
        if "<MOCK_PORT>" in value:
            raise ScheduleBodyRegistryError(
                "dependency seed data may not reference its not-yet-bound port"
            )
        return (
            value.replace("<ROOT>", str(source_root))
            .replace("<FIXTURE>", str(fixture_root))
            .replace("<PYTHON>", str(python))
        )
    return value


@contextmanager
def _localhost_http_dependency(spec: Mapping[str, Any] | None):
    """Serve deterministic provider responses and capture the real body's calls."""

    if spec is None:
        yield {
            "kind": "none",
            "port": None,
            "requests": [],
            "expected_requests": {},
            "match_mode": "exact",
        }
        return
    routes = dict(spec["routes"])
    requests: list[dict[str, Any]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _respond(self, method: str) -> None:
            response = routes.get(self.path)
            request_row: dict[str, Any] = {
                "method": method,
                "path": self.path,
                "matched": response is not None,
            }
            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    length = 0
                raw = self.rfile.read(max(0, min(length, 1_000_000))) if length else b""
                request_row["body_sha256"] = hashlib.sha256(raw).hexdigest()
                try:
                    request_row["json"] = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    request_row["json"] = None
            requests.append(request_row)
            if not isinstance(response, dict):
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"not_found"}')
                return
            if "raw" in response:
                body = str(response.get("raw") or "").encode("utf-8")
                content_type = str(response.get("content_type") or "text/plain; charset=utf-8")
            else:
                body = _canonical_json(response.get("json", {}))
                content_type = "application/json"
            self.send_response(int(response.get("status", 200)))
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            self._respond("POST")

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="magi-v3-schedule-http", daemon=True)
    thread.start()
    try:
        yield {
            "kind": "localhost_http",
            "port": int(server.server_address[1]),
            "requests": requests,
            "expected_requests": dict(spec["expected_requests"]),
            "match_mode": "exact",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _reserve_localhost_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


@contextmanager
def _disposable_mariadb_dependency(
    spec: Mapping[str, Any], sample_root: Path
):
    """Start a throwaway MariaDB schema and expose only its random loopback port."""

    install = _trusted_dependency_executable("mariadb-install-db", "mysql_install_db")
    server_bin = _trusted_dependency_executable("mariadbd", "mysqld")
    datadir = sample_root / "mariadb-data"
    pid_path = sample_root / "mariadb.pid"
    error_log = sample_root / "mariadb-error.log"
    general_log = sample_root / "mariadb-general.log"
    install_result = subprocess.run(
        [
            install,
            "--no-defaults",
            f"--datadir={datadir}",
            "--auth-root-authentication-method=normal",
            "--skip-test-db",
            "--skip-name-resolve",
        ],
        cwd=sample_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    if install_result.returncode != 0:
        diagnostic = (install_result.stderr or install_result.stdout or "").strip()
        encoded = diagnostic.encode("utf-8", errors="replace")
        bounded = encoded[-4096:].decode("utf-8", errors="replace")
        raise ScheduleBodyRegistryError(
            "disposable MariaDB initialization failed "
            f"(rc={install_result.returncode}): {bounded}"
        )
    port = _reserve_localhost_port()
    # MariaDB rejects Unix socket paths longer than 103 bytes. Pytest/Codex
    # workdirs on macOS are much longer, so use a unique harness-owned /tmp
    # socket while all database files and logs remain under the sample root.
    socket_path = Path("/tmp") / f"magi-v3-db-{os.getpid()}-{port}.sock"
    process = subprocess.Popen(
        [
            server_bin,
            "--no-defaults",
            f"--datadir={datadir}",
            "--bind-address=127.0.0.1",
            f"--port={port}",
            f"--socket={socket_path}",
            f"--pid-file={pid_path}",
            f"--log-error={error_log}",
            "--skip-grant-tables",
            "--skip-name-resolve",
            "--general-log=1",
            f"--general-log-file={general_log}",
        ],
        cwd=sample_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    connection = None
    try:
        import mysql.connector

        deadline = time.monotonic() + 20
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                connection = mysql.connector.connect(
                    host="127.0.0.1",
                    port=port,
                    user="root",
                    password="",
                    connection_timeout=1,
                    use_pure=True,
                )
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.05)
        if connection is None:
            raise ScheduleBodyRegistryError(
                f"disposable MariaDB did not become ready: {type(last_error).__name__}"
            )
        cursor = connection.cursor()
        try:
            for statement in spec["seed_sql"]:
                cursor.execute(str(statement))
            connection.commit()
        finally:
            cursor.close()
            connection.close()
            connection = None

        def snapshot() -> list[dict[str, Any]]:
            try:
                flush = mysql.connector.connect(
                    host="127.0.0.1",
                    port=port,
                    user="root",
                    password="",
                    connection_timeout=1,
                    use_pure=True,
                )
                flush.close()
            except Exception:
                pass
            try:
                lines = general_log.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            return [{"path": line} for line in lines]

        def verify() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            if not spec.get("postconditions"):
                return results
            check = mysql.connector.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                password="",
                connection_timeout=2,
                use_pure=True,
            )
            cursor = check.cursor()
            try:
                for item in spec["postconditions"]:
                    cursor.execute(str(item["sql"]))
                    row = cursor.fetchone()
                    actual = row[0] if row else None
                    expected = item["equals"]
                    results.append(
                        {
                            "sql_sha256": hashlib.sha256(str(item["sql"]).encode()).hexdigest(),
                            "actual": actual,
                            "expected": expected,
                            "passed": actual == expected,
                        }
                    )
            finally:
                cursor.close()
                check.close()
            return results

        yield {
            "kind": "disposable_mariadb",
            "port": port,
            "requests": [],
            "snapshot": snapshot,
            "verify": verify,
            "expected_requests": dict(spec["expected_requests"]),
            "match_mode": "contains",
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        # A fresh MariaDB datadir is roughly 60 MB. Capacity certification
        # executes three samples for every enabled job, so retaining each
        # throwaway datadir can consume tens of gigabytes and make the
        # resource-governor test fail because of disk pressure created by the
        # certifier itself. The dependency transcript and postconditions have
        # already been reduced to hash-bound evidence before this context
        # exits; the database files are no longer evidence.
        if datadir.exists():
            resolved_sample = sample_root.resolve(strict=True)
            resolved_datadir = datadir.resolve(strict=True)
            if (
                datadir.is_symlink()
                or resolved_datadir.parent != resolved_sample
                or resolved_datadir.name != "mariadb-data"
            ):
                raise ScheduleBodyRegistryError(
                    "disposable MariaDB cleanup ownership is invalid"
                )
            shutil.rmtree(resolved_datadir)
            if resolved_datadir.exists() or resolved_datadir.is_symlink():
                raise ScheduleBodyRegistryError(
                    "disposable MariaDB cleanup did not complete"
                )


@contextmanager
def _dependency_fixture(spec: Mapping[str, Any] | None, sample_root: Path):
    if spec is None or spec.get("kind") == "localhost_http":
        with _localhost_http_dependency(spec) as dependency:
            yield dependency
        return
    if spec.get("kind") == "disposable_mariadb":
        with _disposable_mariadb_dependency(spec, sample_root) as dependency:
            yield dependency
        return
    if spec.get("kind") == "localhost_http_and_disposable_mariadb":
        with _localhost_http_dependency(spec["http"]) as http_dependency:
            with _disposable_mariadb_dependency(
                spec["database"], sample_root
            ) as database_dependency:
                expected = {
                    **dict(http_dependency["expected_requests"]),
                    **dict(database_dependency["expected_requests"]),
                }

                def snapshot() -> list[dict[str, Any]]:
                    http_rows = [
                        {"dependency": "localhost_http", **dict(row)}
                        for row in http_dependency["requests"]
                    ]
                    database_snapshot = database_dependency.get("snapshot")
                    database_rows = (
                        database_snapshot() if callable(database_snapshot) else []
                    )
                    return http_rows + [
                        {"dependency": "disposable_mariadb", **dict(row)}
                        for row in database_rows
                    ]

                yield {
                    "kind": "localhost_http_and_disposable_mariadb",
                    "port": http_dependency["port"],
                    "database_port": database_dependency["port"],
                    "ports": [http_dependency["port"], database_dependency["port"]],
                    "requests": http_dependency["requests"],
                    "snapshot": snapshot,
                    "verify": database_dependency.get("verify"),
                    "expected_requests": expected,
                    "match_mode": "contains",
                }
        return
    raise ScheduleBodyRegistryError("unknown schedule dependency kind")


def _make_pdf(path: Path) -> None:
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), "INDICTMENT DOCUMENT HEADER\nCase 2099 fixture")
    second = document.new_page()
    second.insert_text((72, 72), "SECOND DOCUMENT HEADER\nFixture body")
    document.set_toc([[1, "起訴書", 1], [1, "附件", 2]])
    document.save(path)
    document.close()


def _osc_events_fixture_expectation(
    *, now: datetime | None = None
) -> dict[str, str]:
    """Bind the OSC hearing fixture to a future Taipei date.

    A former fixed 2026-07-20 hearing became historical on the next day and
    made the validator reject the production entrypoint for correctly
    classifying it as a past todo.  A 30-day horizon stays future even when a
    campaign crosses midnight, while the expectation artifact keeps every
    sample and its success contract hash-bound.
    """

    current = now or datetime.now(TAIPEI_TIMEZONE)
    if current.tzinfo is None:
        raise ScheduleBodyRegistryError("OSC events fixture time must be timezone-aware")
    hearing_date = current.astimezone(TAIPEI_TIMEZONE).date() + timedelta(
        days=OSC_EVENTS_FIXTURE_HORIZON_DAYS
    )
    roc_year = hearing_date.year - 1911
    hearing_time = "10:00"
    file_name = (
        f"{hearing_date:%Y%m%d} 臺灣測試地方法院通知"
        f"（測試當事人；訂{roc_year}年{hearing_date.month}月"
        f"{hearing_date.day}日上午10時開庭）.pdf"
    )
    return {
        "schema": "magi.v3.osc-events-fixture-expectation/v1",
        "hearing_date": hearing_date.isoformat(),
        "hearing_time": hearing_time,
        "file_name": file_name,
    }


def _synthetic_practice_judgment_text() -> str:
    """Return a source-complete, synthetic judgment for quality-gate fixtures.

    The text deliberately contains separate rule and application paragraphs.
    Repeating a generic sentence used to make the adapter look substantial but
    is now (correctly) rejected by the production practical-insight gate.
    """

    return (
        "最高法院民事判決\n"
        "主文\n"
        "被告應給付原告損害賠償。\n"
        "理由\n"
        "按，民法第184條規定，因故意或過失不法侵害他人之權利者，"
        "負損害賠償責任；侵權行為損害賠償請求人就不法行為、損害、"
        "因果關係及行為人之故意或過失，應負舉證責任。\n\n"
        "次按，民法第213條規定，負損害賠償責任者，除法律另有規定或契約"
        "另有訂定外，應回復他方損害發生前之原狀；因回復原狀而應給付金錢"
        "者，自損害發生時起，加給利息。\n\n"
        "又按，民法第216條規定，損害賠償除法律另有規定或契約另有訂定"
        "外，應以填補債權人所受損害及所失利益為限；所失利益必須依通常"
        "情形或已定之計畫可預期取得，請求人對其數額仍應負舉證責任。\n\n"
        "再按，數人共同不法侵害他人之權利者，對被害人所受損害應負連帶"
        "賠償責任；但各行為與損害間仍應存有相當因果關係，始得適用。\n\n"
        "本院認為，本件證據足以證明被告之過失行為與原告所受損害間"
        "具有相當因果關係，原告已盡舉證責任，應認其損害賠償請求有理由。\n"
        "原告請求之金額，其中已由單據及計算說明證明之部分，屬於必要"
        "費用與可預期之所失利益；其餘未提出具體證據之部分，尚難認已盡"
        "舉證責任，不得併為賠償範圍。\n"
        "本院審酌全部證據後，認定侵害行為發生的時間、處所與損害結果"
        "彼此衔接，且沒有足以中斷因果關係的獨立事由，故損害結果屬於"
        "一般可預見的範圍。\n"
        "至於被告所辨已盡相當注意一節，與現存書證、現場記錄與行為前後"
        "客觀狀態不符，無從動搖法院根據完整證據所形成的心證，自不足為有利"
        "於被告的認定。\n"
        "中華民國二百零九年一月二日\n"
    )


def _prepare_fixture(
    kind: str,
    fixture_root: Path,
    job_id: str,
    *,
    source_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    fixture_root.mkdir(parents=True, exist_ok=False)
    marker = fixture_root / ".magi-v3-schedule-fixture"
    marker.write_text(job_id + "\n", encoding="utf-8")
    if kind in {
        "product_cortex_sync_terminal",
        "product_autopilot_tick_terminal",
        "product_autopilot_nightly_terminal",
        "product_operational_hardening_terminal",
    }:
        match = re.fullmatch(r"sample-(\d{3})", fixture_root.parent.name)
        if not match or int(match.group(1)) not in range(1, MIN_SUCCESSFUL_SAMPLES + 1):
            raise ScheduleBodyRegistryError(
                "non-storage fixture requires a bound three-sample directory"
            )
        populate_nonstorage_fixture(
            fixture_root,
            job_id=job_id,
            sample_id=int(match.group(1)),
        )
    elif kind in {
        "product_business_module_live",
        "product_heavy_translation_quality",
        "product_distill_train_gemma",
        "product_insight_sync",
        "product_reprocess_insights",
    }:
        match = re.fullmatch(r"sample-(\d{3})", fixture_root.parent.name)
        if not match or int(match.group(1)) not in range(1, MIN_SUCCESSFUL_SAMPLES + 1):
            raise ScheduleBodyRegistryError(
                "product fixture requires a bound three-sample directory"
            )
        populate_product_fixture(
            fixture_root,
            job_id=job_id,
            sample_id=int(match.group(1)),
        )
    elif kind == "market_watchlist_backup":
        agent = fixture_root / "agent"
        agent.mkdir()
        (agent / "market_watchlist.json").write_text(
            json.dumps({"watchlist": ["2330.TW", "AAPL", "NVDA"]}), encoding="utf-8"
        )
    elif kind == "system_diagnostic_reviewed_macro":
        runtime = fixture_root / "runtime"
        runtime.mkdir()
        (fixture_root / "cron_jobs.json").write_text(
            json.dumps(
                [
                    {
                        "id": "fixture-health",
                        "cron": "0 9 * * *",
                        "command": "fixture",
                        "enabled": True,
                    }
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (runtime / "cron_state.json").write_text("{}\n", encoding="utf-8")
    elif kind in {"obsidian_vector_notes", "obsidian_vector_wiki"}:
        agent = fixture_root / "agent"
        vault = fixture_root / "vault"
        folder = "20_Notes" if kind == "obsidian_vector_notes" else "30_Wiki"
        notes = vault / folder
        agent.mkdir()
        notes.mkdir(parents=True)
        (agent / "obsidian_vault_config.json").write_text(
            json.dumps({"vault_path": str(vault), "vault_name": "fixture"}),
            encoding="utf-8",
        )
        (agent / "obsidian_index.json").write_text(
            '{"notes":{},"updated_at":""}\n', encoding="utf-8"
        )
        (notes / "fixture.md").write_text(
            "---\ntags: [fixture, legal]\n---\n# 隔離筆記\n\n"
            + "法院認為本文件僅用於驗證真實 Obsidian 向量匯入流程與資料庫交易。" * 12
            + "\n",
            encoding="utf-8",
        )
    elif kind == "obsidian_source_ingest":
        agent = fixture_root / "agent"
        vault = fixture_root / "vault"
        source = (
            fixture_root
            / "source-cases"
            / "2026-0001-隔離當事人-民事-一審-測試"
            / "04_我方歷次書狀"
        )
        agent.mkdir()
        (vault / "20_Notes").mkdir(parents=True)
        source.mkdir(parents=True)
        (agent / "obsidian_vault_config.json").write_text(
            json.dumps({"vault_path": str(vault), "vault_name": "fixture"}),
            encoding="utf-8",
        )
        (agent / "obsidian_index.json").write_text(
            '{"notes":{},"updated_at":""}\n', encoding="utf-8"
        )
        (agent / "obsidian_ingest_state.json").write_text(
            '{"files":{},"updated_at":""}\n', encoding="utf-8"
        )
        (source / "fixture-brief.txt").write_text(
            "隔離測試書狀全文。" + "本件僅驗證來源擷取、筆記落盤、向量嵌入與資料庫交易。" * 18,
            encoding="utf-8",
        )
    elif kind == "file_review_staging":
        base = fixture_root / "MAGI-runtime"
        for name in ("閱卷下載", "筆錄下載", "法扶資料"):
            old = base / name / "20000101"
            old.mkdir(parents=True)
            (old / "fixture.txt").write_text("disposable staging\n", encoding="utf-8")
    elif kind == "obsidian_duplicates":
        agent = fixture_root / "agent"
        agent.mkdir()
        (agent / "obsidian_index.json").write_text('{"notes":{}}\n', encoding="utf-8")
        notes = fixture_root / "vault" / "20_Notes"
        notes.mkdir(parents=True)
        body = "法院認為本件測試資料僅供隔離驗證，不涉及任何真實案件。" * 40
        content = (
            "---\ncase_number: 2099-0001\nsummary_schema: magi-obsidian-note-v2\n"
            "extraction_quality: ok\n---\n# Fixture\n\n## Full Text\n\n" + body + "\n"
        )
        (notes / "summary__fixture.md").write_text(content, encoding="utf-8")
        (notes / "summary__fixture_1.md").write_text(content, encoding="utf-8")
    elif kind == "obsidian_note_repair":
        agent = fixture_root / "agent"
        vault = fixture_root / "vault"
        source_dir = fixture_root / "source"
        notes = vault / "20_Notes" / "2099-0001-測試當事人-民事-一審-測試"
        agent.mkdir()
        source_dir.mkdir()
        notes.mkdir(parents=True)
        source = source_dir / "fixture-source.txt"
        source_marker = "FIXTURE_REEXTRACTED_SOURCE_2099_0001"
        source.write_text(
            source_marker
            + "\n"
            + "法院認為本隔離測試文件僅用來驗證筆記修復、重新擷取與索引更新。" * 32
            + "\n",
            encoding="utf-8",
        )
        relative = (
            "20_Notes/2099-0001-測試當事人-民事-一審-測試/summary__fixture.md"
        )
        note = (
            "---\n"
            "summary_schema: magi-obsidian-note-v1\n"
            f"source_path: {source}\n"
            "source_relpath: fixture-source.txt\n"
            "source_root: fixture\n"
            "file_type: txt\n"
            "case_number: 2099-0001\n"
            "client_name: 測試當事人\n"
            "doc_key: fixture-repair\n"
            "file_hash: fixture-old\n"
            "mtime: 1\n"
            "extraction_method: markitdown\n"
            "extraction_quality: weak\n"
            "---\n"
            "# Fixture Weak Note\n\n"
            "## Full Text\n\n弱內容。\n"
        )
        (vault / relative).write_text(note, encoding="utf-8")
        (agent / "obsidian_vault_config.json").write_text(
            json.dumps({"vault_path": str(vault), "vault_name": "fixture"}),
            encoding="utf-8",
        )
        (agent / "obsidian_index.json").write_text(
            json.dumps(
                {
                    "notes": {
                        relative: {
                            "hash": "0000000000000000",
                            "mtime": 1,
                            "doc_key": "fixture-repair",
                            "chunks": 0,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    elif kind == "obsidian_case_cards":
        agent = fixture_root / "agent"
        index_dir = fixture_root / "vault" / "30_Index"
        agent.mkdir()
        index_dir.mkdir(parents=True)
        (index_dir / "2099-0001.md").write_text(
            "---\ntype: stale-fixture-card\n---\n\n# Must be replaced\n",
            encoding="utf-8",
        )
    elif kind == "laf_condition_mediation":
        case = fixture_root / "cases" / "2099-0002-測試當事人-民事-一審-調解"
        document = case / "09_法院通知或程序裁定" / "調解不成立證明書.pdf"
        _make_pdf(document)
        for directory in (
            fixture_root / "agent",
            fixture_root / "runtime",
            fixture_root / "static",
            fixture_root / "magi-root" / "json",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (fixture_root / "magi-root" / "json" / "config.json").write_text(
            "{}\n", encoding="utf-8"
        )
    elif kind == "laf_gmail_willingness_provider":
        for directory in (
            fixture_root / "magi-root" / "json",
            fixture_root / "runtime",
            fixture_root / "static",
        ):
            directory.mkdir(parents=True)
        (fixture_root / "magi-root" / "json" / "config.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (fixture_root / "gmail-provider.json").write_text(
            json.dumps(
                {
                    "messages": [
                        {
                            "message_id": "willingness-fixture-1",
                            "subject": "接案意願徵詢－消費者債務清理事件",
                            "notification_type": "接案意願徵詢",
                            "laf_case_number": "1150709-T-051",
                            "client_name": "",
                            "sender": "fixture@example.invalid",
                            "snippet": "請回覆是否願意接案",
                            "body": "本信僅為意願徵詢，不是正式派案通知。",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    elif kind == "laf_portal_download_provider":
        for directory in (
            fixture_root / "magi-root" / "json",
            fixture_root / "static",
        ):
            directory.mkdir(parents=True)
        # Keep the DB fixture path stale on purpose.  The real job must locate
        # this uniquely moved case through the configured law-aid case root
        # before it is allowed to archive any portal attachment.
        case = (
            fixture_root
            / "case-roots"
            / "法扶案件"
            / "民事"
            / "2026-0060-隔離當事人-法扶"
        )
        case.mkdir(parents=True)
        (fixture_root / "magi-root" / "json" / "config.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (fixture_root / "portal-provider.json").write_text(
            json.dumps(
                {
                    "portal_cases": [
                        {
                            "case_number": "1150529-W-002",
                            "client_name": "隔離當事人",
                            "case_type": "民事",
                            "case_reason": "消費者債務清理",
                            "file_list": [
                                "接案通知書_1150529-W-002_1150601.pdf"
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    elif kind == "laf_portal_retry_provider":
        for directory in (
            fixture_root / "magi-root" / "json",
            fixture_root / "agent",
            fixture_root / "runtime",
            fixture_root / "static",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        case = fixture_root / "cases" / "2099-0003-隔離附件重試案"
        case.mkdir(parents=True)
        (fixture_root / "magi-root" / "json" / "config.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (fixture_root / "workflow-provider.json").write_text(
            json.dumps({"allowed_workflows": ["attachment_retry"]}),
            encoding="utf-8",
        )
        (fixture_root / "agent" / "laf_pending_portal_downloads.json").write_text(
            json.dumps(
                {
                    "updated_at": "2099-01-01T00:00:00",
                    "items": [
                        {
                            "laf_case_number": "1990101-T-001",
                            "client_name": "隔離附件當事人",
                            "case_type": "民事",
                            "case_reason": "測試",
                            "case_folder": str(case),
                            "case_number": "2099-0003",
                            "status": "pending_retry",
                            "reason": "review_result_download",
                            "origin_reason": "review_result_download",
                            "tries": 0,
                            "last_try_at": "",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    elif kind == "laf_condition_draft_provider":
        case = fixture_root / "cases" / "2099-0002-隔離當事人-民事-一審-調解"
        document = case / "09_法院通知或程序裁定" / "調解不成立證明書.pdf"
        _make_pdf(document)
        for directory in (
            fixture_root / "agent",
            fixture_root / "runtime",
            fixture_root / "static",
            fixture_root / "magi-root" / "json",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (fixture_root / "magi-root" / "json" / "config.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (fixture_root / "workflow-provider.json").write_text(
            json.dumps({"allowed_workflows": ["condition"]}), encoding="utf-8"
        )
    elif kind == "laf_nightly_audit_bounded":
        repair_case = (
            fixture_root
            / "01_案件"
            / "法扶案件"
            / "民事"
            / "2099-0001-隔離修復案"
        )
        closing_case = (
            fixture_root
            / "01_案件"
            / "法扶案件"
            / "民事"
            / "2099-0002-隔離報結案"
        )
        portal_document = (
            closing_case
            / "01_法扶資料"
            / "接案通知書_1150529-W-002_1150601.pdf"
        )
        repair_case.mkdir(parents=True)
        _make_pdf(portal_document)
        for directory in (
            fixture_root / "agent",
            fixture_root / "runtime",
            fixture_root / "magi-root" / "json",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (fixture_root / "magi-root" / "json" / "config.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (fixture_root / "nightly-profile.json").write_text(
            json.dumps(
                {
                    "schema": "magi.laf-nightly-audit-bounded/v1",
                    "mode": "proposal_only",
                    "notifications": "disabled",
                    "formal_mutations": "forbidden",
                    "minimum_repair_proposals": 1,
                    "minimum_provider_closes": 3,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (fixture_root / "portal-provider.json").write_text(
            json.dumps(
                {
                    "portal_cases": [
                        {
                            "case_number": "1150529-W-002",
                            "client_name": "隔離報結案",
                            "case_type": "民事",
                            "case_reason": "消費者債務清理",
                            "file_list": [portal_document.name],
                        }
                    ],
                    "closing_statuses": {
                        "1150529-W-002": {
                            "closing": {"found": True, "status": "暫存"},
                            "withdrawal": {"found": False, "status": ""},
                        }
                    },
                    "pending_drafts": {
                        "case_status": [
                            {
                                "applyno": "1150529-W-002",
                                "reply_type": "結案回報",
                                "status": "暫存",
                                "row_text": "1150529-W-002 結案回報 暫存",
                            }
                        ],
                        "closing": [],
                        "condition": [],
                        "go_live": [],
                        "progress": [],
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    elif kind == "self_repair_owned_tmp":
        for directory in ("magi-root", "runtime", "tmp"):
            (fixture_root / directory).mkdir()
        candidate = fixture_root / "tmp" / "magi_owned_stale_fixture.txt"
        marker = candidate.with_name(candidate.name + ".magi-self-repair-owned")
        candidate.write_text("owned disposable residue\n", encoding="utf-8")
        marker.write_text("magi-self-repair-owned-v1\n", encoding="utf-8")
        old = time.time() - 2 * 3600
        os.utime(candidate, (old, old))
        os.utime(marker, (old, old))
    elif kind == "wiki_notes":
        agent = fixture_root / "agent"
        vault = fixture_root / "vault"
        note_dir = vault / "20_Notes" / "2099-0001-測試-民事-一審"
        agent.mkdir()
        note_dir.mkdir(parents=True)
        (agent / "obsidian_vault_config.json").write_text(
            json.dumps({"vault_path": str(vault)}), encoding="utf-8"
        )
        (note_dir / "fixture.md").write_text("# Fixture\n\nlocal note body\n", encoding="utf-8")
    elif kind == "judgment_resummary":
        normalized = fixture_root / "judicial_api" / "normalized" / "TPS"
        judgments = fixture_root / "agent" / "judgment-collector"
        normalized.mkdir(parents=True)
        judgments.mkdir(parents=True)
        text = "法院認為本件為隔離測試判決，依法應為相同之判斷。" * 80
        (normalized / "TPS-2099-fixture.txt").write_text(text, encoding="utf-8")
        (judgments / "judgments.json").write_text("{}\n", encoding="utf-8")
    elif kind in {"pdf_bookmark_batch", "pdf_bookmark_repair", "pdf_bookmark_benchmark"}:
        filename = "起訴書.pdf" if kind == "pdf_bookmark_benchmark" else "fixture.pdf"
        target = fixture_root / "cases" / "2099-0001-測試" / "09_法院通知" / filename
        _make_pdf(target)
        (fixture_root / "agent").mkdir()
    elif kind == "system_diagnostic":
        runtime = fixture_root / "runtime"
        runtime.mkdir()
        (fixture_root / "cron_jobs.json").write_text(
            json.dumps(
                [
                    {"id": "fixture_ok", "enabled": True},
                    {"id": "fixture_disabled", "enabled": False},
                ]
            ),
            encoding="utf-8",
        )
        (runtime / "cron_state.json").write_text(
            json.dumps({"fixture_ok": {"last_run": "2099-01-01T00:00:00", "last_success": True}}),
            encoding="utf-8",
        )
    elif kind == "model_live_gate":
        runtime = fixture_root / "runtime"
        profile = fixture_root / "omlx" / "active_profile"
        runtime.mkdir()
        profile.parent.mkdir()
        profile.write_text("day\n", encoding="utf-8")
    elif kind == "obsidian_acceptance":
        agent = fixture_root / "agent"
        vault = fixture_root / "vault"
        notes = vault / "20_Notes" / "2099-0001-測試-民事-一審"
        agent.mkdir()
        notes.mkdir(parents=True)
        relative = "20_Notes/2099-0001-測試-民事-一審/summary__fixture.md"
        body = "這是完全隔離的驗收內容，僅用來驗證知識品質與索引一致性。" * 20
        note = (
            "---\ncase_number: 2099-0001\nsummary_schema: magi-obsidian-note-v2\n"
            "extraction_quality: ok\n---\n# Fixture\n\n"
            "## 摘要\n\n隔離驗收摘要。\n\n"
            "## 法律/程序意義\n\n隔離驗收程序意義。\n\n"
            "## 期限與待辦\n\n無真實期限。\n\n"
            "## 爭點與證據\n\n僅含測試證據。\n\n"
            f"## Full Text\n\n{body}\n"
        )
        (vault / relative).write_text(note, encoding="utf-8")
        (agent / "obsidian_vault_config.json").write_text(
            json.dumps({"vault_path": str(vault), "vault_name": "fixture"}), encoding="utf-8"
        )
        (agent / "obsidian_index.json").write_text(
            json.dumps({"notes": {relative: {"chunks": 1, "doc_key": "fixture"}}}),
            encoding="utf-8",
        )
        (agent / "wiki_synthesizer_state.json").write_text('{"cases":{}}\n', encoding="utf-8")
    elif kind in {"disk_cleanup_staging", "disk_cleanup_nas_heavy"}:
        for directory in ("runtime", "agent", "staging", "home"):
            (fixture_root / directory).mkdir()
        old = time.time() - 40 * 86400
        if kind == "disk_cleanup_staging":
            candidate = fixture_root / "staging" / "old-report.txt"
            candidate.write_text("disposable cleanup candidate\n", encoding="utf-8")
            os.utime(candidate, (old, old))
        else:
            candidate = fixture_root / "nas" / "#recycle" / "Backup" / "nested" / "old.bin"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"disposable heavy recycle candidate\n")
            for path in (candidate, candidate.parent, candidate.parent.parent):
                os.utime(path, (old, old))
    elif kind == "self_repair_report":
        runtime = fixture_root / "runtime"
        runtime.mkdir()
        now = time.time()
        rows = [
            {
                "ts": now - index * 3600,
                "source": "cron",
                "command": "cron:job_fixture_failure",
                "error": "ConnectionRefused: deterministic fixture",
                "severity": "High",
            }
            for index in range(3)
        ]
        (runtime / "issue_agenda.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        (runtime / "cron_state.json").write_text("{}\n", encoding="utf-8")
    elif kind == "business_readiness_snapshot":
        for directory in ("runtime", "agent", "static", "file-review/bg-jobs"):
            (fixture_root / directory).mkdir(parents=True)
        (fixture_root / "cron_jobs.json").write_text(
            json.dumps(
                [
                    {
                        "id": "job_file_review_check",
                        "enabled": True,
                        "command": "fixture scheduled_check",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (fixture_root / "static" / "laf_portal_new_files_latest.json").write_text(
            '{"portal_still_missing":0,"items":[]}\n', encoding="utf-8"
        )
        (fixture_root / "agent" / "laf_pending_portal_downloads.json").write_text(
            '{"items":[]}\n', encoding="utf-8"
        )
        (fixture_root / "runtime" / "heavy_fallback_live_latest.json").write_text(
            '{"success":true,"model":"fixture-nim"}\n', encoding="utf-8"
        )
        transcript_dir = fixture_root / "runtime" / "transcript_sync"
        transcript_dir.mkdir(parents=True)
        now_iso = datetime.now().isoformat(timespec="seconds")
        (transcript_dir / "transcript_sync_latest.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "created_at": now_iso,
                    "eligible_cases": 0,
                    "sync_status": {
                        "success": True,
                        "eligible_cases": 0,
                        "cycle_scanned_cases": 0,
                        "last_cycle_completed_at": now_iso,
                    },
                    "summary": {
                        "retry_pending_cases_count": 0,
                        "failed_cases_count": 0,
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    elif kind == "commercial_readiness_schedule_fixture":
        for directory in ("runtime", "agent", "metrics", "exports"):
            (fixture_root / directory).mkdir(parents=True)
    elif kind == "nightly_health_report":
        agent = fixture_root / "agent"
        runs = fixture_root / "autopilot-runs"
        runtime = fixture_root / "runtime"
        run = runs / f"{time.strftime('%Y%m%d')}_010000_nightly"
        agent.mkdir()
        runtime.mkdir()
        run.mkdir(parents=True)
        steps = {
            key: {"ok": True, "skipped": False, "parsed": {"message": "fixture complete"}}
            for key, _label in (
                ("pdf_nightly_train", ""),
                ("judicial_api_night_pull", ""),
                ("judicial_api_nightly_process", ""),
                ("laf_deep_extract", ""),
                ("db_bidirectional_sync", ""),
                ("db_daily_backup", ""),
                ("night_talk", ""),
            )
        }
        (run / "report.json").write_text(
            json.dumps({"ok": True, "details": {"steps": steps}}), encoding="utf-8"
        )
        (agent / "red_phone_delivery.jsonl").write_text(
            json.dumps(
                {
                    "ts": time.strftime("%Y-%m-%dT01:30:00"),
                    "event": "sent",
                    "topic_key": "judicial_api",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (runtime / "cron_state.json").write_text("{}\n", encoding="utf-8")
    elif kind == "empty_case_shell_cleanup":
        runtime = fixture_root / "runtime"
        case = (
            fixture_root
            / "active-cases"
            / "法扶案件"
            / "刑事"
            / "2099-0001-測試當事人-刑事-一審-測試"
        )
        runtime.mkdir()
        case.mkdir(parents=True)
        (case / ".DS_Store").write_text("fixture metadata only\n", encoding="utf-8")
    elif kind == "purge_persona_memories":
        (fixture_root / "runtime").mkdir()
    elif kind == "overdue_todo_reconcile":
        (fixture_root / "runtime").mkdir()
    elif kind == "osc_auto_backup":
        (fixture_root / "home").mkdir()
        (fixture_root / "runtime").mkdir()
    elif kind == "knowledge_lint":
        agent = fixture_root / "agent"
        vault = fixture_root / "vault"
        notes = vault / "20_Notes" / "2099-0001-測試當事人-刑事-一審-測試"
        static = fixture_root / "static"
        runtime = fixture_root / "runtime"
        agent.mkdir()
        notes.mkdir(parents=True)
        static.mkdir()
        runtime.mkdir()
        note_rel = "20_Notes/2099-0001-測試當事人-刑事-一審-測試/summary__fixture.md"
        note = notes / "summary__fixture.md"
        note.write_text(
            "---\n"
            "summary_schema: magi-obsidian-note-v2\n"
            "extraction_quality: ok\n"
            "source_path: /fixture/source.pdf\n"
            "---\n"
            "## 摘要\n這是一份完整的測試案件摘要。\n\n"
            "## 法律/程序意義\n本文件用於驗證知識品質掃描程序。\n\n"
            "## 期限與待辦\n目前沒有待辦事項。\n\n"
            "## 爭點與證據\n測試爭點與證據均已整理。\n\n"
            "## Full Text\n"
            + "本測試文件包含足夠的中文法律內容，用來驗證全文訊號與結構化摘要品質。" * 8
            + "\n",
            encoding="utf-8",
        )
        (agent / "obsidian_vault_config.json").write_text(
            json.dumps({"vault_path": str(vault)}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (agent / "obsidian_index.json").write_text(
            json.dumps(
                {"notes": {note_rel: {"chunks": 1, "doc_key": "fixture-note"}}},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (agent / "wiki_synthesizer_state.json").write_text(
            '{"cases": {}}\n', encoding="utf-8"
        )
    elif kind == "transcript_indexer":
        transcript_dir = (
            fixture_root
            / "cases"
            / "法扶案件"
            / "刑事"
            / "2099-0001-測試當事人-刑事-一審-測試"
            / "05_筆錄"
        )
        transcript_dir.mkdir(parents=True)
        (fixture_root / "agent").mkdir()
        _make_pdf(transcript_dir / "20990101 訊問筆錄.pdf")
    elif kind == "osc_case_index":
        (fixture_root / "agent").mkdir()
        fixtures = (
            (
                "法律扶助案件",
                "刑事",
                "2099-0001-第一測試當事人-一審-測試罪名",
                "臺灣測試地方法院2099年度測字第1號測股開庭通知.txt",
            ),
            (
                "自費案件",
                "民事",
                "2099-0002-第二測試當事人-一審-損害賠償",
                "臺灣測試地方法院2099年度訴字第2號測股開庭通知.txt",
            ),
        )
        for category, case_type, folder, notice in fixtures:
            notice_path = (
                fixture_root / "cases" / category / case_type / folder / "09_法院通知" / notice
            )
            notice_path.parent.mkdir(parents=True)
            notice_path.write_text("完全隔離的案件索引驗證資料\n", encoding="utf-8")
    elif kind == "laf_pending_scan":
        (fixture_root / "agent").mkdir()
    elif kind == "nas_pdf_ocr_provider":
        import fitz
        import sqlite3

        for directory in ("agent", "bin", "nas-cases", "ocr-temp", "runtime"):
            (fixture_root / directory).mkdir()
        source = fixture_root / "nas-cases" / "2099-0001" / "scanned-fixture.pdf"
        source.parent.mkdir(parents=True)
        document = fitz.open()
        for page_number in range(4):
            page = document.new_page()
            for offset in range(20):
                x = 40 + offset * 8
                page.draw_rect(
                    fitz.Rect(x, 40 + page_number * 2, x + 5, 760 - offset * 3),
                    color=(0, 0, 0),
                    fill=(0.2, 0.2, 0.2),
                )
        document.save(source)
        document.close()
        provider = fixture_root / "bin" / "ocrmypdf"
        provider.write_text(
            "#!/bin/bash\n"
            "set -eu\n"
            "argc=$#\n"
            "eval input=\\\"\\${$((argc-1))}\\\"\n"
            "eval output=\\\"\\${$argc}\\\"\n"
            "/bin/cp \"$input\" \"$output\"\n",
            encoding="utf-8",
        )
        provider.chmod(0o700)
        queue = fixture_root / "agent" / "nas-ocr-queue.db"
        connection = sqlite3.connect(queue)
        try:
            connection.execute(
                """
                CREATE TABLE ocr_queue (
                    file_path TEXT PRIMARY KEY,
                    status TEXT DEFAULT 'pending',
                    last_attempt TIMESTAMP,
                    attempt_count INTEGER DEFAULT 0,
                    error_msg TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO ocr_queue (file_path,status) VALUES (?, 'pending')",
                (str(source),),
            )
            connection.commit()
        finally:
            connection.close()
    elif kind == "legacy_judgment_resummary":
        (fixture_root / "runtime").mkdir()
    elif kind == "function_health_index":
        health_source = fixture_root / "runtime" / "health-source"
        health_source.mkdir(parents=True)
        (fixture_root / "runtime" / "cron_jobs.json").write_text(
            _bound_cron_bytes(source_root).decode("utf-8"), encoding="utf-8"
        )
        (health_source / "cron_state.json").write_text("{}\n", encoding="utf-8")
        (health_source / "fixture_health_latest.json").write_text(
            '{"ok": true}\n',
            encoding="utf-8",
        )
        (fixture_root / "test_matrix.json").write_text(
            json.dumps(
                {
                    "suites": {
                        "fixture": {
                            "description": "isolated function health fixture",
                            "checks": [
                                {
                                    "id": "fixture_health",
                                    "command": [
                                        "fixture-check",
                                        "--json-out",
                                        "runtime/fixture_health_latest.json",
                                    ],
                                }
                            ],
                        }
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
    elif kind == "token_health_fixture":
        token_dir = fixture_root / "tokens"
        runtime = fixture_root / "runtime" / "token_health"
        token_dir.mkdir(parents=True)
        runtime.mkdir(parents=True)
        (token_dir / "google_calendar.json").write_text(
            json.dumps(
                {
                    "token": "fixture-access-token-metadata-only",
                    "refresh_token": "fixture-refresh-token-metadata-only",
                    "token_uri": "http://127.0.0.1/unused",
                    "client_id": "fixture-client",
                    "client_secret": "fixture-secret",
                    "scopes": ["https://www.googleapis.com/auth/calendar"],
                    "expiry": "2099-01-01T00:00:00+00:00",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (token_dir / "credentials.json").write_text(
            '{"installed":{"client_id":"fixture-client"}}\n', encoding="utf-8"
        )
    elif kind == "tailscale_funnel_provider":
        binary_dir = fixture_root / "bin"
        runtime = fixture_root / "runtime"
        binary_dir.mkdir()
        runtime.mkdir()
        tailscale = binary_dir / "tailscale"
        tailscale.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "if [ \"${1:-}\" = funnel ] && [ \"${2:-}\" = status ]; then\n"
            "  printf '%s\\n' '{\"TCP\":{\"443\":{\"HTTPS\":true}},\"Web\":{\"fixture.tailnet.example:443\":{\"Handlers\":{\"/\":{\"Proxy\":\"http://127.0.0.1:5002\"}}}},\"AllowFunnel\":{\"fixture.tailnet.example:443\":true}}'\n"
            "  exit 0\n"
            "fi\n"
            f"if [ \"${{1:-}}\" = funnel ] && [ \"${{2:-}}\" = --bg ]; then printf '%s\\n' enable >> '{runtime / 'tailscale-actions.log'}'; : > '{runtime / 'repaired'}'; exit 0; fi\n"
            "exit 64\n",
            encoding="utf-8",
        )
        dig = binary_dir / "dig"
        dig.write_text("#!/bin/sh\nprintf '%s\\n' '1.1.1.1'\n", encoding="utf-8")
        curl = binary_dir / "curl"
        curl.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "url=''\n"
            "for arg in \"$@\"; do url=\"$arg\"; done\n"
            f"if [ ! -f '{runtime / 'repaired'}' ]; then printf '503'; exit 0; fi\n"
            "case \"$url\" in\n"
            "  */mobile-app) printf 'HTTP/1.1 302 Found\\nLocation: /login?next=/mobile&mobile_app=1\\n\\n302' ;;\n"
            "  */dashboard) printf 'HTTP/1.1 302 Found\\nLocation: /login?next=/dashboard\\n\\n302' ;;\n"
            "  */osc) printf 'HTTP/1.1 302 Found\\nLocation: /login?next=/osc\\n\\n302' ;;\n"
            "  */mobile) printf 'HTTP/1.1 302 Found\\nLocation: /login?next=/mobile\\n\\n302' ;;\n"
            "  */sentencing-trends) printf 'HTTP/1.1 302 Found\\nLocation: /login?next=/sentencing-trends\\n\\n302' ;;\n"
            "  */api/osc/folders/roots) printf 'HTTP/1.1 302 Found\\nLocation: /login?next=/api/osc/folders/roots\\n\\n302' ;;\n"
            "  */s/__magi_health_probe_invalid__) printf '404' ;;\n"
            "  *) printf '200' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        for executable in (tailscale, dig, curl):
            executable.chmod(0o700)
    elif kind == "toolsai_auto_skill_import":
        repository = fixture_root / "toolsai-auto-skill"
        knowledge = repository / "knowledge-base"
        experience = repository / "experience"
        knowledge.mkdir(parents=True)
        experience.mkdir()
        (knowledge / "_index.json").write_text(
            json.dumps(
                {
                    "categories": [
                        {
                            "file": "fixture-knowledge.md",
                            "keywords": ["isolation", "verification"],
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (knowledge / "fixture-knowledge.md").write_text(
            "# Fixture knowledge\n\n"
            "A schedule adapter must verify the real production handler output.\n",
            encoding="utf-8",
        )
        (experience / "_index.json").write_text(
            json.dumps(
                {
                    "skills": [
                        {
                            "file": "fixture-experience.md",
                            "skillId": "schedule-fixture",
                            "keywords": ["postcondition"],
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (experience / "fixture-experience.md").write_text(
            "# Fixture experience\n\n"
            "A successful command must also satisfy a semantic postcondition.\n",
            encoding="utf-8",
        )
    elif kind == "external_chat_smoke_provider":
        (fixture_root / "runtime").mkdir()
    elif kind == "judicial_api_night_pull_provider":
        for directory in ("agent", "cache", "orch", "runtime"):
            (fixture_root / directory).mkdir()
    elif kind == "judicial_api_day_process":
        for directory in ("agent", "cache", "orch", "runtime"):
            (fixture_root / directory).mkdir()
        raw = fixture_root / "cache" / "judicial_api" / "raw" / "2099-01-02"
        raw.mkdir(parents=True)
        full_text = _synthetic_practice_judgment_text()
        payload = {
            "jid": "TPSV,2099,台上,1,20990102,1",
            "date": "2099/01/02",
            "pulled_at": "2099-01-02T03:04:05",
            "payload": {
                "JID": "TPSV,2099,台上,1,20990102,1",
                "JYEAR": "2099",
                "JCASE": "台上",
                "JNO": "1",
                "JDATE": "20990102",
                "JTITLE": "損害賠償",
                "JFULLX": {
                    "JFULLCONTENT": full_text,
                    "JFULLTYPE": "text",
                    "JFULLPDF": "",
                },
                "ATTACHMENTS": [],
            },
        }
        (raw / "TPSV_2099_fixture.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif kind == "judgment_daily_crawl_provider":
        for directory in (
            "agent/judgment-collector",
            "archive",
            "cache",
            "cases/法扶案件/民事/2099-0001-測試當事人-民事-一審-損害賠償",
            "orch",
            "runtime",
        ):
            (fixture_root / directory).mkdir(parents=True)
        full_text = _synthetic_practice_judgment_text()
        text_path = fixture_root / "archive" / "fixture-judgment.txt"
        text_path.write_text(full_text, encoding="utf-8")
        manifest = {
            "items": [
                {
                    "success": True,
                    "title": "最高法院2099年度台上字第1號民事判決",
                    "url": "https://fixture.invalid/judgment/1",
                    "archived_text_path": str(text_path),
                }
            ]
        }
        (fixture_root / "archive" / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        case_path = (
            fixture_root
            / "cases"
            / "法扶案件"
            / "民事"
            / "2099-0001-測試當事人-民事-一審-損害賠償"
        )
        (fixture_root / "case-index.json").write_text(
            json.dumps(
                [{"path": str(case_path), "folder_name": case_path.name}],
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (fixture_root / "agent" / "judgment-collector" / "judgments.json").write_text(
            "[]\n", encoding="utf-8"
        )
    elif kind == "judgment_retry_provider":
        for directory in ("agent/judgment-collector", "cache", "orch", "runtime"):
            (fixture_root / directory).mkdir(parents=True)
        full_text = (
            "最高法院民事判決\n理由\n"
            "本院認為，契約當事人應依誠信原則履行義務，主張有利事實之一方應負舉證責任。"
            "民法第148條明定權利行使及義務履行應依誠信方法。\n"
        ) * 20
        text_path = fixture_root / "cache" / "retry-source.txt"
        text_path.write_text(full_text, encoding="utf-8")
        queue = fixture_root / "cache" / "summary-retry.jsonl"
        queue.write_text(
            json.dumps(
                {
                    "case_reason": "損害賠償",
                    "title": "最高法院2099年度台上字第1號民事判決",
                    "full_text_path": str(text_path),
                    "attempts": 0,
                    "next_retry_epoch": 0,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    elif kind == "worldmonitor_provider":
        for directory in ("agent", "runtime", "static"):
            (fixture_root / directory).mkdir()
    elif kind == "file_review_portal_provider":
        for directory in ("agent", "orch", "runtime", "staging"):
            (fixture_root / directory).mkdir()
        (fixture_root / "config.json").write_text("{}\n", encoding="utf-8")
        provider = fixture_root / "orch" / "file_review_automation.py"
        provider.write_text(
            "import json, os\n"
            "from pathlib import Path\n"
            "TRACE = Path(os.environ['MAGI_FILE_REVIEW_PROVIDER_TRACE'])\n"
            "def record(event, payload=None):\n"
            "    TRACE.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with TRACE.open('a', encoding='utf-8') as handle:\n"
            "        handle.write(json.dumps({'event': event, 'payload': payload or {}}, ensure_ascii=False) + '\\n')\n"
            "class FileReviewManager:\n"
            "    def __init__(self, *args, **kwargs):\n"
            "        self.dismissed_payments = {}\n"
            "        record('init', {'headless': kwargs.get('headless'), 'download_folder': kwargs.get('download_folder')})\n"
            "    def probe_downloadable_from_portal(self, **kwargs):\n"
            "        record('probe_downloadable_from_portal', kwargs)\n"
            "        return {'success': True, 'count': 0, 'downloadable_count': 0, 'items': []}\n"
            "    def close(self):\n"
            "        record('close')\n",
            encoding="utf-8",
        )
    elif kind == "transcript_sync_provider":
        for directory in ("agent", "cases/2099-0001/05_筆錄", "downloads", "orch", "runtime"):
            (fixture_root / directory).mkdir(parents=True)
        (fixture_root / "config.json").write_text(
            json.dumps(
                {"judicial": {"record_download_folder": str(fixture_root / "downloads")}},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        provider = fixture_root / "orch" / "judicial_automation_v2.py"
        provider.write_text(
            "import hashlib, json, os, shutil\n"
            "from dataclasses import dataclass\n"
            "from pathlib import Path\n"
            "TRACE = Path(os.environ['MAGI_TRANSCRIPT_PROVIDER_TRACE'])\n"
            "CASE_ROOT = Path(os.environ['MAGI_TRANSCRIPT_PROVIDER_CASE_ROOT'])\n"
            "def record(event, payload=None):\n"
            "    TRACE.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with TRACE.open('a', encoding='utf-8') as handle:\n"
            "        handle.write(json.dumps({'event': event, 'payload': payload or {}}, ensure_ascii=False) + '\\n')\n"
            "@dataclass\n"
            "class CourtCase:\n"
            "    case_number: str = '2099-0001'\n"
            "    court_case_number: str = '2099年度測字第1號'\n"
            "    court_name: str = '臺灣測試地方法院'\n"
            "    case_type: str = '刑事'\n"
            "    client_name: str = '測試當事人'\n"
            "class CourtRecordDownloader:\n"
            "    def __init__(self, *args, **kwargs):\n"
            "        self.download_folder = Path(kwargs['download_folder'])\n"
            "        self._last_download_error = ''\n"
            "        self._last_no_new_files_reason = ''\n"
            "        self._last_pdf_fetch_count = 1\n"
            "        self._last_pdf_known_duplicate_count = 0\n"
            "        record('init', {'headless': kwargs.get('headless')})\n"
            "    def scan_case_folders_for_md5(self, rename_files=False): record('md5_scan', {'rename_files': rename_files})\n"
            "    def cleanup_download_folder(self): record('cleanup'); return {'removed': 0}\n"
            "    def login(self): record('login'); return True\n"
            "    def get_cases_from_db(self): record('get_cases'); return [CourtCase()]\n"
            "    def download_record(self, case):\n"
            "        record('download_record', {'case_number': case.case_number})\n"
            "        self.download_folder.mkdir(parents=True, exist_ok=True)\n"
            "        path = self.download_folder / 'fixture-transcript.pdf'\n"
            "        path.write_bytes(b'%PDF-1.4\\nfixture transcript provider\\n%%EOF\\n')\n"
            "        return [str(path)]\n"
            "    def move_to_case_folder(self, case, files):\n"
            "        record('move_to_case_folder', {'count': len(files)})\n"
            "        target = CASE_ROOT / '2099-0001' / '05_筆錄' / '20990101_測試筆錄.pdf'\n"
            "        target.parent.mkdir(parents=True, exist_ok=True)\n"
            "        shutil.move(files[0], target)\n"
            "        return [{'status': 'archived', 'archive_reference': '05_筆錄/20990101_測試筆錄.pdf', 'case_identity_match': True, 'readable': True, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}]\n"
            "    def rename_all_transcripts(self):\n"
            "        record('rename_all_transcripts')\n"
            "        return {'success': True, 'status': 'success', 'renamed_count': 0, 'retry_pending_count': 0, 'parse_failed_count': 0, 'metadata_unresolved_count': 0, 'file_operation_failed_count': 0}\n"
            "    def close(self): record('close')\n",
            encoding="utf-8",
        )
    elif kind == "research_brief_provider":
        for directory in ("agent", "runtime/research_brief/namespaces"):
            (fixture_root / directory).mkdir(parents=True)
        namespace = {
            "namespace": "fixture-law",
            "topic_key": "research_daily",
            "keywords": ["法律"],
            "sources": [
                {
                    "url": "http://127.0.0.1:<MOCK_PORT>/research.xml",
                    "type": "rss",
                    "lang": "zh-Hant",
                    "note": "Fixture Legal Feed",
                }
            ],
        }
        (fixture_root / "runtime" / "research_brief" / "namespaces" / "fixture-law.json").write_text(
            json.dumps(namespace, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif kind == "market_briefing_provider":
        for directory in ("agent", "runtime", "static"):
            (fixture_root / directory).mkdir()
        (fixture_root / "agent" / "market_watchlist.json").write_text(
            json.dumps(
                {
                    "watchlist": [
                        {"symbol": "2330.TW", "label": "台積電", "market": "TW"}
                    ],
                    "first_prompt_date": "2026-01-01",
                    "active_from_date": "2026-01-02",
                    "last_report_date": "",
                    "updated_at": "2026-01-01T00:00:00",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        closes = [100.0 + index * 0.5 for index in range(60)]
        fixture = {
            "history": {
                "2330.TW": {
                    "closes": closes,
                    "timestamps": [1700000000 + index * 86400 for index in range(60)],
                    "highs": [value + 1.0 for value in closes],
                    "lows": [value - 1.0 for value in closes],
                    "volumes": [1000 + index * 10 for index in range(60)],
                }
            },
            "tw_financials": {
                "2330": {"rev": "月營收(209906) YoY 12.5%", "eps": "季報(2099Q2) EPS 9.9"}
            },
            "us_filings": {"FIX": {"filing": "unused"}},
        }
        (fixture_root / "market-provider.json").write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif kind == "omlx_switch_provider":
        for directory in (
            "bin", "cache", "home/.omlx/bin", "home/.omlx/models/gemma-4-e4b-it-4bit",
            "home/.omlx/models/gemma-4-12B-it-4bit",
            "home/.omlx/models/gemma-4-26b-a4b-it-4bit",
            "home/.omlx/models/Phi-4-mini-instruct-4bit",
            "home/.omlx/models/SmolLM3-3B-4bit", "home/.omlx/models-text",
            "home/.omlx/models-text-e4b", "home/.omlx/models-text-phi4",
            "home/.omlx/models-text-smol", "home/Library/LaunchAgents", "magi-root", "runtime",
        ):
            (fixture_root / directory).mkdir(parents=True)
        for name in (
            "com.magi.omlx.plist", "com.magi.omlx-phi4.plist", "com.magi.omlx-smol.plist"
        ):
            (fixture_root / "home" / "Library" / "LaunchAgents" / name).write_text(
                "<?xml version=\"1.0\"?><plist version=\"1.0\"><dict/></plist>\n",
                encoding="utf-8",
            )
        wrapper = fixture_root / "home" / ".omlx" / "bin" / "omlx-gemma4-unified-serve"
        wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        wrapper.chmod(0o700)
        commands: dict[str, str] = {
            "launchctl": (
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$MAGI_OMLX_LAUNCHCTL_TRACE\"\n"
                "[ \"${1:-}\" = print ] && exit 1\n"
                "exit 0\n"
            ),
            "curl": (
                "#!/bin/sh\n"
                "url=''\nfor arg in \"$@\"; do url=\"$arg\"; done\n"
                "case \"$url\" in\n"
                "  *:8080/*) dir=\"$HOME/.omlx/models-text\" ;;\n"
                "  *:8082/*) dir=\"$HOME/.omlx/models-text-phi4\" ;;\n"
                "  *:8083/*) dir=\"$HOME/.omlx/models-text-smol\" ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
                "link=''\n"
                "for candidate in \"$dir\"/*; do\n"
                "  if [ -L \"$candidate\" ]; then link=\"$candidate\"; break; fi\n"
                "done\n"
                "[ -n \"$link\" ] || exit 1\n"
                "id=$(basename \"$(readlink \"$link\")\")\n"
                "printf '{\"data\":[{\"id\":\"%s\"}]}\\n' \"$id\"\n"
            ),
            "vm_stat": (
                "#!/bin/sh\n"
                "printf '%s\\n' 'Mach Virtual Memory Statistics: (page size of 16384 bytes)'\n"
                "printf '%s\\n' 'Pages free: 4000000.' 'Pages inactive: 4000000.'\n"
            ),
            "nc": "#!/bin/sh\nexit 1\n",
            "lsof": "#!/bin/sh\nexit 1\n",
            "pgrep": "#!/bin/sh\nexit 1\n",
            "sleep": "#!/bin/sh\nexit 0\n",
        }
        for name, body in commands.items():
            path = fixture_root / "bin" / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o700)
    elif kind == "osc_events_pdf_scan":
        for directory in ("agent", "runtime"):
            (fixture_root / directory).mkdir()
        expectation = _osc_events_fixture_expectation()
        (fixture_root / "osc-events-fixture.json").write_bytes(
            _canonical_json(expectation) + b"\n"
        )
        pdf = (
            fixture_root
            / "cases"
            / "2026-0001-測試當事人-民事-一審-損害賠償"
            / "09_法院通知或程序裁定"
            / expectation["file_name"]
        )
        _make_pdf(pdf)
    elif kind == "accounting_sheet_provider":
        (fixture_root / "runtime").mkdir()
        (fixture_root / "accounting-sheet.json").write_text(
            json.dumps(
                {
                    "schema": "magi.v3.accounting-sheet-fixture/v1",
                    "source_label": "2026年6月",
                    "selected_by_month": True,
                    "values": [
                        ["日期", "分類", "支出", "收入", "備註", "OSC案號"],
                        ["2026-06-03", "郵資", 80, "", "掛號郵資", "2026-0001"],
                        ["2026-06-04", "一般案件", "", 5000, "委任費", "2026-0001"],
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    elif kind == "accounting_bonus":
        (fixture_root / "runtime").mkdir()
    elif kind == "exam_tutor_yearly_offline":
        (fixture_root / "agent" / "exam-tutor").mkdir(parents=True)
        (fixture_root / "runtime").mkdir()
    elif kind == "pdf_namer_proposal_provider":
        for directory in ("home", "runtime"):
            (fixture_root / directory).mkdir()
        pdf = (
            fixture_root
            / "cases"
            / "2026-0001-測試當事人-民事-一審-損害賠償"
            / "09_法院通知或程序裁定"
            / "20260716 判決（測試當事人；原告之訴駁回）.pdf"
        )
        _make_pdf(pdf)
        (fixture_root / "pdf-namer-proposals.json").write_text(
            json.dumps(
                {
                    "schema": "magi.v3.pdf-namer-proposals/v1",
                    "proposals": {
                        pdf.name: {
                            "filename": pdf.name,
                            "party": "測試當事人",
                            "doc_type": "判決",
                            "holding": "原告之訴駁回",
                        }
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    elif kind == "translator_ape_provider":
        for directory in ("runtime", "static"):
            (fixture_root / directory).mkdir()
        translations = {
            "prayer_for_relief": "The prayer for relief requires the defendant to pay the plaintiff NT$200,000.",
            "criminal_indictment": "The defendant committed fraud and is sentenced to imprisonment for six months.",
            "civil_tort": "The defendant is liable to the plaintiff for tort damages.",
            "case_number": "The court is hearing case 114年度原訴字第000024號.",
        }
        (fixture_root / "translator-ape-provider.json").write_text(
            json.dumps(
                {
                    "schema": "magi.v3.translator-ape-fixture/v1",
                    "translations": {
                        case_id: {
                            "apple_baseline": {
                                "provider": "apple_translation_fixture",
                                "text": text,
                            },
                            "apple_ape": {
                                "provider": "apple_translation_ape_fixture",
                                "text": text,
                                "validator_reasons": [],
                            },
                        }
                        for case_id, text in translations.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    elif kind == "local_deep_queue_idle":
        # Empty private stores certify the real worker's idle terminal without
        # loading a model, reading production prompts, or switching topology.
        for directory in ("agent", "runtime"):
            (fixture_root / directory).mkdir()
    elif kind == "nightly_regression_runtime":
        for directory in ("agent", "runtime"):
            (fixture_root / directory).mkdir()
    elif kind == "drive_case_sync_rules":
        for directory in (
            "runtime",
            "nas/01_案件/一般案件/民事/2026-0001-測試當事人-一審-損害賠償",
            "drive/案件辦理/一般案件/Lumi/測試當事人-一審-損害賠償",
        ):
            (fixture_root / directory).mkdir(parents=True)
        nas_case = fixture_root / "nas/01_案件/一般案件/民事/2026-0001-測試當事人-一審-損害賠償"
        drive_case = fixture_root / "drive/案件辦理/一般案件/Lumi/測試當事人-一審-損害賠償"
        (nas_case / "shared.txt").write_text("same content\n", encoding="utf-8")
        (drive_case / "shared.txt").write_text("same content\n", encoding="utf-8")
        (nas_case / "nas-only.txt").write_text("nas only\n", encoding="utf-8")
        (drive_case / "drive-only.txt").write_text("drive only\n", encoding="utf-8")
        (fixture_root / "drive-sync.json").write_text(
            json.dumps(
                {
                    "schema": "magi.v3.drive-case-sync-fixture/v1",
                    "cases": [
                        {
                            "case_number": "2026-0001",
                            "client_name": "測試當事人",
                            "case_reason": "損害賠償",
                            "category": "一般案件",
                            "case_kind": "民事",
                            "status": "active",
                            "owner_bucket": "Lumi",
                            "nas_case": nas_case.relative_to(fixture_root).as_posix(),
                            "drive_case": drive_case.relative_to(fixture_root).as_posix(),
                            "drive_relative": "一般案件/Lumi/測試當事人-一審-損害賠償",
                        }
                    ],
                    "invalid_drive_paths": [
                        "一般案件/Lumi/.duplicates/測試當事人",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    elif kind == "pdf_namer_nightly_training":
        filename = "20260716 臺灣花蓮地方法院115年度訴字第1號民事判決（測試當事人；原告之訴駁回）.pdf"
        pdf = (
            fixture_root
            / "cases"
            / "2026-0001-測試當事人-民事-一審-損害賠償"
            / "08_判決書"
            / filename
        )
        for directory in ("state", "runtime"):
            (fixture_root / directory).mkdir()
        _make_pdf(pdf)
        (fixture_root / "analysis-provider.json").write_text(
            json.dumps(
                {
                    "schema": "magi.v3.pdf-namer-analysis-fixture/v1",
                    "proposals": {
                        filename: {
                            "expected_text": "INDICTMENT DOCUMENT HEADER",
                            "suggested_filename": filename,
                            "doc_type": "民事判決",
                            "party": "測試當事人",
                            "date": "20260716",
                            "confidence": 0.95,
                        }
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    elif kind == "pdf_namer_layout_backfill":
        filename = "20260716 臺灣花蓮地方法院115年度訴字第1號民事判決（測試當事人；原告之訴駁回）.pdf"
        pdf = (
            fixture_root
            / "cases"
            / "2026-0001-測試當事人-民事-一審-損害賠償"
            / "08_判決書"
            / filename
        )
        for directory in ("state", "runtime"):
            (fixture_root / directory).mkdir()
        _make_pdf(pdf)
        (fixture_root / "layout-provider.json").write_text(
            json.dumps(
                {
                    "schema": "magi.v3.pdf-layout-fixture/v1",
                    "documents": {
                        filename: {"expected_text": "INDICTMENT DOCUMENT HEADER"}
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (fixture_root / "state" / "_filing_log.json").write_text(
            json.dumps(
                [
                    {
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "filed": [
                            {
                                "status": "filed",
                                "destination": str(pdf.parent.resolve()),
                                "new_name": filename,
                            }
                        ],
                    }
                ],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    elif kind == "file_review_scheduled_pipeline":
        for directory in ("runtime", "state", "portal"):
            (fixture_root / directory).mkdir()
        (fixture_root / "portal/卷證A.pdf").write_bytes(b"fixture-review-pdf")
        (fixture_root / "portal/卷證A-重複.pdf").write_bytes(b"fixture-review-pdf")
        (fixture_root / "file-review-provider.json").write_text(
            json.dumps(
                {
                    "schema": "magi.v3.file-review-scheduled-fixture/v1",
                    "emails": [
                        {"kind": "downloadable", "case_number": "2026-0001"},
                        {"kind": "willingness_inquiry", "case_number": ""},
                    ],
                    "portal_files": ["portal/卷證A.pdf", "portal/卷證A-重複.pdf"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    elif kind == "slow_archive_rules":
        for directory in (
            "runtime",
            "archive/03_工作資料/10_結案",
            "nas/01_案件/法扶案件/民事/2025-0002-測試當事人-一審-損害賠償",
        ):
            (fixture_root / directory).mkdir(parents=True)
        source = fixture_root / "nas/01_案件/法扶案件/民事/2025-0002-測試當事人-一審-損害賠償"
        (source / "卷證.pdf").write_bytes(b"fixture-archive-pdf")
        (fixture_root / "slow-archive.json").write_text(
            json.dumps(
                {
                    "schema": "magi.v3.slow-archive-fixture/v1",
                    "cases": [
                        {
                            "id": 1,
                            "case_number": "2025-0002",
                            "client_name": "測試當事人",
                            "source": source.relative_to(fixture_root).as_posix(),
                            "archive_relative_parent": "法扶案件/民事",
                            "folder_path": "Y:/lumi/03_工作資料/10_結案/法扶案件/民事/2025-0002-測試當事人-一審-損害賠償",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        raise ScheduleBodyRegistryError(f"unknown fixture kind: {kind}")
    inventory = _inventory(fixture_root)
    return {"initial_inventory_sha256": inventory["sha256"], "initial_file_count": inventory["files"]}


def _inventory(root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    files = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            files += 1
            rows.append({"path": relative, "kind": "file", "sha256": _sha256_file(path), "size": path.stat().st_size})
        elif path.is_dir():
            rows.append({"path": relative, "kind": "directory"})
    return {"rows": rows, "sha256": _sha256(rows), "files": files}


def _assert_no_symlinks(root: Path) -> None:
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ScheduleBodyRegistryError("adapter fixture contains a symlink")


def _extract_json(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(stdout):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _extract_last_json(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    candidates = [index for index, character in enumerate(stdout) if character == "{"]
    for index in reversed(candidates):
        try:
            value, end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not stdout[index + end :].strip():
            return value
    return {}


def _dotted(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return None
    return current


def _fixture_json(fixture_root: Path, relative: str) -> Any:
    """Read a regular JSON artifact without permitting fixture-root escape."""

    candidate = fixture_root / relative
    if candidate.is_symlink():
        return None
    path = candidate.resolve(strict=False)
    if (
        not _is_relative_to(path, fixture_root.resolve())
        or not path.is_file()
    ):
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _fixture_jsonl(fixture_root: Path, relative: str) -> list[dict[str, Any]] | None:
    candidate = fixture_root / relative
    if candidate.is_symlink():
        return None
    path = candidate.resolve(strict=False)
    if not _is_relative_to(path, fixture_root.resolve()) or not path.is_file():
        return None
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return rows if all(isinstance(row, dict) for row in rows) else None


def _contract_ok(
    contract: Mapping[str, Any], fixture_root: Path, stdout: str, stderr: str
) -> tuple[bool, dict[str, Any]]:
    contract_type = str(contract.get("type") or "")
    payload: dict[str, Any] = {}
    if contract_type == "obsidian_case_card_sync":
        payload = _extract_json(stdout)
        cards = {
            "2099-0001.md": {
                "case_number": 'case_number: "2099-0001"',
                "client_name": 'client_name: "第一測試當事人"',
                "case_type": 'case_type: "刑事"',
                "case_reason": 'case_reason: "測試罪名"',
                "court_case_number": 'court_case_number: "2099年度測字第1號"',
            },
            "2099-0002.md": {
                "case_number": 'case_number: "2099-0002"',
                "client_name": 'client_name: "第二測試當事人"',
                "case_type": 'case_type: "民事"',
                "case_reason": 'case_reason: "損害賠償"',
                "court_case_number": 'court_case_number: "2099年度訴字第2號"',
            },
        }
        contents: dict[str, str] = {}
        try:
            for filename in cards:
                contents[filename] = (
                    fixture_root / "vault" / "30_Index" / filename
                ).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False, {"error": "case card output is unreadable"}
        checks = {
            "stdout": all(
                payload.get(key) == value
                for key, value in {
                    "success": True,
                    "total": 2,
                    "created": 1,
                    "updated": 1,
                    "skipped": 0,
                }.items()
            ),
            "card_count": len(contents) == 2,
            "cards_bound_to_seed_rows": all(
                all(expected in contents[filename] for expected in fields.values())
                for filename, fields in cards.items()
            ),
            "stale_card_replaced": "Must be replaced" not in contents["2099-0001.md"],
            "dataview_query_present": all(
                f'WHERE case_number = "{filename[:-3]}"' in content
                for filename, content in contents.items()
            ),
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "card_sha256": {
                filename: _sha256_file(fixture_root / "vault" / "30_Index" / filename)
                for filename in cards
            },
        }
    if contract_type == "obsidian_note_repair":
        payload = _extract_json(stdout)
        relative = Path(
            "vault/20_Notes/2099-0001-測試當事人-民事-一審-測試/summary__fixture.md"
        )
        note_path = fixture_root / relative
        index_path = fixture_root / "agent" / "obsidian_index.json"
        try:
            note = note_path.read_text(encoding="utf-8")
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "repaired note or index is unreadable"}
        index_relative = relative.relative_to("vault").as_posix()
        index_row = (index.get("notes") or {}).get(index_relative) or {}
        expected_hash = hashlib.sha256(note.encode("utf-8")).hexdigest()[:16]
        required_sections = (
            "## 摘要",
            "## 期限與待辦",
            "## 爭點與證據",
            "## 法律/程序意義",
            "## Full Text",
        )
        notification_artifacts = [
            path.relative_to(fixture_root).as_posix()
            for pattern in ("**/*red_phone*", "**/*outbox*", "**/*discord*", "**/*telegram*")
            for path in fixture_root.glob(pattern)
        ]
        checks = {
            "stdout": all(
                payload.get(key) == value
                for key, value in {
                    "success": True,
                    "scanned": 1,
                    "repaired": 1,
                    "reextracted": 1,
                    "missing_sources": 0,
                    "errors": 0,
                }.items()
            ),
            "schema": "summary_schema: magi-obsidian-note-v2" in note,
            "sections": all(section in note for section in required_sections),
            "source_reextracted": "FIXTURE_REEXTRACTED_SOURCE_2099_0001" in note,
            "weak_body_replaced": "弱內容。" not in note,
            "index_hash_updated": index_row.get("hash") == expected_hash,
            "index_mtime_updated": int(index_row.get("mtime") or 0) > 1,
            "notification_artifacts_absent": not notification_artifacts,
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "repaired_note_sha256": _sha256_file(note_path),
            "index_sha256": _sha256_file(index_path),
            "notification_artifact_count": len(notification_artifacts),
        }
    if contract_type == "laf_gmail_willingness_provider":
        payload = _extract_last_json(stdout)
        state_path = fixture_root / "static" / "laf_gmail_monitor_state.json"
        pending_path = fixture_root / "runtime" / "laf_gmail_dispatch_pending.json"
        transcript_path = fixture_root / "gmail_provider_transcript.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "LAF Gmail terminal evidence is unreadable"}
        cases = payload.get("cases") if isinstance(payload, dict) else None
        first = cases[0] if isinstance(cases, list) and cases else {}
        actions = [row.get("action") for row in transcript if isinstance(row, dict)]
        checks = {
            "terminal_summary": payload.get("ok") is True
            and payload.get("status") == "ok"
            and payload.get("seen") == 1
            and payload.get("handled") == 1
            and payload.get("marked_processed") == 1
            and payload.get("failure_count") == 0
            and payload.get("cleanup_ok") is True,
            "willingness_not_dispatch": first.get("route") == "willingness"
            and first.get("handled") is True
            and first.get("marked_processed") is True,
            "state_persisted": state.get("status") == "ok"
            and state.get("marked_processed") == 1,
            "pending_cleared": pending.get("pending_count") == 0
            and pending.get("cases") == {},
            "provider_terminal_transcript": actions
            == ["authenticate", "check_emails", "mark_laf_processed", "close"],
            "no_case_folder_created": not (fixture_root / "cases").exists(),
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "state_sha256": _sha256_file(state_path),
            "pending_sha256": _sha256_file(pending_path),
            "provider_transcript_sha256": _sha256_file(transcript_path),
            "provider_quality_certified": False,
            "provider_role": "deterministic_gmail_willingness_fixture",
        }
    if contract_type == "laf_portal_download_provider":
        payload = _extract_last_json(stdout)
        state_path = fixture_root / "static" / "laf_portal_new_files_latest.json"
        transcript_path = fixture_root / "portal_provider_transcript.json"
        archived = sorted(
            (fixture_root / "case-roots").glob("**/01_法扶資料/*.pdf")
        )
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "LAF portal terminal evidence is unreadable"}
        actions = [row.get("action") for row in transcript if isinstance(row, dict)]
        checks = {
            "terminal_summary": payload.get("ok") is True
            and payload.get("status") == "downloaded"
            and payload.get("apply") is True
            and payload.get("dry_run") is False
            and payload.get("scanned_cases") == 1
            and payload.get("portal_auto_downloaded") == 1
            and payload.get("portal_still_missing") == 0,
            "state_persisted": state.get("status") == "downloaded"
            and state.get("portal_auto_downloaded") == 1,
            "one_pdf_archived": len(archived) == 1
            and archived[0].read_bytes().startswith(b"%PDF-1.4"),
            "provider_terminal_transcript": actions
            == ["login", "get_downloadable_cases", "download_case_files", "close"],
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "state_sha256": _sha256_file(state_path),
            "archived_sha256": [_sha256_file(path) for path in archived],
            "provider_transcript_sha256": _sha256_file(transcript_path),
            "provider_quality_certified": False,
            "provider_role": "deterministic_laf_portal_fixture",
        }
    if contract_type == "laf_portal_retry_provider":
        payload = _extract_last_json(stdout)
        queue_path = fixture_root / "agent" / "laf_pending_portal_downloads.json"
        heartbeat_path = fixture_root / "static" / "laf_portal_retry_state.json"
        transcript_path = fixture_root / "workflow_provider_transcript.json"
        archived = sorted((fixture_root / "cases").glob("**/*.pdf"))
        try:
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "LAF portal retry terminal evidence is unreadable"}
        actions = [row.get("action") for row in transcript if isinstance(row, dict)]
        checks = {
            "terminal_summary": payload.get("ok") is True
            and payload.get("processed") == 1,
            "queue_cleared": queue.get("items") == [],
            "heartbeat_terminal_ok": heartbeat.get("ok") is True
            and heartbeat.get("status") == "ok"
            and heartbeat.get("processed_count") == 1,
            "one_pdf_archived": len(archived) == 1
            and archived[0].read_bytes().startswith(b"%PDF-1.4"),
            "provider_terminal_transcript": actions
            == ["login", "download_case_files", "close"],
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "queue_sha256": _sha256_file(queue_path),
            "heartbeat_sha256": _sha256_file(heartbeat_path),
            "archived_sha256": [_sha256_file(path) for path in archived],
            "provider_transcript_sha256": _sha256_file(transcript_path),
            "provider_quality_certified": False,
            "provider_role": "deterministic_laf_portal_retry_fixture",
        }
    if contract_type == "laf_condition_draft_provider":
        payload = _extract_last_json(stdout)
        draft_path = fixture_root / "workflow_draft.json"
        transcript_path = fixture_root / "workflow_provider_transcript.json"
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "LAF condition terminal evidence is unreadable"}
        items = payload.get("items") if isinstance(payload, dict) else None
        first = items[0] if isinstance(items, list) and items else {}
        actions = [row.get("action") for row in transcript if isinstance(row, dict)]
        checks = {
            "terminal_summary": payload.get("ok") is True
            and payload.get("scanned") == 1
            and payload.get("processed") == 1,
            "condition_item": first.get("ok") is True
            and first.get("laf_case_number") == "1150709-T-051"
            and first.get("upload_files") == 1
            and first.get("portal_status") == "uploaded",
            "draft_saved_only": draft.get("workflow") == "condition"
            and draft.get("laf_case_number") == "1150709-T-051"
            and draft.get("saved") is True
            and len(draft.get("uploads") or []) == 1,
            "provider_terminal_transcript": actions
            == ["login", "save_workflow_draft", "close"],
            "submit_absent": "submit_workflow" not in actions,
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "draft_sha256": _sha256_file(draft_path),
            "provider_transcript_sha256": _sha256_file(transcript_path),
            "provider_quality_certified": False,
            "provider_role": "deterministic_laf_workflow_draft_fixture",
        }
    if contract_type == "laf_nightly_audit_bounded":
        payload = _extract_last_json(stdout)
        terminal_path = fixture_root / "reports" / "laf_audit_terminal.json"
        transcript_path = fixture_root / "portal_provider_transcript.json"
        reports = sorted((fixture_root / "reports").glob("laf_audit_*.md"))
        try:
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            report_text = reports[0].read_text(encoding="utf-8") if len(reports) == 1 else ""
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "LAF nightly terminal evidence is unreadable"}
        actions = [row.get("action") for row in transcript if isinstance(row, dict)]
        terminal_checks = terminal.get("checks") if isinstance(terminal, dict) else {}
        stdout_terminal = payload.get("bounded_terminal") if isinstance(payload, dict) else {}
        expected_actions = [
            "login",
            "query_closing_status",
            "close",
            "login",
            "get_downloadable_cases",
            "close",
            "login",
            "query_pending_drafts_all",
            "close",
        ]
        temporary_files = sorted(fixture_root.glob("**/*.tmp"))
        checks = {
            "terminal_summary": payload.get("ok") is True
            and payload.get("total_cases") == 2
            and payload.get("repair_proposal_count") == 1
            and payload.get("pending_close_count") == 1
            and payload.get("portal_pending_closing_drafts") == 1
            and payload.get("portal_still_missing_count") == 0,
            "repair_proposal_reported": "待報結誤判修復建議：1 件" in report_text
            and "本次僅產生建議，未修改案件資料" in report_text,
            "durable_report": len(reports) == 1
            and reports[0].stat().st_size > 0
            and terminal.get("report_sha256") == _sha256_file(reports[0]),
            "bounded_terminal": terminal.get("status") == "passed"
            and terminal.get("mode") == "proposal_only"
            and terminal.get("sequence")
            == ["audit", "repair_proposal", "durable_report", "providers_closed"]
            and isinstance(terminal_checks, dict)
            and terminal_checks
            and all(value is True for value in terminal_checks.values()),
            "provider_terminal_transcript": actions == expected_actions
            and terminal.get("provider_actions") == expected_actions
            and terminal.get("provider_login_count") == 3
            and terminal.get("provider_close_count") == 3,
            "formal_mutation_forbidden": terminal.get("database_mutation_attempts") == []
            and terminal.get("forbidden_actions") == []
            and terminal.get("database_mutation_allowed") is False
            and terminal.get("portal_download_allowed") is False
            and terminal.get("portal_draft_allowed") is False
            and terminal.get("portal_submit_allowed") is False
            and terminal.get("notifications_allowed") is False,
            "stdout_terminal_matches_durable": stdout_terminal.get("status") == "passed"
            and stdout_terminal.get("report_sha256") == terminal.get("report_sha256")
            and stdout_terminal.get("provider_transcript_sha256")
            == terminal.get("provider_transcript_sha256"),
            "no_forbidden_artifacts": not temporary_files
            and not (fixture_root / "provider-downloads").exists()
            and not (fixture_root / "workflow_draft.json").exists(),
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "report_sha256": _sha256_file(reports[0]) if len(reports) == 1 else "",
            "terminal_sha256": _sha256_file(terminal_path),
            "provider_transcript_sha256": _sha256_file(transcript_path),
            "provider_quality_certified": False,
            "provider_role": "deterministic_laf_nightly_audit_fixture",
            "database_immutability_verified_by_dependency_postconditions": True,
        }
    if contract_type == "laf_condition_mediation_mark":
        payload = _extract_json(stdout)
        case = fixture_root / "cases" / "2099-0002-測試當事人-民事-一審-調解"
        marker_paths = (
            case / "01_法扶資料" / ".magi_condition_reported.done",
            case / ".magi_condition_reported.done",
        )
        state_path = (
            fixture_root
            / "agent"
            / "laf-orchestrator"
            / "_laf_condition_manual_done.json"
        )
        try:
            markers = [json.loads(path.read_text(encoding="utf-8")) for path in marker_paths]
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "condition marker or state registry is unreadable"}
        notification_artifacts = [
            path.relative_to(fixture_root).as_posix()
            for pattern in ("**/*red_phone*", "**/*outbox*", "**/*discord*", "**/*telegram*")
            for path in fixture_root.glob(pattern)
        ]
        checks = {
            "stdout": all(
                payload.get(key) == value
                for key, value in {
                    "ok": True,
                    "scanned": 1,
                    "matched": 1,
                    "marked": 1,
                    "already_done": 0,
                    "missing_folder": 0,
                }.items()
            )
            and payload.get("errors") == [],
            "both_markers_written": len(markers) == 2,
            "marker_identity": all(
                marker.get("manual_done") is True
                and marker.get("reason") == "auto_detected_mediation_failure_doc"
                and marker.get("laf_case_number") == "LAF-2099-0002"
                and marker.get("osc_case_number") == "2099-0002"
                for marker in markers
            ),
            "state_by_laf": "LAF-2099-0002" in (state.get("by_laf") or {}),
            "state_by_osc": "2099-0002" in (state.get("by_osc") or {}),
            "state_by_client": "測試當事人" in (state.get("by_client") or {}),
            "notification_artifacts_absent": not notification_artifacts,
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "marker_sha256": [_sha256_file(path) for path in marker_paths],
            "state_sha256": _sha256_file(state_path),
            "notification_artifact_count": len(notification_artifacts),
        }
    if contract_type == "nas_ocr_provider_fixture":
        payload = _extract_json(stdout)
        case = fixture_root / "nas-cases" / "2099-0001"
        output = case / "scanned-fixture_OCR.pdf"
        archived = case / "_Archive_No_OCR" / "scanned-fixture.pdf"
        queue = fixture_root / "agent" / "nas-ocr-queue.db"
        status = ""
        attempts = -1
        try:
            connection = sqlite3.connect(queue)
            row = connection.execute(
                "SELECT status,attempt_count FROM ocr_queue"
            ).fetchone()
            connection.close()
            if row:
                status, attempts = str(row[0]), int(row[1])
        except (OSError, sqlite3.Error, TypeError, ValueError):
            pass
        checks = {
            "stdout": all(
                payload.get(key) == value
                for key, value in {
                    "ok": True,
                    "status": "completed",
                    "skipped": False,
                    "processed": 1,
                    "skipped_digital": 0,
                    "completed": 1,
                    "failed": 0,
                }.items()
            ),
            "provider_output_published": output.is_file() and output.stat().st_size > 1000,
            "source_archived_after_publish": archived.is_file() and not (case / "scanned-fixture.pdf").exists(),
            "queue_completed_once": status == "completed" and attempts == 1,
            "provider_is_fixture_owned": (fixture_root / "bin" / "ocrmypdf").is_file(),
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "output_sha256": _sha256_file(output) if output.is_file() else None,
            "archived_source_sha256": _sha256_file(archived) if archived.is_file() else None,
            "provider_quality_certified": False,
            "provider_role": "deterministic_cli_and_publish_workflow_fixture",
        }
    if contract_type == "tailscale_repair_fixture":
        report_path = fixture_root / "runtime" / "tailscale-funnel.json"
        actions_path = fixture_root / "runtime" / "tailscale-actions.log"
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            actions = [
                line.strip()
                for line in actions_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "tailscale repair report/action transcript is unreadable"}
        checks = {
            "recovered": payload.get("status") == "recovered",
            "initial_public_probe_failed": not any(
                row.get("ok") is True for row in payload.get("probes", [])
            ),
            "initial_mobile_probe_failed": (payload.get("mobile_entry") or {}).get("ok")
            is False,
            "repaired_public_probe_passed": any(
                row.get("ok") is True for row in payload.get("reprobes", [])
            ),
            "repaired_mobile_probe_passed": (
                payload.get("mobile_entry_after_repair") or {}
            ).get("ok")
            is True,
            "bounded_apply_action_recorded": actions == ["enable"],
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "action_transcript_sha256": _sha256(actions),
            "provider_quality_certified": False,
            "provider_role": "deterministic_tailscale_cli_dns_curl_fixture",
        }
    if contract_type == "toolsai_import_fixture":
        payload = _extract_json(stdout)
        knowledge_path = fixture_root / "home" / ".magi" / "auto_skill" / "knowledge.json"
        try:
            knowledge = json.loads(knowledge_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "auto-skill knowledge output is unreadable"}
        contexts = {
            str(row.get("context") or "")
            for row in knowledge
            if isinstance(row, dict)
        } if isinstance(knowledge, list) else set()
        notification_artifacts = [
            path.relative_to(fixture_root).as_posix()
            for pattern in ("**/*red_phone*", "**/*outbox*", "**/*discord*", "**/*telegram*")
            for path in fixture_root.glob(pattern)
        ]
        checks = {
            "stdout_success": payload.get("success") is True,
            "status_is_measured_improvement": payload.get("status") == "improved",
            "two_sources_learned": int(
                (payload.get("metrics") or {}).get("knowledge_new") or 0
            ) == 2,
            "two_files_imported": int(
                (payload.get("metrics") or {}).get("source_files_checked") or 0
            ) == 2,
            "receipt_persisted": (
                fixture_root / "runtime" / "daily_self_evolution_latest.json"
            ).is_file(),
            "knowledge_persisted": isinstance(knowledge, list) and len(knowledge) == 2,
            "knowledge_contexts": contexts
            == {"toolsai-auto-skill-kb", "toolsai-auto-skill-exp"},
            "vector_mirror_explicitly_disabled": (
                (payload.get("metrics") or {}).get("vector_mirror_mode") == "disabled"
            ),
            "external_source_is_reference_only": (
                (payload.get("source_policy") or {}).get("external_repository_is_reference_only")
                is True
            ),
            "auto_deploy_forbidden": (
                (payload.get("deployment_policy") or {}).get("auto_deploy") is False
            ),
            "notification_artifacts_absent": not notification_artifacts,
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "knowledge_sha256": _sha256(knowledge),
            "notification_artifact_count": len(notification_artifacts),
        }
    if contract_type == "external_chat_provider_fixture":
        payload = _extract_last_json(stdout)
        rows = payload.get("checks") if isinstance(payload, dict) else None
        checks = {
            "summary_success": payload.get("success") is True,
            "two_successful_checks": payload.get("successful_checks") == 2,
            "simple_and_complex_executed": isinstance(rows, list)
            and {row.get("name") for row in rows if isinstance(row, dict)}
            == {"SIMPLE", "COMPLEX"},
            "both_http_and_semantic_success": isinstance(rows, list)
            and len(rows) == 2
            and all(
                row.get("ok") is True
                and row.get("http_status") == 200
                and row.get("success") is True
                and int(row.get("reply_length") or 0) > 10
                for row in rows
                if isinstance(row, dict)
            ),
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "provider_quality_certified": False,
            "provider_role": "deterministic_external_chat_http_fixture",
        }
    if contract_type == "judicial_api_night_pull_fixture":
        payload = _extract_last_json(stdout)
        raw_files = sorted(
            (fixture_root / "cache" / "judicial_api" / "raw").glob("*/*.json")
        )
        state_path = fixture_root / "cache" / "judicial_api" / "pull_state.json"
        try:
            raw_payloads = [
                json.loads(path.read_text(encoding="utf-8")) for path in raw_files
            ]
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "judicial API pull output/state is unreadable"}
        runs = state.get("runs") if isinstance(state, dict) else None
        first_run = runs[0] if isinstance(runs, list) and runs else {}
        first_raw = raw_payloads[0] if raw_payloads else {}
        first_jdoc = first_raw.get("payload") if isinstance(first_raw, dict) else {}
        checks = {
            "stdout_success": payload.get("success") is True,
            "authenticated": payload.get("auth_success") is True,
            "one_new_document": payload.get("fetched") == 1
            and payload.get("failed") == 0,
            "raw_document_persisted": len(raw_payloads) == 1
            and isinstance(first_jdoc, dict)
            and first_jdoc.get("JID") == "TPSV,2099,台上,1,20990102,1"
            and len(str((first_jdoc.get("JFULLX") or {}).get("JFULLCONTENT") or ""))
            > 300,
            "pull_state_committed": first_run.get("fetched") == 1
            and first_run.get("failed") == 0
            and first_run.get("credentials_source") == "env",
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "raw_payload_sha256": _sha256(raw_payloads),
            "pull_state_sha256": _sha256(state),
            "provider_quality_certified": False,
            "provider_role": "deterministic_judicial_api_http_fixture",
        }
    if contract_type == "judicial_api_day_process_fixture":
        payload = _extract_last_json(stdout)
        normalized = sorted(
            (fixture_root / "cache" / "judicial_api" / "normalized").glob("*/*.txt")
        )
        state_path = fixture_root / "cache" / "judicial_api" / "process_state.json"
        try:
            normalized_texts = [path.read_text(encoding="utf-8") for path in normalized]
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "judicial API normalized output/state is unreadable"}
        last_run = state.get("last_run") if isinstance(state, dict) else {}
        processed = state.get("processed") if isinstance(state, dict) else {}
        checks = {
            "stdout_success": payload.get("success") is True,
            "one_document_handled": payload.get("handled") == 1,
            "no_processing_errors": payload.get("errors") == [],
            "backlog_cleared": payload.get("backlog_before") == 1
            and payload.get("backlog_remaining") == 0,
            "safe_processing_mode": payload.get("summary_mode") == "extractive"
            and payload.get("skip_assets") is True
            and payload.get("vector_ingest") is False,
            "normalized_full_text_persisted": len(normalized_texts) == 1
            and "舉證責任" in normalized_texts[0]
            and len(normalized_texts[0]) > 500,
            "process_state_committed": isinstance(processed, dict)
            and len(processed) == 1
            and isinstance(last_run, dict)
            and last_run.get("handled") == 1
            and last_run.get("errors") == 0,
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "normalized_sha256": [_sha256(value) for value in normalized_texts],
            "process_state_sha256": _sha256(state),
        }
    if contract_type == "judgment_daily_crawl_fixture":
        payload = _extract_last_json(stdout)
        judgments_path = (
            fixture_root / "agent" / "judgment-collector" / "judgments.json"
        )
        reports = sorted((fixture_root / "archive").glob("summary_report.md"))
        try:
            judgments = json.loads(judgments_path.read_text(encoding="utf-8"))
            report = reports[0].read_text(encoding="utf-8") if len(reports) == 1 else ""
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "daily crawl result artifacts are unreadable"}
        result = (payload.get("results") or [{}])[0].get("result") or {}
        first = judgments[0] if isinstance(judgments, list) and judgments else {}
        checks = {
            "stdout_success": payload.get("success") is True,
            "one_reason_processed": payload.get("reasons_processed") == 1
            and payload.get("success_reasons") == 1,
            "one_judgment_collected": payload.get("total_collected") == 1
            and result.get("count") == 1
            and result.get("usable_count") == 1,
            "extractive_real_body": (result.get("items") or [{}])[0].get(
                "summary_route"
            )
            == "extractive_daily",
            "judgment_index_persisted": isinstance(judgments, list)
            and len(judgments) == 1
            and first.get("case_reason") == "損害賠償"
            and "舉證責任" in str(first.get("summary") or ""),
            "summary_report_persisted": "判決收集報告" in report
            and "最高法院2099年度" in report
            and "第1號民事判決" in report,
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "judgments_sha256": _sha256(judgments),
            "summary_report_sha256": _sha256(report),
            "provider_quality_certified": False,
            "provider_role": "deterministic_tools_api_judgment_search_fixture",
        }
    if contract_type == "judgment_retry_fixture":
        payload = _extract_last_json(stdout)
        queue = fixture_root / "cache" / "summary-retry.jsonl"
        try:
            remaining = [line for line in queue.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, UnicodeError):
            return False, {"error": "summary retry queue is unreadable"}
        checks = {
            "stdout_success": payload.get("success") is True,
            "one_item_processed": payload.get("queue_size") == 1
            and payload.get("processed") == 1,
            "one_summary_improved": payload.get("improved") == 1
            and payload.get("remaining") == 0,
            "fixture_database_untouched": payload.get("db_updates") == 0,
            "queue_commit_persisted": remaining == [],
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "remaining_queue_sha256": _sha256(remaining),
            "provider_quality_certified": False,
            "provider_role": "deterministic_omlx_health_and_summary_tools_fixture",
        }
    if contract_type == "worldmonitor_provider_fixture":
        reports = sorted((fixture_root / "static" / "worldmonitor_reports").glob("*.md"))
        sidecars = sorted((fixture_root / "static" / "worldmonitor_reports").glob("*.json"))
        try:
            report = reports[0].read_text(encoding="utf-8") if len(reports) == 1 else ""
            sidecar = (
                json.loads(sidecars[0].read_text(encoding="utf-8"))
                if len(sidecars) == 1
                else {}
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "worldmonitor report artifacts are unreadable"}
        checks = {
            "report_written_once": len(reports) == 1 and len(sidecars) == 1,
            "rss_item_rendered": "Fixture RSS：台灣供應鏈測試消息" in report,
            "market_quote_rendered": "**FIX**: $123.50" in report,
            "source_health_green": "Fixture RSS: OK (1 篇)" in report
            and "市場資料：OK" in report,
            "grounded_fallback_used": "**分析**: 來源整理" in report,
            "sidecar_binds_provider_data": len(sidecar.get("news_items") or []) == 1
            and (sidecar.get("market_status") or {}).get("quotes_ok") == 1
            and sidecar.get("market_symbols") == ["FIX"],
        }
        return all(checks.values()), {
            "checks": checks,
            "report_sha256": _sha256(report),
            "sidecar_sha256": _sha256(sidecar),
            "provider_quality_certified": False,
            "provider_role": "deterministic_rss_and_market_http_fixture",
        }
    if contract_type == "file_review_portal_provider_fixture":
        payload = _extract_last_json(stdout)
        trace_path = fixture_root / "runtime" / "file-review-provider.jsonl"
        try:
            trace = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "file-review provider trace is unreadable"}
        events = [str(row.get("event") or "") for row in trace if isinstance(row, dict)]
        checks = {
            "stdout_success": payload.get("success") is True,
            "portal_is_authoritative": payload.get("source") == "portal"
            and (payload.get("portal") or {}).get("success") is True,
            "empty_portal_result_is_explicit": payload.get("count") == 0
            and payload.get("downloadable_count") == 0
            and payload.get("items") == [],
            "gmail_not_used": (payload.get("gmail") or {}).get("success") is False,
            "provider_lifecycle_complete": events
            == ["init", "probe_downloadable_from_portal", "close"],
            "provider_received_unfiltered_probe": (
                trace[1].get("payload") if len(trace) > 1 else {}
            )
            == {"target_case_number": None},
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "provider_trace_sha256": _sha256(trace),
            "provider_quality_certified": False,
            "provider_role": "deterministic_file_review_portal_module_fixture",
        }
    if contract_type == "transcript_sync_provider_fixture":
        payload = _extract_last_json(stdout)
        trace_path = fixture_root / "runtime" / "transcript-provider.jsonl"
        report_path = Path(str(payload.get("transcript_sync_report") or ""))
        archived = fixture_root / "cases" / "2099-0001" / "05_筆錄" / "20990101_測試筆錄.pdf"
        state_path = fixture_root / "agent" / "transcript_sync_state.json"
        try:
            trace = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line]
            report = json.loads(report_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "transcript sync artifacts are unreadable"}
        events = [str(row.get("event") or "") for row in trace]
        checks = {
            "stdout_success": payload.get("success") is True,
            "one_file_downloaded": payload.get("downloaded_count") == 1,
            "provider_lifecycle_complete": events == [
                "init", "init", "md5_scan", "close", "cleanup", "login",
                "get_cases", "download_record", "move_to_case_folder",
                "rename_all_transcripts", "close",
            ],
            "file_archived": archived.is_file() and archived.read_bytes().startswith(b"%PDF-1.4"),
            "state_committed": len(state.get("cases") or {}) == 1,
            "report_committed": report.get("summary", {}).get("downloaded_count") == 1,
            "notification_side_effect_absent": not any(
                path.is_file()
                for pattern in ("**/*outbox*", "**/*red_phone*", "**/*telegram*", "**/*discord*")
                for path in fixture_root.glob(pattern)
            ),
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "trace_sha256": _sha256(trace),
            "state_sha256": _sha256(state),
            "report_sha256": _sha256(report),
            "provider_quality_certified": False,
            "provider_role": "deterministic_judicial_record_module_fixture",
        }
    if contract_type == "research_brief_provider_fixture":
        payload = _extract_last_json(stdout)
        root = fixture_root / "runtime" / "research_brief"
        sink_path = root / "fixture-memory.jsonl"
        digest_path = root / "last_digest.jsonl"
        try:
            sink = [json.loads(line) for line in sink_path.read_text(encoding="utf-8").splitlines() if line]
            digests = [json.loads(line) for line in digest_path.read_text(encoding="utf-8").splitlines() if line]
            seen = json.loads((root / "seen.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "research brief artifacts are unreadable"}
        result = (payload.get("results") or [{}])[0]
        checks = {
            "stdout_success": payload.get("success") is True,
            "one_namespace": payload.get("total_namespaces") == 1,
            "one_new_entry": result.get("new_entries") == 1
            and result.get("ingested_to_memory") == 1,
            "notification_suppressed": result.get("delivered") is False,
            "memory_sink_committed": len(sink) == 1
            and "法律科技隔離測試" in str(sink[0].get("content") or ""),
            "digest_log_committed": len(digests) == 1
            and digests[0].get("namespace") == "fixture-law",
            "dedupe_state_committed": len(seen) == 1,
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "memory_sink_sha256": _sha256(sink),
            "digest_log_sha256": _sha256(digests),
            "seen_sha256": _sha256(seen),
            "provider_quality_certified": False,
            "provider_role": "deterministic_research_rss_and_memory_sink_fixture",
        }
    if contract_type == "market_briefing_provider_fixture":
        state_path = fixture_root / "agent" / "market_watchlist.json"
        perf_path = fixture_root / "agent" / "market_perf_history.json"
        trace_path = fixture_root / "runtime" / "market-provider.jsonl"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            perf = json.loads(perf_path.read_text(encoding="utf-8"))
            trace = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "market briefing artifacts are unreadable"}
        checks = {
            "report_rendered": "MAGI 每日股價預測" in stdout
            and "台積電 (2330.TW)" in stdout
            and "月營收(209906) YoY 12.5%" in stdout,
            "deep_indicators_rendered": "RSI(14)" in stdout and "ADX=" in stdout,
            "state_committed": bool(state.get("last_report_date")),
            "prediction_committed": len(perf.get("records") or []) == 1
            and (perf.get("records") or [{}])[0].get("symbol") == "2330.TW",
            "provider_accesses_complete": [
                (row.get("section"), row.get("key")) for row in trace
            ]
            == [("history", "2330.TW"), ("tw_financials", "2330")],
            "notification_artifact_absent": not (fixture_root / "static" / "market_briefing_notify.log").exists(),
        }
        return all(checks.values()), {
            "checks": checks,
            "state_sha256": _sha256(state),
            "perf_sha256": _sha256(perf),
            "trace_sha256": _sha256(trace),
            "provider_quality_certified": False,
            "provider_role": "deterministic_market_history_and_fundamentals_fixture",
        }
    if contract_type == "omlx_switch_provider_fixture":
        result_path = fixture_root / "runtime" / "omlx-result.json"
        launchctl_path = fixture_root / "runtime" / "launchctl-actions.log"
        log_path = fixture_root / "runtime" / "omlx-switch.log"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            launchctl_actions = [
                line for line in launchctl_path.read_text(encoding="utf-8").splitlines() if line
            ]
            switch_log = log_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "oMLX fixture handoff artifacts are unreadable"}
        resolved = str(result.get("resolved_mode") or "")
        expected_main = "e4b" if resolved == "day" else "26b"
        checks = {
            "requested_mode_bound": result.get("requested_mode")
            in {"day", "night", "auto"},
            "resolved_mode_valid": resolved in {"day", "night"},
            "active_profile_committed": result.get("active_profile") == resolved,
            "main_model_matches_profile": expected_main
            in str(result.get("main_model") or "").lower(),
            "sidecars_match_profile": (
                bool(result.get("phi_model")) and bool(result.get("smol_model"))
                if resolved == "day"
                else not result.get("phi_model") and not result.get("smol_model")
            ),
            "main_launch_handoff_executed": any("com.magi.omlx" in line for line in launchctl_actions)
            and any("kickstart" in line for line in launchctl_actions),
            "completion_logged": f"Switch to {resolved} complete" in switch_log,
            "fixture_symlinks_cleaned": not any(
                path.is_symlink() for path in (fixture_root / "home" / ".omlx").rglob("*")
            ),
        }
        return all(checks.values()), {
            "checks": checks,
            "result_sha256": _sha256(result),
            "launchctl_trace_sha256": _sha256(launchctl_actions),
            "switch_log_sha256": _sha256(switch_log),
            "provider_quality_certified": False,
            "provider_role": "deterministic_launchctl_model_api_memory_provider_fixture",
        }
    if contract_type == "osc_events_pdf_scan_fixture":
        payload = _extract_last_json(stdout)
        latest_path = fixture_root / "runtime" / "osc-events-latest.json"
        cache_path = fixture_root / "runtime" / "pdf_calendar_scan_cache.json"
        expectation_path = fixture_root / "osc-events-fixture.json"
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            expectation = json.loads(expectation_path.read_text(encoding="utf-8"))
            expected_date = datetime.strptime(
                str(expectation["hearing_date"]), "%Y-%m-%d"
            ).date()
        except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return False, {"error": "OSC events scan artifacts are unreadable"}
        scan = payload.get("pdf_calendar_scan") or {}
        samples = scan.get("sample_items") or []
        first_todo = ((samples[0].get("todos") or [{}])[0]) if samples else {}
        expected_time = str(expectation.get("hearing_time") or "")
        expected_file = str(expectation.get("file_name") or "")
        checks = {
            "fixture_expectation_bound": expectation.get("schema")
            == "magi.v3.osc-events-fixture-expectation/v1"
            and bool(expected_file),
            "fixture_hearing_is_future": expected_date
            > datetime.now(TAIPEI_TIMEZONE).date(),
            "stdout_success": payload.get("ok") is True,
            "safe_scan_mode": payload.get("dry_run") is True,
            "one_pdf_scanned": scan.get("scanned") == 1,
            "hearing_extracted": scan.get("todo_count") == 1
            and first_todo.get("type") == "開庭"
            and first_todo.get("date") == expected_date.isoformat()
            and first_todo.get("time") == expected_time,
            "fixture_file_bound": bool(samples)
            and samples[0].get("file_name") == expected_file,
            "dry_run_prevented_db_write": (samples[0].get("write_result") or {}).get("inserted") == 0
            if samples
            else False,
            "historical_scan_executed": (payload.get("historical_todo_completion") or {}).get("matched") == 1
            and (payload.get("historical_todo_completion") or {}).get("updated") == 0,
            "latest_report_bound": latest.get("pdf_calendar_scan", {}).get("todo_count") == 1,
            "scan_cache_committed": len(cache.get("files") or {}) == 1,
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "latest_sha256": _sha256(latest),
            "cache_sha256": _sha256(cache),
            "expectation_sha256": _sha256(expectation),
            "expected_hearing_date": expected_date.isoformat(),
        }
    if contract_type == "accounting_sheet_import_fixture":
        payload = _extract_last_json(stdout)
        items = payload.get("items") or []
        checks = {
            "stdout_success": payload.get("ok") is True,
            "dry_run_enforced": payload.get("dry_run") is True,
            "fixture_provider_bound": (payload.get("source") or {}).get("kind")
            == "certification_fixture",
            "two_rows_parsed": (payload.get("sheet_stats") or {}).get("parsed") == 2
            and payload.get("importable_count") == 2,
            "income_and_expense_semantics": len(items) == 2
            and [(item.get("type"), item.get("amount")) for item in items]
            == [("支出", 80.0), ("收入", 5000.0)],
            "case_reference_resolved": all(str(item.get("case_ref") or "") == "2026-0001" for item in items),
            "no_transaction_ids_created": all(not item.get("transaction_id") for item in items),
            "no_fixed_expense_false_positive": payload.get("fixed_expense_skip_count") == 0,
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "provider_quality_certified": False,
            "provider_role": "deterministic_accounting_sheet_fixture",
        }
    if contract_type == "accounting_monthly_bonus_fixture":
        payload = _extract_last_json(stdout)
        source_rows = payload.get("source_fee_rows") or []
        checks = {
            "stdout_success": payload.get("ok") is True,
            "dry_run_enforced": payload.get("dry_run") is True,
            "settlement_period_bound": payload.get("month") == "2026-06"
            and payload.get("period_start") == "2026-05-26"
            and payload.get("period_end") == "2026-06-25",
            "laf_fee_and_bonus_calculated": payload.get("legal_aid_debt_fee_total") == 10000.0
            and payload.get("legal_aid_bonus_amount") == 5000.0,
            "income_and_expense_aggregated": payload.get("income_total_before_bonus") == 10000.0
            and payload.get("expense_total_before_bonus") == 1000.0,
            "case_bonus_calculated": payload.get("case_bonus_pool") == 2000.0
            and payload.get("case_bonus_employee_amount") == 1000.0,
            "final_balance_calculated": payload.get("final_expense_total") == 7000.0
            and payload.get("final_balance") == 3000.0,
            "ready_without_db_commit": payload.get("status") == "ready"
            and not payload.get("laf_bonus_transaction_id")
            and not payload.get("case_bonus_transaction_id"),
            "source_fee_traceable": len(source_rows) == 1
            and source_rows[0].get("transaction_id") == 1
            and source_rows[0].get("laf_case_no") == "1150001-E-001",
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
        }
    if contract_type == "pdf_namer_benchmark_fixture":
        result_path = fixture_root / "runtime" / "benchmark_pdf_namer_latest.json"
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "PDF namer benchmark artifact is unreadable"}
        results = payload.get("results") or []
        checks = {
            "benchmark_passed": payload.get("ok") is True,
            "one_pdf_evaluated": payload.get("total") == 1
            and payload.get("effective_total") == 1,
            "format_semantics_passed": payload.get("format_valid_rate") == 1.0
            and payload.get("quality_pass_rate") == 1.0
            and payload.get("overall_pass_rate") == 1.0,
            "holding_semantics_passed": payload.get("holding_coverage") == 1.0,
            "no_empty_or_runtime_error": payload.get("empty_filename_rate") == 0.0
            and payload.get("error_rate") == 0.0,
            "validated_fixture_filename": len(results) == 1
            and results[0].get("valid") is True
            and results[0].get("filename")
            == "20260716 判決（測試當事人；原告之訴駁回）.pdf",
            "provider_claim_scoped": payload.get("provider_quality_certified") is False
            and payload.get("provider_role") == "deterministic_pdf_namer_proposal_fixture",
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "provider_quality_certified": False,
            "provider_role": "deterministic_pdf_namer_proposal_fixture",
        }
    if contract_type == "translator_ape_benchmark_fixture":
        result_path = fixture_root / "runtime" / "translator-ape.json"
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "translator APE benchmark artifact is unreadable"}
        rows = payload.get("rows") or []
        checks = {
            "benchmark_passed": payload.get("success") is True
            and payload.get("ok") is True
            and payload.get("has_failures") is False,
            "all_cases_evaluated": payload.get("cases") == 4
            and len(payload.get("case_results") or []) == 4
            and len(rows) == 8,
            "semantic_terms_preserved": (payload.get("avg_term_hit_rate") or {}).get("apple_baseline") == 1.0
            and (payload.get("avg_term_hit_rate") or {}).get("apple_ape") == 1.0,
            "case_and_validator_gate_passed": payload.get("case_fail_count") == 0
            and all(item.get("ok") is True for item in payload.get("case_results") or []),
            "ape_not_degraded": payload.get("ape_degraded_count") == 0
            and payload.get("ape_beats_baseline") is True,
            "provider_claim_scoped": payload.get("provider_quality_certified") is False
            and payload.get("provider_role") == "deterministic_translation_provider_fixture",
            "case_number_preserved": any(
                row.get("id") == "case_number"
                and "114年度原訴字第000024號" in str(row.get("text") or "")
                for row in rows
            ),
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "provider_quality_certified": False,
            "provider_role": "deterministic_translation_provider_fixture",
        }
    if contract_type == "nightly_regression_fixture":
        report_path = fixture_root / "runtime" / "nightly-regression.json"
        child_path = fixture_root / "runtime" / "magi_smoke_core_routes.json"
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            child = json.loads(child_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "nightly regression artifacts are unreadable"}
        suites = payload.get("suites") or []
        first_suite = suites[0] if suites else {}
        summary = child.get("summary") or {}
        checks = {
            "nightly_runner_passed": payload.get("ok") is True
            and payload.get("overall_ok") is True,
            "no_live_preflight_started": payload.get("preflight") is None,
            "only_core_routes_ran": len(suites) == 1
            and first_suite.get("suite") == "coreroutes",
            "suite_semantics_passed": first_suite.get("ok") is True
            and first_suite.get("passed") == 7
            and first_suite.get("failed") == 0,
            "child_report_bound": summary.get("pass") == 7
            and summary.get("fail") == 0
            and summary.get("total") == 7,
            "network_and_heavy_routes_excluded": (child.get("mode") or {}).get("with_network") is False
            and (child.get("mode") or {}).get("with_heavy") is False,
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "child_sha256": _sha256(child),
        }
    if contract_type == "drive_case_sync_fixture":
        payload = _extract_last_json(stdout)
        cases = payload.get("cases") or []
        summary = payload.get("summary") or {}
        first = cases[0] if cases else {}
        checks = {
            "terminal_success": payload.get("ok") is True and payload.get("status") == "ok",
            "worker_kind_bound": payload.get("worker_kind") == contract.get("worker_kind"),
            "fixture_dry_run": payload.get("mode") == "fixture_bidirectional_dry_run",
            "one_case_rule_checked": summary.get("matched_cases") == 1
            and len(cases) == 1
            and first.get("drive_rule_ok") is True,
            "native_paths_preserved": first.get("expected_drive_relative")
            == "一般案件/Lumi/測試當事人-一審-損害賠償"
            and first.get("actual_drive_relative") == first.get("expected_drive_relative"),
            "bidirectional_missing_plan": summary.get("nas_missing_in_drive_files") == 1
            and summary.get("drive_missing_in_nas_files") == 1
            and len(first.get("uploads") or []) == 1
            and len(first.get("downloads") or []) == 1,
            "same_content_deduped": summary.get("skipped_existing_files") == 1
            and len(first.get("existing") or []) == 1,
            "invalid_folder_blocked": summary.get("blocked_invalid_drive_paths") == 1,
            "no_wrong_folders_created": summary.get("created_folders") == 0
            and payload.get("inventory_unchanged") is True,
            "provider_claim_scoped": payload.get("provider_quality_certified") is False
            and payload.get("provider_role") == "bounded_drive_and_nas_filesystem_fixture",
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "provider_quality_certified": False,
            "provider_role": "bounded_drive_and_nas_filesystem_fixture",
        }
    if contract_type == "file_review_formal_child_terminal":
        payload = _extract_last_json(stdout)
        steps = payload.get("steps") or {}
        email = steps.get("check_emails") or {}
        download = steps.get("download") or {}
        payment = steps.get("download_payment_slips") or {}
        receipt_paths = sorted(
            (fixture_root / "state/formal-receipts").glob("*.json")
        )
        receipts = [_fixture_json(fixture_root, path.relative_to(fixture_root).as_posix()) for path in receipt_paths]
        receipts = [row for row in receipts if isinstance(row, dict)]
        receipt_by_step = {
            str(row.get("step") or ""): row for row in receipts
        }
        expected_handlers = {
            "check_emails": "cmd_check_emails",
            "download_payment_slips": "cmd_download_payment_slips",
            "download_queue": "cmd_download_background",
            "download": "cmd_download",
            "scheduled_check": "cmd_scheduled_check",
        }
        downloaded = [
            (fixture_root / str(relative)).resolve(strict=False)
            for relative in (download.get("files") or [])
        ]
        source_files = sorted((fixture_root / "portal").glob("*.pdf"))
        source_hashes = [_sha256_file(path) for path in source_files]
        receipt_ids = [str(row.get("receipt_id") or "") for row in receipts]
        parent_pid = int((receipt_by_step.get("scheduled_check") or {}).get("pid") or 0)
        child_pid = int((receipt_by_step.get("download") or {}).get("pid") or 0)
        checks = {
            "terminal_success": payload.get("success") is True
            and payload.get("status") == "done"
            and not payload.get("deferred"),
            "complete_pipeline_waited": set(steps) == {
                "check_emails", "download_payment_slips", "download"
            }
            and payload.get("failed_steps") == [],
            "willingness_inquiry_ignored": email.get("matched") == 1
            and email.get("ignored") == 1
            and email.get("downloadable_case_numbers") == ["2026-0001"]
            and email.get("willingness_inquiries_excluded") == 1
            and email.get("ignored_kinds") == ["willingness_inquiry"],
            "payment_step_terminal": payment.get("success") is True
            and payment.get("count") == 0
            and payment.get("provider") == "fixture_payment_portal_provider",
            "portal_files_deduped": download.get("success") is True
            and download.get("downloaded_count") == 1
            and download.get("duplicate_count") == 1
            and len(download.get("files") or []) == 1
            and len(download.get("duplicates") or []) == 1
            and len(download.get("content_hashes") or []) == 1
            and len(set(source_hashes)) == 1
            and len(downloaded) == 1
            and downloaded[0].is_file()
            and _is_relative_to(downloaded[0], fixture_root.resolve())
            and _sha256_file(downloaded[0]) == source_hashes[0],
            "formal_handlers_have_dynamic_receipts": len(receipts) == 5
            and set(receipt_by_step) == set(expected_handlers)
            and all(
                receipt_by_step[step].get("handler") == handler
                for step, handler in expected_handlers.items()
            )
            and len(set(receipt_ids)) == len(receipt_ids)
            and all(bool(HEX64.fullmatch(value)) for value in receipt_ids)
            and all(
                bool(row.get("nonce"))
                and bool(row.get("created_at"))
                and bool(HEX64.fullmatch(str(row.get("input_sha256") or "")))
                and bool(HEX64.fullmatch(str(row.get("provider_sha256") or "")))
                for row in receipts
            ),
            "background_child_waited_terminal": download.get("queued") is True
            and download.get("child_terminal") is True
            and download.get("child_status") == "done"
            and bool(download.get("child_finished_at"))
            and bool(HEX64.fullmatch(str(download.get("terminal_state_sha256") or "")))
            and int(download.get("pid") or 0) == child_pid
            and child_pid > 1
            and parent_pid > 1
            and child_pid != parent_pid
            and ((download.get("queue_receipt") or {}).get("handler"))
            == "cmd_download_background",
            "fake_handler_receipts_fail_closed": all(
                "fake" not in str(row.get("handler") or "").lower()
                and "lambda" not in str(row.get("handler") or "").lower()
                for row in receipts
            ),
            "provider_claim_scoped": payload.get("provider_quality_certified") is False
            and payload.get("provider_role") == "bounded_email_and_portal_fixture",
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "receipt_sha256": [_sha256_file(path) for path in receipt_paths],
            "formal_handlers": {
                step: row.get("handler") for step, row in receipt_by_step.items()
            },
            "parent_pid": parent_pid,
            "child_pid": child_pid,
            "terminal_state_sha256": download.get("terminal_state_sha256"),
            "provider_quality_certified": False,
            "provider_role": "bounded_email_and_portal_fixture",
        }
    if contract_type == "pdf_namer_nightly_fixture":
        filename = "20260716 臺灣花蓮地方法院115年度訴字第1號民事判決（測試當事人；原告之訴駁回）.pdf"
        pdf = (
            fixture_root
            / "cases"
            / "2026-0001-測試當事人-民事-一審-損害賠償"
            / "08_判決書"
            / filename
        )
        result_path = fixture_root / "state" / "nightly-result.json"
        report_path = fixture_root / "state" / "_nightly_report.json"
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            stored = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "pdf-namer nightly report is unreadable"}
        manifest = payload.get("sample_manifest") or []
        sample = manifest[0] if len(manifest) == 1 else {}
        comparison = sample.get("comparison") or {}
        metrics = payload.get("metrics") or {}
        checks = {
            "terminal_report_complete": payload.get("total_samples") == 1
            and payload.get("analyzed") == 1
            and payload.get("errors") == 0
            and payload == stored,
            "naming_matches_ground_truth": sample.get("filename") == filename
            and sample.get("predicted_filename") == filename
            and sample.get("predicted_doc_type") == "民事判決",
            "comparison_terminal": comparison.get("date_match") is True
            and comparison.get("party_match") is True
            and comparison.get("format_valid") is True,
            "metrics_terminal": metrics.get("date_accuracy_pct") == 100.0
            and metrics.get("party_accuracy_pct") == 100.0
            and metrics.get("format_valid_pct") == 100.0,
            "real_pdf_parsed": pdf.is_file()
            and sample.get("pdf_sha256") == _sha256_file(pdf)
            and sample.get("parsed_page_count") == 2
            and bool(HEX64.fullmatch(str(sample.get("parsed_text_sha256") or ""))),
            "provider_claim_scoped": payload.get("provider_quality_certified") is False
            and payload.get("provider_role") == "deterministic_pdf_naming_proposal_fixture"
            and sample.get("provider_quality_certified") is False
            and sample.get("provider_role") == "deterministic_pdf_naming_proposal_fixture",
        }
        return all(checks.values()), {
            "checks": checks,
            "result_sha256": _sha256_file(result_path),
            "stored_report_sha256": _sha256_file(report_path),
            "pdf_sha256": _sha256_file(pdf) if pdf.is_file() else None,
            "parsed_text_sha256": sample.get("parsed_text_sha256"),
            "provider_quality_certified": False,
            "provider_role": "deterministic_pdf_naming_proposal_fixture",
        }
    if contract_type == "pdf_namer_layout_fixture":
        filename = "20260716 臺灣花蓮地方法院115年度訴字第1號民事判決（測試當事人；原告之訴駁回）.pdf"
        pdf = (
            fixture_root
            / "cases"
            / "2026-0001-測試當事人-民事-一審-損害賠償"
            / "08_判決書"
            / filename
        )
        sidecar_path = Path(str(pdf) + ".layout.json")
        report_path = fixture_root / "state" / "_nightly_layout_report.json"
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "pdf layout output is unreadable"}
        items = report.get("items") or []
        item = items[0] if len(items) == 1 else {}
        pages = sidecar.get("pages") or []
        parsed_text = "\n".join(
            str(block.get("text") or "")
            for page in pages
            for block in (page.get("blocks") or [])
        )
        checks = {
            "terminal_manifest_complete": report.get("ok") is True
            and report.get("status") == "passed"
            and report.get("total") == 1
            and report.get("generated") == 1
            and report.get("failed") == 0,
            "layout_schema_complete": sidecar.get("schema") == "magi.pdf-layout-sidecar/v1"
            and sidecar.get("source_pdf") == filename
            and sidecar.get("page_count") == 2
            and len(pages) == 2
            and "INDICTMENT DOCUMENT HEADER" in parsed_text,
            "hash_chain_matches": pdf.is_file()
            and item.get("pdf_sha256") == _sha256_file(pdf)
            and sidecar.get("source_pdf_sha256") == _sha256_file(pdf)
            and item.get("sidecar_sha256") == _sha256_file(sidecar_path)
            and item.get("parsed_text_sha256") == sidecar.get("parsed_text_sha256")
            and bool(HEX64.fullmatch(str(sidecar.get("parsed_text_sha256") or ""))),
            "paths_are_fixture_bound": item.get("pdf") == str(pdf.resolve())
            and item.get("sidecar") == str(sidecar_path.resolve()),
            "provider_claim_scoped": report.get("provider_quality_certified") is False
            and report.get("provider_role") == "deterministic_docling_layout_fixture"
            and sidecar.get("provider_quality_certified") is False
            and sidecar.get("provider_role") == "deterministic_docling_layout_fixture",
        }
        return all(checks.values()), {
            "checks": checks,
            "manifest_sha256": _sha256_file(report_path),
            "sidecar_sha256": _sha256_file(sidecar_path),
            "pdf_sha256": _sha256_file(pdf) if pdf.is_file() else None,
            "parsed_text_sha256": sidecar.get("parsed_text_sha256"),
            "provider_quality_certified": False,
            "provider_role": "deterministic_docling_layout_fixture",
        }
    if contract_type == "slow_archive_fixture":
        payload = _extract_last_json(stdout)
        worker = payload.get("worker_report") or {}
        items = worker.get("items") or []
        first = items[0] if items else {}
        source = fixture_root / "nas/01_案件/法扶案件/民事/2025-0002-測試當事人-一審-損害賠償"
        target = fixture_root / "archive/03_工作資料/10_結案/法扶案件/民事/2025-0002-測試當事人-一審-損害賠償"
        checks = {
            "launcher_waited_for_terminal": payload.get("ok") is True
            and payload.get("foreground") is True
            and payload.get("terminal") is True
            and payload.get("returncode") == 0,
            "worker_terminal_success": worker.get("ok") is True
            and worker.get("mode") == "dry_run"
            and len(items) == 1,
            "case_and_hierarchy_bound": first.get("case_number") == "2025-0002"
            and first.get("target") == str(target),
            "real_tree_signature_checked": first.get("dry_run") is True
            and (first.get("source_signature") or {}).get("files") == 1
            and (first.get("source_signature") or {}).get("size") == len(b"fixture-archive-pdf"),
            "source_preserved": source.is_dir() and (source / "卷證.pdf").is_file(),
            "target_not_created": not target.exists(),
            "provider_claim_scoped": worker.get("provider_quality_certified") is False
            and worker.get("provider_role") == "bounded_nas_archive_filesystem_fixture",
        }
        return all(checks.values()), {
            "checks": checks,
            "payload_sha256": _sha256(payload),
            "worker_sha256": _sha256(worker),
            "provider_quality_certified": False,
            "provider_role": "bounded_nas_archive_filesystem_fixture",
        }
    if contract_type == "autopilot_terminal_fixture":
        payload = _fixture_json(fixture_root, "outputs/result.json")
        manifest = _fixture_json(fixture_root, "fixture.json")
        state = _fixture_json(
            fixture_root, "workspace/autopilot_state.json"
        )
        if not all(isinstance(value, dict) for value in (payload, manifest, state)):
            return False, {"error": "autopilot terminal artifacts are unreadable"}
        product = manifest.get("product_input") or {}
        expected_terminal = product.get("expected_terminal_states") or {}
        product_steps = product.get("steps") if isinstance(product.get("steps"), list) else []
        expected_checks = {
            "fixture_sample_bound",
            "typed_workflow_fixture",
            "all_steps_reached_true_terminal_state",
            "terminal_states_match_expected",
            "repair_state_transitions_persisted",
            "audit_history_has_queued_running_terminal",
            "human_blockers_use_isolated_notification",
            "formal_orchestration_entrypoint_used",
            "background_children_observed",
            "all_background_children_waited_terminal",
            "child_returncodes_match_terminal_states",
            "child_tree_attached_to_orchestrator",
            "popen_audit_matches_child_tree",
            "no_child_process_leaks",
        }
        process = payload.get("process_observation") or {}
        children = process.get("children") or []
        safety = payload.get("safety") or {}
        formal = payload.get("formal_orchestration") or {}
        histories = payload.get("audit_history") or {}
        history_rows_valid = isinstance(histories, dict) and all(
            isinstance(events, list)
            and all(isinstance(event, dict) for event in events)
            for events in histories.values()
        )
        history_states = (
            [
                [str(event.get("state") or "") for event in events]
                for events in histories.values()
            ]
            if history_rows_valid
            else []
        )
        child_pids = [
            child.get("pid") for child in children if isinstance(child, dict)
        ]
        parent_pids = {
            child.get("parent_pid") for child in children if isinstance(child, dict)
        }
        checks = {
            "manifest_bound_to_contract": manifest.get("job_id")
            == contract.get("job_id")
            and product.get("task") == contract.get("task")
            and product.get("sample_id") == payload.get("fixture_sample_id")
            and bool(product_steps)
            and isinstance(expected_terminal, dict),
            "terminal_report_passed": payload.get("schema")
            == "magi.schedule-nonstorage-result/v1"
            and payload.get("job_id") == contract.get("job_id")
            and payload.get("task") == contract.get("task")
            and payload.get("success") is True
            and payload.get("status") == "passed"
            and type(payload.get("fixture_sample_id")) is int
            and 1 <= payload["fixture_sample_id"] <= 3,
            "current_checks_not_legacy_constants": set(payload.get("checks") or {})
            == expected_checks
            and all((payload.get("checks") or {}).values())
            and "no_popen_or_dispatch_shortcut" not in (payload.get("checks") or {}),
            "formal_entrypoint_reached_terminal": formal.get(
                "fixture_formal_orchestration"
            )
            is True
            and formal.get("task") == contract.get("task")
            and formal.get("terminal_states") == expected_terminal
            and payload.get("terminal_states") == expected_terminal,
            "durable_state_matches_report": state == payload.get("final_state")
            and state.get("last_task") == contract.get("task")
            and state.get("repairs") == product.get("expected_repairs"),
            "real_child_tree_observed": isinstance(children, list)
            and all(isinstance(child, dict) for child in children)
            and len(children) >= len(product_steps)
            and process.get("started_count") == len(children)
            and process.get("terminal_count") == len(children)
            and process.get("all_waited") is True
            and process.get("returncode_contract_ok") is True
            and process.get("live_pids") == []
            and process.get("forced_terminations") == []
            and len(parent_pids) == 1
            and all(type(parent_pid) is int and parent_pid > 0 for parent_pid in parent_pids)
            and all(type(pid) is int and pid > 0 for pid in child_pids)
            and len(set(child_pids)) == len(child_pids)
            and all(
                child.get("process_terminal") is True
                and child.get("daemon_waiter_terminal") is True
                and child.get("returncode") == child.get("expected_returncode")
                for child in children
                if isinstance(child, dict)
            ),
            "audit_hook_matches_child_tree": safety.get("subprocess_spawned") is True
            and safety.get("subprocess_spawn_count") == len(children)
            and safety.get("observation_source") == "python_audit_hook"
            and safety.get("dispatcher_invoked") is False,
            "safety_and_providers_bound": safety.get("external_network_accessed") is False
            and safety.get("production_database_accessed") is False
            and safety.get("production_state_written") is False
            and safety.get("nas_accessed") is False
            and safety.get("writes_bounded_to_fixture") is True
            and safety.get("database_provider") == "fixture_database"
            and safety.get("model_provider") == "fixture_model"
            and safety.get("notification_provider") == "fixture_notification",
            "history_proves_wait_and_repair": history_rows_valid
            and len(history_states)
            == len(product_steps)
            and all(
                states[:2] == ["queued", "running"]
                and states[-1] in {"completed", "recovered", "needs_human"}
                for states in history_states
            )
            and (
                product.get("sample_id") != 2
                or ["queued", "running", "failed", "repairing", "recovered"]
                in history_states
            ),
        }
        return all(checks.values()), {
            "checks": checks,
            "result_sha256": _sha256_file(
                fixture_root / "outputs/result.json"
            ),
            "state_sha256": _sha256_file(
                fixture_root / "workspace/autopilot_state.json"
            ),
            "child_count": len(children),
            "child_returncodes": process.get("returncodes"),
            "provider_quality_certified": False,
            "provider_role": "isolated_autopilot_child_lifecycle",
        }
    if contract_type == "operational_hardening_formal_fixture":
        payload = _fixture_json(fixture_root, "outputs/result.json")
        manifest = _fixture_json(fixture_root, "fixture.json")
        state = _fixture_json(
            fixture_root, "workspace/operational_audit_state.json"
        )
        if not all(isinstance(value, dict) for value in (payload, manifest, state)):
            return False, {"error": "hardening formal artifacts are unreadable"}
        product = manifest.get("product_input") or {}
        expected = product.get("expected") or {}
        audit = payload.get("audit") or {}
        initial = audit.get("initial") or {}
        final = audit.get("final") or {}
        initial_report = initial.get("report") or {}
        final_report = final.get("report") or {}
        provider = payload.get("provider_observation") or {}
        provider_calls = provider.get("calls") or []
        safety = payload.get("safety") or {}
        formal_audits = {
            "cron",
            "runtime_root_consistency",
            "stale_runtime_locks",
            "domain_interference",
            "background_task_locks",
            "git",
            "issue_agenda",
            "gmail_monitor",
            "laf_gmail_fallback_job",
            "omlx_profile",
            "silent_exception_handlers",
            "retired_feature_residue",
            "osc_route_integrity",
        }
        expected_checks = {
            "fixture_sample_bound",
            "typed_audit_fixture",
            "cron_commands_parsed_by_product_policy",
            "cron_collisions_match_expected",
            "initial_red_state_audited",
            "stale_lock_repair_matches_expected",
            "audit_reached_expected_terminal_state",
            "fail_on_red_semantics_preserved",
            "formal_audit_suite_executed",
            "external_dependencies_isolated",
            "external_provider_calls_terminal",
        }
        checks = {
            "manifest_bound_to_contract": manifest.get("job_id")
            == "job_operational_hardening_audit"
            and product.get("sample_id") == payload.get("fixture_sample_id")
            and isinstance(expected, dict),
            "terminal_report_passed": payload.get("schema")
            == "magi.schedule-nonstorage-result/v1"
            and payload.get("job_id") == "job_operational_hardening_audit"
            and payload.get("success") is True
            and payload.get("status") == "passed"
            and payload.get("terminal_state") == "green"
            and type(payload.get("fixture_sample_id")) is int
            and 1 <= payload["fixture_sample_id"] <= 3,
            "current_checks_not_legacy_constants": set(payload.get("checks") or {})
            == expected_checks
            and all((payload.get("checks") or {}).values())
            and "audit_findings_remain_read_only" not in (payload.get("checks") or {})
            and "no_popen_or_dispatch_shortcut" not in (payload.get("checks") or {}),
            "formal_suite_present_twice": set(initial_report) == formal_audits
            and set(final_report) == formal_audits,
            "formal_results_match_expected": (
                initial_report.get("cron") or {}
            ).get("parse_failure_count") == expected.get("parse_failure_count")
            and (initial_report.get("cron") or {}).get("collision_count")
            == expected.get("collision_count")
            and initial.get("red_count") == expected.get("initial_red_count")
            and final.get("red_count") == 0
            and (audit.get("repairs") or {}).get("locks")
            == expected.get("repaired_locks"),
            "durable_state_matches_terminal": state.get("initial_red_count")
            == initial.get("red_count")
            and state.get("final_red_count") == final.get("red_count")
            and state.get("repaired_locks") == (audit.get("repairs") or {}).get("locks")
            and state.get("terminal_state") == payload.get("terminal_state"),
            "external_provider_terminal_transcript": provider.get("all_terminal") is True
            and provider.get("forbidden_calls") == []
            and provider.get("call_count") == len(provider_calls)
            and all(
                isinstance(call, dict) and call.get("terminal") is True
                for call in provider_calls
            )
            and {
                "cron",
                "git",
                "process_table",
                "omlx",
                "omlx_state",
                "osc_routes",
            }
            <= {str(call.get("provider")) for call in provider_calls},
            "dynamic_safety_receipt": safety.get("observation_source")
            == "python_audit_hook"
            and safety.get("subprocess_spawned") is False
            and safety.get("subprocess_spawn_count") == 0
            and safety.get("external_network_accessed") is False
            and safety.get("production_database_accessed") is False
            and safety.get("production_state_written") is False
            and safety.get("nas_accessed") is False
            and safety.get("writes_bounded_to_fixture") is True
            and safety.get("dispatcher_invoked") is False
            and safety.get("database_provider") == "fixture_database"
            and safety.get("model_provider") == "fixture_model_probe"
            and safety.get("notification_provider") == "fixture_notification",
        }
        return all(checks.values()), {
            "checks": checks,
            "result_sha256": _sha256_file(
                fixture_root / "outputs/result.json"
            ),
            "state_sha256": _sha256_file(
                fixture_root / "workspace/operational_audit_state.json"
            ),
            "provider_call_count": len(provider_calls),
            "formal_audit_count": len(formal_audits),
            "provider_quality_certified": False,
            "provider_role": "isolated_operational_audit_boundaries",
        }
    if contract_type == "business_module_true_probes":
        payload = _fixture_json(fixture_root, "outputs/result.json")
        manifest = _fixture_json(fixture_root, "fixture.json")
        business_transcript = _fixture_json(
            fixture_root, "business_probe_provider_transcript.json"
        )
        portal_transcript = _fixture_json(
            fixture_root, "portal_provider_transcript.json"
        )
        if not all(
            isinstance(value, dict) for value in (payload, manifest)
        ) or not all(
            isinstance(value, list)
            for value in (business_transcript, portal_transcript)
        ):
            return False, {"error": "business orchestration artifacts are unreadable"}
        product_input = manifest.get("product_input") or {}
        results = payload.get("results") or []
        by_name = {
            str(row.get("name")): row
            for row in results
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
        required = {
            "laf_portal_live",
            "nas_mounts_live",
            "drive_sync_status_live",
            "calendar_todo_status_live",
            "file_review_scheduled_probe",
            "transcript_self_test_probe",
        }
        expected_checks = {
            "fixture_sample_bound",
            "formal_probe_results_typed",
            "all_required_probes_executed",
            "all_probes_reached_terminal_success",
            "summary_contains_each_probe",
            "external_providers_fixture_bound",
            "provider_terminal_close",
            "sensitive_fields_redacted",
        }
        safety = payload.get("safety") or {}
        child_names = {
            "file_review_scheduled_probe",
            "transcript_self_test_probe",
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        checks = {
            "terminal_report_passed": payload.get("schema")
            == "magi.schedule-product-result/v1"
            and payload.get("job_id") == "job_business_module_live_check"
            and payload.get("success") is True
            and payload.get("status") == "passed"
            and type(payload.get("fixture_sample_id")) is int
            and 1 <= payload["fixture_sample_id"] <= 3,
            "true_probe_set_executed": len(results) == 6
            and payload.get("result_count") == 6
            and set(by_name) == required
            and all(row.get("ok") is True for row in by_name.values()),
            "current_checks_not_legacy_constants": set(payload.get("checks") or {})
            == expected_checks
            and all((payload.get("checks") or {}).values())
            and "typed_product_results" not in (payload.get("checks") or {})
            and "results" not in product_input,
            "dynamic_drive_calendar_values": (
                (by_name.get("drive_sync_status_live", {}).get("parsed") or {}).get(
                    "matched_case_folders"
                )
                == product_input.get("expected_drive_matches")
                and (
                    by_name.get("calendar_todo_status_live", {}).get("parsed") or {}
                ).get("imported")
                == product_input.get("expected_calendar_imported")
            ),
            "child_processes_reached_json_terminal": all(
                (by_name.get(name) or {}).get("returncode") == 0
                and (by_name.get(name) or {}).get("contract_error") == ""
                and isinstance((by_name.get(name) or {}).get("parsed"), dict)
                and (by_name.get(name) or {})["parsed"].get(
                    "success", (by_name.get(name) or {})["parsed"].get("ok")
                )
                is True
                for name in child_names
            ),
            "laf_and_nas_provider_outputs_reached_terminal": (
                (by_name.get("laf_portal_live", {}).get("parsed") or {}).get(
                    "error"
                )
                is None
                and bool(
                    (by_name.get("nas_mounts_live", {}).get("parsed") or {}).get(
                        "shares"
                    )
                )
            ),
            "providers_closed_after_expected_actions": [
                row.get("action") for row in business_transcript
            ]
            == ["nas_share_statuses", "close"]
            and [row.get("action") for row in portal_transcript]
            == ["login", "query_pending_drafts_all", "close"],
            "summary_and_redaction_are_dynamic": all(
                name in str(payload.get("summary") or "") for name in required
            )
            and all(
                secret not in serialized
                for secret in (
                    "2026-9999",
                    "fixture.person@example.com",
                    "FIXTURE_PRIVATE_PATH_MARKER",
                )
            ),
            "fixture_safety_terminal": safety.get("external_network_accessed")
            is False
            and safety.get("production_database_accessed") is False
            and safety.get("production_state_written") is False
            and safety.get("nas_accessed") is False
            and safety.get("writes_bounded_to_fixture") is True,
        }
        return all(checks.values()), {
            "checks": checks,
            "report_sha256": _sha256_file(fixture_root / "outputs/result.json"),
            "business_provider_transcript_sha256": _sha256_file(
                fixture_root / "business_probe_provider_transcript.json"
            ),
            "portal_provider_transcript_sha256": _sha256_file(
                fixture_root / "portal_provider_transcript.json"
            ),
            "observed_probe_names": sorted(by_name),
            "observed_dynamic_values": {
                "matched_case_folders": (
                    by_name.get("drive_sync_status_live", {}).get("parsed") or {}
                ).get("matched_case_folders"),
                "calendar_imported": (
                    by_name.get("calendar_todo_status_live", {}).get("parsed") or {}
                ).get("imported"),
            },
        }
    if contract_type == "heavy_translation_provider_terminal":
        payload = _fixture_json(fixture_root, "outputs/result.json")
        transcript = _fixture_json(fixture_root, "heavy_provider_transcript.json")
        if not isinstance(payload, dict) or not isinstance(transcript, list):
            return False, {"error": "heavy translation artifacts are unreadable"}
        product = payload.get("product") or {}
        product_checks = product.get("checks") or []
        by_name = {
            str(row.get("name")): row
            for row in product_checks
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
        route = (by_name.get("heavy_nvidia_route") or {}).get("result") or {}
        docx = Path(str(product.get("docx_path") or "")).resolve(strict=False)
        expected_checks = {
            "fixture_sample_bound",
            "product_gate_passed",
            "required_quality_checks_executed",
            "docx_artifact_bounded",
            "isolated_provider_route_executed",
            "provider_quality_not_certified",
            "provider_terminal_close",
        }
        safety = payload.get("safety") or {}
        checks = {
            "terminal_report_passed": payload.get("schema")
            == "magi.schedule-product-result/v1"
            and payload.get("job_id") == "job_heavy_translation_quality_live"
            and payload.get("success") is True
            and payload.get("status") == "passed"
            and product.get("success") is True,
            "current_checks_not_legacy_constants": set(payload.get("checks") or {})
            == expected_checks
            and all((payload.get("checks") or {}).values())
            and "live_nim_not_contacted" not in (payload.get("checks") or {}),
            "real_provider_route_reached_terminal": (
                by_name.get("heavy_nvidia_route", {}).get("ok") is True
                and route.get("success") is True
                and route.get("route") == "nvidia_nim"
                and route.get("provider") == "bounded_local_nim_provider"
                and route.get("provider_quality_certified") is False
                and int(route.get("text_len") or 0) > 0
                and by_name.get("heavy_title_provider_route", {}).get("ok") is True
            ),
            "provider_chat_and_close_transcript": [
                row.get("action") for row in transcript
            ]
            == ["chat", "chat", "close"]
            and [row.get("stage") for row in transcript[:2]]
            == ["route_probe", "title_translation"]
            and all(row.get("fallback_allowed") is False for row in transcript[:2])
            and all(bool(HEX64.fullmatch(str(row.get("prompt_sha256") or ""))) for row in transcript[:2])
            and all(bool(HEX64.fullmatch(str(row.get("response_sha256") or ""))) for row in transcript[:2]),
            "docx_is_real_bounded_artifact": docx.is_file()
            and _is_relative_to(docx, fixture_root.resolve())
            and _sha256_file(docx) != hashlib.sha256(b"").hexdigest()
            and by_name.get("docx_export", {}).get("ok") is True
            and by_name.get("docx_source_terms_inline", {}).get("ok") is True,
            "provider_claim_remains_scoped": payload.get(
                "provider_quality_certified"
            )
            is False,
            "fixture_safety_terminal": safety.get("external_network_accessed")
            is False
            and safety.get("production_database_accessed") is False
            and safety.get("production_state_written") is False
            and safety.get("nas_accessed") is False
            and safety.get("writes_bounded_to_fixture") is True,
        }
        return all(checks.values()), {
            "checks": checks,
            "report_sha256": _sha256_file(fixture_root / "outputs/result.json"),
            "provider_transcript_sha256": _sha256_file(
                fixture_root / "heavy_provider_transcript.json"
            ),
            "docx_sha256": _sha256_file(docx) if docx.is_file() else None,
            "route": route.get("route"),
            "provider": route.get("provider"),
            "provider_quality_certified": False,
        }
    if contract_type == "distill_training_terminal":
        payload = _fixture_json(fixture_root, "outputs/result.json")
        terminal = _fixture_json(
            fixture_root,
            "workspace/gemma-distill/bounded_training_terminal.json",
        )
        manifest = _fixture_json(fixture_root, "fixture.json")
        profile = _fixture_json(fixture_root, "inputs/training-profile.json")
        if not all(
            isinstance(value, dict)
            for value in (payload, terminal, manifest, profile)
        ):
            return False, {"error": "distill training artifacts are unreadable"}
        training = payload.get("training") or {}
        expected_steps = profile.get("optimizer_steps")
        sample_id = payload.get("fixture_sample_id")
        checkpoint_dir = (
            fixture_root
            / "workspace/gemma-distill/adapters"
            / f"adapter_bounded-sample-{int(sample_id or 0):03d}"
            / "checkpoints"
        )
        checkpoints = sorted(checkpoint_dir.glob("step-*.json"))
        checkpoint_hashes = [_sha256_file(path) for path in checkpoints]
        history = training.get("history") or []
        expected_checks = {
            "fixture_sample_bound",
            "usable_pair_filter_matches_fixture",
            "training_split_accounts_for_usable_pairs",
            "rejected_reasoning_pairs_excluded",
            "bounded_training_child_completed",
            "optimizer_loop_executed",
            "checkpoints_persisted",
            "eval_reached_true_terminal",
            "deploy_forbidden",
        }
        terminal_fields = {
            key: value for key, value in training.items() if key != "terminal_path"
        }
        safety = payload.get("safety") or {}
        checks = {
            "terminal_report_passed": payload.get("schema")
            == "magi.schedule-product-result/v1"
            and payload.get("job_id") == "job_distill_train_gemma"
            and payload.get("success") is True
            and payload.get("status") == "passed"
            and payload.get("training_child_returncode") == 0
            and training.get("status") == "passed",
            "current_checks_not_legacy_constants": set(payload.get("checks") or {})
            == expected_checks
            and all((payload.get("checks") or {}).values())
            and "no_training_or_deploy_invoked" not in (payload.get("checks") or {}),
            "optimizer_history_is_dynamic": type(expected_steps) is int
            and expected_steps >= 2
            and training.get("optimizer_steps") == expected_steps
            and len(history) == expected_steps
            and [row.get("step") for row in history] == list(
                range(1, expected_steps + 1)
            )
            and float(training.get("final_train_loss", 0))
            < float(training.get("initial_train_loss", 0)),
            "atomic_checkpoint_chain_matches_terminal": len(checkpoints)
            == expected_steps
            and training.get("checkpoint_count") == expected_steps
            and training.get("checkpoint_sha256") == checkpoint_hashes
            and all(
                path.name == f"step-{index:03d}.json"
                for index, path in enumerate(checkpoints, 1)
            ),
            "persisted_eval_terminal_matches_child": terminal_fields == terminal
            and terminal.get("schema")
            == "magi.gemma-bounded-training-terminal/v1"
            and terminal.get("validation_pass") is True
            and float(terminal.get("eval_loss", -1)) >= 0,
            "deployment_is_explicitly_forbidden": profile.get("deploy")
            == "forbidden"
            and training.get("deploy_allowed") is False
            and training.get("deployed") is False
            and training.get("active_model_written") is False
            and not (fixture_root / "workspace/gemma-distill/pending_deploy.json").exists()
            and not (fixture_root / "workspace/gemma-distill/active_model.json").exists()
            and not (fixture_root / "workspace/gemma-distill/merged").exists(),
            "fixture_safety_terminal": safety.get("external_network_accessed")
            is False
            and safety.get("production_database_accessed") is False
            and safety.get("production_state_written") is False
            and safety.get("nas_accessed") is False
            and safety.get("writes_bounded_to_fixture") is True,
        }
        return all(checks.values()), {
            "checks": checks,
            "report_sha256": _sha256_file(fixture_root / "outputs/result.json"),
            "training_terminal_sha256": _sha256_file(
                fixture_root
                / "workspace/gemma-distill/bounded_training_terminal.json"
            ),
            "checkpoint_sha256": checkpoint_hashes,
            "optimizer_steps": expected_steps,
            "eval_loss": terminal.get("eval_loss"),
            "deploy_allowed": False,
        }
    if contract_type == "insight_sync_embedding_database_terminal":
        payload = _fixture_json(fixture_root, "outputs/result.json")
        manifest = _fixture_json(fixture_root, "fixture.json")
        receipts = _fixture_jsonl(
            fixture_root, "workspace/embedding-receipts.jsonl"
        )
        database_path = fixture_root / "workspace/insight-sync.sqlite3"
        if (
            not isinstance(payload, dict)
            or not isinstance(manifest, dict)
            or not isinstance(receipts, list)
            or database_path.is_symlink()
            or not database_path.is_file()
        ):
            return False, {"error": "insight sync terminal artifacts are unreadable"}
        try:
            connection = sqlite3.connect(
                f"file:{database_path.resolve()}?mode=ro", uri=True
            )
            terminal_rows = connection.execute(
                "SELECT d.content, d.source, v.embedding FROM documents d "
                "JOIN vectors v ON v.doc_id = d.id ORDER BY d.id"
            ).fetchall()
            source_count = int(
                connection.execute("SELECT COUNT(*) FROM legal_insights").fetchone()[0]
            )
            connection.close()
            vectors = [json.loads(str(row[2])) for row in terminal_rows]
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            return False, {"error": "insight sync disposable database is unreadable"}
        product_input = manifest.get("product_input") or {}
        expected_ids = product_input.get("expected_new_ids") or []
        inserted_ids = [
            int(str(row[1]).split("|id=", 1)[1])
            for row in terminal_rows
            if "|id=" in str(row[1])
        ]
        database_receipt = payload.get("database_receipt") or {}
        safety = payload.get("safety") or {}
        report_receipts = payload.get("embedding_receipts") or []
        checks = {
            "terminal_report_passed": payload.get("schema")
            == "magi.schedule-product-result/v1"
            and payload.get("job_id") == "job_insight_sync"
            and payload.get("success") is True
            and payload.get("status") == "passed"
            and type(payload.get("fixture_sample_id")) is int
            and 1 <= payload["fixture_sample_id"] <= 3,
            "formal_embedding_checks_executed": all(
                (payload.get("checks") or {}).get(name) is True
                for name in (
                    "formal_embedding_provider_invoked",
                    "disposable_database_written",
                    "vectors_reached_terminal_state",
                    "embedding_receipts_dynamic",
                )
            ),
            "source_and_dedup_plan_match_fixture": source_count
            == len(product_input.get("insights") or [])
            and payload.get("planned_ids") == expected_ids
            and payload.get("inserted_ids") == expected_ids
            and inserted_ids == expected_ids
            and payload.get("planned_insert_count") == len(expected_ids),
            "embedding_receipts_are_dynamic_and_formal": receipts == report_receipts
            and len(receipts) == len(expected_ids) > 0
            and len({row.get("receipt_id") for row in receipts}) == len(receipts)
            and all(
                row.get("handler") == "_get_embedding"
                and row.get("provider") == "bounded_embedding_provider"
                and type(row.get("created_ns")) is int
                and int(row.get("pid") or 0) > 1
                and row.get("dimensions") == 16
                and bool(HEX64.fullmatch(str(row.get("input_sha256") or "")))
                and bool(HEX64.fullmatch(str(row.get("embedding_sha256") or "")))
                for row in receipts
            )
            and sorted(row.get("input_sha256") for row in receipts)
            == sorted(payload.get("content_sha256") or []),
            "sqlite_vectors_reached_terminal_state": len(terminal_rows)
            == len(expected_ids)
            and len(vectors) == len(expected_ids)
            and all(
                isinstance(vector, list)
                and len(vector) == 16
                and any(abs(float(value)) > 1e-12 for value in vector)
                for vector in vectors
            )
            and sorted(
                hashlib.sha256(str(row[2]).encode()).hexdigest()
                for row in terminal_rows
            )
            == sorted(str(row.get("embedding_sha256") or "") for row in receipts),
            "database_receipt_matches_artifact": database_receipt.get("schema")
            == "magi.insight-vector-database-receipt/v1"
            and database_receipt.get("inserted") == len(expected_ids)
            and database_receipt.get("vectors") == len(expected_ids)
            and bool(re.fullmatch(r"[0-9a-f]{32}", str(database_receipt.get("transaction_id") or "")))
            and database_receipt.get("database_sha256")
            == _sha256_file(database_path),
            "fake_embedding_handler_fails_closed": all(
                row.get("handler") == "_get_embedding" for row in receipts
            ),
            "fixture_safety_terminal": safety.get("external_network_accessed")
            is False
            and safety.get("production_database_accessed") is False
            and safety.get("production_state_written") is False
            and safety.get("nas_accessed") is False
            and safety.get("writes_bounded_to_fixture") is True
            and safety.get("database_provider") == "disposable_sqlite"
            and safety.get("embedding_provider") == "bounded_embedding_provider",
        }
        return all(checks.values()), {
            "checks": checks,
            "report_sha256": _sha256_file(fixture_root / "outputs/result.json"),
            "receipt_log_sha256": _sha256_file(
                fixture_root / "workspace/embedding-receipts.jsonl"
            ),
            "database_sha256": _sha256_file(database_path),
            "inserted_ids": inserted_ids,
            "vector_count": len(vectors),
            "formal_handlers": sorted({row.get("handler") for row in receipts}),
        }
    if contract_type == "reprocess_insights_api_model_database_terminal":
        payload = _fixture_json(fixture_root, "outputs/result.json")
        manifest = _fixture_json(fixture_root, "fixture.json")
        receipts = _fixture_jsonl(
            fixture_root, "workspace/reprocess-provider-receipts.jsonl"
        )
        database_path = fixture_root / "workspace/reprocess-insights.sqlite3"
        if (
            not isinstance(payload, dict)
            or not isinstance(manifest, dict)
            or not isinstance(receipts, list)
            or database_path.is_symlink()
            or not database_path.is_file()
        ):
            return False, {"error": "reprocess terminal artifacts are unreadable"}
        product_input = manifest.get("product_input") or {}
        expected_ids = product_input.get("expected_selected_ids") or []
        try:
            connection = sqlite3.connect(
                f"file:{database_path.resolve()}?mode=ro", uri=True
            )
            placeholders = ",".join("?" for _value in expected_ids)
            terminal_rows = connection.execute(
                f"SELECT id, raw_text, insight_text, is_degraded FROM legal_insights "
                f"WHERE id IN ({placeholders}) ORDER BY id",
                expected_ids,
            ).fetchall()
            source_count = int(
                connection.execute("SELECT COUNT(*) FROM legal_insights").fetchone()[0]
            )
            connection.close()
        except sqlite3.Error:
            return False, {"error": "reprocess disposable database is unreadable"}
        api_receipts = [row for row in receipts if row.get("kind") == "api"]
        model_receipts = [row for row in receipts if row.get("kind") == "model"]
        database_receipt = payload.get("database_receipt") or {}
        safety = payload.get("safety") or {}
        report_receipts = payload.get("provider_receipts") or []
        terminal_ids = [int(row[0]) for row in terminal_rows]
        checks = {
            "terminal_report_passed": payload.get("schema")
            == "magi.schedule-product-result/v1"
            and payload.get("job_id") == "job_reprocess_insights"
            and payload.get("success") is True
            and payload.get("status") == "passed"
            and type(payload.get("fixture_sample_id")) is int
            and 1 <= payload["fixture_sample_id"] <= 3,
            "formal_api_model_database_checks_executed": all(
                (payload.get("checks") or {}).get(name) is True
                for name in (
                    "formal_api_provider_invoked",
                    "formal_model_provider_invoked",
                    "disposable_database_updated",
                    "database_terminal_state_verified",
                    "provider_receipts_dynamic",
                )
            ),
            "selection_and_updates_match_fixture": source_count
            == len(product_input.get("rows") or [])
            and payload.get("selected_ids") == expected_ids
            and payload.get("updated_ids") == expected_ids
            and terminal_ids == expected_ids,
            "formal_api_and_model_receipts_dynamic": receipts == report_receipts
            and len(api_receipts) >= 1
            and len(model_receipts) == len(expected_ids) > 0
            and len({row.get("receipt_id") for row in receipts}) == len(receipts)
            and all(
                type(row.get("created_ns")) is int
                and int(row.get("pid") or 0) > 1
                and bool(HEX64.fullmatch(str(row.get("request_sha256") or "")))
                and bool(HEX64.fullmatch(str(row.get("response_sha256") or "")))
                and bool(HEX64.fullmatch(str(row.get("provider_sha256") or "")))
                for row in receipts
            )
            and all(
                row.get("handler") == "_fetch_fulltext_for_ref"
                for row in api_receipts
            )
            and all(
                row.get("handler") == "_summarize_with_nim"
                for row in model_receipts
            ),
            "sqlite_updates_reached_terminal_state": len(terminal_rows)
            == len(expected_ids)
            and all(
                isinstance(row[1], str)
                and len(row[1]) > 100
                and isinstance(row[2], str)
                and row[2].startswith("## 實務見解")
                and int(row[3] or 0) == 0
                for row in terminal_rows
            )
            and sorted(payload.get("summary_sha256") or [])
            == sorted(
                hashlib.sha256(str(row[2]).encode("utf-8")).hexdigest()
                for row in terminal_rows
            )
            and all(
                str(row.get("response_sha256") or "")
                in {
                    hashlib.sha256(str(db_row[1]).encode("utf-8")).hexdigest()
                    for db_row in terminal_rows
                }
                for row in api_receipts
            ),
            "database_receipt_matches_artifact": database_receipt.get("schema")
            == "magi.reprocess-database-receipt/v1"
            and database_receipt.get("updated") == len(expected_ids)
            and database_receipt.get("terminal_rows") == len(expected_ids)
            and bool(re.fullmatch(r"[0-9a-f]{32}", str(database_receipt.get("transaction_id") or "")))
            and database_receipt.get("database_sha256")
            == _sha256_file(database_path),
            "fake_api_or_model_handler_fails_closed": all(
                row.get("handler")
                == (
                    "_fetch_fulltext_for_ref"
                    if row.get("kind") == "api"
                    else "_summarize_with_nim"
                )
                for row in receipts
            ),
            "fixture_safety_terminal": safety.get("external_network_accessed")
            is False
            and safety.get("production_database_accessed") is False
            and safety.get("production_state_written") is False
            and safety.get("nas_accessed") is False
            and safety.get("writes_bounded_to_fixture") is True
            and safety.get("api_provider") == "bounded_judicial_api_provider"
            and safety.get("model_provider") == "bounded_nvidia_nim_provider"
            and safety.get("database_provider") == "disposable_sqlite",
        }
        return all(checks.values()), {
            "checks": checks,
            "report_sha256": _sha256_file(fixture_root / "outputs/result.json"),
            "receipt_log_sha256": _sha256_file(
                fixture_root / "workspace/reprocess-provider-receipts.jsonl"
            ),
            "database_sha256": _sha256_file(database_path),
            "updated_ids": terminal_ids,
            "api_receipt_count": len(api_receipts),
            "model_receipt_count": len(model_receipts),
            "formal_handlers": sorted({row.get("handler") for row in receipts}),
        }
    if contract_type == "watchlist_backup_copy":
        source = fixture_root / "agent" / "market_watchlist.json"
        backups = sorted((fixture_root / "runtime" / "backups" / "market_watchlist").glob("*.json"))
        ok = len(backups) == 1 and backups[0].read_bytes() == source.read_bytes()
        return ok, {"backup_count": len(backups), "source_preserved": source.exists()}
    if contract_type == "commercial_readiness_schedule_fixture":
        path = (fixture_root / Path(str(contract.get("path") or ""))).resolve()
        if not _is_relative_to(path, fixture_root.resolve()) or not path.is_file():
            return False, {"error": "commercial readiness fixture report is missing or escaped"}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "commercial readiness fixture report is unreadable"}
        checks = payload.get("checks") if isinstance(payload, dict) else None
        omitted = payload.get("omitted_host_checks") if isinstance(payload, dict) else None
        expected_omitted = {
            "formal_saas_readiness",
            "live_conflict_audit",
            "process_hygiene",
            "resource_governor",
            "model_live_gate",
            "stability_observer_once",
        }
        observed_checks = checks if isinstance(checks, list) else []
        ok = (
            payload.get("schema") == "magi.v3.commercial-readiness-schedule-fixture/v1"
            and payload.get("schedule_fixture") is True
            and payload.get("ok") is True
            and payload.get("summary") == {"pass": 3, "fail": 0, "total": 3}
            and len(observed_checks) == 3
            and all(isinstance(row, dict) and row.get("ok") is True for row in observed_checks)
            and set(omitted or []) == expected_omitted
        )
        return ok, {
            "checks": {
                "schema": payload.get("schema") == "magi.v3.commercial-readiness-schedule-fixture/v1",
                "fixture_mode": payload.get("schedule_fixture") is True,
                "three_deterministic_checks_passed": payload.get("summary") == {"pass": 3, "fail": 0, "total": 3},
                "host_checks_explicitly_omitted": set(omitted or []) == expected_omitted,
            },
            "payload_sha256": _sha256(payload),
        }
    system_diagnostic_terminal = contract_type == "system_diagnostic_terminal"
    if contract_type == "stdout_json" or system_diagnostic_terminal:
        payload = _extract_json(stdout)
    elif contract_type in {"json_file", "jsonl_last", "json_glob"}:
        if contract_type == "json_glob":
            pattern = str(contract.get("path") or "")
            matches = sorted(fixture_root.glob(pattern))
            if len(matches) != 1:
                return False, {"error": "contract glob did not match exactly one file"}
            path = matches[0].resolve()
        else:
            relative = Path(str(contract.get("path") or ""))
            path = (fixture_root / relative).resolve()
        if not _is_relative_to(path, fixture_root.resolve()) or not path.is_file():
            return False, {"error": "contract file missing or escaped"}
        try:
            raw = path.read_text(encoding="utf-8")
            if contract_type == "jsonl_last":
                lines = [line for line in raw.splitlines() if line.strip()]
                payload = json.loads(lines[-1]) if lines else {}
            else:
                payload = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"error": "contract file unreadable"}
    elif contract_type == "stdout_contains":
        stream = stderr if contract.get("stream") == "stderr" else stdout
        values = [str(value) for value in contract.get("values", [])]
        preserve_glob = str(contract.get("preserve_glob") or "")
        preserved = list(fixture_root.glob(preserve_glob)) if preserve_glob else []
        ok = bool(values) and all(value in stream for value in values)
        if preserve_glob:
            ok = ok and bool(preserved)
        jsonl_payload: dict[str, Any] = {}
        jsonl_relative = str(contract.get("jsonl_last_path") or "")
        if jsonl_relative:
            jsonl_path = (fixture_root / jsonl_relative).resolve()
            if not _is_relative_to(jsonl_path, fixture_root.resolve()) or not jsonl_path.is_file():
                ok = False
            else:
                try:
                    lines = [line for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                    jsonl_payload = json.loads(lines[-1]) if lines else {}
                except (OSError, UnicodeError, json.JSONDecodeError):
                    jsonl_payload = {}
                ok = ok and bool(jsonl_payload) and all(
                    _dotted(jsonl_payload, key) == value
                    for key, value in (contract.get("jsonl_last_equals") or {}).items()
                )
        return ok, {
            "matched_values": values,
            "preserve_glob": preserve_glob or None,
            "preserved_count": len(preserved),
            "jsonl_last_payload_sha256": _sha256(jsonl_payload) if jsonl_relative else None,
        }
    else:
        return False, {"error": "unknown success contract"}
    equals = contract.get("equals") or {}
    minimum = contract.get("minimum") or {}
    lengths = contract.get("length") or {}
    ok = bool(payload) and all(_dotted(payload, key) == value for key, value in equals.items())
    ok = ok and all(
        isinstance(_dotted(payload, key), (int, float))
        and not isinstance(_dotted(payload, key), bool)
        and float(_dotted(payload, key)) >= float(value)
        for key, value in minimum.items()
    )
    ok = ok and all(
        isinstance(_dotted(payload, key), (list, dict, str))
        and len(_dotted(payload, key)) == int(value)
        for key, value in lengths.items()
    )
    diagnostic_status: str | None = None
    diagnostic_warnings: list[str] | None = None
    diagnostic_status_accepted: bool | None = None
    diagnostic_warnings_consistent: bool | None = None
    diagnostic_warning_allowlist_bound: bool | None = None
    diagnostic_warnings_allowlisted: bool | None = None
    if system_diagnostic_terminal:
        raw_status = payload.get("status")
        raw_warnings = payload.get("warnings")
        diagnostic_status = raw_status if isinstance(raw_status, str) else None
        diagnostic_warnings = (
            list(raw_warnings)
            if isinstance(raw_warnings, list)
            and all(isinstance(value, str) and value for value in raw_warnings)
            else None
        )
        diagnostic_status_accepted = diagnostic_status in {"healthy", "warning"}
        declared_warning_allowlist = contract.get("warning_allowlist")
        diagnostic_warning_allowlist_bound = (
            declared_warning_allowlist
            == sorted(SYSTEM_DIAGNOSTIC_RESOURCE_WARNING_CODES)
        )
        diagnostic_warnings_allowlisted = (
            diagnostic_warnings is not None
            and len(set(diagnostic_warnings)) == len(diagnostic_warnings)
            and not (
                set(diagnostic_warnings)
                - SYSTEM_DIAGNOSTIC_RESOURCE_WARNING_CODES
            )
        )
        diagnostic_warnings_consistent = diagnostic_warnings is not None and (
            (diagnostic_status == "healthy" and not diagnostic_warnings)
            or (diagnostic_status == "warning" and bool(diagnostic_warnings))
        )
        ok = (
            ok
            and diagnostic_status_accepted
            and diagnostic_warning_allowlist_bound
            and diagnostic_warnings_allowlisted
            and diagnostic_warnings_consistent
        )
    preserve_glob = str(contract.get("preserve_glob") or "")
    if preserve_glob:
        preserved = list(fixture_root.glob(preserve_glob))
        ok = ok and bool(preserved)
    absent_glob = str(contract.get("absent_glob") or "")
    absent_matches = list(fixture_root.glob(absent_glob)) if absent_glob else []
    if absent_glob:
        ok = ok and not absent_matches
    evidence = {
        "payload_sha256": _sha256(payload),
        "preserve_glob": preserve_glob or None,
        "absent_glob": absent_glob or None,
        "absent_glob_match_count": len(absent_matches),
    }
    if system_diagnostic_terminal:
        evidence.update(
            {
                "checks": {
                    "status_accepted": bool(diagnostic_status_accepted),
                    "warning_allowlist_bound": bool(
                        diagnostic_warning_allowlist_bound
                    ),
                    "warning_codes_allowlisted": bool(
                        diagnostic_warnings_allowlisted
                    ),
                    "warnings_consistent_with_status": bool(
                        diagnostic_warnings_consistent
                    ),
                },
                "diagnostic_schema": CONTRACT_DIAGNOSTIC_SCHEMA,
                "diagnostic_kind": SYSTEM_DIAGNOSTIC_KIND,
                "observed_status": diagnostic_status,
                "accepted_statuses": ["healthy", "warning"],
                "accepted_warning_codes": sorted(
                    SYSTEM_DIAGNOSTIC_RESOURCE_WARNING_CODES
                ),
                "status_accepted": diagnostic_status_accepted,
                "warnings": diagnostic_warnings,
                "warning_count": (
                    len(diagnostic_warnings)
                    if diagnostic_warnings is not None
                    else None
                ),
                "warnings_consistent_with_status": diagnostic_warnings_consistent,
            }
        )
    return ok, evidence


def _execute_new_sample(
    source_root: Path,
    sample_root: Path,
    adapter: Mapping[str, Any],
) -> dict[str, Any]:
    execution_nonce_sha256 = hashlib.sha256(os.urandom(32)).hexdigest()
    sandbox_exec = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or not sandbox_exec:
        return {
            "status": "failed",
            "executed": False,
            "gap_code": "SEATBELT_UNAVAILABLE",
            "execution_nonce_sha256": execution_nonce_sha256,
        }
    sample_root.mkdir(parents=True, exist_ok=False)
    fixture_root = sample_root / "fixture"
    fixture_meta = _prepare_fixture(
        str(adapter["fixture_kind"]),
        fixture_root,
        str(adapter["job_id"]),
        source_root=source_root,
    )
    _assert_no_symlinks(fixture_root)
    home = sample_root / "home"
    temp = sample_root / "tmp"
    home.mkdir()
    temp.mkdir()
    # Preserve the venv launcher path.  Resolving its symlink to the Homebrew
    # base interpreter silently drops the release environment's site-packages.
    python = Path(
        os.environ.get("MAGI_V3_PYTHON_RUNTIME") or source_root / "venv/bin/python3"
    ).expanduser()
    raw_dependency = adapter.get("dependency")
    dependency_spec = (
        _expand_dependency_spec(
            raw_dependency,
            source_root=source_root,
            fixture_root=fixture_root,
            python=python,
        )
        if isinstance(raw_dependency, Mapping)
        else None
    )
    with _dependency_fixture(dependency_spec, sample_root) as dependency:
        mock_port = dependency["port"]
        database_port = dependency.get("database_port")
        command = [
            _expand(
                str(value),
                source_root=source_root,
                fixture_root=fixture_root,
                python=python,
                mock_port=mock_port,
                database_port=database_port,
            )
            for value in adapter["argv"]
        ]
        if Path(command[1]).resolve() != (source_root / str(adapter["production_entrypoint"])).resolve():
            raise ScheduleBodyRegistryError(f"adapter command does not call its real body: {adapter['job_id']}")
        allowed_ports = [
            int(port)
            for port in dependency.get(
                "ports", [mock_port] if mock_port is not None else []
            )
        ]
        profile = _seatbelt_profile(
            source_root, sample_root, allowed_localhost_ports=allowed_ports
        )
        env = {
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "MAGI_DISABLE_NOTIFICATIONS": "1",
            "MAGI_V3_REALISM_SANDBOX": "1",
            "MAGI_V3_SCHEDULE_ADAPTER": ADAPTER_MODE,
            "MAGI_V3_SCHEDULE_DRY_RUN": "1",
            "MAGI_V3_SCHEDULE_FIXTURE_ROOT": str(fixture_root),
            "MAGI_V3_SCHEDULE_NO_NETWORK": "1",
            "MAGI_V3_SCHEDULE_NO_NOTIFY": "1",
            "NO_PROXY": "*",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": "/dev/null",
            # A legacy .pth file can add the currently installed live runtime to
            # sys.path.  Put the hash-bound source release first so an adapter can
            # never certify a same-named module from that live installation.
            "PYTHONPATH": str(source_root),
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": str(temp),
        }
        env.update(
            {
                str(key): _expand(
                    str(value),
                    source_root=source_root,
                    fixture_root=fixture_root,
                    python=python,
                    mock_port=mock_port,
                    database_port=database_port,
                )
                for key, value in adapter["environment"].items()
            }
        )
        fixture_binding_receipt = _bind_fixture_v3_shared_state(
            fixture_root, env, source_root=source_root
        )
        fixture_cron = str(env.get("MAGI_CRON_JOBS_FILE") or "").strip()
        if fixture_cron:
            fixture_cron_path = Path(fixture_cron)
            adapter_environment = adapter.get("environment")
            if not isinstance(adapter_environment, Mapping):
                raise ScheduleBodyRegistryError("adapter environment is invalid")
            if any(
                name in adapter_environment
                for name in ("MAGI_CRON_JOBS_SHA256", "MAGI_CRON_JOBS_SOURCE_SHA256")
            ):
                raise ScheduleBodyRegistryError("fixture cron hashes must be generated by the runner")
            if (
                not fixture_cron_path.is_absolute()
                or fixture_cron_path.is_symlink()
                or not _is_relative_to(fixture_cron_path.resolve(), fixture_root.resolve())
            ):
                raise ScheduleBodyRegistryError("fixture cron path escapes the owned fixture")
            # Validate the ambient deployment triple before deriving a complete,
            # runner-owned binding for the isolated fixture snapshot.  Diagnostic
            # fixtures intentionally contain a reduced cron set, so their snapshot
            # hash need not equal the deployed snapshot hash.
            _bound_cron_bytes(source_root)
            fixture_payload = _stable_regular_bytes(
                fixture_cron_path, label="fixture cron copy"
            )
            env["MAGI_CRON_JOBS_SHA256"] = hashlib.sha256(fixture_payload).hexdigest()
            env["MAGI_CRON_JOBS_SOURCE_SHA256"] = _cron_policy_source_sha256(source_root)
        started = time.perf_counter()
        try:
            result = subprocess.run(
                [sandbox_exec, "-p", profile, "--", *command],
                cwd=sample_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            diagnostic = _write_execution_diagnostic(
                sample_root,
                source_root=source_root,
                job_id=str(adapter["job_id"]),
                returncode=None,
                stdout=str(exc.stdout or ""),
                stderr=str(exc.stderr or ""),
                semantic_success=False,
                dependency_evidence=None,
                fixture_binding_receipt=fixture_binding_receipt,
            )
            return {
                "status": "failed",
                "executed": True,
                "gap_code": "REAL_BODY_TIMEOUT",
                "execution_nonce_sha256": execution_nonce_sha256,
                "duration_seconds": round(time.perf_counter() - started, 6),
                "stdout_sha256": hashlib.sha256(str(exc.stdout or "").encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256(str(exc.stderr or "").encode()).hexdigest(),
                **diagnostic,
            }
        verify = dependency.get("verify")
        postcondition_error = ""
        try:
            postconditions = verify() if callable(verify) else []
        except Exception as exc:
            # A missing fixture table or a stopped disposable dependency is a
            # failed body contract, not a reason for the certification runner
            # itself to crash without preserving the command transcript.
            postconditions = []
            postcondition_error = f"{type(exc).__name__}: {exc}"
        postconditions_ok = all(row.get("passed") is True for row in postconditions)
        if postcondition_error:
            postconditions_ok = False
        snapshot = dependency.get("snapshot")
        transcript = snapshot() if callable(snapshot) else list(dependency["requests"])
        if dependency.get("match_mode") == "contains":
            request_counts = {
                str(expected): sum(
                    str(expected).lower() in str(row.get("path") or "").lower()
                    for row in transcript
                )
                for expected in dependency["expected_requests"]
            }
        else:
            request_counts = dict(
                Counter(str(row.get("path") or "") for row in transcript)
            )
        dependency_ok = postconditions_ok and all(
            int(request_counts.get(str(path), 0)) >= int(count)
            for path, count in dependency["expected_requests"].items()
        )
        dependency_evidence = {
            "kind": dependency["kind"],
            "request_count": len(transcript),
            "request_counts": dict(sorted(request_counts.items())),
            "expected_requests_satisfied": dependency_ok,
            "transcript_sha256": _sha256(transcript),
            "postcondition_count": len(postconditions),
            "passed_postcondition_count": sum(
                row.get("passed") is True for row in postconditions
            ),
            "postconditions_passed": postconditions_ok,
            "postconditions_sha256": _sha256(postconditions),
            "postcondition_error": postcondition_error,
        }
    duration = round(time.perf_counter() - started, 6)
    contract_ok, contract_evidence = _contract_ok(
        adapter["success_contract"], fixture_root, result.stdout, result.stderr
    )
    diagnostic = _write_execution_diagnostic(
        sample_root,
        source_root=source_root,
        job_id=str(adapter["job_id"]),
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        semantic_success=contract_ok,
        dependency_evidence=dependency_evidence,
        fixture_binding_receipt=fixture_binding_receipt,
    )
    final_inventory = _inventory(fixture_root)
    no_symlinks = not any(row["kind"] == "symlink" for row in final_inventory["rows"])
    passed = result.returncode == 0 and contract_ok and dependency_ok and no_symlinks
    return {
        "status": "passed" if passed else "failed",
        "executed": True,
        "gap_code": None if passed else "REAL_JOB_BODY_FIXTURE_CONTRACT_FAILED",
        "execution_nonce_sha256": execution_nonce_sha256,
        "returncode": result.returncode,
        "duration_seconds": duration,
        "semantic_success": contract_ok,
        "no_fixture_symlinks": no_symlinks,
        "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
        "sandbox_profile_sha256": hashlib.sha256(profile.encode()).hexdigest(),
        "fixture_initial_inventory_sha256": fixture_meta["initial_inventory_sha256"],
        "fixture_final_inventory_sha256": final_inventory["sha256"],
        "fixture_final_file_count": final_inventory["files"],
        "success_contract_evidence": contract_evidence,
        "dependency_evidence": dependency_evidence,
        "adapter_mode": ADAPTER_MODE,
        "network_denied_by_seatbelt": True,
        "localhost_dependency_allowlisted": dependency_spec is not None,
        "notifications_disabled": True,
        **diagnostic,
    }


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def _execute_new_samples(
    source_root: Path, workdir: Path, adapter: Mapping[str, Any]
) -> dict[str, Any]:
    samples = [
        _execute_new_sample(source_root, workdir / f"sample-{index:03d}", adapter)
        for index in range(1, MIN_SUCCESSFUL_SAMPLES + 1)
    ]
    passed = [row for row in samples if row.get("status") == "passed"]
    durations = [float(row["duration_seconds"]) for row in passed]
    complete = len(passed) == MIN_SUCCESSFUL_SAMPLES and len(durations) == MIN_SUCCESSFUL_SAMPLES
    entrypoint_sha256 = _sha256_file(
        source_root / str(adapter["production_entrypoint"])
    )
    sample_evidence = [
        build_sample_evidence(
            row,
            sample_index=index,
            execution_kind="reviewed_real_entrypoint_fixture_v1",
            entrypoint_sha256=entrypoint_sha256,
        )
        for index, row in enumerate(samples, 1)
    ]
    return {
        "job_id": adapter["job_id"],
        "status": "passed" if complete else "failed",
        "gap_code": None if complete else "INSUFFICIENT_REAL_BODY_SUCCESS_SAMPLES",
        "executed": all(row.get("executed") is True for row in samples),
        "semantic_success": complete,
        "samples_requested": MIN_SUCCESSFUL_SAMPLES,
        "successful_samples": len(passed),
        "duration_sample_count": len(durations),
        "duration_samples_seconds": durations,
        "duration_p95_seconds": round(_p95(durations), 6) if complete else None,
        "sample_statuses": [str(row.get("status") or "") for row in samples],
        "sample_evidence": sample_evidence,
        "sample_evidence_sha256": _sample_evidence_sha256(sample_evidence),
        "adapter_mode": ADAPTER_MODE,
        "safety_class": adapter["safety_class"],
        "fixture_kind": adapter["fixture_kind"],
        "entrypoint_sha256": entrypoint_sha256,
        "sandbox_profile_sha256_samples": [str(row.get("sandbox_profile_sha256") or "") for row in samples],
        "stdout_sha256_samples": [str(row.get("stdout_sha256") or "") for row in samples],
        "stderr_sha256_samples": [str(row.get("stderr_sha256") or "") for row in samples],
        "network_denied_by_seatbelt": all(row.get("network_denied_by_seatbelt") is True for row in samples),
        "localhost_dependency_allowlisted": any(
            row.get("localhost_dependency_allowlisted") is True for row in samples
        ),
        "notifications_disabled": all(row.get("notifications_disabled") is True for row in samples),
    }


def run_sandbox_escape_probes(source_root: Path, workdir: Path) -> dict[str, Any]:
    sandbox_exec = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or not sandbox_exec:
        return {"status": "failed", "reason": "SEATBELT_UNAVAILABLE"}
    workdir.mkdir(parents=True, exist_ok=False)
    allowed = workdir / "allowed"
    allowed.mkdir()
    outside = workdir / "outside.txt"
    link = allowed / "escape-link"
    link.symlink_to(outside)
    profile = _seatbelt_profile(source_root, allowed)
    direct = subprocess.run(
        [sandbox_exec, "-p", profile, "--", "/usr/bin/touch", str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    symlink = subprocess.run(
        [sandbox_exec, "-p", profile, "--", "/bin/sh", "-c", 'printf escaped > "$1"', "--", str(link)],
        capture_output=True,
        text=True,
        check=False,
    )
    python = Path(
        os.environ.get("MAGI_V3_PYTHON_RUNTIME") or source_root / "venv/bin/python3"
    ).expanduser()
    network = subprocess.run(
        [
            sandbox_exec, "-p", profile, "--", str(python), "-c",
            "import socket; s=socket.socket(); s.connect(('127.0.0.1',9))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    passed = (
        direct.returncode != 0
        and symlink.returncode != 0
        and network.returncode != 0
        and not outside.exists()
    )
    return {
        "status": "passed" if passed else "failed",
        "direct_write_escape_denied": direct.returncode != 0,
        "symlink_write_escape_denied": symlink.returncode != 0,
        "network_escape_denied": network.returncode != 0,
        "outside_file_absent": not outside.exists(),
        "sandbox_profile_sha256": hashlib.sha256(profile.encode()).hexdigest(),
        "probe_transcript_sha256": _sha256(
            {
                "direct": [direct.returncode, direct.stderr],
                "symlink": [symlink.returncode, symlink.stderr],
                "network": [network.returncode, network.stderr],
            }
        ),
    }


def run_registry_assessment(
    source_root: Path,
    workdir: Path,
    *,
    release_id: str,
    release_manifest_sha256: str,
) -> dict[str, Any]:
    if not release_id.strip() or not HEX64.fullmatch(release_manifest_sha256):
        raise ScheduleBodyRegistryError("formal registry execution requires a release id and manifest SHA-256")
    root = _owned_empty_workdir(workdir)
    jobs, cron_sha = bound_cron_jobs(source_root)
    registry_jobs, registry_cron_sha = _source_bound_cron_jobs(source_root)
    runtime_identity = {
        (str(job.get("id") or ""), job.get("enabled")) for job in jobs
    }
    source_identity = {
        (str(job.get("id") or ""), job.get("enabled")) for job in registry_jobs
    }
    if runtime_identity != source_identity:
        raise ScheduleBodyRegistryError(
            "deployed and source cron snapshots do not share the same job inventory"
        )
    registry = _load_registry(source_root, registry_jobs, registry_cron_sha)
    entries, legacy, new = resolve_registry(source_root, registry, registry_jobs)
    by_id = {str(job["id"]): job for job in registry_jobs}

    body_results: list[dict[str, Any]] = []
    for job_id in sorted(legacy):
        row = _execute_body_samples(
            source_root,
            root / "inherited" / job_id,
            by_id[job_id],
            legacy[job_id],
        )
        row["runner"] = "inherited_real_entrypoint_dry_run_v1"
        row["entrypoint_sha256"] = _sha256_file(source_root / str(legacy[job_id]["entrypoint"]))
        body_results.append(row)
    for job_id in sorted(new):
        row = _execute_new_samples(source_root, root / "new" / job_id, new[job_id])
        row["runner"] = ADAPTER_MODE
        body_results.append(row)

    escape_probes = run_sandbox_escape_probes(source_root, root / "escape-probes")
    enabled_count = len(entries)
    safe_count = sum(row["classification"] == "safe_adapter" for row in entries)
    blocked_count = enabled_count - safe_count
    passed_count = sum(row.get("status") == "passed" for row in body_results)
    all_safe_passed = passed_count == safe_count
    groups = Counter(row["actual_entrypoint"] for row in entries)
    blocker_counts = Counter(reason for row in entries for reason in row["blockers"])
    complete = blocked_count == 0 and all_safe_passed and escape_probes.get("status") == "passed"
    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "passed" if complete else "incomplete",
        "completion_claimed": complete,
        "release_binding": {
            "release_id": release_id,
            "release_manifest_sha256": release_manifest_sha256,
            "cron_jobs_sha256": cron_sha,
            "cron_jobs_snapshot_sha256": cron_sha,
            "cron_jobs_source_sha256": registry["release_binding"][
                "cron_jobs_source_sha256"
            ],
            "cron_binding_mode": (
                "source_exact"
                if cron_sha == registry["release_binding"]["cron_jobs_source_sha256"]
                else "release_rebased_logical_definition"
            ),
            "logical_definition_sha256": _logical_definition_sha256(registry_jobs),
            "scheduled_replay_logical_definition_sha256": _logical_definition_sha256(jobs),
            "registry_sha256": _sha256_file(source_root / REGISTRY_PATH),
            "inherited_baseline_sha256": _sha256_file(source_root / BASELINE_PATH),
        },
        "execution_policy": registry["execution_contract"],
        "measurements": {
            "enabled_jobs": enabled_count,
            "inherited_safe_adapters": len(legacy),
            "new_safe_adapters": len(new),
            "safe_adapter_coverage_jobs": safe_count,
            "safe_adapter_coverage_ratio": round(safe_count / enabled_count, 6),
            "blocked_jobs": blocked_count,
            "body_jobs_passed": passed_count,
            "minimum_successful_samples_per_adapter": MIN_SUCCESSFUL_SAMPLES,
            "all_safe_bodies_passed": all_safe_passed,
            "registry_complete_for_enabled_jobs": len(entries) == enabled_count,
        },
        "blocker": {
            "code": "REAL_JOB_BODY_ADAPTER_COVERAGE_INCOMPLETE",
            "eligible_to_clear": complete,
            "decision": "clear" if complete else "blocker_retained",
            "remaining_blocked_jobs": blocked_count,
        },
        "entrypoint_groups": [
            {"entrypoint": name, "enabled_jobs": count}
            for name, count in sorted(groups.items())
        ],
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "registry_entries": entries,
        "body_results": body_results,
        "sandbox_escape_probes": escape_probes,
        "network_access_performed": any(
            row.get("localhost_dependency_allowlisted") is True for row in body_results
        ),
        "external_network_access_performed": False,
        "production_database_access_performed": False,
        "nas_access_performed": False,
        "production_state_write_performed": False,
    }
    evidence["evidence_sha256"] = _sha256(evidence)
    return evidence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    args = parser.parse_args(argv)
    evidence = run_registry_assessment(
        REPO_ROOT,
        args.workdir,
        release_id=args.release_id,
        release_manifest_sha256=args.release_manifest_sha256,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": evidence["status"],
        "coverage": evidence["measurements"]["safe_adapter_coverage_jobs"],
        "enabled": evidence["measurements"]["enabled_jobs"],
        "passed": evidence["measurements"]["body_jobs_passed"],
        "evidence_sha256": evidence["evidence_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if evidence["measurements"]["all_safe_bodies_passed"] and evidence["sandbox_escape_probes"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
