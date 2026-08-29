"""Fail-closed readers for mutable inputs bound outside sealed V3 releases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ExternalInputError(RuntimeError):
    """A deployed external input is incomplete, unsafe, or has drifted."""


# Exact mutable-path bindings required by sealed V3 consumers.  The tuple
# contains the deployment-manifest field followed by its canonical path below
# ``runtime_root/shared``.  Deployment and every verifier share this table so
# a consumer cannot silently fall back into the immutable release tree.
NAMED_MUTABLE_STATE_BINDINGS = {
    "MAGI_LAF_PROCESSED_EMAILS_PATH": (
        "laf_processed_emails_path",
        "agent/laf-orchestrator/processed_laf_emails.json",
    ),
    "MAGI_PAYMENT_REGISTRY_PATH": (
        "payment_registry_path",
        "file-review/downloads/payment_registry.json",
    ),
    "MAGI_PAYMENT_PROOF_REGISTRY_PATH": (
        "payment_proof_registry_path",
        "file-review/downloads/payment_proof_registry.json",
    ),
    "MAGI_JUDGMENTS_JSON_PATH": (
        "judgments_json_path",
        "agent/judgment-collector/judgments.json",
    ),
    "MAGI_PDF_NAMER_CASE_INDEX": (
        "pdf_namer_case_index",
        "pdf-namer/_case_index.json",
    ),
    "MAGI_CORTEX_SYNC_STATE_PATH": (
        "cortex_sync_state_path",
        "runtime/cortex_sync_state.json",
    ),
    "MAGI_DEBT_ADDRESS_BOOK_DIR": (
        "debt_address_book_dir",
        "debt/address-book",
    ),
}


def named_mutable_state_paths(runtime_root: Path | str) -> dict[str, str]:
    """Derive exact named mutable files from the canonical runtime root."""

    shared = Path(runtime_root).expanduser().resolve(strict=False) / "shared"
    return {
        binding_name: str(shared / relative)
        for binding_name, relative in NAMED_MUTABLE_STATE_BINDINGS.values()
    }


def live_shared_state_environment(shared_root: Path | str) -> dict[str, str]:
    """Return the shared-state environment used by deployed V3 probes.

    LaunchAgents receive these values from ``v3_deploy_prepare``.  Validators
    are also invoked directly by operators and by the acceptance gate, where
    only ``MAGI_RUNTIME_DIR`` may be present.  Keeping the derivation here
    prevents those entrypoints from silently selecting different mutable
    stores (or falling back into the immutable release).
    """

    shared = Path(shared_root).expanduser().resolve(strict=False)
    values = {
        "MAGI_SHARED_STATE_DIR": str(shared),
        "MAGI_V3_SHARED_STATE_DIR": str(shared),
        "MAGI_FILE_REVIEW_STATE_DIR": str(shared / "file-review"),
        "MAGI_FILE_REVIEW_BG_JOB_DIR": str(shared / "file-review" / "bg-jobs"),
        "MAGI_EEFILE_DOWNLOAD_FOLDER": str(shared / "file-review" / "downloads"),
        "MAGI_LAF_GMAIL_STATE_PATH": str(shared / "static" / "laf_gmail_monitor_state.json"),
        "MAGI_LAF_GMAIL_MONITOR_STATE": str(shared / "static" / "laf_gmail_monitor_state.json"),
        "MAGI_LAF_GMAIL_PENDING_PATH": str(shared / "runtime" / "laf_gmail_dispatch_pending.json"),
        "MAGI_FILE_REVIEW_EMAIL_MONITOR_STATE": str(
            shared / "static" / "file_review_email_monitor_state.json"
        ),
        "MAGI_FILE_REVIEW_PENDING_PATH": str(
            shared / "agent" / "file-review" / "review_submit_pending.json"
        ),
        "MAGI_BRAIN_SQLITE_PATH": str(shared / "agent" / "magi_brain.db"),
    }
    values.update(
        {
            key: str(path)
            for key, path in (
                ("MAGI_PAYMENT_REGISTRY_PATH", shared / "file-review" / "downloads" / "payment_registry.json"),
                (
                    "MAGI_PAYMENT_PROOF_REGISTRY_PATH",
                    shared / "file-review" / "downloads" / "payment_proof_registry.json",
                ),
                (
                    "MAGI_LAF_PROCESSED_EMAILS_PATH",
                    shared / "agent" / "laf-orchestrator" / "processed_laf_emails.json",
                ),
            )
        }
    )
    return values


@dataclass(frozen=True, slots=True)
class BoundCronJobs:
    path: Path
    sha256: str
    source_sha256: str
    jobs: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class BoundLAFConfig:
    path: Path
    sha256: str
    config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BoundExternalFile:
    path: Path
    sha256: str


SEALED_RUNTIME_FILE_BINDINGS = (
    ("runtime config", "MAGI_CONFIG_PATH", "MAGI_CONFIG_SHA256", "MAGI_CONFIG_MODE"),
    (
        "Google credentials",
        "MAGI_GOOGLE_CREDENTIALS_PATH",
        "MAGI_GOOGLE_CREDENTIALS_SHA256",
        "MAGI_GOOGLE_CREDENTIALS_MODE",
    ),
    (
        "accounting credentials",
        "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_PATH",
        "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_SHA256",
        "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_MODE",
    ),
)
MUTABLE_SECRET_BINDINGS = (
    ("Google Calendar token", "MAGI_GOOGLE_CALENDAR_TOKEN_PATH", "google_calendar_token.json", True),
    ("LAF Gmail token", "MAGI_LAF_GMAIL_TOKEN_PATH", "laf_gmail_token.pickle", True),
    ("FileReview token", "MAGI_FILE_REVIEW_TOKEN_PATH", "filereview_token.pickle", True),
    ("Gmail compose token", "MAGI_GMAIL_COMPOSE_TOKEN_PATH", "gmail_compose_token.json", False),
    (
        "accounting Sheets token",
        "MAGI_ACCOUNTING_GOOGLE_SHEETS_TOKEN",
        "accounting_sheets_token.json",
        True,
    ),
    ("Drive sync token", "MAGI_DRIVE_SYNC_TOKEN", "drive_sync_token.json", True),
    (
        "Drive sync write token",
        "MAGI_DRIVE_SYNC_WRITE_TOKEN",
        "drive_sync_write_token.json",
        True,
    ),
)


def _sealed_context(root: Path) -> bool:
    return bool(
        str(os.environ.get("MAGI_V3_RELEASE_ID") or "").strip()
        or str(os.environ.get("MAGI_V3_DEPLOYMENT_MODE") or "").strip()
        or str(os.environ.get("MAGI_V3_RELEASE_MANIFEST") or "").strip()
        or (root / "release-manifest.json").is_file()
        or (root / "RELEASE_COMPLETE.json").is_file()
    )


def bound_shared_directory(
    release_root: Path | str,
    *,
    env_name: str,
    shared_leaf: str,
    source_fallback: str,
) -> Path:
    """Resolve one V3 mutable directory without permitting release writes."""

    root = Path(release_root).expanduser().resolve()
    relative = Path(shared_leaf)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("shared mutable directory path must be relative and normalized")
    sealed = _sealed_context(root)
    declared = str(os.environ.get(env_name) or "").strip()
    shared_declared = str(
        os.environ.get("MAGI_V3_SHARED_STATE_DIR")
        or os.environ.get("MAGI_SHARED_STATE_DIR")
        or ""
    ).strip()
    if sealed:
        if not declared or not shared_declared:
            raise ExternalInputError(
                f"sealed V3 release requires {env_name} and shared-state bindings"
            )
        raw_candidate = Path(declared).expanduser()
        raw_shared = Path(shared_declared).expanduser()
        if not raw_candidate.is_absolute() or not raw_shared.is_absolute():
            raise ExternalInputError("sealed V3 mutable paths must be absolute")
        if raw_candidate.is_symlink() or raw_shared.is_symlink():
            raise ExternalInputError("sealed V3 mutable paths must not be symlinks")
        candidate = raw_candidate.resolve(strict=False)
        shared = raw_shared.resolve(strict=False)
        expected = shared / relative
        if (
            raw_candidate != candidate
            or raw_shared != shared
            or candidate != expected
            or candidate == root
            or candidate.is_relative_to(root)
        ):
            raise ExternalInputError(f"{env_name} escapes its canonical shared-state binding")
        current = shared
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ExternalInputError(f"{env_name} contains a symlinked directory")
        return candidate
    if declared:
        return Path(declared).expanduser()
    return root / source_fallback


def laf_download_directory(release_root: Path | str) -> Path:
    """Return the canonical writable LAF download directory.

    A sealed V3 release must never resolve the historical ``./laf_downloads``
    fallback inside its immutable release tree.  The deployment's shared-state
    root is the single writable owner; an optional explicit override is
    accepted only when it names that exact canonical directory.
    """

    root = Path(release_root).expanduser().resolve()
    declared = str(os.environ.get("MAGI_LAF_DOWNLOAD_FOLDER") or "").strip()
    if not _sealed_context(root):
        return Path(declared).expanduser() if declared else root / "laf_downloads"

    shared_declared = str(
        os.environ.get("MAGI_V3_SHARED_STATE_DIR")
        or os.environ.get("MAGI_SHARED_STATE_DIR")
        or ""
    ).strip()
    if not shared_declared:
        raise ExternalInputError(
            "sealed V3 LAF downloads require a shared-state binding"
        )
    raw_shared = Path(shared_declared).expanduser()
    raw_candidate = (
        Path(declared).expanduser()
        if declared
        else raw_shared / "laf-downloads"
    )
    if not raw_shared.is_absolute() or not raw_candidate.is_absolute():
        raise ExternalInputError("sealed V3 LAF download paths must be absolute")
    if raw_shared.is_symlink() or raw_candidate.is_symlink():
        raise ExternalInputError("sealed V3 LAF download paths must not be symlinks")
    shared = raw_shared.resolve(strict=False)
    candidate = raw_candidate.resolve(strict=False)
    expected = shared / "laf-downloads"
    if (
        raw_shared != shared
        or raw_candidate != candidate
        or candidate != expected
        or candidate == root
        or candidate.is_relative_to(root)
    ):
        raise ExternalInputError(
            "MAGI_LAF_DOWNLOAD_FOLDER escapes its canonical shared-state binding"
        )
    return candidate


def bound_shared_file(
    release_root: Path | str,
    *,
    env_name: str,
    shared_relative: str,
    source_fallback: Path | str,
) -> Path:
    """Resolve one mutable file while keeping sealed releases read-only.

    A sealed launcher must name both the exact file and the canonical shared
    root.  Requiring the two independent bindings prevents a missing or stale
    environment variable from silently turning a release-relative fallback
    into a write target.  Source/V2 callers retain their historical fallback.
    """

    root = Path(release_root).expanduser().resolve()
    declared = str(os.environ.get(env_name) or "").strip()
    shared_declared = str(
        os.environ.get("MAGI_V3_SHARED_STATE_DIR")
        or os.environ.get("MAGI_SHARED_STATE_DIR")
        or ""
    ).strip()
    relative = Path(shared_relative)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("shared mutable file path must be relative and normalized")

    if _sealed_context(root):
        if not declared or not shared_declared:
            raise ExternalInputError(
                f"sealed V3 release requires {env_name} and shared-state bindings"
            )
        raw_candidate = Path(declared).expanduser()
        raw_shared = Path(shared_declared).expanduser()
        if not raw_candidate.is_absolute() or not raw_shared.is_absolute():
            raise ExternalInputError("sealed V3 mutable paths must be absolute")
        if raw_candidate.is_symlink() or raw_shared.is_symlink():
            raise ExternalInputError("sealed V3 mutable paths must not be symlinks")
        candidate = raw_candidate.resolve(strict=False)
        shared = raw_shared.resolve(strict=False)
        expected = shared / relative
        if (
            raw_candidate != candidate
            or raw_shared != shared
            or candidate != expected
            or candidate == root
            or candidate.is_relative_to(root)
        ):
            raise ExternalInputError(f"{env_name} escapes its canonical shared-state binding")
        current = shared
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink():
                raise ExternalInputError(f"{env_name} contains a symlinked directory")
        return candidate
    if declared:
        return Path(declared).expanduser()
    return Path(source_fallback).expanduser()


def _stable_regular_bytes(path: Path, *, label: str) -> bytes:
    try:
        declared = path.lstat()
    except OSError as exc:
        raise ExternalInputError(f"{label} is unavailable: {exc}") from exc
    if stat.S_ISLNK(declared.st_mode) or not stat.S_ISREG(declared.st_mode):
        raise ExternalInputError(f"{label} must be a non-symlink regular file")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExternalInputError(f"{label} is unavailable: {exc}") from exc
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or any(
            getattr(declared, field) != getattr(before, field) for field in fields
        ):
            raise ExternalInputError(f"{label} changed before it was read")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ExternalInputError(f"{label} is unreadable: {exc}") from exc
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise ExternalInputError(f"{label} changed while it was read")
    if len(payload) != before.st_size:
        raise ExternalInputError(f"{label} changed while it was read")
    return payload


def _bound_external_file(
    release_root: Path,
    *,
    label: str,
    declared_path: str,
    declared_sha: str,
) -> BoundExternalFile:
    if not declared_path or not declared_sha:
        raise ExternalInputError(f"external {label} binding is incomplete")
    raw = Path(declared_path).expanduser()
    if not raw.is_absolute():
        raise ExternalInputError(f"external {label} path must be absolute")
    if re.fullmatch(r"[0-9a-f]{64}", declared_sha) is None:
        raise ExternalInputError(f"external {label} SHA-256 is invalid")
    try:
        path = raw.resolve(strict=True)
    except OSError as exc:
        raise ExternalInputError(f"external {label} is unavailable: {exc}") from exc
    if raw.is_symlink() or path != raw:
        raise ExternalInputError(f"external {label} path must be canonical and non-symlinked")
    if path == release_root or path.is_relative_to(release_root):
        raise ExternalInputError(f"external {label} must stay outside the sealed release")
    payload = _stable_regular_bytes(path, label=label)
    observed_sha = hashlib.sha256(payload).hexdigest()
    if observed_sha != declared_sha:
        raise ExternalInputError(f"external {label} SHA-256 mismatched")
    return BoundExternalFile(path=path, sha256=observed_sha)


def verify_sealed_runtime_inputs(
    release_root: Path | str,
    environ: dict[str, str] | Any | None = None,
) -> dict[str, BoundExternalFile]:
    """Verify every credential/config file required by a sealed role at start."""

    root = Path(release_root).expanduser().resolve()
    env = os.environ if environ is None else environ
    if not _sealed_context(root) and not str(env.get("MAGI_V3_RELEASE_ID") or "").strip():
        return {}
    bound: dict[str, BoundExternalFile] = {}
    for label, path_env, sha_env, mode_env in SEALED_RUNTIME_FILE_BINDINGS:
        item = _bound_external_file(
            root,
            label=label,
            declared_path=str(env.get(path_env) or "").strip(),
            declared_sha=str(env.get(sha_env) or "").strip(),
        )
        try:
            expected_mode = int(str(env.get(mode_env) or ""), 8)
        except ValueError as exc:
            raise ExternalInputError(f"external {label} mode binding is invalid") from exc
        if expected_mode not in {0o600, 0o640, 0o644}:
            raise ExternalInputError(f"external {label} mode binding is unsafe")
        if stat.S_IMODE(item.path.stat().st_mode) != expected_mode:
            raise ExternalInputError(f"external {label} mode mismatched")
        bound[path_env] = item
    config_path = bound["MAGI_CONFIG_PATH"].path
    json_raw = Path(str(env.get("MAGI_JSON_DIR") or "").strip()).expanduser()
    if (
        not json_raw.is_absolute()
        or json_raw.is_symlink()
        or json_raw.resolve(strict=True) != json_raw
        or json_raw != config_path.parent
    ):
        raise ExternalInputError("MAGI_JSON_DIR must equal the canonical external config directory")
    aliases = {
        "MAGI_LAF_CONFIG_FILE": str(config_path),
        "MAGI_LAF_CONFIG_SHA256": bound["MAGI_CONFIG_PATH"].sha256,
        "MAGI_GMAIL_CREDENTIALS_PATH": str(bound["MAGI_GOOGLE_CREDENTIALS_PATH"].path),
    }
    for name, expected in aliases.items():
        if str(env.get(name) or "").strip() != expected:
            raise ExternalInputError(f"{name} does not match its canonical external binding")
    shared_raw = Path(str(env.get("MAGI_V3_SHARED_STATE_DIR") or "").strip()).expanduser()
    if (
        not shared_raw.is_absolute()
        or shared_raw.is_symlink()
        or shared_raw.resolve(strict=False) != shared_raw
        or shared_raw == root
        or shared_raw.is_relative_to(root)
    ):
        raise ExternalInputError("sealed mutable secrets require a canonical external shared root")
    secrets_root = shared_raw / "secrets"
    for label, path_env, leaf, required in MUTABLE_SECRET_BINDINGS:
        raw = Path(str(env.get(path_env) or "").strip()).expanduser()
        expected = secrets_root / leaf
        if not raw.is_absolute() or raw != expected or raw.is_symlink():
            raise ExternalInputError(f"{path_env} escapes its canonical shared secrets binding")
        if not raw.exists():
            if required:
                raise ExternalInputError(f"required mutable {label} is unavailable")
            continue
        metadata = raw.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or raw.resolve(strict=True) != raw
        ):
            raise ExternalInputError(f"mutable {label} path, owner, or mode is unsafe")
    ocr_raw = Path(str(env.get("MAGI_NAS_OCR_QUEUE_DB_PATH") or "").strip()).expanduser()
    if not ocr_raw.is_absolute() or ocr_raw.is_symlink() or ocr_raw.resolve(strict=True) != ocr_raw:
        raise ExternalInputError("NAS OCR queue path must be canonical and non-symlinked")
    ocr_metadata = ocr_raw.lstat()
    if (
        not stat.S_ISREG(ocr_metadata.st_mode)
        or ocr_metadata.st_uid != os.getuid()
        or ocr_metadata.st_nlink != 1
        or stat.S_IMODE(ocr_metadata.st_mode) not in {0o600, 0o640, 0o644}
    ):
        raise ExternalInputError("NAS OCR queue owner or mode is unsafe")
    try:
        with sqlite3.connect(f"file:{ocr_raw}?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ExternalInputError("NAS OCR queue quick_check did not return ok")
    except sqlite3.Error as exc:
        raise ExternalInputError(f"NAS OCR queue quick_check failed: {exc}") from exc
    return bound


def load_bound_cron_jobs(
    release_root: Path | str,
    *,
    missing_source_default: bool = True,
) -> BoundCronJobs:
    """Load the deployed cron snapshot, verifying its complete path/hash binding.

    Source and V2 development layouts may still use ``root/cron_jobs.json``.
    A sealed V3 candidate has no such member and must provide both deployment
    variables; once either is present, fallback is forbidden.
    """

    root = Path(release_root).expanduser().resolve()
    declared_path = str(os.environ.get("MAGI_CRON_JOBS_FILE") or "").strip()
    declared_sha = str(os.environ.get("MAGI_CRON_JOBS_SHA256") or "").strip()
    declared_source_sha = str(
        os.environ.get("MAGI_CRON_JOBS_SOURCE_SHA256") or ""
    ).strip()
    declared = (declared_path, declared_sha, declared_source_sha)
    sealed = _sealed_context(root)
    if any(declared) and not all(declared):
        raise ExternalInputError("external cron snapshot binding is incomplete")

    deployed = bool(declared_path)
    if deployed:
        path = Path(declared_path).expanduser()
        if not path.is_absolute():
            raise ExternalInputError("external cron snapshot path must be absolute")
        if re.fullmatch(r"[0-9a-f]{64}", declared_sha) is None:
            raise ExternalInputError("external cron snapshot SHA-256 is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", declared_source_sha) is None:
            raise ExternalInputError("external cron source SHA-256 is invalid")
    else:
        if sealed:
            raise ExternalInputError(
                "sealed V3 release requires a complete external cron snapshot binding"
            )
        path = root / "cron_jobs.json"
        if missing_source_default and not path.exists() and not path.is_symlink():
            return BoundCronJobs(path=path, sha256="", source_sha256="", jobs=())

    payload = _stable_regular_bytes(path, label="cron snapshot")
    observed_sha = hashlib.sha256(payload).hexdigest()
    if deployed and observed_sha != declared_sha:
        raise ExternalInputError("external cron snapshot SHA-256 mismatched")
    source_sha = declared_source_sha if deployed else observed_sha
    if deployed:
        policy_path = root / "config/v3_schedule_dispatch_policy.json"
        policy_payload = _stable_regular_bytes(policy_path, label="cron dispatch policy")
        try:
            policy = json.loads(policy_payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ExternalInputError(f"cron dispatch policy is invalid JSON: {exc}") from exc
        if not isinstance(policy, dict) or policy.get("cron_jobs_sha256") != source_sha:
            raise ExternalInputError("external cron source/policy binding mismatched")
    try:
        jobs = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalInputError(f"cron snapshot is invalid JSON: {exc}") from exc
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ExternalInputError("cron snapshot must contain a JSON object list")
    return BoundCronJobs(
        path=path,
        sha256=observed_sha,
        source_sha256=source_sha,
        jobs=tuple(dict(job) for job in jobs),
    )


def load_bound_laf_config(
    release_root: Path | str,
    *,
    source_fallback: Path | str | None = None,
) -> BoundLAFConfig:
    """Load the external LAF config required by a sealed V3 release."""

    root = Path(release_root).expanduser().resolve()
    declared_path = str(
        os.environ.get("MAGI_CONFIG_PATH") or os.environ.get("MAGI_LAF_CONFIG_FILE") or ""
    ).strip()
    declared_sha = str(
        os.environ.get("MAGI_CONFIG_SHA256")
        or os.environ.get("MAGI_LAF_CONFIG_SHA256")
        or ""
    ).strip()
    sealed = _sealed_context(root)
    if bool(declared_path) != bool(declared_sha):
        if sealed:
            raise ExternalInputError("external LAF config binding is incomplete")
        declared_path = ""
        declared_sha = ""
    if declared_path:
        binding = _bound_external_file(
            root,
            label="LAF config",
            declared_path=declared_path,
            declared_sha=declared_sha,
        )
        path = binding.path
    else:
        if sealed:
            raise ExternalInputError(
                "sealed V3 release requires a complete external LAF config binding"
            )
        if source_fallback is None:
            raise ExternalInputError("source LAF config fallback is unavailable")
        path = Path(source_fallback).expanduser()

    payload = _stable_regular_bytes(path, label="LAF config")
    observed_sha = hashlib.sha256(payload).hexdigest()
    if declared_path and observed_sha != declared_sha:
        raise ExternalInputError("external LAF config SHA-256 mismatched")
    try:
        config = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalInputError(f"LAF config is invalid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ExternalInputError("LAF config must contain a JSON object")
    return BoundLAFConfig(path=path, sha256=observed_sha, config=dict(config))
