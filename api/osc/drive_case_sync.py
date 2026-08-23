"""Google Drive/NAS case inventory and conservative sync for MAGI.

This module keeps OSC's NAS-first folder layout and the legacy Google Drive
layout separate.  Sync actions use a boundary mapping layer so each side keeps
its own naming rules; no action overwrites existing files or deletes content.
"""

from __future__ import annotations

import argparse
import csv
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from api.domains.case_file_operation_lock import (
    acquire_case_file_operation_lock,
    release_case_file_operation_lock,
)
from api.osc.case_folder_schema import (
    case_subfolders as osc_case_subfolders,
    strip_number_prefix as osc_strip_number_prefix,
)
from skills.bridge.shared_utils.judgment_folder_names import (
    JUDGMENT_FOLDER_LABEL,
    judgment_folder_name,
    legacy_judgment_folder_name,
)
from scripts.ops.token_health_check import google_token_file_lock
from magi_v3.drive_file_checkpoint import (
    DriveFileCheckpoint,
    item_token as drive_checkpoint_item_token,
    proof_hash as drive_checkpoint_proof_hash,
    source_fingerprint as drive_checkpoint_source_fingerprint,
)

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_WRITE_SCOPE = "https://www.googleapis.com/auth/drive"
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
READONLY_SCOPES = [DRIVE_READONLY_SCOPE, SHEETS_READONLY_SCOPE]
WRITE_SCOPES = [DRIVE_WRITE_SCOPE]
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_EXPORT_MIME_MAP = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}

DEFAULT_DRIVE_ROOT_NAME = "案件辦理"
DEFAULT_OWNER_BUCKETS = {"Aaron", "Lumi", "Aaron&Lumi", "Lumi-2"}
OSC_NUMBER_REQUIRED_CATEGORIES = {"諮詢案件", "縣府調解案件"}
LOCAL_CASE_CATEGORIES = {"一般案件", "法扶案件", "無償案件", "指定辯護案件"}
LOCAL_CASE_KIND_FOLDERS = {
    "民事",
    "刑事",
    "行政",
    "非訟",
    "消費者債務清理",
    "法律顧問",
    "其他",
}
DRIVE_CASE_KIND_BUCKETS = {
    "陪偵": "陪偵",
    "消債": "消費者債務清理",
}
DRIVE_CASE_KIND_BUCKET_BY_CASE_KIND = {
    "陪偵": "陪偵",
    "消費者債務清理": "01.消債",
}
SYNC_IGNORE_NAMES = {
    ".DS_Store",
    ".duplicates",
    ".trash",
    ".runtime",
    ".magi",
    "desktop.ini",
    "Thumbs.db",
    "@eaDir",
    "#recycle",
    ".SynologyWorkingDirectory",
    ".TemporaryItems",
    ".Trashes",
}
SYNC_IGNORE_PREFIXES = ("~$", "._", ".magi_", ".magi-")
DEFAULT_LOCAL_HASH_MAX_BYTES = 25_000_000
DEFAULT_LOCAL_HASH_TIMEOUT_SEC = 300
DEFAULT_LOCAL_HASH_MIN_BYTES_PER_SEC = 1024 * 1024
DEFAULT_LOCAL_HASH_TIMEOUT_HEADROOM_SEC = 30
DEFAULT_SMB_STAGE_TIMEOUT_SEC = 300
DEFAULT_SMB_STAGE_MIN_BYTES_PER_SEC = 256 * 1024
DEFAULT_LOCAL_SCAN_TIMEOUT_SEC = 20
DEFAULT_MAX_SINGLE_UPLOAD_BYTES = 100_000_000
DEFAULT_RESUMABLE_UPLOAD_MIN_BYTES = 8 * 1024 * 1024
DEFAULT_RESUMABLE_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_DRIVE_DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024
DRIVE_SYNC_TMP_PREFIX = ".magi-drive-sync-"
DRIVE_SYNC_TMP_SUFFIX = ".tmp"
DEFAULT_STALE_TMP_MAX_AGE_SEC = 600


def _continue_signal_chain(previous_handler: Any, signum: int, frame: Any) -> None:
    """Run the worker's outer termination handler after child cleanup.

    The worker owns the status/checkpoint/lock contract.  A nested SMB helper
    may terminate its child process first, but it must not swallow that outer
    handler or LIVE health will be left with a false ``running`` record.
    """

    if callable(previous_handler):
        previous_handler(signum, frame)
    raise SystemExit(128 + int(signum))


OSC_CASE_RE = re.compile(r"(20\d{2}-\d{4})")
LAF_CASE_RE = re.compile(r"(\d{6,7}-[A-Z]-\d{3})")
COURT_CASE_RE = re.compile(r"(\d{2,3}年度[^\\/\s()（）-]{1,12}字第?\d{1,8}號)")
ROC_COURT_NO_RE = re.compile(r"(\d{2,3})年度(.+?)字第?0*(\d+)號")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,24}")
GENERIC_CONTEXT_TERMS = {
    "一般案件",
    "法扶案件",
    "無償案件",
    "指定辯護案件",
    "行政",
    "刑事",
    "民事",
    "非訟",
    "一審",
    "二審",
    "偵查",
    "更審",
    "案件",
    "訴訟",
    "行政訴訟",
    "行政訴訟案",
    "訴願",
    "訴願案",
    "事件",
    "資料",
    "法院",
    "裁判",
    "書狀",
    "歷次書狀",
    "函文",
    "附件",
    "委任狀",
    "收據",
    "存底",
    "決行版本",
    "掛號郵件",
    "收件回執",
    "財團法人",
    "被害者",
    "權利回復",
    "權利回",
    "一般",
    "被害者權利回復",
}
CONTEXT_SUFFIXES = (
    "行政訴訟案",
    "行政訴訟",
    "訴願案",
    "訴訟案",
    "等人案",
    "事件",
    "案",
    "等人",
    "等",
)
NON_DECISIVE_CONTEXT_SUBSTRINGS = (
    "訴願",
    "訴訟",
    "裁定",
    "判決",
    "答辯",
    "書狀",
    "答辯狀",
    "委任狀",
    "收據",
    "函",
    "憑單",
    "回執",
    "法院",
    "費用",
    "主文",
    "原告",
    "被告",
    "決定",
    "用印",
    "管轄",
    "案卷",
    "卷",
)
CONTEXT_DECISIVE_ALLOWLIST = {
    "律師函",
}


class DriveCaseSyncError(RuntimeError):
    pass


class DriveCaseSyncDeadline(TimeoutError):
    """Outer worker deadline that inner per-file handlers must never swallow."""


class DriveCaseSyncStorageDeferred(DriveCaseSyncError):
    """A resumable SMB staging interruption that does not require a human."""


# These are deliberately closed sets.  Only an interrupted/storage condition
# may preserve the same cursor for a retry; an unknown helper failure remains
# a hard, no-write result until its cause is understood.
SMB_STAGE_RETRYABLE_FAILURE_CODES = frozenset({
    "smb_stage_timeout",
    "smb_stage_interrupted",
    "smb_stage_storage_unavailable",
    "local_source_stat_unavailable",
})
LOCAL_HASH_RETRYABLE_FAILURE_CODES = frozenset({
    "FileNotFoundError",
    "local_hash_timeout",
    "local_hash_smb_helper_failed",
    "local_hash_smb_helper_storage_unavailable",
    "local_hash_smb_helper_file_missing",
    "local_hash_smb_helper_permission_denied",
    "local_hash_smb_helper_signal_terminated",
    *SMB_STAGE_RETRYABLE_FAILURE_CODES,
})


def local_hash_failure_code(exc: BaseException) -> str:
    """Return the fixed, path-free code used by plan and worker policy."""

    if isinstance(exc, DriveCaseSyncError):
        candidate = str(exc).split(":", 1)[0].strip()
        if candidate:
            return candidate
    return type(exc).__name__


def is_retryable_local_hash_failure(code: str) -> bool:
    return str(code or "") in LOCAL_HASH_RETRYABLE_FAILURE_CODES


def is_storage_unavailable_error(exc: BaseException) -> bool:
    """Return True only for an OS report that the mounted device vanished.

    A regular missing file is deliberately not included: it must remain a
    failed upload.  These errnos mean that continuing to inspect NAS paths can
    manufacture misleading ``file not found`` failures after a mount drops.
    """
    if isinstance(exc, DriveCaseSyncStorageDeferred):
        return True
    if not isinstance(exc, OSError):
        return False
    unavailable_errnos = {errno.ENXIO, errno.ENODEV, errno.EIO}
    stale = getattr(errno, "ESTALE", None)
    if stale is not None:
        unavailable_errnos.add(stale)
    return exc.errno in unavailable_errnos


def is_download_target_storage_unavailable_error(
    exc: BaseException,
    target_path: Path,
) -> bool:
    """Detect a vanished NAS mount without hiding ordinary ACL failures.

    When an smbfs share disappears between inventory and download, ``mkdir``
    can raise EACCES/EPERM for the share root (for example
    ``/Volumes/lumi``).  A permission error for a nested case/file remains a
    real failure; only the mounted volume root is treated as a reconnectable
    storage interruption.
    """
    if is_storage_unavailable_error(exc):
        return True
    if not isinstance(exc, OSError) or exc.errno not in {errno.EACCES, errno.EPERM}:
        return False
    try:
        resolved_target = Path(target_path)
        parts = resolved_target.parts
        if len(parts) < 3 or parts[0] != "/" or parts[1] != "Volumes":
            return False
        volume_root = Path("/") / parts[1] / parts[2]
        failed_path = Path(str(getattr(exc, "filename", "") or ""))
        return failed_path == volume_root
    except (OSError, TypeError, ValueError):
        return False


class BoundedEntries(list):
    """List-compatible scan result that records whether the bound was hit.

    Callers historically consumed a plain list.  Keeping that contract while
    exposing ``truncated`` lets write paths fail closed instead of treating an
    incomplete Drive/NAS scan as proof that the other side is missing data.
    """

    def __init__(self, *args: Any, truncated: bool = False):
        super().__init__(*args)
        self.truncated = bool(truncated)


class DriveCaseSyncAuthRequired(DriveCaseSyncError):
    """Raised when a non-interactive Drive sync needs a fresh OAuth grant."""

    def __init__(self, message: str, *, token_path: Path | None = None, write: bool = False):
        super().__init__(message)
        self.token_path = token_path
        self.write = write


@dataclass
class FileEntry:
    source: str
    path: str
    relative_path: str
    name: str
    is_folder: bool
    modified_time: str = ""
    size: int | None = None
    md5: str = ""
    drive_id: str = ""
    web_url: str = ""
    mime_type: str = ""


@dataclass
class CaseMeta:
    case_number: str = ""
    laf_case_no: str = ""
    court_case_no: str = ""
    client_hint: str = ""
    reason_hint: str = ""


@dataclass
class CaseFolder:
    source: str
    path: str
    relative_path: str
    name: str
    category: str = ""
    status: str = ""
    case_kind: str = ""
    owner_bucket: str = ""
    modified_time: str = ""
    meta: CaseMeta = field(default_factory=CaseMeta)
    drive_id: str = ""
    web_url: str = ""
    local_path: str = ""
    suggested_canonical_path: str = ""
    suggested_path_confidence: str = ""
    suggested_path_note: str = ""


@dataclass
class ContextScore:
    candidate: CaseFolder
    score: int
    matched_terms: list[str] = field(default_factory=list)
    candidate_terms: list[str] = field(default_factory=list)
    context_sample: list[str] = field(default_factory=list)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_dir() -> Path:
    root = Path(os.environ.get("MAGI_RUNTIME_DIR") or repo_root() / ".runtime")
    return root / "drive_sync"


def case_alias_file_path() -> Path:
    return Path(
        os.environ.get("MAGI_DRIVE_SYNC_CASE_ALIAS_FILE")
        or runtime_dir() / "case_aliases.json"
    ).expanduser()


def case_exclusion_file_path() -> Path:
    return Path(
        os.environ.get("MAGI_DRIVE_SYNC_CASE_EXCLUSION_FILE")
        or runtime_dir() / "case_exclusions.json"
    ).expanduser()


def load_local_env() -> None:
    """Load .env with no dependency on python-dotenv."""
    env_path = repo_root() / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def drive_sync_token_path(*, write: bool = False) -> Path:
    if write:
        return Path(
            os.environ.get("MAGI_DRIVE_SYNC_WRITE_TOKEN")
            or "~/.magi/google/drive_sync_write_token.json"
        ).expanduser()
    return Path(
        os.environ.get("MAGI_DRIVE_SYNC_TOKEN")
        or os.environ.get("MAGI_ACCOUNTING_GOOGLE_SHEETS_TOKEN")
        or os.environ.get("MAGI_GOOGLE_SHEETS_TOKEN")
        or "~/.magi/google/drive_sync_token.json"
    ).expanduser()


def drive_sync_credentials_path() -> Path:
    return Path(
        os.environ.get("MAGI_DRIVE_SYNC_CREDENTIALS_PATH")
        or os.environ.get("MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_PATH")
        or os.environ.get("MAGI_GOOGLE_CREDENTIALS_PATH")
        or repo_root() / "json" / "credentials.json"
    ).expanduser()


def drive_sync_account_hint() -> str:
    return (
        os.environ.get("MAGI_DRIVE_SYNC_ACCOUNT_HINT")
        or os.environ.get("MAGI_ACCOUNTING_GOOGLE_ACCOUNT_HINT")
        or "primary"
    ).strip() or "primary"


def _write_google_credentials_unlocked(creds: Any, *, token_path: Path) -> None:
    token_path = token_path.expanduser()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(token_path.parent), delete=False) as tmp:
            try:
                os.fchmod(tmp.fileno(), 0o600)
            except Exception:
                pass
            tmp.write(creds.to_json())
            tmp.flush()
            try:
                os.fsync(tmp.fileno())
            except Exception:
                pass
            tmp_path = Path(tmp.name)
        os.replace(str(tmp_path), token_path)
        try:
            token_path.chmod(0o600)
        except Exception:
            pass
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _write_google_credentials(creds: Any, *, token_path: Path) -> None:
    with google_token_file_lock(token_path):
        _write_google_credentials_unlocked(creds, token_path=token_path)


def _backup_google_token(token_path: Path) -> Path | None:
    token_path = token_path.expanduser()
    if not token_path.exists():
        return None
    backup_path = token_path.with_name(
        f"{token_path.name}.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )
    shutil.copy2(token_path, backup_path)
    try:
        backup_path.chmod(0o600)
    except Exception:
        pass
    return backup_path


def _google_auth_required_message(exc: Exception, *, token_path: Path, write: bool) -> str:
    text = str(exc)
    scope_text = "寫入" if write else "唯讀"
    if "invalid_grant" in text or "expired or revoked" in text.lower():
        return f"Google Drive {scope_text} refresh token 已被 Google 判定過期或撤銷，必須重新授權：{token_path}"
    return f"Google Drive 授權已失效，請重新授權：{token_path}"


def _load_google_credentials(*, interactive: bool = False, force_auth: bool = False, write: bool = False):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception as exc:  # pragma: no cover - import depends on runtime extras
        raise DriveCaseSyncError(f"Google API 套件未安裝：{exc}") from exc

    scopes = WRITE_SCOPES if write else READONLY_SCOPES
    token_path = drive_sync_token_path(write=write)
    credentials_path = drive_sync_credentials_path()
    account_hint = drive_sync_account_hint()
    creds = None
    deferred_auth_error: Exception | None = None
    if force_auth and interactive:
        _backup_google_token(token_path)
        token_path.unlink(missing_ok=True)
    if token_path.exists():
        with google_token_file_lock(token_path):
            read_failed = False
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), scopes)
            except Exception as exc:
                read_exc = exc
                creds = None
                read_failed = True
            else:
                read_exc = None
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    _write_google_credentials_unlocked(creds, token_path=token_path)
                except Exception as exc:
                    read_exc = exc
                    creds = None
        if read_exc is not None and creds is None:
            exc = read_exc
            if not interactive and write and read_failed:
                raise DriveCaseSyncAuthRequired(
                    f"Google Drive 授權檔無法讀取，請重新授權：{token_path}",
                    token_path=token_path,
                    write=write,
                ) from exc
            deferred_auth_error = exc
            if interactive:
                token_path.unlink(missing_ok=True)
            creds = None
    if not write and (not creds or not creds.valid or not creds.has_scopes(scopes)):
        fallback_path = drive_sync_token_path(write=True)
        if fallback_path.exists():
            try:
                with google_token_file_lock(fallback_path):
                    fallback = Credentials.from_authorized_user_file(str(fallback_path), WRITE_SCOPES)
                    if fallback.expired and fallback.refresh_token:
                        fallback.refresh(Request())
                        _write_google_credentials_unlocked(fallback, token_path=fallback_path)
                if fallback and fallback.valid and fallback.has_scopes(WRITE_SCOPES):
                    return fallback
            except Exception as exc:
                deferred_auth_error = deferred_auth_error or exc
    if not creds or not creds.valid or not creds.has_scopes(scopes):
        if not interactive:
            if deferred_auth_error:
                raise DriveCaseSyncAuthRequired(
                    _google_auth_required_message(deferred_auth_error, token_path=token_path, write=write),
                    token_path=token_path,
                    write=write,
                ) from deferred_auth_error
            scope_text = "Google Drive 寫入" if write else "Google Drive 唯讀"
            raise DriveCaseSyncAuthRequired(
                f"尚未授權 {scope_text}。請先以 --auth 建立 "
                f"{'MAGI_DRIVE_SYNC_WRITE_TOKEN' if write else 'MAGI_DRIVE_SYNC_TOKEN'}。",
                token_path=token_path,
                write=write,
            )
        if not credentials_path.exists():
            raise DriveCaseSyncError(f"找不到 Google OAuth credentials：{credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
        open_browser = str(os.environ.get("MAGI_DRIVE_SYNC_OAUTH_OPEN_BROWSER", "1") or "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "y",
        }
        creds = flow.run_local_server(
            port=0,
            open_browser=open_browser,
            prompt="consent",
            login_hint=account_hint,
            authorization_prompt_message=(
                f"請用 {account_hint} 授權 MAGI {'上傳缺檔到' if write else '唯讀盤點'}雲端案件資料夾：{{url}}"
            ),
        )
        _write_google_credentials(creds, token_path=token_path)
    return creds


def build_drive_service(*, interactive: bool = False, force_auth: bool = False, write: bool = False):
    test_mode = str(os.environ.get("MAGI_TEST_MODE") or "").strip().lower() in {"1", "true", "yes", "on"}
    live_tests = str(os.environ.get("MAGI_ENABLE_LIVE_TESTS") or "").strip().lower() in {"1", "true", "yes", "on"}
    write_enabled = str(os.environ.get("MAGI_DRIVE_SYNC_ENABLE_WRITE") or "").strip().lower() in {"1", "true", "yes", "on"}
    if test_mode and not live_tests:
        raise DriveCaseSyncError("Google Drive service construction is blocked in ordinary pytest")
    if test_mode and write and not write_enabled:
        raise DriveCaseSyncError("Google Drive write service requires MAGI_DRIVE_SYNC_ENABLE_WRITE=1")
    try:
        from googleapiclient.discovery import build
        import google_auth_httplib2
        import httplib2
    except Exception as exc:  # pragma: no cover - import depends on runtime extras
        raise DriveCaseSyncError(f"Google Drive API 套件未安裝：{exc}") from exc
    timeout = int(
        os.environ.get("MAGI_DRIVE_SYNC_HTTP_TIMEOUT")
        or os.environ.get("MAGI_DRIVE_SYNC_DRIVE_LIST_TIMEOUT_SEC")
        or "30"
    )
    timeout = max(5, min(timeout, 120))
    creds = _load_google_credentials(interactive=interactive, force_auth=force_auth, write=write)
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))
    return build("drive", "v3", http=http, cache_discovery=False)


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("臺", "台")
    text = re.sub(r"\s+", "", text)
    return text.lower()


def _clean_exclusion_path(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = text.replace("\\", "/")
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip("/")


def normalize_court_case_no(value: str) -> str:
    text = normalize_text(value)
    match = ROC_COURT_NO_RE.search(text)
    if not match:
        return text
    return f"{int(match.group(1))}年度{match.group(2)}字第{int(match.group(3))}號"


def normalize_context_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("臺", "台")
    return text


@lru_cache(maxsize=1)
def load_case_aliases() -> dict[str, list[str]]:
    """Load local-only legacy Drive aliases.

    The alias file intentionally lives under runtime by default so private
    client/case nicknames do not have to be committed to git.
    """
    aliases: dict[str, list[str]] = {}
    raw_sources: list[Any] = []
    env_raw = os.environ.get("MAGI_DRIVE_SYNC_CASE_ALIASES_JSON", "").strip()
    if env_raw:
        try:
            raw_sources.append(json.loads(env_raw))
        except Exception:
            pass
    alias_path = case_alias_file_path()
    if alias_path.exists():
        try:
            raw_sources.append(json.loads(alias_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    for raw in raw_sources:
        if not isinstance(raw, dict):
            continue
        for key, values in raw.items():
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                continue
            normalized_key = normalize_text(key)
            cleaned = [str(v).strip() for v in values if str(v or "").strip()]
            if normalized_key and cleaned:
                aliases.setdefault(normalized_key, [])
                for value in cleaned:
                    if value not in aliases[normalized_key]:
                        aliases[normalized_key].append(value)
    return aliases


def expand_alias_values(values: Iterable[Any]) -> list[str]:
    aliases = load_case_aliases()
    expanded: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_text(value)
        for key, targets in aliases.items():
            if key and key in text:
                for target in targets:
                    if target not in seen:
                        seen.add(target)
                        expanded.append(target)
    return expanded


@lru_cache(maxsize=1)
def load_case_exclusions() -> set[str]:
    """Load local-only Drive case paths excluded from sync scope."""
    return {normalize_text(path) for path in load_case_exclusion_payload().get("relative_paths", [])}


def load_case_exclusion_payload(*, include_env: bool = True, exclusion_path: Path | None = None) -> dict[str, Any]:
    """Load case exclusion metadata payload from configured runtime file."""
    raw_sources: list[Any] = []
    if include_env:
        env_raw = os.environ.get("MAGI_DRIVE_SYNC_CASE_EXCLUSIONS_JSON", "").strip()
        if env_raw:
            try:
                raw_sources.append(json.loads(env_raw))
            except Exception:
                pass
    target = exclusion_path or case_exclusion_file_path()
    if target.exists():
        try:
            raw_sources.append(json.loads(target.read_text(encoding="utf-8")))
        except Exception:
            pass

    path_values: list[str] = []
    seen: set[str] = set()
    reason = ""
    updated_at = ""

    for raw in raw_sources:
        if isinstance(raw, dict):
            if not reason:
                reason = str(raw.get("reason") or "").strip()
            if not updated_at:
                updated_at = str(raw.get("updated_at") or "").strip()
            values = raw.get("relative_paths") or raw.get("paths") or raw.get("drive_paths") or []
        elif isinstance(raw, list):
            values = raw
        else:
            continue
        if not isinstance(values, list):
            continue
        for value in values:
            cleaned = _clean_exclusion_path(value)
            normalized_key = normalize_text(cleaned)
            if cleaned and normalized_key and normalized_key not in seen:
                seen.add(normalized_key)
                path_values.append(cleaned)

    return {
        "updated_at": updated_at,
        "reason": reason,
        "relative_paths": path_values,
    }


def _write_case_exclusion_payload(payload: dict[str, Any], *, exclusion_path: Path | None = None) -> Path:
    """Atomically write Drive case exclusion payload and keep permissions stable."""
    target = exclusion_path or case_exclusion_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(".tmp")
    data = {
        "updated_at": str(payload.get("updated_at") or datetime.now().astimezone().isoformat()),
        "reason": str(payload.get("reason") or ""),
        "relative_paths": list(dict.fromkeys([_clean_exclusion_path(value) for value in payload.get("relative_paths", []) if _clean_exclusion_path(value)])),
    }
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(target.parent), delete=False) as tmp:
            tmp.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            tmp.flush()
            try:
                os.fsync(tmp.fileno())
            except Exception:
                pass
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, target)
        try:
            target.chmod(0o600)
        except Exception:
            pass
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    load_case_exclusions.cache_clear()
    return target


def sync_case_exclusions(
    relative_paths: Iterable[Any],
    *,
    reason: str = "",
    exclusion_path: Path | None = None,
) -> dict[str, Any]:
    """Merge and persist exclusion paths with normalization and deduplication."""
    payload = load_case_exclusion_payload(include_env=False, exclusion_path=exclusion_path)
    keep: list[str] = list(payload.get("relative_paths") or [])
    seen = {normalize_text(path) for path in keep}
    changed = False
    for path in relative_paths or []:
        cleaned = _clean_exclusion_path(path)
        normalized = normalize_text(cleaned)
        if normalized and normalized not in seen:
            seen.add(normalized)
            keep.append(cleaned)
            changed = True

    if not changed:
        return {
            "updated_at": payload.get("updated_at", ""),
            "reason": payload.get("reason") or "manual update",
            "relative_paths": keep,
        }

    out = {
        "updated_at": datetime.now().astimezone().isoformat(),
        "reason": str(reason).strip() or payload.get("reason") or "manual update",
        "relative_paths": keep,
    }
    _write_case_exclusion_payload(out, exclusion_path=exclusion_path)
    return out


def unsync_case_exclusions(
    relative_paths: Iterable[Any],
    *,
    exclusion_path: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Remove exclusion paths and persist normalized remainder."""
    payload = load_case_exclusion_payload(include_env=False, exclusion_path=exclusion_path)
    before = list(payload.get("relative_paths") or [])
    remove_targets = {normalize_text(path) for path in relative_paths or [] if normalize_text(path)}
    kept = [path for path in before if normalize_text(path) not in remove_targets]
    removed_count = len(before) - len(kept)
    if removed_count == 0:
        return (
            {
                "updated_at": payload.get("updated_at", ""),
                "reason": payload.get("reason", ""),
                "relative_paths": before,
            },
            0,
        )

    out = {
        "updated_at": datetime.now().astimezone().isoformat(),
        "reason": payload.get("reason") or "manual update",
        "relative_paths": kept,
    }
    _write_case_exclusion_payload(out, exclusion_path=exclusion_path)
    return out, removed_count


def is_drive_case_excluded(case: CaseFolder) -> bool:
    if case.source != "drive":
        return False
    return normalize_text(case.relative_path) in load_case_exclusions()


def _trim_context_suffix(term: str) -> str:
    out = str(term or "").strip()
    changed = True
    while changed:
        changed = False
        for suffix in CONTEXT_SUFFIXES:
            if out.endswith(suffix) and len(out) > len(suffix) + 1:
                out = out[: -len(suffix)]
                changed = True
                break
    return out.strip()


def meaningful_terms(values: Iterable[Any]) -> list[str]:
    """Extract conservative human/case hint tokens from folder/file/DB text."""
    seen: set[str] = set()
    terms: list[str] = []
    for value in values:
        text = normalize_context_text(value)
        for court_no in COURT_CASE_RE.findall(text):
            key = normalize_court_case_no(court_no)
            if key and key not in seen:
                seen.add(key)
                terms.append(key)
        for osc_no in OSC_CASE_RE.findall(text):
            key = normalize_text(osc_no)
            if key and key not in seen:
                seen.add(key)
                terms.append(key)
        for laf_no in LAF_CASE_RE.findall(text):
            key = normalize_text(laf_no)
            if key and key not in seen:
                seen.add(key)
                terms.append(key)
        for run in CJK_RUN_RE.findall(text):
            candidates = [run, _trim_context_suffix(run)]
            if len(run) >= 4:
                candidates.append(run[:3])
            for candidate in candidates:
                token = _trim_context_suffix(candidate)
                if len(token) < 2 or token in GENERIC_CONTEXT_TERMS:
                    continue
                if any(generic in token and len(token) > 8 for generic in GENERIC_CONTEXT_TERMS):
                    continue
                key = normalize_text(token)
                if key in seen:
                    continue
                seen.add(key)
                terms.append(token)
    return terms


def _contains_term(haystack: str, term: str) -> bool:
    key = normalize_text(term)
    return bool(key and key in haystack)


def is_decisive_context_term(term: str) -> bool:
    text = str(term or "").strip()
    if not text or text in GENERIC_CONTEXT_TERMS:
        return False
    if text in CONTEXT_DECISIVE_ALLOWLIST:
        return True
    normalized = normalize_text(text)
    if OSC_CASE_RE.fullmatch(text) or LAF_CASE_RE.fullmatch(text) or ROC_COURT_NO_RE.search(text):
        return True
    if any(normalize_text(fragment) in normalized for fragment in NON_DECISIVE_CONTEXT_SUBSTRINGS):
        return False
    if len(text) > 6:
        return False
    return bool(CJK_RUN_RE.fullmatch(text))


def _clean_folder_token(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\d+[.．、_ ]+", "", text)
    return text.strip(" -_－—")


def drive_case_kind_bucket_name(value: str) -> str:
    token = _clean_folder_token(value)
    return DRIVE_CASE_KIND_BUCKETS.get(token, "")


def infer_case_kind(category: str, name: str, relative_path: str = "") -> tuple[str, str]:
    text = f"{category} {name} {relative_path}"
    if "消債" in text or "債務清理" in text or "更生" in text or "清算" in text:
        return "消費者債務清理", "high"
    if "刑事" in text or any(k in text for k in ("詐欺", "毒品", "殺人", "傷害", "公共危險", "洗錢", "妨害性自主", "貪污")):
        return "刑事", "medium"
    if "行政" in text or "處遇" in text or "勞工保險" in text:
        return "行政", "medium"
    if "法律顧問" in text or "顧問" in text:
        return "法律顧問", "medium"
    if "非訟" in text or "支付命令" in text or "本票" in text:
        return "非訟", "medium"
    if category == "指定辯護案件":
        return "刑事", "high"
    if category in {"一般案件", "無償案件", "法扶案件"}:
        return "", "needs_review"
    return "", "needs_review"


def extract_case_meta(name: str) -> CaseMeta:
    raw = _clean_folder_token(name)
    case_number = (OSC_CASE_RE.search(raw) or [""])[0] if OSC_CASE_RE.search(raw) else ""
    laf_case_no = (LAF_CASE_RE.search(raw) or [""])[0] if LAF_CASE_RE.search(raw) else ""
    court_case_no = (COURT_CASE_RE.search(raw) or [""])[0] if COURT_CASE_RE.search(raw) else ""

    client = ""
    reason = ""
    if case_number:
        tail = raw.split(case_number, 1)[1].strip(" -_－—")
        parts = [p for p in re.split(r"[-－—]", tail) if p.strip()]
        if parts:
            client = _clean_folder_token(parts[0])
            if len(parts) >= 3:
                reason = _clean_folder_token(parts[-1])
    elif laf_case_no:
        head, tail = raw.split(laf_case_no, 1)
        client = _clean_folder_token(head)
        parts = [p for p in re.split(r"[-－—]", tail.strip(" -_－—")) if p.strip()]
        if parts:
            reason = _clean_folder_token(parts[-1])
    elif "(" in raw or "（" in raw:
        m = re.match(r"(.+?)[(（](.+?)[)）]", raw)
        if m:
            client = _clean_folder_token(m.group(1))
            reason = _clean_folder_token(m.group(2))
    else:
        parts = [p for p in re.split(r"[-－—]", raw) if p.strip()]
        if parts:
            client = _clean_folder_token(parts[0])
            if len(parts) > 1:
                reason = _clean_folder_token(parts[-1])
    return CaseMeta(
        case_number=case_number,
        laf_case_no=laf_case_no,
        court_case_no=court_case_no,
        client_hint=client,
        reason_hint=reason,
    )


def match_keys(meta: CaseMeta) -> list[str]:
    keys: list[str] = []
    if meta.case_number:
        keys.append(f"case:{normalize_text(meta.case_number)}")
    if meta.laf_case_no:
        keys.append(f"laf:{normalize_text(meta.laf_case_no)}")
    if meta.court_case_no:
        keys.append(f"court:{normalize_court_case_no(meta.court_case_no)}")
    name_variants: list[str] = []
    for candidate in (meta.client_hint, _trim_context_suffix(meta.client_hint)):
        normalized = normalize_text(candidate)
        if normalized and normalized not in name_variants:
            name_variants.append(normalized)
    reason = normalize_text(meta.reason_hint)
    for name in name_variants:
        if name and reason:
            keys.append(f"name_reason:{name}|{reason}")
        if name:
            keys.append(f"name:{name}")
    return keys


def classify_drive_case_folder(relative_path: str) -> dict[str, str] | None:
    parts = [p for p in Path(relative_path).parts if p not in {"", "."}]
    if not parts:
        return None
    top = parts[0]
    status = "active"
    category = top
    owner = ""
    case_idx: int | None = None
    case_kind = ""

    if top in {"一般案件", "法扶案件", "無償案件"} and len(parts) >= 3 and parts[1] in DEFAULT_OWNER_BUCKETS:
        owner = parts[1]
        bucket_kind = drive_case_kind_bucket_name(parts[2])
        if bucket_kind:
            if len(parts) == 3:
                return None
            case_kind = bucket_kind
            case_idx = 3
        else:
            case_idx = 2
    elif top == "結案案件" and len(parts) >= 3:
        status = "closed"
        category = parts[1]
        if len(parts) >= 4 and parts[2] in DEFAULT_OWNER_BUCKETS:
            owner = parts[2]
            bucket_kind = drive_case_kind_bucket_name(parts[3])
            if bucket_kind:
                if len(parts) == 4:
                    return None
                case_kind = bucket_kind
                case_idx = 4
            else:
                case_idx = 3
        elif parts[2] in DEFAULT_OWNER_BUCKETS:
            return None
        else:
            case_idx = 2
    elif top in {"指定辯護案件", "代庭案件"} and len(parts) >= 2:
        case_idx = 1
    elif top in {"縣府調解案件", "諮詢案件"} and len(parts) >= 3:
        case_kind = parts[1]
        case_idx = 2

    if case_idx is None or len(parts) != case_idx + 1:
        return None
    return {
        "category": category,
        "status": status,
        "owner_bucket": owner,
        "case_kind": case_kind,
    }


def drive_case_sync_exclusion_reason(
    relative_path: str,
    *,
    require_canonical_layout: bool = False,
) -> str:
    """Return a fail-closed reason for Drive paths that are never canonical.

    Duplicate repair intentionally moves old shells below ``MAGI待整理`` and
    gives them review-only names. Broad Drive name search can still find those
    folders by OSC/LAF number, so every resolver must reject them before score
    or identity metadata can promote them back into bidirectional sync.
    """
    cleaned = _clean_exclusion_path(relative_path)
    if not cleaned:
        return "empty_drive_case_path"
    parts = [normalize_text(part) for part in PurePosixPath(cleaned).parts if part not in {"", "."}]
    if any(
        "待整理" in part
        or "重複案件資料夾" in part
        or "重複案件隔離" in part
        for part in parts
    ):
        return "drive_review_quarantine_path"
    joined = "/".join(parts)
    if any(
        marker in joined
        for marker in (
            "重複副本",
            "drive重複來源-",
            "同名資料夾-",
        )
    ) or any(part in {".duplicates", ".trash", "#recycle"} for part in parts):
        return "drive_duplicate_copy_path"
    if any(
        (("衝突" in part or "conflict" in part) and ("副本" in part or "copy" in part or "隔離" in part))
        for part in parts
    ):
        return "drive_conflict_copy_path"
    if require_canonical_layout and classify_drive_case_folder(cleaned) is None:
        return "drive_noncanonical_case_layout"
    return ""


def classify_local_case_folder(relative_path: str, *, status: str) -> dict[str, str] | None:
    parts = [p for p in Path(relative_path).parts if p not in {"", "."}]
    if len(parts) != 3:
        return None
    category, case_kind, _case_name = parts
    if category not in LOCAL_CASE_CATEGORIES:
        return None
    if case_kind not in LOCAL_CASE_KIND_FOLDERS:
        return None
    return {
        "category": category,
        "status": status,
        "owner_bucket": "",
        "case_kind": case_kind,
    }


def _ignore_name(name: str) -> bool:
    return name in SYNC_IGNORE_NAMES or any(name.startswith(p) for p in SYNC_IGNORE_PREFIXES)


def _first_existing_homes_case_root() -> Path:
    explicit = os.environ.get("MAGI_NAS_CASE_ROOT") or os.environ.get("MAGI_V3_CASE_ROOT")
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.exists() else Path()
    if os.name == "nt":
        return Path()
    homes_root = Path("/Volumes/homes")
    if not homes_root.exists():
        return Path()
    try:
        candidates = sorted(homes_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return Path()
    for child in candidates:
        case_root = child / "01_案件"
        if case_root.exists():
            return case_root
    return Path()


def default_active_case_roots() -> list[Path]:
    env = os.environ.get("MAGI_DRIVE_SYNC_ACTIVE_CASE_ROOT") or os.environ.get("MAGI_ACTIVE_CASE_ROOT")
    if env:
        p = Path(env).expanduser()
        return [p] if p.exists() else []
    real_nas = _first_existing_homes_case_root()
    if real_nas.exists():
        return [real_nas]
    cloud = Path.home() / "Library/CloudStorage/SynologyDrive-homes/01_案件"
    return [cloud] if cloud.exists() else []


def default_closed_case_roots() -> list[Path]:
    env = os.environ.get("MAGI_DRIVE_SYNC_CLOSED_CASE_ROOT") or os.environ.get("MAGI_CLOSED_CASE_ROOT")
    if env:
        p = Path(env).expanduser()
        return [p] if p.exists() else []
    from api.case_path_mapper import preferred_case_roots

    mapped = [Path(item) for item in preferred_case_roots(include_closed=True)]
    if len(mapped) > 1 and mapped[-1].exists():
        return [mapped[-1]]
    cloud = Path.home() / "Library/CloudStorage/SynologyDrive-homes/03_工作資料/10_結案"
    return [cloud] if cloud.exists() else []


def _default_active_canonical_prefix() -> str:
    real_nas = _first_existing_homes_case_root()
    parts = real_nas.parts
    if len(parts) >= 5 and parts[1:3] == ("Volumes", "homes") and parts[-1] == "01_案件":
        return f"Z:/{parts[3]}/01_案件"
    return "Z:/<NAS_ACCOUNT>/01_案件"


def canonical_base_for_status(status: str) -> str:
    if status == "closed":
        closed_share = os.environ.get("MAGI_NAS_CLOSED_SHARE_NAME") or "lumi"
        default_closed = f"Y:/{closed_share}/03_工作資料/10_結案"
        return (os.environ.get("MAGI_CANONICAL_CLOSED_CASE_PREFIX") or default_closed).replace("\\", "/").rstrip("/")
    return (os.environ.get("MAGI_CANONICAL_ACTIVE_CASE_PREFIX") or _default_active_canonical_prefix()).replace("\\", "/").rstrip("/")


def suggest_canonical_path(case: CaseFolder) -> tuple[str, str, str]:
    standard_categories = {"一般案件", "法扶案件", "無償案件", "指定辯護案件"}
    if case.category not in standard_categories:
        return "", "needs_review", "非 OSC 標準案件根目錄，需先決定要同步到哪個 NAS 根目錄"
    case_kind = case.case_kind
    confidence = "high" if case_kind else ""
    if not case_kind:
        case_kind, confidence = infer_case_kind(case.category, case.name, case.relative_path)
    if not case_kind:
        return "", "needs_review", "缺少案件種類，不能安全推定 NAS 路徑"
    base = canonical_base_for_status(case.status)
    return f"{base}/{case.category}/{case_kind}/{case.name}", confidence, ""


def drive_owner_bucket() -> str:
    """Default Google Drive owner bucket for NAS-created OSC folders."""
    return (
        os.environ.get("MAGI_DRIVE_SYNC_OWNER_BUCKET")
        or os.environ.get("MAGI_DRIVE_OWNER_BUCKET")
        or "Lumi"
    ).strip() or "Lumi"


def drive_case_kind_bucket_for_local_case(case_kind: str) -> str:
    return DRIVE_CASE_KIND_BUCKET_BY_CASE_KIND.get(str(case_kind or "").strip(), "")


def normalize_drive_case_category(category: str) -> str:
    text = str(category or "").strip()
    if text in {"法律扶助案件", "法扶", "法扶案"}:
        return "法扶案件"
    return text


def drive_case_display_name_for_local_case(case: CaseFolder) -> str:
    """Drive-facing case folder name without OSC's internal case number."""
    name = (case.name or "").strip()
    case_number = (case.meta.case_number or extract_case_meta(name).case_number or "").strip()
    if case_number and name.startswith(case_number):
        stripped = re.sub(rf"^{re.escape(case_number)}[\s_\-－—]*", "", name).strip()
        if stripped:
            name = stripped
    category = normalize_drive_case_category(case.category)
    laf_case_no = (case.meta.laf_case_no or extract_case_meta(name).laf_case_no or "").strip()
    if category == "法扶案件" and laf_case_no and laf_case_no not in name:
        client = (case.meta.client_hint or extract_case_meta(name).client_hint or "").strip()
        descriptor = name
        if client and descriptor.startswith(client):
            descriptor = re.sub(rf"^{re.escape(client)}[\s_\-－—]*", "", descriptor).strip()
        case_kind = (case.case_kind or "").strip()
        if case_kind == "消費者債務清理":
            return "-".join(p for p in [client or name, laf_case_no, "消費者債務清理事件", "消費者債務清理事件"] if p)
        if case_kind == "刑事":
            chunks = [p for p in re.split(r"[\-_－—]+", descriptor) if p]
            stage = chunks[0] if chunks else ""
            reason = "-".join(chunks[1:]) if len(chunks) > 1 else ""
            stage_label = {
                "偵查": "刑事偵查中辯護",
                "一審": "刑事一審辯護",
                "二審": "刑事二審辯護",
                "三審": "刑事三審辯護",
                "更審": "刑事更審辯護",
            }.get(stage, "")
            if stage_label:
                return "-".join(p for p in [client or name, laf_case_no, stage_label, reason] if p)
        if descriptor and case_kind and not descriptor.startswith(case_kind):
            descriptor = f"{case_kind}{descriptor}"
        parts = [client or name, laf_case_no]
        if descriptor:
            parts.append(descriptor)
        return "-".join(p for p in parts if p)
    return name


def drive_relative_path_for_local_case(case: CaseFolder, *, owner_bucket: str | None = None) -> str:
    """Return the native Google Drive case folder path for a NAS case.

    NAS/OSC stores cases as `<category>/<case_kind>/<case-folder>`, while the
    shared Drive stores normal cases under an owner bucket and only uses special
    buckets for a small number of case kinds, such as 消債 and 陪偵.
    """
    category = normalize_drive_case_category(case.category)
    case_kind = (case.case_kind or "").strip()
    status = (case.status or "active").strip() or "active"
    name = drive_case_display_name_for_local_case(case)
    if not category or not name:
        return ""
    if not (case.meta.case_number or extract_case_meta(name).case_number):
        return ""
    if category == "指定辯護案件":
        if status == "closed":
            return PurePosixPath("結案案件", category, name).as_posix()
        return PurePosixPath(category, name).as_posix()
    if category not in {"一般案件", "法扶案件", "無償案件"}:
        return ""
    owner = (owner_bucket if owner_bucket is not None else drive_owner_bucket()).strip()
    if not owner:
        return ""
    parts: list[str] = []
    if status == "closed":
        parts.extend(["結案案件", category, owner])
    else:
        parts.extend([category, owner])
    special_bucket = drive_case_kind_bucket_for_local_case(case_kind)
    if special_bucket:
        parts.append(special_bucket)
    parts.append(name)
    return PurePosixPath(*parts).as_posix()


def _parse_modified_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def case_modified_within_hours(case: CaseFolder, max_age_hours: int) -> bool:
    if max_age_hours <= 0:
        return True
    modified = _parse_modified_time(case.modified_time)
    if not modified:
        return False
    return datetime.now(timezone.utc) - modified <= timedelta(hours=max_age_hours)


def local_file_entries(root: Path, *, status: str, max_depth: int, max_items: int) -> tuple[list[FileEntry], list[CaseFolder]]:
    entries = BoundedEntries()
    cases: list[CaseFolder] = []
    stack: list[Path] = [root]
    while stack and len(entries) < max_items:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                children = sorted(list(it), key=lambda e: e.name)
        except OSError:
            continue
        for child in children:
            if _ignore_name(child.name):
                continue
            try:
                rel = Path(child.path).relative_to(root).as_posix()
                depth = len(Path(rel).parts)
                st = child.stat()
                is_dir = child.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if depth > max_depth:
                continue
            entry = FileEntry(
                source="nas",
                path=child.path,
                relative_path=rel,
                name=child.name,
                is_folder=is_dir,
                modified_time=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                size=None if is_dir else int(st.st_size),
            )
            entries.append(entry)
            if is_dir:
                cls = classify_local_case_folder(rel, status=status)
                if cls:
                    cases.append(CaseFolder(
                        source="nas",
                        path=child.path,
                        local_path=child.path,
                        relative_path=rel,
                        name=child.name,
                        category=cls["category"],
                        status=cls["status"],
                        case_kind=cls["case_kind"],
                        owner_bucket=cls["owner_bucket"],
                        modified_time=entry.modified_time,
                        meta=extract_case_meta(child.name),
                    ))
                # Phase 1 is a case-folder inventory.  Do not descend into case
                # folders by default; deep file comparison should run later on
                # confirmed mappings to avoid unnecessary NAS load.
                if (not cls) and depth < max_depth:
                    stack.append(Path(child.path))
                elif not cls:
                    entries.truncated = True
            if len(entries) >= max_items:
                entries.truncated = True
                break
    return entries, cases


def _drive_list_children(service: Any, folder_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    token = None
    fields = (
        "nextPageToken, files(id,name,mimeType,parents,modifiedTime,size,md5Checksum,"
        "webViewLink,driveId,shortcutDetails,appProperties)"
    )
    while True:
        request = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            corpora="allDrives",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=1000,
            pageToken=token,
            fields=fields,
        )
        resp = _drive_execute_with_timeout(request, context=f"list_children:{folder_id}")
        out.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            return sorted(out, key=lambda x: str(x.get("name") or ""))


def _drive_execute_with_timeout(request: Any, *, context: str) -> dict[str, Any]:
    """Execute a Drive request without creating unkillable SSL worker threads.

    Previous builds wrapped every Google API call in a daemon thread and joined
    with a timeout.  When the join timed out, Python could not actually cancel
    the native SSL read, so orphaned threads accumulated inside OpenSSL and
    eventually crashed the whole process with libmalloc heap corruption.  The
    Drive service already has an httplib2 socket timeout; the sync worker also
    has a process-level SIGALRM/subprocess timeout.  Keep the old thread wrapper
    only as an explicit diagnostic opt-in.
    """
    timeout = float(os.environ.get("MAGI_DRIVE_SYNC_DRIVE_LIST_TIMEOUT_SEC") or "20")
    retries = max(0, int(os.environ.get("MAGI_DRIVE_SYNC_API_RETRIES", "1") or "1"))
    legacy_thread_timeout = str(
        os.environ.get("MAGI_DRIVE_SYNC_LEGACY_THREAD_TIMEOUT") or ""
    ).strip().lower() in {"1", "true", "yes", "on", "y"}
    def _execute_request() -> dict[str, Any]:
        try:
            value = request.execute(num_retries=retries)
        except TypeError:
            value = request.execute()
        return value if isinstance(value, dict) else {}

    if not legacy_thread_timeout:
        can_alarm = (
            timeout > 0
            and hasattr(signal, "SIGALRM")
            and threading.current_thread() is threading.main_thread()
        )
        if not can_alarm:
            return _execute_request()

        def _handle_timeout(_signum: int, _frame: Any) -> None:
            raise DriveCaseSyncError(f"drive_api_timeout:{context}:{timeout:g}s")

        previous_handler = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        if hasattr(signal, "setitimer"):
            previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, _handle_timeout)
            signal.setitimer(signal.ITIMER_REAL, max(0.01, timeout))
            try:
                return _execute_request()
            finally:
                elapsed = time.monotonic() - started
                delay = max(0.0, float(previous_timer[0] or 0.0) - elapsed)
                interval = float(previous_timer[1] or 0.0)
                signal.setitimer(signal.ITIMER_REAL, delay, interval)
                signal.signal(signal.SIGALRM, previous_handler)

        previous_alarm = signal.alarm(0)
        try:
            signal.signal(signal.SIGALRM, _handle_timeout)
            signal.alarm(max(1, int(timeout)))
            return _execute_request()
        finally:
            signal.alarm(0)
            elapsed = time.monotonic() - started
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_alarm:
                signal.alarm(max(1, int(max(0.0, float(previous_alarm) - elapsed))))

    result: dict[str, Any] = {"done": False, "value": None, "error": None}

    def _execute() -> None:
        try:
            try:
                result["value"] = request.execute(num_retries=retries)
            except TypeError:
                result["value"] = request.execute()
        except Exception as exc:
            result["error"] = exc
        result["done"] = True

    worker = threading.Thread(target=_execute, daemon=True, name="drive-api-legacy-execute")
    worker.start()
    worker.join(timeout=max(0.5, timeout))
    if not result["done"]:
        raise DriveCaseSyncError(f"drive_api_timeout:{context}:{timeout:g}s")
    if result["error"] is not None:
        raise result["error"]
    value = result.get("value")
    return value if isinstance(value, dict) else {}


def drive_descendant_context(
    service: Any,
    folder_id: str,
    *,
    max_depth: int = 3,
    max_items: int = 300,
) -> list[FileEntry]:
    """Read a bounded set of names under one Drive case folder for disambiguation."""
    entries = BoundedEntries()
    stack: list[tuple[str, str, int]] = [(folder_id, "", 0)]
    while stack and len(entries) < max_items:
        parent_id, parent_rel, depth = stack.pop()
        if depth >= max_depth:
            continue
        for item in _drive_list_children(service, parent_id):
            name = str(item.get("name") or "")
            if _ignore_name(name):
                continue
            rel = f"{parent_rel}/{name}".strip("/")
            is_folder = item.get("mimeType") == GOOGLE_FOLDER_MIME
            entries.append(FileEntry(
                source="drive",
                path=rel,
                relative_path=rel,
                name=name,
                is_folder=is_folder,
                modified_time=str(item.get("modifiedTime") or ""),
                size=int(item["size"]) if str(item.get("size") or "").isdigit() else None,
                md5=str(item.get("md5Checksum") or ""),
                drive_id=str(item.get("id") or ""),
                web_url=str(item.get("webViewLink") or ""),
                mime_type=str(item.get("mimeType") or ""),
            ))
            if is_folder and depth + 1 < max_depth:
                stack.append((str(item["id"]), rel, depth + 1))
            elif is_folder:
                entries.truncated = True
            if len(entries) >= max_items:
                entries.truncated = True
                break
    return entries


def _safe_local_is_dir(path: str, *, timeout_sec: float | None = None) -> bool:
    timeout = float(
        timeout_sec
        if timeout_sec is not None
        else os.environ.get("MAGI_DRIVE_SYNC_LOCAL_SCAN_TIMEOUT_SEC") or DEFAULT_LOCAL_SCAN_TIMEOUT_SEC
    )
    result = {"done": False, "value": False}

    def _check() -> None:
        try:
            result["value"] = os.path.isdir(path)
        except OSError:
            result["value"] = False
        result["done"] = True

    worker = threading.Thread(target=_check, daemon=True)
    worker.start()
    worker.join(timeout=max(0.1, timeout))
    if not result["done"]:
        raise DriveCaseSyncError(f"local_dir_probe_timeout:{path}")
    return bool(result["value"])


def _safe_local_dir_children(path: str, *, timeout_sec: float | None = None) -> list[dict[str, Any]]:
    """Return stat'd children or raise before blocking the whole worker.

    NAS SMB and macOS File Provider can occasionally block in os.scandir/stat.
    A timeout is treated as a case-level scan error, not as an empty folder; this
    prevents false "NAS missing" actions and duplicate downloads.
    """
    timeout = float(
        timeout_sec
        if timeout_sec is not None
        else os.environ.get("MAGI_DRIVE_SYNC_LOCAL_SCAN_TIMEOUT_SEC") or DEFAULT_LOCAL_SCAN_TIMEOUT_SEC
    )
    result: dict[str, Any] = {"done": False, "children": [], "error": None}

    def _scan() -> None:
        children: list[dict[str, Any]] = []
        try:
            with os.scandir(path) as it:
                dirents = sorted(list(it), key=lambda e: e.name)
            for child in dirents:
                if _ignore_name(child.name):
                    continue
                try:
                    is_dir = child.is_dir(follow_symlinks=False)
                    st = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                children.append({
                    "name": child.name,
                    "path": child.path,
                    "is_dir": is_dir,
                    "mtime": float(st.st_mtime),
                    "size": None if is_dir else int(st.st_size),
                })
        except Exception as exc:
            result["error"] = exc
        result["children"] = children
        result["done"] = True

    worker = threading.Thread(target=_scan, daemon=True)
    worker.start()
    worker.join(timeout=max(0.1, timeout))
    if not result["done"]:
        raise DriveCaseSyncError(f"local_scandir_timeout:{path}")
    if result["error"] is not None:
        raise result["error"]
    return list(result["children"] or [])


def local_descendant_context(
    root: str,
    *,
    max_depth: int = 3,
    max_items: int = 300,
) -> list[FileEntry]:
    entries = BoundedEntries()
    base = Path(root)
    if not _safe_local_is_dir(str(base)):
        return entries
    stack: list[Path] = [base]
    while stack and len(entries) < max_items:
        cur = stack.pop()
        children = _safe_local_dir_children(str(cur))
        for child in children:
            try:
                child_path = Path(str(child["path"]))
                rel = child_path.relative_to(base).as_posix()
                depth = len(Path(rel).parts)
                is_dir = bool(child["is_dir"])
            except (OSError, KeyError, ValueError):
                continue
            if depth > max_depth:
                continue
            entries.append(FileEntry(
                source="nas",
                path=str(child_path),
                relative_path=rel,
                name=str(child["name"]),
                is_folder=is_dir,
                modified_time=datetime.fromtimestamp(float(child["mtime"]), tz=timezone.utc).isoformat(),
                size=child.get("size"),
            ))
            if is_dir and depth < max_depth:
                stack.append(child_path)
            elif is_dir:
                entries.truncated = True
            if len(entries) >= max_items:
                entries.truncated = True
                break
    return entries


def find_drive_root(service: Any, *, root_id: str = "", root_name: str = DEFAULT_DRIVE_ROOT_NAME) -> dict[str, Any]:
    if root_id:
        return _drive_execute_with_timeout(service.files().get(
            fileId=root_id,
            supportsAllDrives=True,
            fields="id,name,mimeType,parents,modifiedTime,webViewLink,driveId",
        ), context=f"get_root:{root_id}")
    resp = _drive_execute_with_timeout(service.files().list(
        q=f"name = '{root_name}' and mimeType = '{GOOGLE_FOLDER_MIME}' and trashed = false",
        spaces="drive",
        corpora="allDrives",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=10,
        fields="files(id,name,mimeType,parents,modifiedTime,webViewLink,driveId)",
    ), context=f"find_root:{root_name}")
    files = resp.get("files", [])
    if not files:
        raise DriveCaseSyncError(f"找不到 Google Drive 資料夾：{root_name}")
    if len(files) > 1:
        raise DriveCaseSyncError(f"找到多個 Google Drive 資料夾 `{root_name}`，請設定 MAGI_DRIVE_SYNC_ROOT_FOLDER_ID")
    return files[0]


def drive_file_entries(service: Any, root_id: str, *, max_depth: int, max_items: int) -> tuple[list[FileEntry], list[CaseFolder]]:
    entries = BoundedEntries()
    cases: list[CaseFolder] = []
    stack: list[tuple[str, str, int]] = [(root_id, "", 0)]
    while stack and len(entries) < max_items:
        parent_id, parent_rel, depth = stack.pop()
        if depth >= max_depth:
            continue
        for item in _drive_list_children(service, parent_id):
            name = str(item.get("name") or "")
            if _ignore_name(name):
                continue
            rel = f"{parent_rel}/{name}".strip("/")
            is_folder = item.get("mimeType") == GOOGLE_FOLDER_MIME
            entry = FileEntry(
                source="drive",
                path=rel,
                relative_path=rel,
                name=name,
                is_folder=is_folder,
                modified_time=str(item.get("modifiedTime") or ""),
                size=int(item["size"]) if str(item.get("size") or "").isdigit() else None,
                md5=str(item.get("md5Checksum") or ""),
                drive_id=str(item.get("id") or ""),
                web_url=str(item.get("webViewLink") or ""),
                mime_type=str(item.get("mimeType") or ""),
            )
            entries.append(entry)
            if is_folder:
                # Review/quarantine trees are intentionally outside the case
                # inventory. Do not descend into them: doing so can exhaust the
                # bounded scan and must never rediscover a quarantined shell.
                if drive_case_sync_exclusion_reason(rel):
                    continue
                cls = classify_drive_case_folder(rel)
                if cls:
                    meta = extract_case_meta(name)
                    app_props = item.get("appProperties") or {}
                    if isinstance(app_props, dict):
                        hidden_case_no = str(app_props.get("magi_osc_case_number") or "").strip()
                        if OSC_CASE_RE.fullmatch(hidden_case_no):
                            meta.case_number = hidden_case_no
                        hidden_laf_no = str(app_props.get("magi_laf_case_no") or "").strip()
                        if LAF_CASE_RE.fullmatch(hidden_laf_no):
                            meta.laf_case_no = hidden_laf_no
                    case = CaseFolder(
                        source="drive",
                        path=rel,
                        relative_path=rel,
                        name=name,
                        category=cls["category"],
                        status=cls["status"],
                        case_kind=cls["case_kind"],
                        owner_bucket=cls["owner_bucket"],
                        modified_time=str(item.get("modifiedTime") or ""),
                        meta=meta,
                        drive_id=str(item.get("id") or ""),
                        web_url=str(item.get("webViewLink") or ""),
                    )
                    suggested, confidence, note = suggest_canonical_path(case)
                    case.suggested_canonical_path = suggested
                    case.suggested_path_confidence = confidence
                    case.suggested_path_note = note
                    cases.append(case)
                # Phase 1 stops at the case folder boundary.  This keeps Drive
                # API calls bounded and avoids scanning every pleading/PDF
                # before the path mapping is reviewed.
                if not cls:
                    if depth + 1 < max_depth:
                        stack.append((str(item["id"]), rel, depth + 1))
                    else:
                        entries.truncated = True
            if len(entries) >= max_items:
                entries.truncated = True
                break
    return entries, cases


def _index_cases(cases: Iterable[CaseFolder], *, include_name_only: bool = False) -> dict[str, list[CaseFolder]]:
    idx: dict[str, list[CaseFolder]] = {}
    for case in cases:
        for key in match_keys(case.meta):
            if key.startswith("name:") and not include_name_only:
                continue
            idx.setdefault(key, []).append(case)
    return idx


def _case_identity(case: CaseFolder) -> str:
    keys = [k for k in match_keys(case.meta) if not k.startswith("name:")]
    return keys[0] if keys else f"path:{normalize_text(case.relative_path)}"


def _primary_duplicate_identity_key(case: CaseFolder) -> str:
    """Return a high-confidence key for Drive-side duplicate-folder detection.

    Google Drive permits duplicate folder names, and MAGI's Drive layout often
    omits OSC case numbers.  The duplicate detector must therefore use stable
    case identity rather than the visible folder path.  Name-only matches are
    intentionally excluded because the same client can have multiple matters.
    """
    if case.meta.case_number:
        return f"case:{normalize_text(case.meta.case_number)}"
    if case.meta.laf_case_no:
        return f"laf:{normalize_text(case.meta.laf_case_no)}"
    if case.meta.court_case_no:
        return f"court:{normalize_court_case_no(case.meta.court_case_no)}"
    name = normalize_text(case.meta.client_hint)
    reason = normalize_text(case.meta.reason_hint)
    if name and reason and reason not in GENERIC_CONTEXT_TERMS:
        scope = normalize_text("|".join([
            normalize_drive_case_category(case.category),
            case.case_kind,
            case.status,
        ]))
        return f"name_reason:{scope}:{name}|{reason}"
    return ""


def _duplicate_identity_keys(case: CaseFolder) -> set[str]:
    keys: set[str] = set()
    if case.meta.case_number:
        keys.add(f"case:{normalize_text(case.meta.case_number)}")
    if case.meta.laf_case_no:
        keys.add(f"laf:{normalize_text(case.meta.laf_case_no)}")
    if case.meta.court_case_no:
        keys.add(f"court:{normalize_court_case_no(case.meta.court_case_no)}")
    if keys:
        return keys
    name = normalize_text(case.meta.client_hint)
    reason = normalize_text(case.meta.reason_hint)
    if name and reason and reason not in GENERIC_CONTEXT_TERMS:
        scope = normalize_text("|".join([
            normalize_drive_case_category(case.category),
            case.case_kind,
            case.status,
        ]))
        keys.add(f"name_reason:{scope}:{name}|{reason}")
    return keys


def _duplicate_name_reason_review_key(case: CaseFolder) -> str:
    """Return a broad review key even when the case also has a stable ID.

    Weak duplicate Drive shells have no OSC/LAF/court key, while the matching
    NAS case usually does.  Keeping this review signature separate prevents a
    weak duplicate group from making that NAS case look `local_only` and being
    provisioned as a third Drive folder.
    """
    name = normalize_text(case.meta.client_hint)
    reason = normalize_text(case.meta.reason_hint)
    if not name or not reason or reason in GENERIC_CONTEXT_TERMS:
        return ""
    scope = normalize_text("|".join([
        normalize_drive_case_category(case.category),
        case.status,
    ]))
    return f"review_name_reason_no_owner:{scope}:{name}|{reason}"


def _has_strong_case_identity(case: CaseFolder) -> bool:
    return bool(
        case.meta.case_number
        or case.meta.laf_case_no
        or case.meta.court_case_no
    )


def _potential_duplicate_identity_key(case: CaseFolder) -> str:
    """Return a review-only key for likely stale Drive folders.

    This deliberately stays weaker than `_duplicate_identity_keys`: it catches
    "same client + same matter + one side has no stable case id" cases, but it
    must never drive automatic deletion.  Same-name clients often have multiple
    procedures, so groups where every folder has a different strong id are not
    treated as duplicates.
    """
    name = normalize_text(case.meta.client_hint)
    reason = normalize_text(case.meta.reason_hint)
    if not name or not reason or reason in GENERIC_CONTEXT_TERMS:
        return ""
    scope = normalize_text("|".join([
        normalize_drive_case_category(case.category),
        case.status,
        case.owner_bucket,
        case.case_kind,
    ]))
    return f"review_name_reason:{scope}:{name}|{reason}"


def detect_drive_duplicate_case_groups(drive_cases: Iterable[CaseFolder]) -> list[dict[str, Any]]:
    """Find Drive folders that represent the same case identity.

    All folders in a duplicate group are blocked from bidirectional file sync.
    MAGI should first merge or manually resolve the Drive side; otherwise a
    single NAS case can download/upload against multiple cloud folders and
    produce the exact duplicate-folder pollution reported by users.
    """
    indexed: list[tuple[CaseFolder, set[str]]] = []
    for case in drive_cases:
        if drive_case_sync_exclusion_reason(case.relative_path or case.path):
            continue
        keys = _duplicate_identity_keys(case)
        if keys:
            indexed.append((case, keys))

    parent = list(range(len(indexed)))

    def find(idx: int) -> int:
        while parent[idx] != idx:
            parent[idx] = parent[parent[idx]]
            idx = parent[idx]
        return idx

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    first_seen_key: dict[str, int] = {}
    for idx, (_case, keys) in enumerate(indexed):
        for key in keys:
            if key in first_seen_key:
                union(idx, first_seen_key[key])
            else:
                first_seen_key[key] = idx

    buckets: dict[int, list[tuple[CaseFolder, set[str]]]] = {}
    for idx, item in enumerate(indexed):
        buckets.setdefault(find(idx), []).append(item)

    groups: list[dict[str, Any]] = []
    for _root, items in sorted(
        buckets.items(),
        key=lambda item: sorted(item[1][0][1])[0] if item[1] and item[1][0][1] else "",
    ):
        unique: dict[str, CaseFolder] = {}
        key_sets: list[set[str]] = []
        for case, keys in items:
            unique[case.drive_id or case.relative_path] = case
            key_sets.append(keys)
        if len(unique) <= 1:
            continue
        common_keys = set.intersection(*key_sets) if key_sets else set()
        all_keys = set().union(*key_sets) if key_sets else set()
        identity_key = sorted(common_keys or all_keys)[0] if (common_keys or all_keys) else ""
        ordered = sorted(
            unique.values(),
            key=lambda c: (
                0 if c.meta.case_number else 1,
                0 if c.meta.laf_case_no else 1,
                c.status != "active",
                c.relative_path,
            ),
        )
        groups.append({
            "identity_key": identity_key,
            "identity_keys": sorted(all_keys),
            "cases": ordered,
            "reason": "Google Drive 端同一案件身份有多個資料夾；同步前必須先合併或排除重複資料夾",
        })
    return groups


def detect_drive_potential_duplicate_case_groups(
    drive_cases: Iterable[CaseFolder],
    *,
    confirmed_duplicate_groups: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Find Drive folders that look duplicated but are unsafe to auto-merge.

    The result is for reports and guards only.  MAGI should not trash or merge
    these folders until a stable id, alias, or explicit user decision resolves
    the ambiguity.
    """
    confirmed_refs = {
        c.drive_id or c.relative_path
        for group in (confirmed_duplicate_groups or [])
        for c in (group.get("cases") or [])
    }
    buckets: dict[str, list[CaseFolder]] = {}
    for case in drive_cases:
        if drive_case_sync_exclusion_reason(case.relative_path or case.path):
            continue
        if (case.drive_id or case.relative_path) in confirmed_refs:
            continue
        key = _potential_duplicate_identity_key(case)
        if key:
            buckets.setdefault(key, []).append(case)

    groups: list[dict[str, Any]] = []
    for key, items in sorted(buckets.items(), key=lambda item: item[0]):
        unique = {c.drive_id or c.relative_path: c for c in items}
        if len(unique) <= 1:
            continue
        ordered = sorted(
            unique.values(),
            key=lambda c: (
                0 if _has_strong_case_identity(c) else 1,
                c.status != "active",
                c.relative_path,
            ),
        )
        strong_ids = {
            tuple(sorted(_duplicate_identity_keys(c)))
            for c in ordered
            if _has_strong_case_identity(c)
        }
        weak_cases = [c for c in ordered if not _has_strong_case_identity(c)]
        if not weak_cases:
            # Same client/reason with multiple stable ids is usually multiple
            # procedures, not a duplicate.  Keep it out of cleanup reports.
            continue
        groups.append({
            "identity_key": key,
            "identity_keys": sorted({k for c in ordered for k in _duplicate_identity_keys(c)}),
            "strong_identity_count": len(strong_ids),
            "weak_folder_count": len(weak_cases),
            "cases": ordered,
            "reason": (
                "Google Drive 端疑似有同名同案由的舊殼或未編號資料夾；"
                "因缺少穩定案號/法扶案號，僅列入待確認，不自動刪除"
            ),
        })
    return groups


def _drive_duplicate_public_group(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity_key": group.get("identity_key", ""),
        "identity_keys": group.get("identity_keys", []),
        "reason": group.get("reason", ""),
        "strong_identity_count": group.get("strong_identity_count", 0),
        "weak_folder_count": group.get("weak_folder_count", 0),
        "cases": [_case_to_dict(c) for c in group.get("cases", [])],
    }


def is_aaron_drive_bucket(case: CaseFolder) -> bool:
    return case.source == "drive" and normalize_text(case.owner_bucket) == "aaron"


def sync_scope_exclusion_reason(case: CaseFolder) -> str:
    if case.source != "drive":
        return ""
    path_reason = drive_case_sync_exclusion_reason(case.relative_path or case.path)
    if path_reason:
        return f"Google Drive 路徑屬於待整理/重複/衝突隔離區（{path_reason}）；不得當作 canonical 案件資料夾或進入雙向同步"
    if is_drive_case_excluded(case):
        return "使用者確認此雲端資料夾不納入 Drive/NAS 案件同步；不建立 NAS 資料夾、不下載"
    if case.category in OSC_NUMBER_REQUIRED_CATEGORIES and not case.meta.case_number:
        return "諮詢/縣府調解資料夾沒有 OSC 案號；依規則不納入案件同步，也不建立 NAS 資料夾"
    return ""


def lookup_db_case_contexts(case_numbers: Iterable[str]) -> dict[str, dict[str, Any]]:
    numbers = [str(n or "").strip() for n in case_numbers if str(n or "").strip()]
    if not numbers:
        return {}
    try:
        from api.osc.utils import _osc_exec
    except Exception:
        return {}
    try:
        ph = ",".join(["%s"] * len(numbers))
        rows, _ = _osc_exec(
            f"""
            SELECT id, case_number, client_name, case_reason, court_name, court_case_no,
                   case_category, case_type, case_stage,
                   notes, folder_path, status, legal_aid_status, manual_status_lock,
                   legal_aid_number, laf_case_no, application_no
            FROM cases
            WHERE case_number IN ({ph})
            """,
            tuple(numbers),
            fetch="all",
        )
        opponents, _ = _osc_exec(
            f"""
            SELECT case_number, name, address, is_active
            FROM opponents
            WHERE case_number IN ({ph})
            ORDER BY updated_date DESC, id DESC
            """,
            tuple(numbers),
            fetch="all",
        )
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        cn = str(row.get("case_number") or "").strip()
        if cn:
            row = dict(row)
            row["opponents"] = []
            out[cn] = row
    for opp in opponents or []:
        cn = str(opp.get("case_number") or "").strip()
        if cn in out:
            out[cn].setdefault("opponents", []).append(dict(opp))
    return out


def _db_case_status(row: dict[str, Any]) -> str:
    return "closed" if _db_context_marks_closed(row) else "active"


def _local_case_kind_from_db(row: dict[str, Any], folder_name: str = "") -> str:
    text = str(row.get("case_type") or "").strip()
    if text:
        return text
    category = normalize_drive_case_category(str(row.get("case_category") or "").strip())
    reason = str(row.get("case_reason") or "").strip()
    if reason:
        kind, _confidence = infer_case_kind(category, reason, folder_name)
        if kind:
            return kind
    return ""


def _db_row_to_local_case(
    row: dict[str, Any],
    *,
    for_write: bool = False,
    allow_missing_closed: bool = False,
) -> CaseFolder | None:
    """Build a NAS-side CaseFolder directly from the DB canonical path.

    This is the fast path used by the worker for urgent/upcoming cases.  It
    avoids a full Drive and NAS inventory pass, while preserving both sides'
    native folder layouts through the existing boundary mapping functions.
    """
    folder_path = str(row.get("folder_path") or "").strip()
    if not folder_path:
        return None
    try:
        from api.case_path_mapper import local_case_path_candidates
    except Exception:
        local_case_path_candidates = None  # type: ignore[assignment]

    local_path = ""
    candidates: list[str] = []
    if local_case_path_candidates is not None:
        try:
            try:
                candidates = [str(p) for p in local_case_path_candidates(folder_path, for_write=for_write)]
            except TypeError:
                candidates = [str(p) for p in local_case_path_candidates(folder_path)]
        except Exception:
            candidates = []
    if not candidates:
        candidates = [folder_path.replace("\\", "/")]
    for candidate in candidates:
        try:
            if candidate and os.path.isdir(candidate):
                local_path = candidate
                break
        except OSError:
            continue
    if not local_path and allow_missing_closed and _db_context_marks_closed(row):
        try:
            from api.blueprints.osc_cases import _osc_expected_closed_case_folder_for_row

            local_path = _osc_expected_closed_case_folder_for_row(
                row,
                require_existing=False,
            )
        except Exception:
            local_path = ""
    if not local_path:
        return None

    name = Path(local_path).name
    status = _db_case_status(row)
    category = normalize_drive_case_category(str(row.get("case_category") or "").strip()) or "一般案件"
    case_kind = _local_case_kind_from_db(row, name)
    meta = extract_case_meta(name)
    meta.case_number = str(row.get("case_number") or meta.case_number or "").strip()
    meta.laf_case_no = (
        str(row.get("laf_case_no") or "").strip()
        or str(row.get("legal_aid_number") or "").strip()
        or str(row.get("application_no") or "").strip()
        or meta.laf_case_no
    )
    meta.client_hint = str(row.get("client_name") or meta.client_hint or "").strip()
    meta.reason_hint = str(row.get("case_reason") or meta.reason_hint or "").strip()
    meta.court_case_no = str(row.get("court_case_no") or meta.court_case_no or "").strip()
    relative_path = _relative_case_path_from_db_row(row, local_path)
    return CaseFolder(
        source="nas",
        path=local_path,
        local_path=local_path,
        relative_path=relative_path,
        name=name,
        category=category,
        status=status,
        case_kind=case_kind,
        meta=meta,
    )


def _relative_case_path_from_db_row(row: dict[str, Any], local_path: str) -> str:
    folder_path = str(row.get("folder_path") or "").replace("\\", "/").strip("/")
    for marker in ("01_案件/", "03_工作資料/10_結案/"):
        idx = folder_path.find(marker)
        if idx >= 0:
            return folder_path[idx + len(marker) :].strip("/")
    path = str(local_path or "").replace("\\", "/").strip("/")
    for marker in ("01_案件/", "03_工作資料/10_結案/"):
        idx = path.find(marker)
        if idx >= 0:
            return path[idx + len(marker) :].strip("/")
    return Path(local_path).name


def db_local_cases_for_numbers(
    case_numbers: Iterable[str],
    *,
    for_write: bool = False,
    allow_missing_closed: bool = False,
) -> tuple[list[CaseFolder], list[dict[str, Any]]]:
    contexts = lookup_db_case_contexts(case_numbers)
    local_cases: list[CaseFolder] = []
    skipped: list[dict[str, Any]] = []
    for case_number in [str(x or "").strip() for x in case_numbers if str(x or "").strip()]:
        row = contexts.get(case_number)
        if not row:
            skipped.append({"case_number": case_number, "reason": "db_case_not_found"})
            continue
        case = _db_row_to_local_case(
            row,
            for_write=for_write,
            allow_missing_closed=allow_missing_closed,
        )
        if not case:
            skipped.append({
                "case_number": case_number,
                "reason": "local_case_folder_write_mount_required" if for_write else "local_case_folder_not_accessible",
                "folder_path": str(row.get("folder_path") or ""),
            })
            continue
        local_cases.append(case)
    return local_cases, skipped


def _closed_status_text(value: str) -> bool:
    from magi_v3.case_lifecycle import phase_for_status, CaseLifecyclePhase

    return phase_for_status(value) is not CaseLifecyclePhase.OPEN


def _closed_canonical_path(value: str) -> bool:
    text = str(value or "").replace("\\", "/")
    return text.upper().startswith("Y:/") or "/10_結案/" in text or text.endswith("/10_結案")


def _db_context_marks_closed(db_context: dict[str, Any] | None) -> bool:
    if not db_context:
        return False
    from magi_v3.case_lifecycle import requires_closed_storage

    lifecycle_closed = requires_closed_storage(db_context)
    try:
        locked = int(db_context.get("manual_status_lock") or 0) == 1
    except Exception:
        locked = False
    path_closed = _closed_canonical_path(str(db_context.get("folder_path") or ""))
    return lifecycle_closed or (locked and path_closed)


def _db_laf_numbers(db_context: dict[str, Any] | None) -> set[str]:
    if not db_context:
        return set()
    values = {
        str(db_context.get("legal_aid_number") or "").strip(),
        str(db_context.get("laf_case_no") or "").strip(),
        str(db_context.get("application_no") or "").strip(),
    }
    return {v for v in values if LAF_CASE_RE.fullmatch(v)}


def _drive_local_match_conflict_reason(
    drive_case: CaseFolder,
    local_case: CaseFolder,
    db_context: dict[str, Any] | None,
) -> str:
    """Block stale active shells and same-name/different-LAF false matches."""
    drive_laf = str(drive_case.meta.laf_case_no or "").strip()
    local_lafs = {
        str(local_case.meta.laf_case_no or "").strip(),
        *_db_laf_numbers(db_context),
    }
    local_lafs = {v for v in local_lafs if LAF_CASE_RE.fullmatch(v)}
    if drive_laf and local_lafs and drive_laf not in local_lafs:
        return (
            f"法扶案號不同（雲端 {drive_laf}；本機/DB {', '.join(sorted(local_lafs))}），"
            "不得只靠同姓名或案由同步，避免同一當事人不同程序混檔"
        )
    if not _db_context_marks_closed(db_context):
        return ""
    if drive_case.status == "active":
        return (
            "DB 已鎖定為結案或結案路徑，但雲端資料夾仍在進行中區；"
            "避免 NAS/Synology Drive 備援把已結案舊案重新當成進行中同步"
        )
    if local_case.status == "active":
        return (
            "DB 已鎖定為結案或結案路徑，但本機候選仍在進行中路徑；"
            "等待結案慢搬完成前不做雙向同步，避免新舊案混檔"
        )
    return ""


def _context_values_from_case(
    case: CaseFolder,
    *,
    db_context: dict[str, Any] | None = None,
    file_entries: list[FileEntry] | None = None,
) -> list[str]:
    values = [
        case.name,
        case.relative_path,
        case.meta.case_number,
        case.meta.laf_case_no,
        case.meta.court_case_no,
        case.meta.client_hint,
        case.meta.reason_hint,
    ]
    if db_context:
        for key in ("case_number", "client_name", "case_reason", "court_name", "court_case_no", "notes", "folder_path", "status"):
            values.append(str(db_context.get(key) or ""))
        for opp in db_context.get("opponents") or []:
            values.append(str(opp.get("name") or ""))
            values.append(str(opp.get("address") or ""))
    for entry in file_entries or []:
        values.append(entry.relative_path)
        values.append(entry.name)
    values.extend(expand_alias_values(values))
    return values


def score_context_candidates(
    drive_case: CaseFolder,
    candidate_cases: list[CaseFolder],
    *,
    drive_entries: list[FileEntry] | None = None,
    local_entries_by_case: dict[str, list[FileEntry]] | None = None,
    db_context_by_case: dict[str, dict[str, Any]] | None = None,
) -> list[ContextScore]:
    drive_values = _context_values_from_case(drive_case, file_entries=drive_entries)
    drive_text = normalize_text(" ".join(drive_values))
    drive_terms = set(meaningful_terms(drive_values))

    all_candidate_terms: dict[str, set[str]] = {}
    for candidate in candidate_cases:
        cn = candidate.meta.case_number
        values = _context_values_from_case(
            candidate,
            db_context=(db_context_by_case or {}).get(cn, {}),
            file_entries=(local_entries_by_case or {}).get(candidate.relative_path, []),
        )
        all_candidate_terms[candidate.relative_path] = set(meaningful_terms(values))

    scores: list[ContextScore] = []
    for candidate in candidate_cases:
        own_terms = all_candidate_terms.get(candidate.relative_path, set())
        other_terms = set().union(*(v for k, v in all_candidate_terms.items() if k != candidate.relative_path))
        distinctive_terms = sorted(own_terms - other_terms, key=lambda x: (-len(x), x))
        matched_terms: list[str] = []
        score = 0
        for term in distinctive_terms:
            if not is_decisive_context_term(term):
                continue
            if _contains_term(drive_text, term):
                matched_terms.append(term)
                if term in drive_terms:
                    score += 4
                elif len(term) >= 3:
                    score += 3
                else:
                    score += 1
        if candidate.meta.case_number and _contains_term(drive_text, candidate.meta.case_number):
            matched_terms.append(candidate.meta.case_number)
            score += 8
        if candidate.meta.laf_case_no and _contains_term(drive_text, candidate.meta.laf_case_no):
            matched_terms.append(candidate.meta.laf_case_no)
            score += 8
        if candidate.meta.court_case_no and _contains_term(drive_text, candidate.meta.court_case_no):
            matched_terms.append(candidate.meta.court_case_no)
            score += 8
        context_values = _context_values_from_case(
            candidate,
            db_context=(db_context_by_case or {}).get(candidate.meta.case_number, {}),
            file_entries=(local_entries_by_case or {}).get(candidate.relative_path, []),
        )
        scores.append(ContextScore(
            candidate=candidate,
            score=score,
            matched_terms=sorted(set(matched_terms), key=lambda x: (-len(x), x)),
            candidate_terms=distinctive_terms[:20],
            context_sample=[v for v in context_values if v][:12],
        ))
    return sorted(scores, key=lambda s: s.score, reverse=True)


def resolve_ambiguous_cases_with_context(
    comparison: dict[str, Any],
    *,
    drive_service: Any | None = None,
    max_drive_context_depth: int = 3,
    max_context_items: int = 300,
) -> dict[str, Any]:
    if not comparison.get("ambiguous"):
        comparison.setdefault("out_of_scope", [])
        return comparison

    resolved: list[dict[str, Any]] = []
    still_ambiguous: list[dict[str, Any]] = []
    out_of_scope = list(comparison.get("out_of_scope") or [])
    case_numbers = [
        c.meta.case_number
        for item in comparison.get("ambiguous") or []
        for c in item.get("candidates", [])
        if c.meta.case_number
    ]
    db_contexts = lookup_db_case_contexts(case_numbers)

    for item in comparison.get("ambiguous") or []:
        drive_case: CaseFolder = item["drive"]
        candidates: list[CaseFolder] = item.get("candidates") or []
        reason = sync_scope_exclusion_reason(drive_case)
        if reason:
            out_of_scope.append({
                "drive": drive_case,
                "reason": reason,
                "candidates": candidates,
            })
            continue
        if is_aaron_drive_bucket(drive_case):
            out_of_scope.append({
                "drive": drive_case,
                "reason": "Aaron 雲端資料夾沒有 NAS 唯一對應；依規則不建立 NAS 資料夾、不進同步佇列",
                "candidates": candidates,
            })
            continue

        drive_entries: list[FileEntry] = []
        local_entries_by_case: dict[str, list[FileEntry]] = {}
        scores = score_context_candidates(
            drive_case,
            candidates,
            drive_entries=drive_entries,
            local_entries_by_case=local_entries_by_case,
            db_context_by_case=db_contexts,
        )
        if not (scores and scores[0].score >= 4 and (len(scores) == 1 or scores[0].score > scores[1].score)):
            if drive_service is not None and drive_case.drive_id:
                try:
                    drive_entries = drive_descendant_context(
                        drive_service,
                        drive_case.drive_id,
                        max_depth=max_drive_context_depth,
                        max_items=max_context_items,
                    )
                except Exception as exc:
                    item["context_resolution"] = {
                        "status": "context_probe_failed",
                        "reason": f"雲端檔名讀取失敗：{type(exc).__name__}",
                    }

            for candidate in candidates:
                if candidate.local_path:
                    local_entries_by_case[candidate.relative_path] = local_descendant_context(
                        candidate.local_path,
                        max_depth=3,
                        max_items=max_context_items,
                    )
            scores = score_context_candidates(
                drive_case,
                candidates,
                drive_entries=drive_entries,
                local_entries_by_case=local_entries_by_case,
                db_context_by_case=db_contexts,
            )
        drive_terms = meaningful_terms(_context_values_from_case(drive_case, file_entries=drive_entries))
        item["context_resolution"] = {
            "status": "unresolved",
            "drive_terms": drive_terms[:40],
            "drive_context_sample": [e.relative_path for e in drive_entries[:30]],
            "candidate_scores": [
                {
                    "case_number": score.candidate.meta.case_number,
                    "relative_path": score.candidate.relative_path,
                    "score": score.score,
                    "matched_terms": score.matched_terms,
                    "candidate_terms": score.candidate_terms,
                }
                for score in scores
            ],
        }

        if scores and scores[0].score >= 4 and (len(scores) == 1 or scores[0].score > scores[1].score):
            best = scores[0]
            resolved.append({
                "drive": drive_case,
                "local": best.candidate,
                "match_keys": item.get("match_keys") or [],
                "context_resolution": {
                    "status": "resolved_by_context",
                    "score": best.score,
                    "matched_terms": best.matched_terms,
                    "drive_terms": drive_terms[:40],
                },
            })
            comparison["local_only"] = [
                c for c in comparison.get("local_only", [])
                if c.relative_path != best.candidate.relative_path
            ]
        else:
            if (not scores) or scores[0].score < 4:
                item["context_resolution"]["status"] = "context_mismatch"
                item["context_resolution"]["reason"] = "雲端線索未命中候選案件的明確人名或案號"
            still_ambiguous.append(item)

    comparison["matched"].extend(resolved)
    comparison["ambiguous"] = still_ambiguous
    comparison["out_of_scope"] = out_of_scope
    return enforce_reverse_unique_matches(comparison)


def resolve_drive_only_cases_with_context(
    comparison: dict[str, Any],
    *,
    drive_service: Any | None = None,
    max_drive_context_depth: int = 3,
    max_context_items: int = 300,
) -> dict[str, Any]:
    """Try to match legacy Drive-only folders against local-only OSC cases.

    This is intentionally conservative: it only promotes a Drive-only folder to
    matched when DB notes, folder names, or local context produce one clear
    candidate.  It never creates folders.
    """
    drive_only: list[CaseFolder] = list(comparison.get("drive_only") or [])
    local_only: list[CaseFolder] = list(comparison.get("local_only") or [])
    if not drive_only or not local_only:
        return comparison

    db_contexts = lookup_db_case_contexts(
        c.meta.case_number for c in local_only if c.meta.case_number
    )
    resolved: list[dict[str, Any]] = []
    unresolved: list[CaseFolder] = []
    used_local_paths: set[str] = set()
    for drive_case in drive_only:
        candidates = [
            c for c in local_only
            if c.relative_path not in used_local_paths
            and (
                not drive_case.category
                or not c.category
                or drive_case.category == c.category
                or drive_case.category == "代庭案件"
            )
        ]
        if not candidates:
            unresolved.append(drive_case)
            continue

        scores = score_context_candidates(
            drive_case,
            candidates,
            drive_entries=[],
            db_context_by_case=db_contexts,
        )
        if not (scores and scores[0].score >= 4 and (len(scores) == 1 or scores[0].score > scores[1].score)):
            drive_entries: list[FileEntry] = []
            if drive_service is not None and drive_case.drive_id:
                try:
                    drive_entries = drive_descendant_context(
                        drive_service,
                        drive_case.drive_id,
                        max_depth=max_drive_context_depth,
                        max_items=max_context_items,
                    )
                except Exception:
                    drive_entries = []
            if drive_entries:
                scores = score_context_candidates(
                    drive_case,
                    candidates,
                    drive_entries=drive_entries,
                    db_context_by_case=db_contexts,
                )
        if scores and scores[0].score >= 4 and (len(scores) == 1 or scores[0].score > scores[1].score):
            best = scores[0]
            used_local_paths.add(best.candidate.relative_path)
            resolved.append({
                "drive": drive_case,
                "local": best.candidate,
                "match_keys": match_keys(drive_case.meta),
                "context_resolution": {
                    "status": "resolved_drive_only_by_context",
                    "score": best.score,
                    "matched_terms": best.matched_terms,
                },
            })
        else:
            unresolved.append(drive_case)

    if resolved:
        comparison["matched"].extend(resolved)
        comparison["drive_only"] = unresolved
        comparison["local_only"] = [
            c for c in local_only
            if c.relative_path not in used_local_paths
        ]
    return enforce_reverse_unique_matches(comparison)


def compare_case_folders(drive_cases: list[CaseFolder], local_cases: list[CaseFolder]) -> dict[str, Any]:
    forbidden_drive_cases = [
        (case, drive_case_sync_exclusion_reason(case.relative_path or case.path))
        for case in drive_cases
        if drive_case_sync_exclusion_reason(case.relative_path or case.path)
    ]
    eligible_drive_cases = [
        case
        for case in drive_cases
        if not drive_case_sync_exclusion_reason(case.relative_path or case.path)
    ]
    drive_duplicate_groups = detect_drive_duplicate_case_groups(eligible_drive_cases)
    drive_potential_duplicate_groups = detect_drive_potential_duplicate_case_groups(
        eligible_drive_cases,
        confirmed_duplicate_groups=drive_duplicate_groups,
    )
    duplicate_identity_keys = {
        str(key or "")
        for group in drive_duplicate_groups
        for key in (group.get("identity_keys") or [group.get("identity_key")])
        if str(key or "")
    }
    duplicate_name_reason_review_keys = {
        key
        for group in drive_duplicate_groups
        for case in group.get("cases", [])
        if (key := _duplicate_name_reason_review_key(case))
    }
    duplicate_drive_refs: set[str] = {
        c.drive_id or c.relative_path
        for group in drive_duplicate_groups
        for c in group.get("cases", [])
    }
    potential_drive_refs: set[str] = {
        c.drive_id or c.relative_path
        for group in drive_potential_duplicate_groups
        for c in group.get("cases", [])
    }
    potential_strong_identity_keys = {
        key
        for group in drive_potential_duplicate_groups
        for case in group.get("cases", [])
        if _has_strong_case_identity(case)
        for key in _duplicate_identity_keys(case)
    }
    active_drive_cases = [
        c for c in eligible_drive_cases
        if (c.drive_id or c.relative_path) not in duplicate_drive_refs
        and (c.drive_id or c.relative_path) not in potential_drive_refs
    ]
    strong_local = _index_cases(local_cases)
    weak_local = _index_cases(local_cases, include_name_only=True)
    db_contexts = lookup_db_case_contexts(
        c.meta.case_number for c in local_cases if c.meta.case_number
    )
    matched: list[dict[str, Any]] = []
    drive_only: list[CaseFolder] = []
    out_of_scope: list[dict[str, Any]] = [
        {
            "drive": case,
            "reason": (
                "drive_sync_forbidden_path:"
                f"{reason}; 待整理/重複/衝突隔離路徑不得當作 canonical 或進入雙向同步"
            ),
        }
        for case, reason in forbidden_drive_cases
    ]
    ambiguous: list[dict[str, Any]] = []
    matched_local_ids: set[str] = set()

    for d in active_drive_cases:
        scope_reason = sync_scope_exclusion_reason(d)
        if scope_reason:
            out_of_scope.append({"drive": d, "reason": scope_reason})
            continue
        keys = match_keys(d.meta)
        candidates: dict[str, CaseFolder] = {}
        blocked: list[tuple[CaseFolder, str]] = []
        def add_candidate(candidate: CaseFolder) -> None:
            reason = _drive_local_match_conflict_reason(
                d,
                candidate,
                db_contexts.get(candidate.meta.case_number, {}),
            )
            if reason:
                blocked.append((candidate, reason))
                return
            candidates[candidate.relative_path] = candidate

        for key in keys:
            if key.startswith("name:"):
                continue
            for c in strong_local.get(key, []):
                add_candidate(c)
        if not candidates and keys:
            for key in keys:
                for c in weak_local.get(key, []):
                    add_candidate(c)
        if len(candidates) == 1:
            local = next(iter(candidates.values()))
            matched_local_ids.add(local.relative_path)
            matched.append({"drive": d, "local": local, "match_keys": keys})
        elif len(candidates) > 1:
            ambiguous.append({"drive": d, "candidates": list(candidates.values()), "match_keys": keys})
        elif blocked:
            out_of_scope.append({
                "drive": d,
                "reason": blocked[0][1],
                "candidates": [c for c, _ in blocked],
            })
        else:
            if is_aaron_drive_bucket(d):
                out_of_scope.append({
                    "drive": d,
                    "reason": "Aaron 雲端資料夾沒有 NAS 唯一對應；依規則不建立 NAS 資料夾、不進同步佇列",
                })
            else:
                drive_only.append(d)

    # Enforce a one-to-one boundary.  Distinct Drive aliases, old shells, or
    # cross-category copies can each be individually plausible yet all point
    # to the same NAS case.  Syncing any of them would keep multiple cloud
    # folders alive, so remove the entire many-to-one group from executable
    # matches until it is explicitly consolidated.
    matches_by_local: dict[str, list[dict[str, Any]]] = {}
    for item in matched:
        local = item.get("local")
        if local:
            matches_by_local.setdefault(local.relative_path, []).append(item)
    drive_many_to_one: list[dict[str, Any]] = []
    blocked_many_local_ids = {
        local_path
        for local_path, items in matches_by_local.items()
        if len(items) > 1
    }
    if blocked_many_local_ids:
        kept_matches: list[dict[str, Any]] = []
        for local_path, items in sorted(matches_by_local.items()):
            if local_path not in blocked_many_local_ids:
                kept_matches.extend(items)
                continue
            local = items[0]["local"]
            drives = [item["drive"] for item in items]
            drive_many_to_one.append({
                "local": local,
                "drives": drives,
                "reason": (
                    "多個 Google Drive 資料夾同時命中同一 NAS canonical 案件；"
                    "整組同步已阻斷，必須先合併或明確排除舊殼"
                ),
            })
            for drive in drives:
                out_of_scope.append({
                    "drive": drive,
                    "local": local,
                    "reason": "multiple_drive_folders_match_same_nas_case",
                })
        matched = kept_matches
        matched_local_ids = {
            item["local"].relative_path
            for item in matched
            if item.get("local")
        }

    drive_strong = _index_cases(active_drive_cases)
    local_only: list[CaseFolder] = []
    for local in local_cases:
        if local.relative_path in matched_local_ids:
            continue
        if local.relative_path in blocked_many_local_ids:
            out_of_scope.append({
                "local": local,
                "reason": (
                    "同一 NAS canonical 案件被多個 Google Drive 資料夾命中；"
                    "在 Drive 舊殼合併前不得同步或另建資料夾"
                ),
            })
            continue
        local_identities = _duplicate_identity_keys(local)
        if local_identities and local_identities.intersection(duplicate_identity_keys):
            out_of_scope.append({
                "local": local,
                "reason": (
                    "Google Drive 端已有同一案件身份的重複資料夾；"
                    "本機/NAS 端不得再建立或同步到任一雲端資料夾，需先清理 Drive 重複群組"
                ),
            })
            continue
        local_review_key = _duplicate_name_reason_review_key(local)
        if local_review_key and local_review_key in duplicate_name_reason_review_keys:
            out_of_scope.append({
                "local": local,
                "reason": (
                    "Google Drive 端已有同名同案由的未編號重複資料夾；"
                    "即使 NAS 已有 OSC 案號，也不得把它視為 local-only 或再建立第三個 Drive 案件殼"
                ),
            })
            continue
        if local_identities and local_identities.intersection(potential_strong_identity_keys):
            out_of_scope.append({
                "local": local,
                "reason": (
                    "Google Drive 端同名同案由含未編號舊殼，疑似重複案件資料夾；"
                    "在人工確認前，本機/NAS 不得上傳、下載或建立另一個 Drive 案件殼"
                ),
            })
            continue
        db_context = db_contexts.get(local.meta.case_number, {})
        if local.status == "active" and _db_context_marks_closed(db_context):
            out_of_scope.append({
                "local": local,
                "reason": (
                    "DB 已鎖定為結案或結案路徑，但 Synology Drive/本機仍有進行中殼資料夾；"
                    "不建立雲端同步任務，待結案慢搬或空殼清理處理"
                ),
            })
            continue
        keys = [k for k in match_keys(local.meta) if not k.startswith("name:")]
        if keys and any(drive_strong.get(k) for k in keys):
            continue
        local_only.append(local)

    return {
        "matched": matched,
        "drive_only": drive_only,
        "local_only": local_only,
        "ambiguous": ambiguous,
        "out_of_scope": out_of_scope,
        "drive_duplicates": drive_duplicate_groups,
        "drive_potential_duplicates": drive_potential_duplicate_groups,
        "drive_many_to_one": drive_many_to_one,
    }


def enforce_reverse_unique_matches(comparison: dict[str, Any]) -> dict[str, Any]:
    """Apply the one-NAS-to-one-Drive invariant after every resolver stage."""
    matches_by_local: dict[str, list[dict[str, Any]]] = {}
    for item in comparison.get("matched") or []:
        local = item.get("local")
        if local:
            matches_by_local.setdefault(local.relative_path, []).append(item)
    duplicates = {path: items for path, items in matches_by_local.items() if len(items) > 1}
    if not duplicates:
        return comparison

    blocked_paths = set(duplicates)
    comparison["matched"] = [
        item for item in comparison.get("matched") or []
        if not item.get("local") or item["local"].relative_path not in blocked_paths
    ]
    comparison["local_only"] = [
        case for case in comparison.get("local_only") or []
        if case.relative_path not in blocked_paths
    ]

    groups_by_local: dict[str, dict[str, Any]] = {
        group["local"].relative_path: group
        for group in comparison.get("drive_many_to_one") or []
        if group.get("local")
    }
    out_of_scope = list(comparison.get("out_of_scope") or [])
    existing_scope_keys = {
        (
            (item.get("drive").drive_id or item.get("drive").relative_path) if item.get("drive") else "",
            item.get("local").relative_path if item.get("local") else "",
            item.get("reason", ""),
        )
        for item in out_of_scope
    }
    for local_path, items in sorted(duplicates.items()):
        local = items[0]["local"]
        drives_by_ref: dict[str, CaseFolder] = {}
        existing = groups_by_local.get(local_path)
        for drive in (existing or {}).get("drives") or []:
            drives_by_ref[drive.drive_id or drive.relative_path] = drive
        for item in items:
            drive = item.get("drive")
            if drive:
                drives_by_ref[drive.drive_id or drive.relative_path] = drive
        groups_by_local[local_path] = {
            "local": local,
            "drives": sorted(drives_by_ref.values(), key=lambda case: case.relative_path),
            "reason": (
                "多個 Google Drive 資料夾同時命中同一 NAS canonical 案件；"
                "整組同步已阻斷，必須先合併或明確排除舊殼"
            ),
        }
        for drive in drives_by_ref.values():
            key = (drive.drive_id or drive.relative_path, local_path, "multiple_drive_folders_match_same_nas_case")
            if key not in existing_scope_keys:
                out_of_scope.append({
                    "drive": drive,
                    "local": local,
                    "reason": "multiple_drive_folders_match_same_nas_case",
                })
                existing_scope_keys.add(key)
        local_key = ("", local_path, "multiple_drive_folders_match_same_nas_case")
        if local_key not in existing_scope_keys:
            out_of_scope.append({
                "local": local,
                "reason": "multiple_drive_folders_match_same_nas_case",
            })
            existing_scope_keys.add(local_key)
    comparison["drive_many_to_one"] = [groups_by_local[key] for key in sorted(groups_by_local)]
    comparison["out_of_scope"] = out_of_scope
    return comparison


def _case_to_dict(case: CaseFolder) -> dict[str, Any]:
    data = asdict(case)
    data["match_keys"] = match_keys(case.meta)
    return data


def build_sync_plan(comparison: dict[str, Any]) -> dict[str, Any]:
    """Build a non-executing plan. No file operation is performed here."""
    actions: list[dict[str, Any]] = []
    for item in comparison.get("matched", []):
        drive = item.get("drive")
        local = item.get("local")
        if not drive or not local:
            continue
        actions.append({
            "action": "deep_compare_after_approval",
            "safety": "no_overwrite_no_delete",
            "drive_path": drive.relative_path,
            "drive_id": drive.drive_id,
            "local_path": local.local_path or local.path,
            "case_number": local.meta.case_number,
            "status": "ready_for_file_diff",
            "context_resolution": item.get("context_resolution", {}),
        })
    for item in comparison.get("drive_only", []):
        actions.append({
            "action": "manual_map_or_create_case",
            "safety": "requires_user_approval",
            "drive_path": item.relative_path,
            "drive_id": item.drive_id,
            "suggested_canonical_path": item.suggested_canonical_path,
            "reason": item.suggested_path_note or "雲端案件尚未找到唯一 NAS 對應",
            "status": "needs_review",
        })
    for item in comparison.get("ambiguous", []):
        drive = item.get("drive")
        if not drive:
            continue
        resolution = item.get("context_resolution") or {}
        actions.append({
            "action": "manual_disambiguation",
            "safety": "blocked_until_unique_match",
            "drive_path": drive.relative_path,
            "drive_id": drive.drive_id,
            "candidate_count": len(item.get("candidates") or []),
            "reason": resolution.get("reason") or "找到多個候選案件",
            "status": resolution.get("status") or "ambiguous",
        })
    for item in comparison.get("out_of_scope", []):
        case = item.get("drive") or item.get("local")
        if not case:
            continue
        actions.append({
            "action": "skip",
            "safety": "out_of_scope",
            "drive_path": case.relative_path if case.source == "drive" else "",
            "drive_id": case.drive_id if case.source == "drive" else "",
            "local_path": case.local_path or case.path if case.source != "drive" else "",
            "reason": item.get("reason", ""),
            "status": "skipped",
        })
    for group in comparison.get("drive_duplicates", []):
        cases = group.get("cases") or []
        actions.append({
            "action": "resolve_drive_duplicate_case_folders",
            "safety": "blocked_until_google_drive_duplicates_are_merged",
            "drive_path": "",
            "drive_id": "",
            "duplicate_count": len(cases),
            "duplicate_paths": [c.relative_path for c in cases],
            "identity_key": group.get("identity_key", ""),
            "identity_keys": group.get("identity_keys", []),
            "reason": group.get("reason", ""),
            "status": "blocked_duplicate_drive_folder",
        })
    for group in comparison.get("drive_potential_duplicates", []):
        cases = group.get("cases") or []
        actions.append({
            "action": "review_potential_drive_duplicate_case_folders",
            "safety": "review_only_no_delete_no_merge",
            "drive_path": "",
            "drive_id": "",
            "duplicate_count": len(cases),
            "duplicate_paths": [c.relative_path for c in cases],
            "identity_key": group.get("identity_key", ""),
            "identity_keys": group.get("identity_keys", []),
            "reason": group.get("reason", ""),
            "status": "potential_duplicate_drive_folder",
        })
    for group in comparison.get("drive_many_to_one", []):
        drives = group.get("drives") or []
        local = group.get("local")
        actions.append({
            "action": "resolve_drive_many_to_one_case_folders",
            "safety": "blocked_until_one_canonical_drive_folder_remains",
            "drive_path": "",
            "drive_id": "",
            "duplicate_count": len(drives),
            "duplicate_paths": [c.relative_path for c in drives],
            "local_path": (local.local_path or local.path) if local else "",
            "reason": group.get("reason", ""),
            "status": "blocked_drive_many_to_one",
        })
    return {
        "mode": "dry_run_plan",
        "write_actions_enabled": False,
        "actions": actions,
        "summary": {
            "ready_for_file_diff": sum(1 for a in actions if a["status"] == "ready_for_file_diff"),
            "needs_review": sum(1 for a in actions if a["status"] == "needs_review"),
            "manual_disambiguation": sum(1 for a in actions if a["action"] == "manual_disambiguation"),
            "skipped": sum(1 for a in actions if a["status"] == "skipped"),
            "blocked_duplicate_drive_folders": sum(1 for a in actions if a["status"] == "blocked_duplicate_drive_folder"),
            "potential_duplicate_drive_folders": sum(1 for a in actions if a["status"] == "potential_duplicate_drive_folder"),
            "blocked_drive_many_to_one_groups": sum(1 for a in actions if a["status"] == "blocked_drive_many_to_one"),
        },
    }


def export_relative_path(entry: FileEntry) -> str:
    rel = str(entry.relative_path or "").strip("/")
    if not rel:
        return rel
    export = GOOGLE_EXPORT_MIME_MAP.get(entry.mime_type)
    if not export:
        return rel
    suffix = export[1]
    path = PurePosixPath(rel)
    if path.name.lower().endswith(suffix):
        return rel
    return path.with_name(path.name + suffix).as_posix()


def normalized_relative_file_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\\", "/").strip("/")
    parts = [p for p in text.split("/") if p and p not in {".", ".."}]
    return "/".join(parts).lower()


NAS_TO_DRIVE_FIRST_SEGMENT = {
    "01_法扶資料": "法扶資料",
    "02_開辦資料": "開辦資料",
    "04_我方歷次書狀": "我方書狀",
    "05_對方歷次書狀": "對造書狀",
    "06_閱卷資料": "閱卷資料",
    "07_證據資料": "證據資料",
    "08_筆錄": "筆錄",
    "09_法院通知或程序裁定": "法院通知",
    judgment_folder_name(10): "法院判決",
    legacy_judgment_folder_name(10): "法院判決",
    JUDGMENT_FOLDER_LABEL: "法院判決",
    "判決書": "法院判決",
    "11_回執": "回執",
    "12_信件往返": "信件往返",
}
NAS_TO_DRIVE_PREFIXES = {
    ("08_筆錄",): ("閱卷資料", "筆錄"),
}
DRIVE_EXISTING_ALIAS_PRIORITY = {
    "法扶資料": ("法扶資料",),
    "開辦資料": ("開辦資料", "開辨資料", "委任狀", "委任契約書、委任狀"),
    "結案資料": ("結案資料",),
    "結案酬金領款單": ("結案酬金領款單",),
    "我方書狀": ("我方書狀", "我方歷次書狀", "歷次書狀", "書狀資料"),
    "對造書狀": ("對造書狀", "對方歷次書狀"),
    "閱卷資料": ("閱卷資料", "01卷宗", "卷宗"),
    "證據資料": ("證據資料", "法律資料", "提供資料", "鑑定資料", "資助人聲明書"),
    "筆錄": ("訊問筆錄", "電子筆錄", "調解筆錄", "筆錄"),
    "法院通知": (
        "法院通知",
        "開庭通知",
        "法庭通知",
        "傳票",
        "地檢署通知",
        "調解通知",
        "調解委員會通知",
        "法院資料",
        "程序裁定",
    ),
    "法院判決": (
        "法院裁判",
        "法院裁定",
        "法院判決",
        JUDGMENT_FOLDER_LABEL,
        "判決書",
        "起訴書",
        "地檢署起訴書",
        "調解不成立證明書",
    ),
    "回執": ("回執", "自行收納款項收據", "法院收據", "律師酬金收據"),
    "信件往返": ("信件", "信件往返"),
}
DRIVE_EXISTING_PREFIX_PRIORITY = {
    "筆錄": (("閱卷資料", "筆錄"),),
    "法院通知": (("法院資料", "法院通知"), ("法院資料", "程序裁定")),
    "法院判決": (("法院資料", "法院判決"), ("法院資料", JUDGMENT_FOLDER_LABEL), ("法院資料", "判決書")),
}
DRIVE_TO_NAS_FIRST_SEGMENT = {
    "法扶資料": "01_法扶資料",
    "開辦資料": "02_開辦資料",
    "開辨資料": "02_開辦資料",
    "委任狀": "02_開辦資料",
    "委任契約書、委任狀": "02_開辦資料",
    "結案資料": "03_結案資料",
    "結案酬金領款單": "03_結案資料",
    "我方書狀": "04_我方歷次書狀",
    "我方歷次書狀": "04_我方歷次書狀",
    "歷次書狀": "04_我方歷次書狀",
    "書狀資料": "04_我方歷次書狀",
    "對造書狀": "05_對方歷次書狀",
    "對方歷次書狀": "05_對方歷次書狀",
    "閱卷資料": "06_閱卷資料",
    "01卷宗": "06_閱卷資料",
    "卷宗": "06_閱卷資料",
    "證據資料": "07_證據資料",
    "法律資料": "07_證據資料",
    "提供資料": "07_證據資料",
    "鑑定資料": "07_證據資料",
    "資助人聲明書": "07_證據資料",
    "筆錄": "08_筆錄",
    "訊問筆錄": "08_筆錄",
    "電子筆錄": "08_筆錄",
    "調解筆錄": "08_筆錄",
    "審判筆錄": "08_筆錄",
    "調查筆錄": "08_筆錄",
    "法院通知": "09_法院通知或程序裁定",
    "法院通知與程序裁定": "09_法院通知或程序裁定",
    "開庭通知": "09_法院通知或程序裁定",
    "法庭通知": "09_法院通知或程序裁定",
    "傳票": "09_法院通知或程序裁定",
    "另案傳票": "09_法院通知或程序裁定",
    "地檢署通知": "09_法院通知或程序裁定",
    "調解通知": "09_法院通知或程序裁定",
    "調解委員會通知": "09_法院通知或程序裁定",
    "最高檢察署函": "09_法院通知或程序裁定",
    "法院資料": "09_法院通知或程序裁定",
    "程序裁定": "09_法院通知或程序裁定",
    "起訴書": judgment_folder_name(10),
    "地檢署起訴書": judgment_folder_name(10),
    "另案起訴書": judgment_folder_name(10),
    "法院判決": judgment_folder_name(10),
    "法院裁判": judgment_folder_name(10),
    JUDGMENT_FOLDER_LABEL: judgment_folder_name(10),
    "判決書": judgment_folder_name(10),
    "調解不成立證明書": judgment_folder_name(10),
    "回執": "11_回執",
    "自行收納款項收據": "11_回執",
    "法院收據": "11_回執",
    "律師酬金收據": "11_回執",
    "信件": "12_信件往返",
    "信件往返": "12_信件往返",
}
DRIVE_TO_NAS_PREFIXES = {
    ("閱卷資料", "筆錄"): ("08_筆錄",),
    ("法院資料", "法院判決"): (judgment_folder_name(10),),
    ("法院資料", JUDGMENT_FOLDER_LABEL): (judgment_folder_name(10),),
    ("法院資料", "判決書"): (judgment_folder_name(10),),
    ("法院資料", "法院通知"): ("09_法院通知或程序裁定",),
    ("法院資料", "程序裁定"): ("09_法院通知或程序裁定",),
}
COURT_PROCEDURAL_FORM_RE = re.compile(
    r"(?:通知|通知書|函|傳票|開庭|庭期|補正|陳報|表示意見|文到|繳費|送達|公告|調查命令|準備程序|言詞辯論|調解|羈押|接續羈押|延長羈押|訴訟參與|參與本案訴訟|國民參與審判)"
)
COURT_FINAL_DOC_RE = re.compile(
    r"(?:判決|起訴書|不起訴處分書|緩起訴處分書|確定證明|執行命令|支付命令|調解不成立證明|復權裁定|免責裁定|不免責裁定)"
)
COURT_FINAL_RULING_RE = re.compile(
    r"(?:(?:裁定).{0,24}(?:駁回|准許|許可|認可|免責|不免責|復權|終結|開始更生|開始清算|廢棄|撤銷|移送|確定)"
    r"|(?:駁回|准許|許可|認可|免責|不免責|復權|終結|開始更生|開始清算|廢棄|撤銷|移送|確定).{0,24}(?:裁定))"
)
PLEADING_FILENAME_RE = re.compile(
    r"(?:書狀|(?<!證)狀|上訴理由|上訴|抗告|聲請|陳報|補正|答辯|準備|意見|更生方案)"
)
EVIDENCE_FILENAME_RE = re.compile(r"(?:證據|附件|照片|截圖|錄音|錄影|鑑定|診斷證明|病歷)")
ACCOUNTING_IMPORT_ONLY_SEGMENTS = {"收支紀錄", "收支明細", "收支明細表", "帳務資料"}
ACCOUNTING_IMPORT_ONLY_FILENAME_RE = re.compile(r"(?:收支紀錄|收支明細|收入支出|帳務資料|帳務)")
SEMANTIC_FIRST_SEGMENT = {
    "01_法扶資料": "法扶資料",
    "法扶資料": "法扶資料",
    "02_開辦資料": "開辦資料",
    "開辦資料": "開辦資料",
    "開辨資料": "開辦資料",
    "委任狀": "開辦資料",
    "委任契約書、委任狀": "開辦資料",
    "03_結案資料": "結案資料",
    "結案資料": "結案資料",
    "結案酬金領款單": "結案酬金領款單",
    "04_我方歷次書狀": "我方書狀",
    "我方書狀": "我方書狀",
    "我方歷次書狀": "我方書狀",
    "歷次書狀": "我方書狀",
    "書狀資料": "我方書狀",
    "05_對方歷次書狀": "對造書狀",
    "對造書狀": "對造書狀",
    "對方歷次書狀": "對造書狀",
    "06_閱卷資料": "閱卷資料",
    "閱卷資料": "閱卷資料",
    "01卷宗": "閱卷資料",
    "卷宗": "閱卷資料",
    "07_證據資料": "證據資料",
    "證據資料": "證據資料",
    "法律資料": "證據資料",
    "提供資料": "證據資料",
    "鑑定資料": "證據資料",
    "資助人聲明書": "證據資料",
    "08_筆錄": "筆錄",
    "筆錄": "筆錄",
    "訊問筆錄": "筆錄",
    "電子筆錄": "筆錄",
    "調解筆錄": "筆錄",
    "審判筆錄": "筆錄",
    "調查筆錄": "筆錄",
    "09_法院通知或程序裁定": "法院通知",
    "法院通知": "法院通知",
    "法院通知與程序裁定": "法院通知",
    "開庭通知": "法院通知",
    "法庭通知": "法院通知",
    "傳票": "法院通知",
    "另案傳票": "法院通知",
    "地檢署通知": "法院通知",
    "調解通知": "法院通知",
    "調解委員會通知": "法院通知",
    "最高檢察署函": "法院通知",
    "法院資料": "法院通知",
    "程序裁定": "法院通知",
    "起訴書": "法院判決",
    "地檢署起訴書": "法院判決",
    "另案起訴書": "法院判決",
    judgment_folder_name(10): "法院判決",
    legacy_judgment_folder_name(10): "法院判決",
    "法院判決": "法院判決",
    "法院裁判": "法院判決",
    "法院裁定": "法院判決",
    JUDGMENT_FOLDER_LABEL: "法院判決",
    "判決書": "法院判決",
    "調解不成立證明書": "法院判決",
    "11_回執": "回執",
    "回執": "回執",
    "自行收納款項收據": "回執",
    "法院收據": "回執",
    "律師酬金收據": "回執",
    "12_信件往返": "信件往返",
    "信件": "信件往返",
    "信件往返": "信件往返",
}
SEMANTIC_PREFIXES = {
    ("08_筆錄",): ("筆錄",),
    ("閱卷資料", "筆錄"): ("筆錄",),
    ("法院資料", "法院裁判"): ("法院判決",),
    ("法院資料", "法院判決"): ("法院判決",),
    ("法院資料", JUDGMENT_FOLDER_LABEL): ("法院判決",),
    ("法院資料", "判決書"): ("法院判決",),
    ("法院資料", "法院通知"): ("法院通知",),
    ("法院資料", "程序裁定"): ("法院通知",),
}

CASE_CATEGORY_SCHEMA_ALIASES = {
    "一般案件": "一般案件",
    "法扶案件": "法律扶助案件",
    "法律扶助案件": "法律扶助案件",
    "指定辯護案件": "指定辯護案件",
    "無償案件": "無償案件",
}
NAS_FOLDER_LABEL_ALIASES = {
    "委任契約書": {"委任契約書", "委任狀", "委任契約書、委任狀", "開辦資料"},
    "開辦資料": {"開辦資料", "委任狀", "委任契約書、委任狀", "委任契約書"},
    "無償委任資料": {"無償委任資料", "開辦資料", "委任狀", "委任契約書、委任狀", "委任契約書"},
    "我方歷次書狀": {"我方歷次書狀", "我方書狀", "歷次書狀", "書狀資料"},
    "對方歷次書狀": {"對方歷次書狀", "對造歷次書狀", "對造書狀"},
    "閱卷資料": {"閱卷資料", "01卷宗", "卷宗"},
    "證據資料": {"證據資料", "法律資料", "提供資料", "鑑定資料", "資助人聲明書"},
    "筆錄": {"筆錄", "訊問筆錄", "電子筆錄", "調解筆錄", "審判筆錄", "調查筆錄"},
    "法院通知或程序裁定": {"法院通知或程序裁定", "法院通知", "法院通知與程序裁定", "開庭通知", "法庭通知", "傳票", "地檢署通知", "調解通知", "調解委員會通知", "程序裁定"},
    JUDGMENT_FOLDER_LABEL: {JUDGMENT_FOLDER_LABEL, "判決書", "法院判決", "法院裁判", "法院裁定", "起訴書", "地檢署起訴書", "調解不成立證明書"},
    "回執": {"回執", "自行收納款項收據", "法院收據", "律師酬金收據"},
    "信件往返": {"信件往返", "信件"},
}
SEMANTIC_BY_NAS_LABEL = {
    "法扶資料": "法扶資料",
    "委任契約書": "開辦資料",
    "開辦資料": "開辦資料",
    "無償委任資料": "開辦資料",
    "結案資料": "結案資料",
    "我方歷次書狀": "我方書狀",
    "對方歷次書狀": "對造書狀",
    "閱卷資料": "閱卷資料",
    "證據資料": "證據資料",
    "筆錄": "筆錄",
    "法院通知或程序裁定": "法院通知",
    JUDGMENT_FOLDER_LABEL: "法院判決",
    "判決書": "法院判決",
    "回執": "回執",
    "信件往返": "信件往返",
}
SEMANTIC_TO_DRIVE_DEFAULT_PARTS = {
    "法扶資料": ("法扶資料",),
    "開辦資料": ("開辦資料",),
    "結案資料": ("結案資料",),
    "結案酬金領款單": ("結案酬金領款單",),
    "我方書狀": ("我方書狀",),
    "對造書狀": ("對造書狀",),
    "閱卷資料": ("閱卷資料",),
    "證據資料": ("證據資料",),
    "筆錄": ("閱卷資料", "筆錄"),
    "法院通知": ("法院通知",),
    "法院判決": ("法院判決",),
    "回執": ("回執",),
    "信件往返": ("信件往返",),
}
SEMANTIC_TO_NAS_DEFAULT_FIRST = {
    "法扶資料": "01_法扶資料",
    "開辦資料": "02_開辦資料",
    "結案資料": "03_結案資料",
    "我方書狀": "04_我方歷次書狀",
    "對造書狀": "05_對方歷次書狀",
    "閱卷資料": "06_閱卷資料",
    "證據資料": "07_證據資料",
    "筆錄": "08_筆錄",
    "法院通知": "09_法院通知或程序裁定",
    "法院判決": judgment_folder_name(10),
    "回執": "11_回執",
    "信件往返": "12_信件往返",
}


def _schema_case_category(case_category: str = "") -> str:
    return CASE_CATEGORY_SCHEMA_ALIASES.get(str(case_category or "").strip(), str(case_category or "").strip())


def _folder_label_aliases(label: str) -> set[str]:
    clean = osc_strip_number_prefix(str(label or "").strip())
    aliases = {clean}
    aliases.update(NAS_FOLDER_LABEL_ALIASES.get(clean, set()))
    for canonical, values in NAS_FOLDER_LABEL_ALIASES.items():
        if clean in values:
            aliases.add(canonical)
            aliases.update(values)
    return {value for value in aliases if value}


def _folder_labels_match(left: str, right: str) -> bool:
    return bool(_folder_label_aliases(left).intersection(_folder_label_aliases(right)))


def _semantic_first_for_nas_segment(segment: str) -> str:
    clean = osc_strip_number_prefix(str(segment or "").strip())
    if not clean:
        return ""
    if clean in SEMANTIC_BY_NAS_LABEL:
        return SEMANTIC_BY_NAS_LABEL[clean]
    for label, semantic in SEMANTIC_BY_NAS_LABEL.items():
        if _folder_labels_match(clean, label):
            return semantic
    return ""


def _canonical_nas_first_segment(
    target_first: str,
    *,
    case_category: str = "",
    existing_nas_first_segments: Iterable[str] | None = None,
) -> str:
    clean_target = osc_strip_number_prefix(str(target_first or "").strip())
    if not clean_target:
        return str(target_first or "").strip()
    for existing in existing_nas_first_segments or ():
        if _folder_labels_match(existing, clean_target):
            return str(existing)
    category = _schema_case_category(case_category)
    if category:
        for folder in osc_case_subfolders(category):
            if _folder_labels_match(folder, clean_target):
                return folder
    return str(target_first or "").strip()


def _remap_nas_first_segment(
    parts: list[str],
    *,
    case_category: str = "",
    existing_nas_first_segments: Iterable[str] | None = None,
) -> list[str]:
    if not parts:
        return []
    return [
        _canonical_nas_first_segment(
            parts[0],
            case_category=case_category,
            existing_nas_first_segments=existing_nas_first_segments,
        ),
        *parts[1:],
    ]


def _transcript_folder_target_segment(segment: str) -> str:
    text = str(segment or "").strip()
    if "筆錄" in text and "不成立證明" not in text:
        return "08_筆錄"
    return ""


def _drive_first_segment_semantic(segment: str) -> str:
    text = str(segment or "").strip()
    if not text:
        return ""
    exact = SEMANTIC_FIRST_SEGMENT.get(text) or _semantic_first_for_nas_segment(text)
    if exact:
        return exact
    if _transcript_folder_target_segment(text):
        return "筆錄"
    if re.search(r"(?:開辦|開辨|委任)", text):
        return "開辦資料"
    if re.search(r"(?:對造|對方).{0,12}(?:書狀|答辯|陳報|準備|狀)", text):
        return "對造書狀"
    if "書狀" in text or (
        text.endswith("狀") and not re.search(r"(?:委任狀|證明|證據|獎狀)", text)
    ):
        return "我方書狀"
    if re.search(r"(?:閱卷|卷宗|卷證)", text):
        return "閱卷資料"
    if EVIDENCE_FILENAME_RE.search(text):
        return "證據資料"
    if re.search(r"(?:開庭|庭期|傳票|法院通知|地檢署通知|調解通知|程序裁定|補正通知)", text):
        return "法院通知"
    if re.search(r"(?:判決|起訴書|不起訴處分|緩起訴處分|執行命令|支付命令|調解不成立證明|確定證明|免責裁定|復權裁定)", text):
        return "法院判決"
    if re.search(r"(?:回執|收據)", text):
        return "回執"
    if re.search(r"(?:信件|郵件|函文|律師函)", text):
        return "信件往返"
    return ""


def split_relative_parts(value: str) -> list[str]:
    text = str(value or "").replace("\\", "/").strip("/")
    return [p for p in PurePosixPath(text).parts if p and p not in {"."}]


def _case_folder_segment_matches_context(segment: str, case_context_name: str) -> bool:
    segment_meta = extract_case_meta(segment)
    context_meta = extract_case_meta(case_context_name)

    for field in ("case_number", "laf_case_no"):
        left = normalize_text(getattr(segment_meta, field, ""))
        right = normalize_text(getattr(context_meta, field, ""))
        if left and right and left == right:
            return True
    left_court = normalize_court_case_no(segment_meta.court_case_no)
    right_court = normalize_court_case_no(context_meta.court_case_no)
    if left_court and right_court and left_court == right_court:
        return True

    segment_client = normalize_text(_trim_context_suffix(segment_meta.client_hint))
    segment_reason = normalize_text(_trim_context_suffix(segment_meta.reason_hint))
    context_clients = {
        normalize_text(context_meta.client_hint),
        normalize_text(_trim_context_suffix(context_meta.client_hint)),
    }
    context_reasons = {
        normalize_text(context_meta.reason_hint),
        normalize_text(_trim_context_suffix(context_meta.reason_hint)),
    }
    context_clients.discard("")
    context_reasons.discard("")
    if not segment_client or not segment_reason or not context_clients or not context_reasons:
        return False
    if segment_client not in context_clients:
        return False
    return any(
        segment_reason == reason
        or (len(segment_reason) >= 3 and segment_reason in reason)
        or (len(reason) >= 3 and reason in segment_reason)
        for reason in context_reasons
    )


def looks_like_drive_case_folder_segment(segment: str, *, case_context_name: str = "") -> bool:
    text = str(segment or "").strip()
    if not text or text in DRIVE_TO_NAS_FIRST_SEGMENT or text in NAS_TO_DRIVE_FIRST_SEGMENT:
        return False
    if OSC_CASE_RE.search(text) or LAF_CASE_RE.search(text) or ROC_COURT_NO_RE.search(text):
        return True
    # Drive-side LAF folders often omit OSC case numbers but include
    # "client-lafNo-stage-reason"; the LAF number is enough to prove this is an
    # outer case folder, not a document category.
    if case_context_name and _case_folder_segment_matches_context(text, case_context_name):
        return True
    return False


def strip_embedded_drive_case_folder(relative_path: str, *, case_context_name: str = "") -> str:
    parts = split_relative_parts(relative_path)
    if len(parts) <= 1:
        return PurePosixPath(*parts).as_posix() if parts else ""
    if looks_like_drive_case_folder_segment(parts[0], case_context_name=case_context_name):
        return PurePosixPath(*parts[1:]).as_posix()
    return PurePosixPath(*parts).as_posix()


def infer_nas_folder_for_drive_root_file(
    filename: str,
    *,
    case_category: str = "",
    existing_nas_first_segments: Iterable[str] | None = None,
) -> str:
    name = PurePosixPath(str(filename or "")).name
    if not name:
        return ""
    if COURT_PROCEDURAL_FORM_RE.search(name) or COURT_FINAL_DOC_RE.search(name) or "裁定" in name:
        return _canonical_nas_first_segment(
            court_document_target_segment(name),
            case_category=case_category,
            existing_nas_first_segments=existing_nas_first_segments,
        )
    if "筆錄" in name:
        return _canonical_nas_first_segment(
            "08_筆錄",
            case_category=case_category,
            existing_nas_first_segments=existing_nas_first_segments,
        )
    if PLEADING_FILENAME_RE.search(name) and not re.search(r"(?:對造|對方|被告|原告).{0,12}(?:書狀|答辯|陳報)", name):
        return _canonical_nas_first_segment(
            "04_我方歷次書狀",
            case_category=case_category,
            existing_nas_first_segments=existing_nas_first_segments,
        )
    if EVIDENCE_FILENAME_RE.search(name) or re.search(r"(?:財產清單|所得資料)", name):
        return _canonical_nas_first_segment(
            "07_證據資料",
            case_category=case_category,
            existing_nas_first_segments=existing_nas_first_segments,
        )
    if "回執" in name or "收據" in name:
        return _canonical_nas_first_segment(
            "11_回執",
            case_category=case_category,
            existing_nas_first_segments=existing_nas_first_segments,
        )
    return ""


def closing_drive_folder_for_nas_path(parts: list[str]) -> str:
    filename = parts[-1] if parts else ""
    if any(term in filename for term in ("結案酬金", "結案審查", "變動審查")):
        return "結案酬金領款單"
    return "結案資料"


def drive_to_nas_relative_path(
    relative_path: str,
    *,
    case_category: str = "",
    case_context_name: str = "",
    existing_nas_first_segments: Iterable[str] | None = None,
) -> str:
    parts = split_relative_parts(relative_path)
    if not parts:
        return ""
    stripped = strip_embedded_drive_case_folder(relative_path, case_context_name=case_context_name)
    if stripped and stripped != PurePosixPath(*parts).as_posix():
        return drive_to_nas_relative_path(
            stripped,
            case_category=case_category,
            case_context_name=case_context_name,
            existing_nas_first_segments=existing_nas_first_segments,
        )
    if len(parts) == 1:
        inferred = infer_nas_folder_for_drive_root_file(
            parts[0],
            case_category=case_category,
            existing_nas_first_segments=existing_nas_first_segments,
        )
        if inferred:
            return PurePosixPath(inferred, parts[0]).as_posix()
    for source, target in sorted(DRIVE_TO_NAS_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if tuple(parts[: len(source)]) == source:
            target_parts = _remap_nas_first_segment(
                list(target) + parts[len(source) :],
                case_category=case_category,
                existing_nas_first_segments=existing_nas_first_segments,
            )
            return PurePosixPath(*target_parts).as_posix()
    if len(parts) > 1:
        transcript_target = _transcript_folder_target_segment(parts[0])
        if transcript_target:
            target_parts = _remap_nas_first_segment(
                [transcript_target] + parts[1:],
                case_category=case_category,
                existing_nas_first_segments=existing_nas_first_segments,
            )
            return PurePosixPath(*target_parts).as_posix()
        if parts[0] in {"法院資料", "閱卷資料"} and len(parts) > 2:
            transcript_target = _transcript_folder_target_segment(parts[1])
            if transcript_target:
                target_parts = _remap_nas_first_segment(
                    [transcript_target] + parts[2:],
                    case_category=case_category,
                    existing_nas_first_segments=existing_nas_first_segments,
                )
                return PurePosixPath(*target_parts).as_posix()
    if parts[0] in {"法院裁判", "法院裁定"} or tuple(parts[:2]) in {("法院資料", "法院裁判"), ("法院資料", "法院裁定")}:
        rest = parts[1:] if parts[0] in {"法院裁判", "法院裁定"} else parts[2:]
        probe = PurePosixPath(parts[0] if parts[0] in {"法院裁判", "法院裁定"} else parts[1], *rest).as_posix()
        target_first = court_document_target_segment(probe)
        target_parts = _remap_nas_first_segment(
            [target_first] + rest,
            case_category=case_category,
            existing_nas_first_segments=existing_nas_first_segments,
        )
        return PurePosixPath(*target_parts).as_posix()
    if parts[0] == "起訴書" or tuple(parts[:2]) == ("法院資料", "起訴書"):
        rest = parts[1:] if parts[0] == "起訴書" else parts[2:]
        probe = PurePosixPath("起訴書", *rest).as_posix()
        target_first = court_document_target_segment(probe)
        target_parts = _remap_nas_first_segment(
            [target_first] + rest,
            case_category=case_category,
            existing_nas_first_segments=existing_nas_first_segments,
        )
        return PurePosixPath(*target_parts).as_posix()
    if len(parts) > 1:
        semantic_first = ""
        semantic_rest = parts[1:]
        if parts[0] in {"法院資料", "閱卷資料"} and len(parts) > 2:
            semantic_first = _drive_first_segment_semantic(parts[1])
            semantic_rest = parts[2:]
        if not semantic_first:
            semantic_first = _drive_first_segment_semantic(parts[0])
            semantic_rest = parts[1:]
        target_first = SEMANTIC_TO_NAS_DEFAULT_FIRST.get(semantic_first, "")
        if target_first:
            target_parts = _remap_nas_first_segment(
                [target_first] + semantic_rest,
                case_category=case_category,
                existing_nas_first_segments=existing_nas_first_segments,
            )
            return PurePosixPath(*target_parts).as_posix()
    parts[0] = DRIVE_TO_NAS_FIRST_SEGMENT.get(parts[0], parts[0])
    parts = _remap_nas_first_segment(
        parts,
        case_category=case_category,
        existing_nas_first_segments=existing_nas_first_segments,
    )
    return PurePosixPath(*parts).as_posix()


def court_document_target_segment(relative_path: str) -> str:
    """Return the NAS folder for a mixed Drive court-document path.

    `法院裁判` on Google Drive is a legacy mixed bucket.  We cannot route it by
    folder name alone: an indictment can be the dispositive document in an
    investigation case, while hearing notices and supplemental-order letters are
    ongoing procedural documents.  The filename/path therefore decides.
    """
    text = str(relative_path or "")
    if COURT_PROCEDURAL_FORM_RE.search(text) and not re.search(r"(?:確定證明|調解不成立證明)", text):
        return "09_法院通知或程序裁定"
    if COURT_FINAL_DOC_RE.search(text):
        return judgment_folder_name(10)
    if "裁定" in text:
        return judgment_folder_name(10) if COURT_FINAL_RULING_RE.search(text) else "09_法院通知或程序裁定"
    return "09_法院通知或程序裁定"


def _existing_drive_first_segments(entries: Iterable[FileEntry]) -> set[str]:
    out: set[str] = set()
    for entry in entries or []:
        parts = split_relative_parts(entry.relative_path)
        if parts:
            out.add(parts[0])
    return out


def _existing_local_first_segments(entries: Iterable[FileEntry]) -> set[str]:
    out: set[str] = set()
    for entry in entries or []:
        parts = split_relative_parts(entry.relative_path)
        if parts and re.match(r"^\d{2}_", parts[0]):
            out.add(parts[0])
    return out


def _prefer_existing_drive_alias(semantic_segment: str, existing_first_segments: set[str]) -> str:
    for alias in DRIVE_EXISTING_ALIAS_PRIORITY.get(semantic_segment, ()):
        if alias in existing_first_segments:
            return alias
    for existing in sorted(existing_first_segments):
        # A numbered OSC/NAS folder already present on Drive is pollution to
        # repair, not a Drive-native alias to preserve for future uploads.
        if re.match(r"^\d{2}_", existing) or existing.startswith("."):
            continue
        if existing in DRIVE_TO_NAS_FIRST_SEGMENT:
            continue
        if _drive_first_segment_semantic(existing) == semantic_segment:
            return existing
    return ""


def _prefer_existing_drive_prefix(semantic_segment: str, existing_first_segments: set[str]) -> tuple[str, ...]:
    for prefix_parts in DRIVE_EXISTING_PREFIX_PRIORITY.get(semantic_segment, ()):
        if prefix_parts and prefix_parts[0] in existing_first_segments:
            return prefix_parts
    return ()


def nas_to_drive_relative_path(
    relative_path: str,
    *,
    drive_existing_first_segments: set[str] | None = None,
) -> str:
    parts = split_relative_parts(relative_path)
    if not parts:
        return ""
    existing_first_segments = drive_existing_first_segments or set()
    semantic_first = semantic_relative_path(relative_path).split("/", 1)[0]
    preferred_first = _prefer_existing_drive_alias(semantic_first, existing_first_segments)
    if preferred_first:
        return PurePosixPath(*(list([preferred_first]) + parts[1:])).as_posix()
    preferred_prefix = _prefer_existing_drive_prefix(semantic_first, existing_first_segments)
    if preferred_prefix:
        return PurePosixPath(*(list(preferred_prefix) + parts[1:])).as_posix()
    for source, target in sorted(NAS_TO_DRIVE_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if tuple(parts[: len(source)]) == source:
            return PurePosixPath(*(list(target) + parts[len(source) :])).as_posix()
    if parts[0] == "03_結案資料":
        parts[0] = closing_drive_folder_for_nas_path(parts)
    else:
        mapped = NAS_TO_DRIVE_FIRST_SEGMENT.get(parts[0], "")
        if mapped:
            parts[0] = mapped
        else:
            default_drive_parts = SEMANTIC_TO_DRIVE_DEFAULT_PARTS.get(semantic_first)
            if default_drive_parts:
                return PurePosixPath(*(list(default_drive_parts) + parts[1:])).as_posix()
    return PurePosixPath(*parts).as_posix()


def nas_to_drive_upload_skip_reason(source_relative_path: str, target_relative_path: str) -> str:
    """Fail closed when an upload target is not native to Google Drive.

    Only the first category segment is governed here; nested pleading/date
    folders are allowed after a recognised Drive category.  This prevents
    OSC's numbered NAS folders and internal maintenance directories from being
    recreated under a Drive case.
    """
    source_parts = split_relative_parts(source_relative_path)
    target_parts = split_relative_parts(target_relative_path)
    if not source_parts or not target_parts:
        return "empty_upload_path"
    # A file at the case root does not create any Drive folder.  Preserve the
    # legacy Drive root-file convention while keeping folder creation strict.
    if len(target_parts) == 1:
        return ""
    first = target_parts[0]
    if first.startswith(".") or first in SYNC_IGNORE_NAMES:
        return f"drive_admin_folder:{first}"
    if re.match(r"^\d{2}_", first):
        return f"nas_numbered_folder_on_drive:{first}"
    semantic = _drive_first_segment_semantic(first)
    if not semantic or semantic not in SEMANTIC_TO_DRIVE_DEFAULT_PARTS:
        return f"unmapped_nas_folder:{source_parts[0]}"
    source_semantic_path = semantic_relative_path(source_relative_path)
    target_semantic_path = semantic_relative_path(target_relative_path)
    source_semantic = split_relative_parts(source_semantic_path)[0] if source_semantic_path else ""
    target_semantic = split_relative_parts(target_semantic_path)[0] if target_semantic_path else ""
    source_first_semantic = _semantic_first_for_nas_segment(source_parts[0])
    target_first_semantic = _drive_first_segment_semantic(first)
    if not source_semantic:
        return f"unmapped_nas_folder:{source_parts[0]}"
    if source_semantic != target_semantic and source_first_semantic != target_first_semantic:
        return f"cross_rule_mismatch:{source_parts[0]}->{first}"
    return ""


def semantic_relative_path(relative_path: str) -> str:
    parts = split_relative_parts(relative_path)
    if not parts:
        return ""
    stripped = strip_embedded_drive_case_folder(relative_path)
    if stripped and stripped != PurePosixPath(*parts).as_posix():
        return semantic_relative_path(stripped)
    if len(parts) == 1:
        inferred = infer_nas_folder_for_drive_root_file(parts[0])
        if inferred:
            return semantic_relative_path(PurePosixPath(inferred, parts[0]).as_posix())
    if parts[0] in {"法院裁判", "法院裁定"} or tuple(parts[:2]) in {("法院資料", "法院裁判"), ("法院資料", "法院裁定")}:
        rest = parts[1:] if parts[0] in {"法院裁判", "法院裁定"} else parts[2:]
        target = court_document_target_segment(
            PurePosixPath(parts[0] if parts[0] in {"法院裁判", "法院裁定"} else parts[1], *rest).as_posix()
        )
        first = "法院通知" if target == "09_法院通知或程序裁定" else "法院判決"
        return PurePosixPath(*([first] + rest)).as_posix()
    if parts[0] == "起訴書" or tuple(parts[:2]) == ("法院資料", "起訴書"):
        rest = parts[1:] if parts[0] == "起訴書" else parts[2:]
        target = court_document_target_segment(PurePosixPath("起訴書", *rest).as_posix())
        first = "法院通知" if target == "09_法院通知或程序裁定" else "法院判決"
        return PurePosixPath(*([first] + rest)).as_posix()
    for source, target in sorted(SEMANTIC_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if tuple(parts[: len(source)]) == source:
            return PurePosixPath(*(list(target) + parts[len(source) :])).as_posix()
    if len(parts) > 1:
        if _transcript_folder_target_segment(parts[0]):
            return PurePosixPath(*(["筆錄"] + parts[1:])).as_posix()
        if parts[0] in {"法院資料", "閱卷資料"} and len(parts) > 2 and _transcript_folder_target_segment(parts[1]):
            return PurePosixPath(*(["筆錄"] + parts[2:])).as_posix()
        if parts[0] in {"法院資料", "閱卷資料"} and len(parts) > 2:
            semantic_first = _drive_first_segment_semantic(parts[1])
            if semantic_first:
                return PurePosixPath(*([semantic_first] + parts[2:])).as_posix()
        semantic_first = _drive_first_segment_semantic(parts[0])
        if semantic_first:
            return PurePosixPath(*([semantic_first] + parts[1:])).as_posix()
    if parts[0] == "03_結案資料" and closing_drive_folder_for_nas_path(parts) == "結案酬金領款單":
        parts[0] = "結案酬金領款單"
    else:
        parts[0] = SEMANTIC_FIRST_SEGMENT.get(parts[0]) or _semantic_first_for_nas_segment(parts[0]) or parts[0]
    return PurePosixPath(*parts).as_posix()


def is_accounting_import_only_path(relative_path: str) -> bool:
    """Return True if a path/name should never be synced into OSC case folders."""
    parts = split_relative_parts(relative_path)
    if not parts:
        return False
    if any(part in ACCOUNTING_IMPORT_ONLY_SEGMENTS for part in parts):
        return True
    filename = PurePosixPath(parts[-1]).name
    return bool(ACCOUNTING_IMPORT_ONLY_FILENAME_RE.search(filename))


def drive_to_nas_download_skip_reason(
    source_relative_path: str,
    target_relative_path: str,
    *,
    case_category: str = "",
) -> str:
    """Return a reason when a Drive file would be copied as a raw Drive folder.

    Google Drive case folders intentionally use different first-level buckets from
    OSC/NAS.  A file under an unknown Drive bucket must not be downloaded with
    that bucket name unchanged, because it creates mixed layouts such as
    `開庭通知/` next to `09_法院通知或程序裁定/`.  Unknown buckets are reported for
    rule review instead of silently polluting the case folder.
    """
    source_parts = split_relative_parts(source_relative_path)
    target_parts = split_relative_parts(target_relative_path)
    if is_accounting_import_only_path(source_relative_path):
        return "accounting_import_only"
    if len(source_parts) <= 1 or not target_parts:
        return ""
    source_first = source_parts[0]
    target_first = target_parts[0]
    if source_first.startswith("."):
        return "drive_admin_folder"
    if target_first != source_first:
        return ""
    if re.match(r"^\d{2}_", target_first):
        schema_category = _schema_case_category(case_category)
        allowed = set(osc_case_subfolders(schema_category)) if schema_category else set()
        if target_first in allowed:
            return ""
        return f"unmapped_nas_numbered_drive_folder:{source_first}"
    return f"unmapped_drive_folder:{source_first}"


def _fit_filename_utf8(name: str, *, max_bytes: int = 220) -> str:
    """Fit a filename into SMB/APFS byte limits while preserving identity."""
    raw = str(name or "").strip()
    if not raw or len(raw.encode("utf-8")) <= max_bytes:
        return raw
    suffix = ""
    if "." in raw:
        dot = raw.rfind(".")
        suffix = raw[dot:]
        stem = raw[:dot]
    else:
        stem = raw
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    tail = f"-{digest}{suffix}"
    allowance = max(16, max_bytes - len(tail.encode("utf-8")))
    out = stem
    while out and len(out.encode("utf-8")) > allowance:
        out = out[:-1]
    out = out.rstrip(" -_，,；;（）()")
    if not out:
        out = "drive-file"
    return f"{out}{tail}"


def nas_filesystem_relative_path(relative_path: str) -> str:
    """Return a NAS-safe relative path without changing Drive naming rules."""
    parts = list(split_relative_parts(relative_path))
    if not parts:
        return ""
    parts[-1] = _fit_filename_utf8(parts[-1])
    return PurePosixPath(*parts).as_posix()


def safe_child_path(base: Path, relative_path: str) -> Path:
    rel = str(relative_path or "").replace("\\", "/").strip("/")
    parts = PurePosixPath(rel).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise DriveCaseSyncError(f"不安全的相對路徑：{relative_path}")
    if PurePosixPath(rel).is_absolute():
        raise DriveCaseSyncError(f"不允許絕對路徑：{relative_path}")
    base_resolved = base.resolve(strict=False)
    target = base_resolved.joinpath(*parts).resolve(strict=False)
    if os.path.commonpath([str(base_resolved), str(target)]) != str(base_resolved):
        raise DriveCaseSyncError(f"路徑超出案件資料夾：{relative_path}")
    return target


def _entry_public_dict(entry: FileEntry) -> dict[str, Any]:
    return {
        "path": entry.path,
        "relative_path": entry.relative_path,
        "name": entry.name,
        "is_folder": entry.is_folder,
        "modified_time": entry.modified_time,
        "size": entry.size,
        "md5": entry.md5,
        "drive_id": entry.drive_id,
        "web_url": entry.web_url,
        "mime_type": entry.mime_type,
    }


def _semantic_first_key(relative_path: str) -> str:
    semantic = semantic_relative_path(relative_path)
    if not semantic:
        return ""
    first = PurePosixPath(semantic).parts[0]
    return normalized_relative_file_key(first)


def _local_duplicate_keep_key(entry: FileEntry) -> tuple[int, int, int, str]:
    parts = split_relative_parts(entry.relative_path)
    nested = 1 if len(parts) > 2 else 0
    dated_child = 1 if len(parts) > 2 and re.match(r"^(?:19|20)\d{6}", parts[1]) else 0
    return (nested, dated_child, len(parts), normalized_relative_file_key(entry.relative_path))


def _local_duplicate_quarantine_path(case_root: Path, batch: str, relative_path: str) -> Path:
    quarantine_root = case_root / ".duplicates" / batch
    target = safe_child_path(quarantine_root, relative_path)
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for idx in range(2, 1000):
        candidate = target.with_name(f"{stem}_{idx}{suffix}")
        if not candidate.exists():
            return candidate
    digest = hashlib.sha1(str(relative_path).encode("utf-8")).hexdigest()[:8]
    return target.with_name(f"{stem}_{digest}{suffix}")


def build_local_duplicate_content_plan(
    local_root: str,
    local_entries: Iterable[FileEntry],
    *,
    execute: bool = False,
    repair_limit: int = 0,
    quarantine_batch: str = "",
    checkpoint: DriveFileCheckpoint | None = None,
) -> dict[str, Any]:
    """Plan or quarantine same-content local duplicates inside one case folder.

    The scope is deliberately narrow: files are grouped only within the same
    semantic first folder (for example ``我方書狀``).  This catches parent/child
    clutter without treating an attachment reused in a different document class
    as automatically disposable.
    """
    case_root = Path(str(local_root or ""))
    batch = quarantine_batch or datetime.now().strftime("%Y%m%d_%H%M%S")
    max_hash_bytes = int(os.environ.get("MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES") or DEFAULT_LOCAL_HASH_MAX_BYTES)
    summary = {
        "groups": 0,
        "duplicates_planned": 0,
        "duplicates_quarantined": 0,
        "duplicate_bytes": 0,
        "hash_errors": 0,
        "skipped_large_files": 0,
        "failed": 0,
        "stopped_by_limit": False,
        "execute": bool(execute),
    }
    size_buckets: dict[tuple[str, int], list[FileEntry]] = {}
    for entry in local_entries or []:
        if entry.is_folder or not entry.path or entry.size is None:
            continue
        semantic_first = _semantic_first_key(entry.relative_path)
        if not semantic_first:
            continue
        try:
            size = int(entry.size)
        except Exception as exc:
            if isinstance(exc, DriveCaseSyncDeadline):
                raise
            continue
        if max_hash_bytes > 0 and size > max_hash_bytes:
            summary["skipped_large_files"] += 1
            continue
        size_buckets.setdefault((semantic_first, size), []).append(entry)

    buckets: dict[tuple[str, int, str], list[FileEntry]] = {}
    for (semantic_first, size), same_size_entries in size_buckets.items():
        if len(same_size_entries) <= 1:
            continue
        for entry in same_size_entries:
            try:
                digest = _checkpointed_local_md5(entry, checkpoint)
            except Exception as exc:
                if isinstance(exc, DriveCaseSyncDeadline):
                    raise
                summary["hash_errors"] += 1
                continue
            buckets.setdefault((semantic_first, size, digest), []).append(entry)

    records: list[dict[str, Any]] = []
    for (_semantic_first, size, digest), entries in sorted(
        buckets.items(),
        key=lambda item: (item[0][0], item[0][1], item[0][2]),
    ):
        if len(entries) <= 1:
            continue
        summary["groups"] += 1
        canonical = sorted(entries, key=_local_duplicate_keep_key, reverse=True)[0]
        for duplicate in sorted((e for e in entries if e is not canonical), key=lambda e: e.relative_path):
            if repair_limit and summary["duplicates_planned"] >= repair_limit:
                summary["stopped_by_limit"] = True
                break
            target = _local_duplicate_quarantine_path(case_root, batch, duplicate.relative_path)
            record = {
                "status": "planned" if not execute else "",
                "reason": "same_content_elsewhere_in_case_folder",
                "canonical": _entry_public_dict(canonical),
                "duplicate": _entry_public_dict(duplicate),
                "canonical_semantic_key": normalized_relative_file_key(semantic_relative_path(canonical.relative_path)),
                "duplicate_semantic_key": normalized_relative_file_key(semantic_relative_path(duplicate.relative_path)),
                "size": size,
                "md5": digest,
                "quarantine_path": str(target),
            }
            summary["duplicates_planned"] += 1
            if execute:
                try:
                    source_path = Path(duplicate.path)
                    canonical_path = Path(canonical.path)
                    if not source_path.exists():
                        raise DriveCaseSyncError(f"duplicate_missing:{source_path}")
                    if not canonical_path.exists():
                        raise DriveCaseSyncError(f"canonical_missing:{canonical_path}")
                    if normalize_text(_checkpointed_local_md5(duplicate, checkpoint)) != digest:
                        raise DriveCaseSyncError(f"duplicate_hash_changed:{source_path}")
                    if normalize_text(_checkpointed_local_md5(canonical, checkpoint)) != digest:
                        raise DriveCaseSyncError(f"canonical_hash_changed:{canonical_path}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source_path), str(target))
                    record["status"] = "quarantined"
                    summary["duplicates_quarantined"] += 1
                    summary["duplicate_bytes"] += size
                except Exception as exc:
                    if isinstance(exc, DriveCaseSyncDeadline):
                        raise
                    record["status"] = "failed"
                    record["error"] = f"{type(exc).__name__}: {exc}"
                    summary["failed"] += 1
            records.append(record)
        if summary["stopped_by_limit"]:
            break
    return {
        "ok": summary["failed"] == 0,
        "mode": "local_duplicate_content_repair",
        "write_actions_enabled": bool(execute),
        "safety": "same_semantic_folder_same_md5_quarantine_no_delete_no_overwrite",
        "local_root": str(case_root),
        "quarantine_batch": batch,
        "summary": summary,
        "records": records,
    }


def repair_local_duplicate_content_files(
    local_root: str,
    *,
    execute: bool = False,
    repair_limit: int = 0,
    max_depth: int = 20,
    max_items: int = 10000,
    local_entries: Iterable[FileEntry] | None = None,
    quarantine_batch: str = "",
) -> dict[str, Any]:
    entries = list(local_entries) if local_entries is not None else local_descendant_context(
        local_root,
        max_depth=max_depth,
        max_items=max_items,
    )
    return build_local_duplicate_content_plan(
        local_root,
        entries,
        execute=execute,
        repair_limit=repair_limit,
        quarantine_batch=quarantine_batch,
    )


def repair_local_duplicate_content_for_cases(
    comparison: dict[str, Any],
    *,
    execute: bool = False,
    repair_limit: int = 0,
    max_case_depth: int = 20,
    max_case_items: int = 10000,
) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary = {
        "cases_scanned": 0,
        "cases_with_duplicates": 0,
        "groups": 0,
        "duplicates_planned": 0,
        "duplicates_quarantined": 0,
        "duplicate_bytes": 0,
        "case_errors": 0,
        "failed": 0,
        "stopped_by_limit": False,
        "execute": bool(execute),
    }
    records: list[dict[str, Any]] = []
    for item in comparison.get("matched", []) or []:
        if repair_limit and summary["duplicates_planned"] >= repair_limit:
            summary["stopped_by_limit"] = True
            break
        local: CaseFolder | None = item.get("local")
        local_root = str((local.local_path or local.path) if local else "")
        if not local_root:
            continue
        summary["cases_scanned"] += 1
        try:
            remaining = max(0, int(repair_limit or 0) - int(summary["duplicates_planned"] or 0))
            result = repair_local_duplicate_content_files(
                local_root,
                execute=execute,
                repair_limit=remaining if repair_limit else 0,
                max_depth=max_case_depth,
                max_items=max_case_items,
                quarantine_batch=stamp,
            )
        except Exception as exc:
            if isinstance(exc, DriveCaseSyncDeadline):
                raise
            summary["case_errors"] += 1
            summary["failed"] += 1
            records.append({
                "local_root": local_root,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        rs = result.get("summary") or {}
        if int(rs.get("duplicates_planned") or 0):
            summary["cases_with_duplicates"] += 1
            records.append(result)
        summary["groups"] += int(rs.get("groups") or 0)
        summary["duplicates_planned"] += int(rs.get("duplicates_planned") or 0)
        summary["duplicates_quarantined"] += int(rs.get("duplicates_quarantined") or 0)
        summary["duplicate_bytes"] += int(rs.get("duplicate_bytes") or 0)
        summary["failed"] += int(rs.get("failed") or 0)
        if rs.get("stopped_by_limit"):
            summary["stopped_by_limit"] = True
            break
    return {
        "ok": summary["failed"] == 0,
        "mode": "local_duplicate_content_case_repair",
        "write_actions_enabled": bool(execute),
        "safety": "same_semantic_folder_same_md5_quarantine_no_delete_no_overwrite",
        "quarantine_batch": stamp,
        "summary": summary,
        "records": records,
    }


def _find_same_content_local_entry(
    drive_entry: FileEntry,
    local_entries: Iterable[FileEntry],
    *,
    checkpoint: DriveFileCheckpoint | None = None,
) -> tuple[FileEntry | None, str, str]:
    """Find an already archived NAS file with the same size and hash.

    MAGI keeps this conservative: exact visible filename matches are always
    eligible for checksum verification; different filenames are eligible only
    inside the same semantic case folder, such as Drive ``我方書狀`` versus NAS
    ``04_我方歷次書狀`` and its child folders. This avoids parent/child duplicate
    downloads without turning the sync into unsafe global hash dedupe.
    """
    if drive_entry.size is None:
        return None, "", ""
    drive_rel = export_relative_path(drive_entry)
    drive_name_key = normalized_relative_file_key(PurePosixPath(drive_rel).name)
    drive_semantic_first_key = _semantic_first_key(drive_rel)
    max_hash_bytes = int(os.environ.get("MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES") or DEFAULT_LOCAL_HASH_MAX_BYTES)
    pending: tuple[FileEntry | None, str, str] = (None, "", "")
    for local_entry in local_entries:
        if local_entry.is_folder or not local_entry.path or local_entry.size is None:
            continue
        if int(local_entry.size) != int(drive_entry.size):
            continue
        local_name_key = normalized_relative_file_key(PurePosixPath(local_entry.relative_path).name)
        same_visible_name = local_name_key == drive_name_key
        local_semantic_first_key = _semantic_first_key(local_entry.relative_path)
        same_semantic_folder = bool(
            drive_semantic_first_key
            and local_semantic_first_key
            and drive_semantic_first_key == local_semantic_first_key
        )
        if not same_visible_name and not (drive_entry.md5 and same_semantic_folder):
            continue
        key = normalized_relative_file_key(semantic_relative_path(local_entry.relative_path))
        if not drive_entry.md5:
            if pending[0] is None:
                pending = (local_entry, key, "drive_checksum_missing")
            continue
        if max_hash_bytes > 0 and int(local_entry.size) > max_hash_bytes:
            if pending[0] is None:
                pending = (local_entry, key, "local_hash_deferred_large_file")
            continue
        try:
            local_md5 = _checkpointed_local_md5(local_entry, checkpoint)
        except Exception as exc:
            if isinstance(exc, DriveCaseSyncDeadline):
                raise
            if pending[0] is None:
                pending = (local_entry, key, f"local_hash_failed:{local_hash_failure_code(exc)}")
            continue
        if normalize_text(local_md5) == normalize_text(drive_entry.md5):
            return local_entry, key, "verified_checksum"
    return pending


def _drive_entry_downloadable(entry: FileEntry) -> bool:
    if entry.is_folder:
        return False
    mime = str(entry.mime_type or "")
    if mime.startswith("application/vnd.google-apps.") and mime not in GOOGLE_EXPORT_MIME_MAP:
        return False
    return True


def find_drive_child_folder(service: Any, parent_id: str, name: str) -> str:
    matches = [
        item for item in _drive_list_children(service, parent_id)
        if item.get("mimeType") == GOOGLE_FOLDER_MIME and str(item.get("name") or "") == name
    ]
    if len(matches) > 1:
        raise DriveCaseSyncError(f"duplicate_drive_child_folders:{parent_id}:{name}:{len(matches)}")
    return str(matches[0].get("id") or "") if matches else ""


def find_drive_child_file(service: Any, parent_id: str, name: str) -> str:
    item = find_drive_child_file_metadata(service, parent_id, name)
    return str(item.get("id") or "") if item else ""


def find_drive_child_file_metadata(service: Any, parent_id: str, name: str) -> dict[str, Any]:
    matches = [
        item for item in _drive_list_children(service, parent_id)
        if item.get("mimeType") != GOOGLE_FOLDER_MIME and str(item.get("name") or "") == name
    ]
    if len(matches) > 1:
        raise DriveCaseSyncError(f"duplicate_drive_child_files:{parent_id}:{name}:{len(matches)}")
    return dict(matches[0]) if matches else {}


def _drive_query_literal(value: str) -> str:
    return "'" + str(value or "").replace("\\", "\\\\").replace("'", "\\'") + "'"


def create_drive_folder(
    service: Any,
    parent_id: str,
    name: str,
    *,
    app_properties: dict[str, str] | None = None,
) -> str:
    body = {"name": name, "mimeType": GOOGLE_FOLDER_MIME, "parents": [parent_id]}
    clean_props = {
        str(k): str(v)
        for k, v in (app_properties or {}).items()
        if str(k or "").strip() and str(v or "").strip()
    }
    clean_props.setdefault(
        "magi_sync_folder_key",
        hashlib.sha256(f"{parent_id}\0{name}".encode("utf-8")).hexdigest(),
    )
    if clean_props:
        body["appProperties"] = clean_props
    created = _drive_execute_with_timeout(service.files().create(
        body=body,
        supportsAllDrives=True,
        fields="id,name,webViewLink",
    ), context=f"create_folder:{parent_id}:{name}")
    return str(created.get("id") or "")


def find_drive_child_folder_by_osc_case_number(service: Any, parent_id: str, case_number: str) -> str:
    case_no = str(case_number or "").strip()
    if not case_no:
        return ""
    resp = _drive_execute_with_timeout(service.files().list(
        q=(
            f"'{parent_id}' in parents and trashed = false "
            f"and mimeType = '{GOOGLE_FOLDER_MIME}' "
            f"and appProperties has {{ key='magi_osc_case_number' and value={_drive_query_literal(case_no)} }}"
        ),
        spaces="drive",
        corpora="allDrives",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=10,
        fields="files(id,name,appProperties)",
    ), context=f"find_child_by_osc:{parent_id}:{case_no}")
    files = resp.get("files", []) if isinstance(resp, dict) else []
    if len(files) == 1:
        return str(files[0].get("id") or "")
    return ""


def _drive_folder_relative_path_to_root(
    service: Any,
    folder_id: str,
    root_folder_id: str,
    *,
    max_hops: int = 12,
) -> str:
    """Return a folder path relative to the configured Drive root.

    Google Drive search is global, so a name hit must still be proven to live
    under the user's `案件辦理` root before MAGI may treat it as a case folder.
    """
    current = str(folder_id or "").strip()
    root_id = str(root_folder_id or "").strip()
    if not current or not root_id:
        return ""
    names: list[str] = []
    seen: set[str] = set()
    for _ in range(max(1, max_hops)):
        if current in seen:
            return ""
        seen.add(current)
        item = _drive_execute_with_timeout(service.files().get(
            fileId=current,
            supportsAllDrives=True,
            fields="id,name,parents,mimeType,appProperties,modifiedTime,webViewLink,driveId",
        ), context=f"folder_path:{current}")
        item_id = str(item.get("id") or "")
        if item_id == root_id:
            return PurePosixPath(*reversed(names)).as_posix() if names else ""
        names.append(str(item.get("name") or ""))
        parents = item.get("parents") or []
        if not parents:
            return ""
        current = str(parents[0] or "")
    return ""


def _drive_case_search_tokens(case: CaseFolder) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    display_name = drive_case_display_name_for_local_case(case)
    candidates = [
        case.meta.case_number,
        case.meta.laf_case_no,
        case.meta.court_case_no,
        case.meta.client_hint,
        case.meta.reason_hint,
        display_name,
        re.sub(r"[\-_－—]+", " ", display_name),
    ]
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        normalized = normalize_text(text)
        if normalized in seen:
            continue
        if (
            OSC_CASE_RE.fullmatch(text)
            or LAF_CASE_RE.fullmatch(text)
            or ROC_COURT_NO_RE.search(text)
            or len(normalized) >= 3
        ):
            seen.add(normalized)
            tokens.append(text)
    return tokens


def _search_drive_folders_by_name_tokens(
    service: Any,
    tokens: Iterable[str],
    *,
    page_size: int = 25,
    max_results: int = 80,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for token in tokens:
        text = str(token or "").strip()
        if not text:
            continue
        resp = _drive_execute_with_timeout(service.files().list(
            q=(
                f"mimeType = '{GOOGLE_FOLDER_MIME}' and trashed = false "
                f"and name contains {_drive_query_literal(text)}"
            ),
            spaces="drive",
            corpora="allDrives",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=max(1, min(100, page_size)),
            fields="files(id,name,mimeType,parents,modifiedTime,webViewLink,driveId,appProperties)",
        ), context=f"search_folder_token:{text}")
        for item in resp.get("files", []) if isinstance(resp, dict) else []:
            item_id = str(item.get("id") or "")
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            out.append(item)
            if len(out) >= max_results:
                return out
    return out


def _drive_case_from_search_candidate(
    item: dict[str, Any],
    relative_path: str,
    *,
    local_case: CaseFolder,
) -> CaseFolder:
    name = str(item.get("name") or PurePosixPath(relative_path).name)
    cls = classify_drive_case_folder(relative_path) or {}
    meta = extract_case_meta(name)
    app_props = item.get("appProperties") or {}
    if isinstance(app_props, dict):
        hidden_case_no = str(app_props.get("magi_osc_case_number") or "").strip()
        if OSC_CASE_RE.fullmatch(hidden_case_no):
            meta.case_number = hidden_case_no
        hidden_laf_no = str(app_props.get("magi_laf_case_no") or "").strip()
        if LAF_CASE_RE.fullmatch(hidden_laf_no):
            meta.laf_case_no = hidden_laf_no
    return CaseFolder(
        source="drive",
        path=relative_path,
        relative_path=relative_path,
        name=name,
        category=str(cls.get("category") or local_case.category),
        status=str(cls.get("status") or ("closed" if relative_path.startswith("結案案件/") else local_case.status or "active")),
        case_kind=str(cls.get("case_kind") or local_case.case_kind),
        owner_bucket=str(cls.get("owner_bucket") or drive_owner_bucket()),
        modified_time=str(item.get("modifiedTime") or ""),
        meta=meta,
        drive_id=str(item.get("id") or ""),
        web_url=str(item.get("webViewLink") or ""),
    )


def _score_drive_search_candidate(
    candidate: CaseFolder,
    local_case: CaseFolder,
    *,
    score_forbidden_for_diagnostics: bool = False,
) -> tuple[int, list[str]]:
    exclusion_reason = drive_case_sync_exclusion_reason(
        candidate.relative_path or candidate.path,
        require_canonical_layout=True,
    )
    if exclusion_reason and not score_forbidden_for_diagnostics:
        return -1000, [exclusion_reason]
    text = normalize_text(" ".join([
        candidate.name,
        candidate.relative_path,
        candidate.meta.case_number,
        candidate.meta.laf_case_no,
        candidate.meta.court_case_no,
        candidate.meta.client_hint,
        candidate.meta.reason_hint,
    ]))
    matched: list[str] = []
    score = 0
    local_laf = str(local_case.meta.laf_case_no or "").strip()
    candidate_laf = str(candidate.meta.laf_case_no or "").strip()
    if local_laf and candidate_laf and normalize_text(local_laf) != normalize_text(candidate_laf):
        return -1000, [f"法扶案號不同:{candidate_laf}"]
    local_case_no = str(local_case.meta.case_number or "").strip()
    if local_case_no and _contains_term(text, local_case_no):
        score += 90
        matched.append(local_case_no)
    if local_laf and _contains_term(text, local_laf):
        score += 100
        matched.append(local_laf)
    court_no = str(local_case.meta.court_case_no or "").strip()
    if court_no and normalize_court_case_no(court_no) in text:
        score += 80
        matched.append(court_no)
    client = str(local_case.meta.client_hint or "").strip()
    if client and _contains_term(text, client):
        score += 35
        matched.append(client)
    reason = str(local_case.meta.reason_hint or "").strip()
    if reason and reason not in GENERIC_CONTEXT_TERMS and _contains_term(text, reason):
        score += 25
        matched.append(reason)
    if candidate.category and local_case.category and normalize_drive_case_category(candidate.category) == normalize_drive_case_category(local_case.category):
        score += 5
    if candidate.status and local_case.status and candidate.status == local_case.status:
        score += 5
    elif candidate.status and local_case.status and candidate.status != local_case.status:
        score -= 20
    if candidate.case_kind and local_case.case_kind and candidate.case_kind == local_case.case_kind:
        score += 5
    return score, sorted(set(matched), key=lambda x: (-len(x), x))


def find_drive_case_folder_by_broad_search(
    service: Any,
    drive_root_id: str,
    case: CaseFolder,
    *,
    min_score: int = 50,
) -> dict[str, Any]:
    """Fallback Drive search for legacy folders outside MAGI's expected path."""
    tokens = _drive_case_search_tokens(case)
    if not tokens:
        return {"ok": False, "skipped": True, "reason": "no_broad_search_tokens"}
    candidates: list[dict[str, Any]] = []
    rejected_candidates: list[dict[str, Any]] = []
    for item in _search_drive_folders_by_name_tokens(service, tokens):
        rel = _drive_folder_relative_path_to_root(service, str(item.get("id") or ""), drive_root_id)
        if not rel:
            continue
        drive_case = _drive_case_from_search_candidate(item, rel, local_case=case)
        score, matched_terms = _score_drive_search_candidate(
            drive_case,
            case,
            score_forbidden_for_diagnostics=True,
        )
        if score < min_score:
            continue
        exclusion_reason = drive_case_sync_exclusion_reason(
            drive_case.relative_path,
            require_canonical_layout=True,
        )
        if exclusion_reason:
            rejected_candidates.append({
                "relative_path": drive_case.relative_path,
                "drive_id": drive_case.drive_id,
                "score": score,
                "matched_terms": matched_terms,
                "exclusion_reason": exclusion_reason,
            })
            continue
        candidates.append({
            "drive_case": drive_case,
            "score": score,
            "matched_terms": matched_terms,
        })
    candidates.sort(key=lambda x: int(x.get("score") or 0), reverse=True)
    if not candidates:
        return {
            "ok": False,
            "skipped": True,
            "reason": (
                "drive_case_folder_broad_search_forbidden_candidates"
                if rejected_candidates
                else "drive_case_folder_missing_after_broad_search"
            ),
            "searched_tokens": tokens,
            "candidates": rejected_candidates[:10],
        }
    if len(candidates) > 1 and int(candidates[0]["score"]) <= int(candidates[1]["score"]) + 10:
        return {
            "ok": False,
            "skipped": True,
            "reason": "drive_case_folder_broad_search_ambiguous",
            "searched_tokens": tokens,
            "candidates": [
                {
                    "relative_path": item["drive_case"].relative_path,
                    "drive_id": item["drive_case"].drive_id,
                    "score": item["score"],
                    "matched_terms": item["matched_terms"],
                }
                for item in candidates[:10]
            ],
            "rejected_candidates": rejected_candidates[:10],
        }
    best = candidates[0]
    drive_case: CaseFolder = best["drive_case"]
    return {
        "ok": True,
        "drive_id": drive_case.drive_id,
        "relative_path": drive_case.relative_path,
        "created_folders": [],
        "created_count": 0,
        "status": "existing_by_broad_search",
        "case_number": case.meta.case_number,
        "case_name": case.name,
        "drive_visible_name": drive_case.name,
        "search_score": best["score"],
        "matched_terms": best["matched_terms"],
        "searched_tokens": tokens,
        "rejected_candidates": rejected_candidates[:10],
    }


def _blocked_broad_search_result(
    broad: dict[str, Any],
    case: CaseFolder,
    expected_relative_path: str,
) -> dict[str, Any] | None:
    if broad.get("reason") != "drive_case_folder_broad_search_forbidden_candidates":
        return None
    result = dict(broad)
    result.update({
        "ok": False,
        "skipped": True,
        "status": "blocked_forbidden_drive_case_candidates",
        "relative_path": expected_relative_path,
        "case_number": case.meta.case_number,
        "case_name": case.name,
        "created_folders": [],
        "created_count": 0,
        "message": (
            "Broad search 只找到待整理/重複/衝突隔離路徑；"
            "本案 fail-closed，不綁定該資料夾也不新建 Drive 案件資料夾。"
        ),
    })
    return result


def find_duplicate_drive_case_folders_for_local_case(
    service: Any,
    drive_root_id: str,
    case: CaseFolder,
    *,
    min_score: int = 50,
) -> list[CaseFolder]:
    """Search the Drive tree for multiple folders for one local case.

    This is used by the direct DB sync path, where MAGI may not have a full
    Drive inventory.  It prevents the worker from syncing against one folder
    while another folder with the same OSC/LAF/court identity remains alive.
    """
    identity_key = _primary_duplicate_identity_key(case)
    if not identity_key:
        return []
    found: dict[str, CaseFolder] = {}
    try:
        searched_items = _search_drive_folders_by_name_tokens(service, _drive_case_search_tokens(case))
    except AttributeError:
        # Unit tests use tiny fake service objects that only implement the exact
        # child-folder calls under test.  A real Drive service exposes files().
        return []
    for item in searched_items:
        folder_id = str(item.get("id") or "")
        if not folder_id:
            continue
        rel = _drive_folder_relative_path_to_root(service, folder_id, drive_root_id)
        if not rel:
            continue
        drive_case = _drive_case_from_search_candidate(item, rel, local_case=case)
        if drive_case_sync_exclusion_reason(
            drive_case.relative_path,
            require_canonical_layout=True,
        ):
            continue
        score, _matched_terms = _score_drive_search_candidate(drive_case, case)
        if score < min_score:
            continue
        candidate_key = _primary_duplicate_identity_key(drive_case)
        stable_match = False
        if candidate_key and candidate_key == identity_key:
            stable_match = True
        elif case.meta.laf_case_no and normalize_text(case.meta.laf_case_no) in normalize_text(drive_case.relative_path):
            stable_match = True
        elif case.meta.case_number and normalize_text(case.meta.case_number) in normalize_text(drive_case.relative_path):
            stable_match = True
        if not stable_match:
            continue
        found[drive_case.drive_id or drive_case.relative_path] = drive_case
    return _collapse_descendant_drive_case_candidates(found.values())


def _relative_path_is_descendant(child: str, parent: str) -> bool:
    child_parts = split_relative_parts(child)
    parent_parts = split_relative_parts(parent)
    return (
        bool(child_parts)
        and bool(parent_parts)
        and len(child_parts) > len(parent_parts)
        and child_parts[: len(parent_parts)] == parent_parts
    )


def _collapse_descendant_drive_case_candidates(candidates: Iterable[CaseFolder]) -> list[CaseFolder]:
    """Keep parent case folders when Drive search also hits child folders.

    Google Drive name search can return folders below the case folder, such as
    evidence subfolders that repeat a party name or case number.  Those are
    not duplicate case folders and should not block incremental sync.
    """
    kept: list[CaseFolder] = []
    ordered = sorted(
        [c for c in candidates if str(c.relative_path or "").strip()],
        key=lambda c: (len(split_relative_parts(c.relative_path)), c.relative_path, c.drive_id),
    )
    for candidate in ordered:
        if any(_relative_path_is_descendant(candidate.relative_path, parent.relative_path) for parent in kept):
            continue
        kept = [
            existing
            for existing in kept
            if not _relative_path_is_descendant(existing.relative_path, candidate.relative_path)
        ]
        kept.append(candidate)
    return sorted(kept, key=lambda c: c.relative_path)


def _duplicate_drive_case_result(case: CaseFolder, duplicates: list[CaseFolder]) -> dict[str, Any]:
    return {
        "ok": False,
        "skipped": True,
        "reason": "duplicate_drive_case_folders",
        "case_number": case.meta.case_number,
        "case_name": case.name,
        "identity_key": _primary_duplicate_identity_key(case),
        "candidates": [
            {
                "relative_path": d.relative_path,
                "drive_id": d.drive_id,
                "name": d.name,
                "web_url": d.web_url,
            }
            for d in duplicates
        ],
        "message": "Google Drive 端同一案件有多個資料夾；為避免 NAS/Drive 混檔，本案同步已阻斷，請先合併或排除重複資料夾。",
    }


def update_drive_folder_app_properties(
    service: Any,
    folder_id: str,
    app_properties: dict[str, str],
) -> None:
    update_drive_folder_metadata(service, folder_id, app_properties=app_properties)


def update_drive_folder_metadata(
    service: Any,
    folder_id: str,
    *,
    name: str = "",
    app_properties: dict[str, str] | None = None,
) -> None:
    body: dict[str, Any] = {}
    if str(name or "").strip():
        body["name"] = str(name).strip()
    clean_props = {
        str(k): str(v)
        for k, v in (app_properties or {}).items()
        if str(k or "").strip() and str(v or "").strip()
    }
    if clean_props:
        body["appProperties"] = clean_props
    if not body:
        return
    _drive_execute_with_timeout(service.files().update(
        fileId=folder_id,
        body=body,
        supportsAllDrives=True,
        fields="id,name,appProperties",
    ), context=f"update_folder:{folder_id}")


def move_drive_item(
    service: Any,
    item_id: str,
    *,
    add_parent_id: str,
    remove_parent_ids: Iterable[str] = (),
    new_name: str = "",
) -> dict[str, Any]:
    """Move or rename one Drive item without downloading it."""
    body: dict[str, Any] = {}
    if str(new_name or "").strip():
        body["name"] = str(new_name).strip()
    return _drive_execute_with_timeout(service.files().update(
        fileId=item_id,
        addParents=add_parent_id,
        removeParents=",".join(str(x) for x in remove_parent_ids if str(x or "").strip()),
        body=body,
        supportsAllDrives=True,
        fields="id,name,mimeType,parents,modifiedTime,size,md5Checksum,webViewLink,driveId",
    ), context=f"move_drive_item:{item_id}")


def trash_drive_item(service: Any, item_id: str) -> dict[str, Any]:
    """Move one Drive item to trash after its content was safely merged."""
    return _drive_execute_with_timeout(service.files().update(
        fileId=item_id,
        body={"trashed": True},
        supportsAllDrives=True,
        fields="id,name,mimeType,parents,trashed,modifiedTime,size,md5Checksum,webViewLink,driveId",
    ), context=f"trash_drive_item:{item_id}")


def rename_drive_item(service: Any, item_id: str, new_name: str) -> dict[str, Any]:
    """Rename one Drive item in place without changing its parent."""
    return _drive_execute_with_timeout(service.files().update(
        fileId=item_id,
        body={"name": str(new_name or "").strip()},
        supportsAllDrives=True,
        fields="id,name,mimeType,parents,modifiedTime,size,md5Checksum,webViewLink,driveId",
    ), context=f"rename_drive_item:{item_id}")


def _drive_item_metadata(service: Any, item_id: str) -> dict[str, Any]:
    return _drive_execute_with_timeout(service.files().get(
        fileId=item_id,
        supportsAllDrives=True,
        fields="id,name,mimeType,parents,modifiedTime,size,md5Checksum,webViewLink,driveId,appProperties",
    ), context=f"get_item:{item_id}")


def ensure_drive_folder_path(service: Any, root_folder_id: str, relative_folder_path: str) -> dict[str, Any]:
    rel = str(relative_folder_path or "").replace("\\", "/").strip("/")
    parts = PurePosixPath(rel).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise DriveCaseSyncError(f"不安全的雲端資料夾路徑：{relative_folder_path}")
    current = root_folder_id
    created: list[str] = []
    walked: list[str] = []
    for part in parts:
        walked.append(part)
        found = find_drive_child_folder(service, current, part)
        if found:
            current = found
            continue
        current = create_drive_folder(service, current, part)
        created.append(PurePosixPath(*walked).as_posix())
    return {
        "ok": True,
        "drive_id": current,
        "relative_path": PurePosixPath(*parts).as_posix(),
        "created_folders": created,
        "created_count": len(created),
    }


def ensure_drive_case_folder_for_local_case(
    service: Any,
    drive_root_id: str,
    case: CaseFolder,
    *,
    owner_bucket: str | None = None,
) -> dict[str, Any]:
    if not _has_strong_case_identity(case):
        return {
            "ok": False,
            "skipped": True,
            "reason": "missing_stable_case_identity",
            "case": _case_to_dict(case),
        }
    relative_path = drive_relative_path_for_local_case(case, owner_bucket=owner_bucket)
    if not relative_path:
        return {
            "ok": False,
            "skipped": True,
            "reason": "cannot_build_drive_case_path",
            "case": _case_to_dict(case),
        }
    parts = split_relative_parts(relative_path)
    if not parts:
        return {
            "ok": False,
            "skipped": True,
            "reason": "empty_drive_case_path",
            "case": _case_to_dict(case),
        }
    try:
        duplicates = find_duplicate_drive_case_folders_for_local_case(service, drive_root_id, case)
    except Exception as exc:
        return {
            "ok": False,
            "skipped": True,
            "reason": "drive_duplicate_probe_failed",
            "case_number": case.meta.case_number,
            "case_name": case.name,
            "message": f"建立/同步前的 Google Drive 重複資料夾探測失敗：{type(exc).__name__}: {exc}",
        }
    if len(duplicates) > 1:
        return _duplicate_drive_case_result(case, duplicates)
    parent_parts = parts[:-1]
    case_folder_name = parts[-1]
    case_number = (case.meta.case_number or extract_case_meta(case.name).case_number or "").strip()
    app_props = {
        "magi_osc_case_number": case_number,
        "magi_source": "osc",
    }
    laf_case_no = (case.meta.laf_case_no or extract_case_meta(case.name).laf_case_no or "").strip()
    if laf_case_no:
        app_props["magi_laf_case_no"] = laf_case_no
    if parent_parts:
        probe_parent = drive_root_id
        parent_missing = False
        for part in parent_parts:
            found = find_drive_child_folder(service, probe_parent, part)
            if not found:
                parent_missing = True
                break
            probe_parent = found
        if parent_missing:
            broad = find_drive_case_folder_by_broad_search(service, drive_root_id, case)
            if broad.get("ok"):
                update_drive_folder_app_properties(service, str(broad.get("drive_id") or ""), app_props)
                return broad
            blocked = _blocked_broad_search_result(broad, case, relative_path)
            if blocked:
                return blocked
    created_folders: list[str] = []
    if parent_parts:
        parent_result = ensure_drive_folder_path(service, drive_root_id, PurePosixPath(*parent_parts).as_posix())
        parent_id = str(parent_result.get("drive_id") or "")
        created_folders.extend(parent_result.get("created_folders") or [])
    else:
        parent_id = drive_root_id
    folder_id = find_drive_child_folder_by_osc_case_number(service, parent_id, case_number)
    if folder_id:
        status = "existing_by_osc_metadata"
        update_drive_folder_metadata(service, folder_id, name=case_folder_name, app_properties=app_props)
    else:
        folder_id = find_drive_child_folder(service, parent_id, case_folder_name)
        if folder_id:
            status = "existing_by_name"
            update_drive_folder_app_properties(service, folder_id, app_props)
        else:
            legacy_name = (case.name or "").strip()
            legacy_id = ""
            if legacy_name and legacy_name != case_folder_name:
                legacy_id = find_drive_child_folder(service, parent_id, legacy_name)
            if legacy_id:
                folder_id = legacy_id
                update_drive_folder_metadata(service, folder_id, name=case_folder_name, app_properties=app_props)
                status = "renamed_legacy_osc_number_folder"
            else:
                broad = find_drive_case_folder_by_broad_search(service, drive_root_id, case)
                if broad.get("ok"):
                    folder_id = str(broad.get("drive_id") or "")
                    update_drive_folder_app_properties(service, folder_id, app_props)
                    status = str(broad.get("status") or "existing_by_broad_search")
                    result = dict(broad)
                    result["created_folders"] = created_folders
                    result["created_count"] = len(created_folders)
                    return result
                blocked = _blocked_broad_search_result(broad, case, relative_path)
                if blocked:
                    return blocked
                folder_id = create_drive_folder(service, parent_id, case_folder_name, app_properties=app_props)
                created_folders.append(PurePosixPath(*parts).as_posix())
                status = "created"
    result = {
        "ok": True,
        "drive_id": folder_id,
        "relative_path": PurePosixPath(*parts).as_posix(),
        "created_folders": created_folders,
        "created_count": len(created_folders),
        "status": status,
    }
    result["case_number"] = case_number
    result["case_name"] = case.name
    result["drive_visible_name"] = case_folder_name
    return result


def find_existing_drive_case_folder_for_local_case(
    service: Any,
    drive_root_id: str,
    case: CaseFolder,
    *,
    owner_bucket: str | None = None,
) -> dict[str, Any]:
    """Find the Drive case folder for a local OSC case without creating it."""
    relative_path = drive_relative_path_for_local_case(case, owner_bucket=owner_bucket)
    if not relative_path:
        return {
            "ok": False,
            "skipped": True,
            "reason": "cannot_build_drive_case_path",
            "case": _case_to_dict(case),
        }
    parts = split_relative_parts(relative_path)
    if not parts:
        return {
            "ok": False,
            "skipped": True,
            "reason": "empty_drive_case_path",
            "case": _case_to_dict(case),
        }
    try:
        duplicates = find_duplicate_drive_case_folders_for_local_case(service, drive_root_id, case)
    except Exception as exc:
        return {
            "ok": False,
            "skipped": True,
            "reason": "drive_duplicate_probe_failed",
            "relative_path": relative_path,
            "case_number": case.meta.case_number,
            "message": f"查找前的 Google Drive 重複資料夾探測失敗：{type(exc).__name__}: {exc}",
        }
    if len(duplicates) > 1:
        return _duplicate_drive_case_result(case, duplicates)
    current = drive_root_id
    for part in parts[:-1]:
        found = find_drive_child_folder(service, current, part)
        if not found:
            return {
                "ok": False,
                "skipped": True,
                "reason": "drive_parent_folder_missing",
                "relative_path": relative_path,
                "missing_part": part,
            }
        current = found
    case_folder_name = parts[-1]
    case_number = (case.meta.case_number or extract_case_meta(case.name).case_number or "").strip()
    folder_id = find_drive_child_folder_by_osc_case_number(service, current, case_number)
    status = "existing_by_osc_metadata"
    if not folder_id:
        folder_id = find_drive_child_folder(service, current, case_folder_name)
        status = "existing_by_name" if folder_id else "missing"
    if not folder_id:
        legacy_name = (case.name or "").strip()
        if legacy_name and legacy_name != case_folder_name:
            folder_id = find_drive_child_folder(service, current, legacy_name)
            status = "existing_by_legacy_osc_number_folder" if folder_id else "missing"
    if not folder_id:
        broad = find_drive_case_folder_by_broad_search(service, drive_root_id, case)
        if broad.get("ok"):
            return broad
        return {
            "ok": False,
            "skipped": True,
            "reason": broad.get("reason") or "drive_case_folder_missing",
            "relative_path": relative_path,
            "case_number": case_number,
            "searched_tokens": broad.get("searched_tokens") or [],
            "candidates": broad.get("candidates") or [],
        }
    return {
        "ok": True,
        "drive_id": folder_id,
        "relative_path": PurePosixPath(*parts).as_posix(),
        "created_folders": [],
        "created_count": 0,
        "status": status,
        "case_number": case_number,
        "case_name": case.name,
        "drive_visible_name": case_folder_name,
    }


def ensure_drive_case_folder_for_new_case(
    service: Any,
    drive_root_id: str,
    *,
    full_path: str,
    case_number: str,
    client_name: str,
    case_category: str,
    case_type: str,
    laf_case_no: str = "",
    case_stage: str = "",
    case_reason: str = "",
    status: str = "active",
    owner_bucket: str | None = None,
) -> dict[str, Any]:
    case_name = PurePosixPath(str(full_path or "").replace("\\", "/").rstrip("/")).name
    if not case_name:
        chunks = [case_number, client_name, case_stage, case_reason]
        case_name = "-".join(str(c or "").strip() for c in chunks if str(c or "").strip())
    case = CaseFolder(
        source="nas",
        path=str(full_path or ""),
        local_path=str(full_path or ""),
        relative_path="",
        name=case_name,
        category=case_category or "一般案件",
        status=status or "active",
        case_kind=case_type or "",
        meta=CaseMeta(
            case_number=case_number or extract_case_meta(case_name).case_number,
            laf_case_no=laf_case_no or extract_case_meta(case_name).laf_case_no,
            client_hint=client_name or extract_case_meta(case_name).client_hint,
            reason_hint=case_reason or extract_case_meta(case_name).reason_hint,
        ),
    )
    return ensure_drive_case_folder_for_local_case(
        service,
        drive_root_id,
        case,
        owner_bucket=owner_bucket,
    )


def ensure_drive_parent_folder(service: Any, root_folder_id: str, relative_file_path: str) -> tuple[str, list[str]]:
    parent = PurePosixPath(str(relative_file_path or "").replace("\\", "/")).parent
    if str(parent) in {"", "."}:
        return root_folder_id, []
    current = root_folder_id
    created: list[str] = []
    for part in parent.parts:
        if part in {"", ".", ".."}:
            raise DriveCaseSyncError(f"不安全的雲端相對資料夾：{relative_file_path}")
        found = find_drive_child_folder(service, current, part)
        if found:
            current = found
            continue
        current = create_drive_folder(service, current, part)
        created.append(part)
    return current, created


def upload_local_file_to_drive(
    service: Any,
    *,
    local_path: Path,
    drive_case_folder_id: str,
    relative_path: str,
    checkpoint: DriveFileCheckpoint | None = None,
    checkpoint_item_token: str = "",
    checkpoint_source_fingerprint: str = "",
) -> dict[str, Any]:
    if not local_path.exists() or not local_path.is_file():
        raise DriveCaseSyncError(f"找不到可上傳檔案：{local_path}")
    from googleapiclient.http import MediaFileUpload

    skip_reason = nas_to_drive_upload_skip_reason(relative_path, relative_path)
    if skip_reason:
        raise DriveCaseSyncError(f"invalid_drive_upload_layout:{skip_reason}:{relative_path}")
    local_size = int(local_path.stat().st_size)
    local_md5 = ""
    if (
        checkpoint is not None
        and len(checkpoint_item_token) == 64
        and len(checkpoint_source_fingerprint) == 64
    ):
        local_md5 = checkpoint.cached_hash(
            checkpoint_item_token, checkpoint_source_fingerprint
        )
    if not local_md5:
        local_md5 = local_file_md5(str(local_path))
        if (
            checkpoint is not None
            and len(checkpoint_item_token) == 64
            and len(checkpoint_source_fingerprint) == 64
        ):
            checkpoint.cache_hash(
                checkpoint_item_token,
                checkpoint_source_fingerprint,
                local_md5,
            )
    parent_id, created_folders = ensure_drive_parent_folder(service, drive_case_folder_id, relative_path)
    name = PurePosixPath(str(relative_path).replace("\\", "/")).name
    existing = find_drive_child_file_metadata(service, parent_id, name)
    if existing:
        existing_size = int(existing["size"]) if str(existing.get("size") or "").isdigit() else None
        existing_md5 = str(existing.get("md5Checksum") or existing.get("md5") or "")
        if existing_size == local_size and existing_md5 and normalize_text(existing_md5) == normalize_text(local_md5):
            destination_proof = drive_checkpoint_proof_hash(
                "upload", existing_size, existing_md5, str(existing.get("id") or "")
            )
            return {
                "status": "skipped_existing",
                "drive_id": str(existing.get("id") or ""),
                "web_url": str(existing.get("webViewLink") or ""),
                "bytes": 0,
                "created_folders": created_folders,
                "hash_verification": "verified_checksum",
                "destination_proof": destination_proof,
            }
        return {
            "status": (
                "pending_existing_unverified"
                if existing_size == local_size and not existing_md5
                else "pending_existing_conflict"
            ),
            "drive_id": str(existing.get("id") or ""),
            "web_url": str(existing.get("webViewLink") or ""),
            "bytes": 0,
            "created_folders": created_folders,
            "reason": "drive_existing_checksum_missing" if not existing_md5 else "drive_existing_checksum_differs",
            "local_md5": local_md5,
            "drive_md5": existing_md5,
        }
    resumable_min, upload_chunk = _drive_upload_transport_settings()
    upload_attempts = max(
        1,
        int(os.environ.get("MAGI_DRIVE_SYNC_UPLOAD_ATTEMPTS") or "3"),
    )
    upload_key = hashlib.sha256(
        f"{parent_id}\0{name}\0{local_md5}".encode("utf-8")
    ).hexdigest()
    created: dict[str, Any] = {}
    recovered_after_transient = False
    force_nonresumable = False
    for attempt in range(1, upload_attempts + 1):
        # Recreate MediaFileUpload and the request for every outer attempt.  A
        # failed resumable request can retain a stale upload URI and must not be
        # reused after RedirectMissingLocation or a socket timeout.
        media = MediaFileUpload(
            str(local_path),
            chunksize=upload_chunk,
            resumable=(
                bool(resumable_min and local_size >= resumable_min)
                and not force_nonresumable
            ),
        )
        try:
            created = _drive_execute_with_timeout(service.files().create(
                body={
                    "name": name,
                    "parents": [parent_id],
                    "appProperties": {"magi_sync_file_key": upload_key},
                },
                media_body=media,
                supportsAllDrives=True,
                fields="id,name,size,md5Checksum,webViewLink",
            ), context=f"upload_file:{parent_id}:{name}:attempt{attempt}")
            break
        except Exception as exc:
            if isinstance(exc, DriveCaseSyncDeadline):
                raise
            if not _drive_upload_error_is_transient(exc) or attempt >= upload_attempts:
                raise
            # The server may have committed the object even though the client
            # lost the final response.  Prove whether it exists before retrying
            # so a transport retry can never manufacture a duplicate file.
            try:
                recovered = find_drive_child_file_metadata(service, parent_id, name)
            except Exception as verify_exc:
                if isinstance(verify_exc, DriveCaseSyncDeadline):
                    raise
                raise DriveCaseSyncError(
                    "drive_upload_ambiguous_recheck_failed:"
                    f"{parent_id}:{name}:{type(verify_exc).__name__}"
                ) from exc
            if recovered:
                recovered_size = (
                    int(recovered["size"])
                    if str(recovered.get("size") or "").isdigit()
                    else None
                )
                recovered_md5 = str(
                    recovered.get("md5Checksum") or recovered.get("md5") or ""
                )
                if (
                    recovered_size == local_size
                    and recovered_md5
                    and normalize_text(recovered_md5) == normalize_text(local_md5)
                ):
                    created = recovered
                    recovered_after_transient = True
                    break
                raise DriveCaseSyncError(
                    "drive_upload_ambiguous_existing_unverified:"
                    f"{parent_id}:{name}"
                ) from exc
            # A resumable-upload initiation can occasionally be redirected
            # without a Location header. Repeating the same transport mode
            # reproduces that failure. Only after proving the object was not
            # committed, fall back to a non-resumable request for this file.
            if _drive_upload_redirect_missing_location(exc):
                force_nonresumable = True
            time.sleep(min(2.0, 0.5 * attempt))
    created_md5 = str(created.get("md5Checksum") or "")
    created_size = (
        int(created["size"])
        if str(created.get("size") or "").isdigit()
        else None
    )
    created_verified = bool(
        created_md5
        and created_size == local_size
        and normalize_text(created_md5) == normalize_text(local_md5)
    )
    status = (
        "skipped_existing"
        if recovered_after_transient and created_verified
        else "uploaded"
        if created_verified
        else "pending_uploaded_unverified"
    )
    destination_proof = (
        drive_checkpoint_proof_hash(
            "upload", created_size, created_md5, str(created.get("id") or "")
        )
        if created_verified
        else ""
    )
    return {
        "status": status,
        "drive_id": str(created.get("id") or ""),
        "web_url": str(created.get("webViewLink") or ""),
        "bytes": 0 if recovered_after_transient else int(created_size or 0),
        "created_folders": created_folders,
        "md5": created_md5,
        "hash_verification": "verified_checksum" if created_verified else "",
        "destination_proof": destination_proof,
        "recovered_after_transient": recovered_after_transient,
    }


def _drive_upload_error_is_transient(exc: BaseException) -> bool:
    """Return true only for upload failures that are safe to recheck/retry."""
    if isinstance(exc, DriveCaseSyncDeadline):
        return False
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in {
        errno.ETIMEDOUT,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.EPIPE,
    }:
        return True
    exc_name = type(exc).__name__.lower()
    message = str(exc or "").lower()
    if "timeout" in exc_name or "redirectmissinglocation" in exc_name:
        return True
    if "redirected but the response is missing a location" in message:
        return True
    if "drive_api_timeout:" in message:
        return True
    response = getattr(exc, "resp", None)
    try:
        status = int(getattr(response, "status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    return status in {408, 429} or 500 <= status <= 599


def _drive_upload_redirect_missing_location(exc: BaseException) -> bool:
    exc_name = type(exc).__name__.lower()
    message = str(exc or "").lower()
    return (
        "redirectmissinglocation" in exc_name
        or "redirected but the response is missing a location" in message
    )


def _drive_upload_transport_settings() -> tuple[int, int]:
    """Use bounded SMB reads for uploads so one slow file cannot wedge a worker."""
    resumable_min = max(
        0,
        int(
            os.environ.get("MAGI_DRIVE_SYNC_RESUMABLE_UPLOAD_MIN_BYTES")
            or DEFAULT_RESUMABLE_UPLOAD_MIN_BYTES
        ),
    )
    chunk = max(
        256 * 1024,
        int(
            os.environ.get("MAGI_DRIVE_SYNC_UPLOAD_CHUNK_BYTES")
            or DEFAULT_RESUMABLE_UPLOAD_CHUNK_BYTES
        ),
    )
    # Google resumable chunks must be a multiple of 256 KiB.
    quantum = 256 * 1024
    chunk = max(quantum, (chunk // quantum) * quantum)
    return resumable_min, chunk


def _local_hash_timeout_seconds(path: str) -> float:
    """Scale the SMB hash deadline to file size instead of a fixed 20 seconds."""
    configured = float(
        os.environ.get("MAGI_DRIVE_SYNC_LOCAL_HASH_TIMEOUT_SEC")
        or DEFAULT_LOCAL_HASH_TIMEOUT_SEC
    )
    if configured <= 0:
        return 0.0
    try:
        size = max(0, int(os.path.getsize(path)))
    except OSError:
        size = 0
    try:
        minimum_rate = max(
            1.0,
            float(
                os.environ.get("MAGI_DRIVE_SYNC_LOCAL_HASH_MIN_BYTES_PER_SEC")
                or DEFAULT_LOCAL_HASH_MIN_BYTES_PER_SEC
            ),
        )
    except (TypeError, ValueError):
        minimum_rate = float(DEFAULT_LOCAL_HASH_MIN_BYTES_PER_SEC)
    adaptive = DEFAULT_LOCAL_HASH_TIMEOUT_HEADROOM_SEC + (float(size) / minimum_rate)
    return max(configured, adaptive)


def _smb_file_size_with_retry(path: str) -> int:
    """Survive short smbfs directory-cache dropouts before hashing.

    Synology reconnects can briefly make a file returned by ``scandir`` fail
    the immediately following ``stat`` with ENOENT/ENXIO/EIO.  Re-probe for a
    small bounded window; a genuinely removed file still fails closed and is
    deferred by the worker rather than being copied or overwritten.
    """

    attempts = max(
        1,
        int(os.environ.get("MAGI_DRIVE_SYNC_SMB_STAT_RETRY_ATTEMPTS") or "7"),
    )
    delay = max(
        0.0,
        float(os.environ.get("MAGI_DRIVE_SYNC_SMB_STAT_RETRY_DELAY_SEC") or "0.5"),
    )
    retryable = {
        errno.ENOENT,
        errno.ENXIO,
        errno.EIO,
        getattr(errno, "ESTALE", -1),
    }
    latest: OSError | None = None
    for index in range(attempts):
        try:
            return int(os.path.getsize(path))
        except OSError as exc:
            latest = exc
            if not str(path).startswith("/Volumes/") or exc.errno not in retryable:
                raise
            if index + 1 < attempts and delay > 0:
                time.sleep(delay)
    assert latest is not None
    raise latest


def _smb_stage_timeout_seconds(path: Path, expected_size: int) -> float:
    """Return a bounded-by-workload SMB staging deadline.

    A fixed deadline makes a healthy but slow NAS look like a hard upload
    failure for large PDFs.  Keep the operator-configured floor and the hash
    deadline, then add a conservative size-derived floor.  A genuinely wedged
    smbfs read still runs in a disposable child and is killed at this deadline.
    """

    configured = float(
        os.environ.get("MAGI_DRIVE_SYNC_SMB_STAGE_TIMEOUT_SEC")
        or DEFAULT_SMB_STAGE_TIMEOUT_SEC
    )
    minimum_bytes_per_second = max(
        1,
        int(
            os.environ.get("MAGI_DRIVE_SYNC_SMB_STAGE_MIN_BYTES_PER_SEC")
            or DEFAULT_SMB_STAGE_MIN_BYTES_PER_SEC
        ),
    )
    size_deadline = max(0, int(expected_size)) / minimum_bytes_per_second
    return max(configured, _local_hash_timeout_seconds(str(path)), size_deadline)


def _smb_upload_stage_root() -> Path:
    configured = os.environ.get("MAGI_DRIVE_SYNC_UPLOAD_STAGE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    runtime = Path(
        os.environ.get("MAGI_RUNTIME_DIR", "").strip()
        or tempfile.gettempdir()
    ).expanduser()
    return runtime / "drive_sync" / "upload_stage"


def _secure_smb_upload_stage_root() -> Path:
    """Return a local, non-symlink staging root with private permissions.

    The staging directory is a resumable cache, not an application data
    directory.  Refuse a configured path below a mounted volume (or through
    a symlink) so a bad environment cannot redirect private case bytes back
    to a share or an attacker-controlled directory.
    """

    root = _smb_upload_stage_root()
    if (
        not root.is_absolute()
        or root == Path("/Volumes")
        or Path("/Volumes") in root.parents
    ):
        raise DriveCaseSyncError("smb_stage_root_not_local")
    current = Path(root.anchor)
    for component in root.relative_to(root.anchor).parts:
        current /= component
        if current.is_symlink():
            raise DriveCaseSyncError("smb_stage_root_symlink")
        if current.exists() and not current.is_dir():
            raise DriveCaseSyncError("smb_stage_root_not_directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError as exc:
        raise DriveCaseSyncError("smb_stage_root_permissions") from exc
    if root.is_symlink():
        raise DriveCaseSyncError("smb_stage_root_symlink")
    return root


def _private_smb_stage_part(path: Path) -> None:
    """Create/check a resumable stage part without following symlinks."""

    nofollow = int(getattr(os, "O_NOFOLLOW", 0))
    nonblock = int(getattr(os, "O_NONBLOCK", 0))
    try:
        try:
            fd = os.open(path, os.O_WRONLY | nonblock | nofollow)
        except FileNotFoundError:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nonblock | nofollow,
                0o600,
            )
    except OSError as exc:
        if path.is_symlink() or getattr(exc, "errno", None) == errno.ELOOP:
            raise DriveCaseSyncError("smb_stage_part_symlink") from exc
        raise DriveCaseSyncError("smb_stage_part_not_regular") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
            raise DriveCaseSyncError("smb_stage_part_not_regular")
        os.fchmod(fd, 0o600)
        metadata = os.fstat(fd)
        if stat.S_IMODE(metadata.st_mode) != 0o600 or int(metadata.st_nlink) != 1:
            raise DriveCaseSyncError("smb_stage_part_permissions")
    except OSError as exc:
        raise DriveCaseSyncError("smb_stage_part_permissions") from exc
    finally:
        os.close(fd)


_SMB_STAGE_SIDECAR_SCHEMA = 2


def _local_stat_identity(source_stat: Any) -> str:
    """Bind cached bytes to SMB replacement-sensitive metadata.

    Synology/SMB may replace a file while preserving its size and mtime.  The
    prior size+mtime fingerprint could therefore reuse an MD5 or resumable
    stage belonging to different bytes.  ctime, device, and inode add the
    object identity signals exposed by macOS smbfs; missing synthetic fields
    remain deterministic zeros.
    """

    material = "\0".join(
        (
            "drive-local-stat-v2",
            str(int(getattr(source_stat, "st_size", 0) or 0)),
            str(int(getattr(source_stat, "st_mtime_ns", 0) or 0)),
            str(int(getattr(source_stat, "st_ctime_ns", 0) or 0)),
            str(int(getattr(source_stat, "st_dev", 0) or 0)),
            str(int(getattr(source_stat, "st_ino", 0) or 0)),
        )
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()


def _smb_source_identity(source: Path, source_stat: Any) -> str:
    return hashlib.sha256(
        (
            f"drive-smb-stage-v2\0{source}\0"
            f"{_local_stat_identity(source_stat)}"
        ).encode("utf-8", errors="surrogateescape")
    ).hexdigest()


def _smb_stage_partial_sha256(path: Path) -> str:
    """Hash a token-owned local stage without opening FIFOs or symlinks."""

    nofollow = int(getattr(os, "O_NOFOLLOW", 0))
    nonblock = int(getattr(os, "O_NONBLOCK", 0))
    try:
        fd = os.open(path, os.O_RDONLY | nonblock | nofollow)
    except OSError as exc:
        raise DriveCaseSyncError("smb_stage_part_not_regular") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
            raise DriveCaseSyncError("smb_stage_part_not_regular")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise DriveCaseSyncError("smb_stage_part_permissions")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _write_smb_stage_sidecar(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist path-free source/partial proof with private mode."""

    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _smb_stage_sidecar_payload(
    *, source_identity: str, source_stat: Any, partial: Path, expected_size: int
) -> dict[str, Any]:
    return {
        "schema_version": _SMB_STAGE_SIDECAR_SCHEMA,
        "source_identity": source_identity,
        "source_size": expected_size,
        "source_mtime_ns": int(getattr(source_stat, "st_mtime_ns", 0) or 0),
        "source_stat_identity": _local_stat_identity(source_stat),
        "partial_bytes": int(partial.stat().st_size),
        "partial_sha256": _smb_stage_partial_sha256(partial),
    }


def _smb_stage_sidecar_valid(
    sidecar: Path,
    *,
    source_identity: str,
    source_stat: Any,
    partial: Path,
    expected_size: int,
) -> bool:
    if sidecar.is_symlink() or not sidecar.is_file():
        return False
    try:
        sidecar_stat = sidecar.stat()
        if (
            not stat.S_ISREG(sidecar_stat.st_mode)
            or int(sidecar_stat.st_nlink) != 1
            or stat.S_IMODE(sidecar_stat.st_mode) != 0o600
            or partial.is_symlink()
        ):
            return False
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        if payload.get("schema_version") != _SMB_STAGE_SIDECAR_SCHEMA:
            return False
        if payload.get("source_identity") != source_identity:
            return False
        if int(payload.get("source_size")) != expected_size:
            return False
        if int(payload.get("source_mtime_ns")) != int(
            getattr(source_stat, "st_mtime_ns", 0) or 0
        ):
            return False
        if payload.get("source_stat_identity") != _local_stat_identity(source_stat):
            return False
        partial_bytes = int(payload.get("partial_bytes"))
        if partial_bytes < 0 or partial_bytes > expected_size:
            return False
        if partial_bytes != int(partial.stat().st_size):
            return False
        digest = str(payload.get("partial_sha256") or "").lower()
        return bool(re.fullmatch(r"[0-9a-f]{64}", digest)) and digest == _smb_stage_partial_sha256(partial)
    except (OSError, TypeError, ValueError, json.JSONDecodeError, DriveCaseSyncError):
        return False


def _clear_smb_stage_artifacts(partial: Path, sidecar: Path) -> None:
    """Remove only the token-owned part and proof, never an arbitrary path."""

    for artifact in (partial, sidecar):
        try:
            if artifact.is_symlink() or artifact.exists():
                artifact.unlink()
        except OSError:
            pass


def _bounded_child_stderr(stream: Any, limit: int = 4096) -> tuple[threading.Thread, bytearray]:
    """Drain child stderr in memory only; raw NAS paths must never persist."""

    captured = bytearray()

    def _drain() -> None:
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    return
                remaining = limit - len(captured)
                if remaining > 0:
                    captured.extend(bytes(chunk)[:remaining])
        finally:
            try:
                stream.close()
            except OSError:
                pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    return reader, captured


@contextmanager
def staged_upload_source(path: Path) -> Iterable[Path]:
    """Stage SMB input locally so a wedged NAS read cannot wedge the worker.

    macOS can leave a Python process in an uninterruptible smbfs ``read(2)``;
    SIGALRM therefore cannot reliably enforce the hash/upload deadline.  Keep
    all SMB I/O in a disposable child process and only hash/upload the completed
    local spool file.  On timeout the parent remains responsive, releases its
    MAGI mutation lock, and the action is safely retried by the next sweep.
    """

    source = Path(path)
    if not str(source).startswith("/Volumes/"):
        yield source
        return

    source_stat = source.stat()
    expected_size = int(source_stat.st_size)
    source_identity = _smb_source_identity(source, source_stat)
    stage_dir = _secure_smb_upload_stage_root() / source_identity
    if stage_dir.is_symlink():
        raise DriveCaseSyncError("smb_stage_dir_symlink")
    stage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(stage_dir, 0o700)
    except OSError as exc:
        raise DriveCaseSyncError("smb_stage_dir_permissions") from exc
    staged = stage_dir / "payload.part"
    sidecar = stage_dir / "payload.part.meta"
    stage_proof_valid = False
    if staged.is_symlink() or staged.exists() or sidecar.is_symlink() or sidecar.exists():
        stage_proof_valid = _smb_stage_sidecar_valid(
            sidecar,
            source_identity=source_identity,
            source_stat=source_stat,
            partial=staged,
            expected_size=expected_size,
        )
        if not stage_proof_valid:
            _clear_smb_stage_artifacts(staged, sidecar)
    try:
        partial_size = int(staged.stat().st_size) if staged.exists() else 0
    except OSError:
        partial_size = 0
    if partial_size > expected_size:
        if staged.is_symlink():
            raise DriveCaseSyncError("smb_stage_part_symlink")
        staged.unlink(missing_ok=True)
        partial_size = 0
    if partial_size == expected_size and stage_proof_valid:
        # A prior bounded rsync can copy every byte and still return 23 when
        # smbfs drops the session while rsync is collecting final metadata.
        # The private sidecar already binds the source identity/stat, exact
        # length, and SHA-256 of the regular 0600 stage.  Re-running rsync in
        # that state only re-enters the unreliable share and can wait through
        # the full size-derived deadline without changing a byte.
        after_stat = source.stat()
        if (
            int(after_stat.st_size) != expected_size
            or int(getattr(after_stat, "st_mtime_ns", 0) or 0)
            != int(getattr(source_stat, "st_mtime_ns", 0) or 0)
        ):
            raise DriveCaseSyncError("smb_source_changed_during_stage")
        consumer_succeeded = False
        try:
            yield staged
            consumer_succeeded = True
        finally:
            if consumer_succeeded:
                shutil.rmtree(stage_dir, ignore_errors=True)
        return
    _private_smb_stage_part(staged)
    partial_size = int(staged.stat().st_size)
    timeout = _smb_stage_timeout_seconds(source, expected_size)
    try:
        proc = subprocess.Popen(
            ["/usr/bin/rsync", "--partial", "--append", str(source), str(staged)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except BaseException:
        raise
    deadline = time.monotonic() + timeout
    stderr_reader, stderr_capture = _bounded_child_stderr(proc.stderr)

    def _terminate_copy() -> None:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    previous_sigterm: Any = None
    owns_sigterm = threading.current_thread() is threading.main_thread()
    if owns_sigterm:
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def _handle_sigterm(signum: int, frame: Any) -> None:
            _terminate_copy()
            _continue_signal_chain(previous_sigterm, signum, frame)

        signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if proc.poll() is None:
            _terminate_copy()
            current_size = int(staged.stat().st_size) if staged.exists() else 0
            if current_size > partial_size and current_size <= expected_size:
                _write_smb_stage_sidecar(
                    sidecar,
                    _smb_stage_sidecar_payload(
                        source_identity=source_identity,
                        source_stat=source_stat,
                        partial=staged,
                        expected_size=expected_size,
                    ),
                )
            raise DriveCaseSyncStorageDeferred(
                f"smb_stage_timeout:{timeout:g}s"
            )
        proc.wait(timeout=2)
        stderr_reader.join(timeout=1.0)
        if proc.returncode != 0:
            current_size = int(staged.stat().st_size) if staged.exists() else 0
            if current_size > partial_size:
                if current_size <= expected_size:
                    _write_smb_stage_sidecar(
                        sidecar,
                        _smb_stage_sidecar_payload(
                            source_identity=source_identity,
                            source_stat=source_stat,
                            partial=staged,
                            expected_size=expected_size,
                        ),
                    )
                raise DriveCaseSyncStorageDeferred(
                    f"smb_stage_interrupted:rc={proc.returncode}:"
                    f"cached={current_size}:expected={expected_size}"
                )
            failure_code = _smb_md5_failure_code(
                int(proc.returncode or 0), bytes(stderr_capture)
            )
            if failure_code == "local_hash_smb_helper_storage_unavailable":
                raise DriveCaseSyncStorageDeferred("smb_stage_storage_unavailable")
            raise DriveCaseSyncError("smb_stage_failed")
        actual_size = int(staged.stat().st_size)
        if actual_size != expected_size:
            raise DriveCaseSyncError(
                f"smb_stage_size_mismatch:{actual_size}!={expected_size}"
            )
        _write_smb_stage_sidecar(
            sidecar,
            _smb_stage_sidecar_payload(
                source_identity=source_identity,
                source_stat=source_stat,
                partial=staged,
                expected_size=expected_size,
            ),
        )
        after_stat = source.stat()
        if (
            int(after_stat.st_size) != expected_size
            or int(getattr(after_stat, "st_mtime_ns", 0) or 0)
            != int(getattr(source_stat, "st_mtime_ns", 0) or 0)
        ):
            raise DriveCaseSyncError("smb_source_changed_during_stage")
        consumer_succeeded = False
        try:
            yield staged
            consumer_succeeded = True
        finally:
            if consumer_succeeded:
                shutil.rmtree(stage_dir, ignore_errors=True)
    finally:
        # Also cover caller cancellation, resource-guard termination, and an
        # exception raised while consuming the staged file.  The parent must
        # never leave an smbfs reader behind after releasing the MAGI lock.
        _terminate_copy()
        stderr_reader.join(timeout=1.0)
        if owns_sigterm:
            signal.signal(signal.SIGTERM, previous_sigterm)


def _smb_md5_failure_code(returncode: int, stderr: bytes) -> str:
    """Classify an md5 helper failure without retaining its path-bearing text."""

    message = bytes(stderr or b"")[:4096].decode("utf-8", errors="replace").lower()
    if any(token in message for token in (
        "input/output error", "stale file handle", "no such device",
        "operation timed out", "network is down", "connection reset",
    )):
        return "local_hash_smb_helper_storage_unavailable"
    if "no such file" in message:
        return "local_hash_smb_helper_file_missing"
    if any(token in message for token in ("permission denied", "operation not permitted")):
        return "local_hash_smb_helper_permission_denied"
    if int(returncode or 0) < 0:
        return "local_hash_smb_helper_signal_terminated"
    return "local_hash_smb_helper_failed"


def local_file_md5(path: str, *, chunk_size: int = 1024 * 1024, max_bytes: int | None = None) -> str:
    def _isolated_smb_md5() -> str:
        """Hash a NAS file out of process so smbfs cannot wedge the worker."""

        limit = max_bytes
        if limit is None:
            limit = int(
                os.environ.get("MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES")
                or DEFAULT_LOCAL_HASH_MAX_BYTES
            )
        size = _smb_file_size_with_retry(path)
        if limit > 0 and size > limit:
            raise DriveCaseSyncError(
                f"local_hash_skipped_large_file:{size}>{limit}:{path}"
            )
        tool = Path("/sbin/md5")
        if not tool.is_file():
            raise DriveCaseSyncError("local_hash_helper_unavailable:/sbin/md5")
        fd, output_name = tempfile.mkstemp(prefix="magi-drive-smb-md5-", suffix=".txt")
        output_path = Path(output_name)
        output = os.fdopen(fd, "wb")
        # md5 includes its input name in stderr.  Keep only a small, in-memory
        # prefix for category matching; never create a path-bearing diagnostic
        # file that could survive a crash or forced termination.
        stderr_capture = bytearray()
        stderr_limit = 4096
        try:
            proc = subprocess.Popen(
                [str(tool), "-q", path],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        finally:
            output.close()

        def _drain_stderr() -> None:
            stream = proc.stderr
            if stream is None:
                return
            try:
                while True:
                    chunk = stream.read(1024)
                    if not chunk:
                        return
                    remaining = stderr_limit - len(stderr_capture)
                    if remaining > 0:
                        stderr_capture.extend(chunk[:remaining])
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        stderr_reader = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_reader.start()

        def _terminate_hash() -> None:
            if proc.poll() is not None:
                return
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

        timeout = _local_hash_timeout_seconds(path)
        previous_sigterm: Any = None
        owns_sigterm = threading.current_thread() is threading.main_thread()
        if owns_sigterm:
            previous_sigterm = signal.getsignal(signal.SIGTERM)

            def _handle_sigterm(signum: int, frame: Any) -> None:
                _terminate_hash()
                _continue_signal_chain(previous_sigterm, signum, frame)

            signal.signal(signal.SIGTERM, _handle_sigterm)
        try:
            deadline = time.monotonic() + timeout if timeout > 0 else None
            while proc.poll() is None and (
                deadline is None or time.monotonic() < deadline
            ):
                time.sleep(0.1)
            if proc.poll() is None:
                _terminate_hash()
                raise DriveCaseSyncError(f"local_hash_timeout:{timeout:g}s:{path}")
            stderr_reader.join(timeout=1.0)
            if proc.returncode != 0:
                failure_code = _smb_md5_failure_code(
                    int(proc.returncode or 0),
                    bytes(stderr_capture),
                )
                raise DriveCaseSyncError(
                    f"{failure_code}:{proc.returncode}:{path}"
                )
            value = output_path.read_text(encoding="ascii", errors="replace").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{32}", value):
                raise DriveCaseSyncError(f"local_hash_smb_helper_invalid:{path}")
            return value
        finally:
            _terminate_hash()
            stderr_reader.join(timeout=1.0)
            if owns_sigterm:
                signal.signal(signal.SIGTERM, previous_sigterm)
            output_path.unlink(missing_ok=True)

    def _macos_smb_md5_fallback(error: OSError) -> str:
        tool = Path("/sbin/md5")
        if error.errno != errno.EBADF or not str(path).startswith("/Volumes/") or not tool.is_file():
            raise error
        try:
            fallback_timeout = max(30.0, _local_hash_timeout_seconds(path))
            proc = subprocess.run(
                [str(tool), "-q", path],
                capture_output=True,
                text=True,
                timeout=fallback_timeout,
                check=False,
            )
        except Exception as exc:
            if isinstance(exc, DriveCaseSyncDeadline):
                raise
            raise DriveCaseSyncError(f"local_hash_smb_fallback_failed:{type(exc).__name__}:{path}") from exc
        value = str(proc.stdout or "").strip().lower()
        if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{32}", value):
            raise DriveCaseSyncError(f"local_hash_smb_fallback_invalid:{proc.returncode}:{path}")
        return value

    def _hash() -> str:
        limit = max_bytes
        if limit is None:
            limit = int(os.environ.get("MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES") or DEFAULT_LOCAL_HASH_MAX_BYTES)
        if limit > 0:
            try:
                size = _smb_file_size_with_retry(path)
            except OSError:
                size = 0
            if size > limit:
                raise DriveCaseSyncError(f"local_hash_skipped_large_file:{size}>{limit}:{path}")
        digest = hashlib.md5()
        try:
            fh = open(path, "rb")
            reached_eof = False
            try:
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        reached_eof = True
                        break
                    digest.update(chunk)
            finally:
                try:
                    fh.close()
                except OSError as exc:
                    # After a successful full read, macOS smbfs can report
                    # EBADF while closing a server-side handle already closed
                    # during reconnect. The digest is complete in that case.
                    if not (reached_eof and exc.errno == errno.EBADF):
                        raise
        except OSError as exc:
            return _macos_smb_md5_fallback(exc)
        return digest.hexdigest()

    if str(path).startswith("/Volumes/"):
        return _isolated_smb_md5()

    timeout = _local_hash_timeout_seconds(path)
    can_alarm = (
        timeout > 0
        and hasattr(signal, "SIGALRM")
        and threading.current_thread() is threading.main_thread()
    )
    if not can_alarm:
        return _hash()

    def _handle_timeout(_signum: int, _frame: Any) -> None:
        raise DriveCaseSyncError(f"local_hash_timeout:{timeout:g}s:{path}")

    previous_handler = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    if hasattr(signal, "setitimer"):
        previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, max(0.01, timeout))
        try:
            return _hash()
        finally:
            elapsed = time.monotonic() - started
            delay = max(0.0, float(previous_timer[0] or 0.0) - elapsed)
            interval = float(previous_timer[1] or 0.0)
            signal.setitimer(signal.ITIMER_REAL, delay, interval)
            signal.signal(signal.SIGALRM, previous_handler)

    previous_alarm = signal.alarm(0)
    try:
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.alarm(max(1, int(timeout)))
        return _hash()
    finally:
        signal.alarm(0)
        elapsed = time.monotonic() - started
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_alarm:
            signal.alarm(max(1, int(max(0.0, float(previous_alarm) - elapsed))))


def _semantic_file_index(
    entries: Iterable[FileEntry],
    *,
    drive_side: bool,
    checkpoint: DriveFileCheckpoint | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[
    dict[str, FileEntry],
    dict[str, list[FileEntry]],
    dict[str, list[FileEntry]],
]:
    """Index files while separating verified duplicates from real collisions.

    Google Drive can retain the same object in two historical alias folders,
    and NAS can retain identical content under two folder-label aliases.  A
    deterministic representative is safe only after Drive checksum/source-ID
    proof or bounded, checkpointed NAS hashing.  Different content remains a
    fail-closed collision; unavailable NAS proof is returned separately so the
    worker retries the same cursor without executing a file action.
    """
    buckets: dict[str, list[FileEntry]] = {}
    for entry in entries:
        if entry.is_folder:
            continue
        if drive_side:
            if not _drive_entry_downloadable(entry):
                continue
            relative_path = export_relative_path(entry)
        else:
            if is_accounting_import_only_path(entry.relative_path):
                continue
            relative_path = entry.relative_path
        key = normalized_relative_file_key(semantic_relative_path(relative_path))
        if key:
            buckets.setdefault(key, []).append(entry)
    unique: dict[str, FileEntry] = {}
    collisions: dict[str, list[FileEntry]] = {}
    verified_alias_duplicates: dict[str, list[FileEntry]] = {}
    fingerprint_state_by_key: dict[str, str] = {}
    fingerprint_failure_by_key: dict[str, str] = {}

    def _representative(values: list[FileEntry], key: str) -> FileEntry:
        def _rank(entry: FileEntry) -> tuple[int, int, str, str]:
            relative = export_relative_path(entry) if drive_side else entry.relative_path
            normalized = normalized_relative_file_key(relative)
            return (
                0 if normalized == key else 1,
                len(PurePosixPath(relative).parts),
                unicodedata.normalize("NFKC", relative).casefold(),
                str(entry.drive_id or ""),
            )

        return min(values, key=_rank)

    for key, values in buckets.items():
        if len(values) == 1:
            unique[key] = values[0]
            continue
        if drive_side:
            fingerprints = [
                (str(entry.md5 or "").strip().lower(), entry.size)
                for entry in values
            ]
            checksums_complete = all(
                re.fullmatch(r"[0-9a-fA-F]{32}", digest) and size is not None
                for digest, size in fingerprints
            )
            if checksums_complete and len(set(fingerprints)) == 1:
                unique[key] = _representative(values, key)
                verified_alias_duplicates[key] = values
                fingerprint_state_by_key[key] = "drive_checksum_identical"
                continue
            if checksums_complete:
                collisions[key] = values
                fingerprint_state_by_key[key] = "verified_different"
                continue
            source_ids = {str(entry.drive_id or "").strip() for entry in values}
            if len(source_ids) == 1 and "" not in source_ids:
                unique[key] = _representative(values, key)
                verified_alias_duplicates[key] = values
                fingerprint_state_by_key[key] = "drive_source_id_identical"
                continue
            collisions[key] = values
            fingerprint_state_by_key[key] = (
                "drive_native_unverified"
                if any(GOOGLE_EXPORT_MIME_MAP.get(entry.mime_type) for entry in values)
                else "drive_checksum_unavailable"
            )
            continue

        known_sizes = {int(entry.size) for entry in values if entry.size is not None}
        if len(known_sizes) > 1:
            collisions[key] = values
            fingerprint_state_by_key[key] = "verified_different"
            continue
        try:
            local_hashes = {
                _checkpointed_local_md5(entry, checkpoint).strip().lower()
                for entry in values
            }
        except Exception as exc:
            if isinstance(exc, DriveCaseSyncDeadline):
                raise
            failure_code = local_hash_failure_code(exc)
            fingerprint_state_by_key[key] = "local_hash_unavailable"
            fingerprint_failure_by_key[key] = failure_code
            continue
        if len(local_hashes) == 1 and all(
            re.fullmatch(r"[0-9a-f]{32}", digest) for digest in local_hashes
        ):
            unique[key] = _representative(values, key)
            verified_alias_duplicates[key] = values
            fingerprint_state_by_key[key] = "local_hash_identical"
            continue
        collisions[key] = values
        fingerprint_state_by_key[key] = "verified_different"
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update({
            "bucket_entries": buckets,
            "fingerprint_state_by_key": fingerprint_state_by_key,
            "fingerprint_failure_by_key": fingerprint_failure_by_key,
        })
    return unique, collisions, verified_alias_duplicates


def _checkpointed_local_md5(
    entry: FileEntry,
    checkpoint: DriveFileCheckpoint | None,
) -> str:
    """Hash once per unchanged private locator and durably reuse the proof."""

    if checkpoint is None:
        return local_file_md5(entry.path)
    source_size = entry.size
    source_modified: Any = entry.modified_time
    smb_source = str(entry.path or "").startswith("/Volumes/")
    try:
        before_stat = Path(entry.path).stat()
        source_size = int(before_stat.st_size)
        source_modified = _local_stat_identity(before_stat)
    except OSError as exc:
        if isinstance(exc, DriveCaseSyncDeadline):
            raise
        if smb_source:
            raise DriveCaseSyncStorageDeferred("local_source_stat_unavailable") from exc
        before_stat = None
    source_key = drive_checkpoint_source_fingerprint(
        direction="local_hash",
        case_key=checkpoint.case_key,
        locator=entry.relative_path or entry.path,
        size=source_size,
        modified=source_modified,
    )
    token = drive_checkpoint_item_token(
        direction="local_hash",
        case_key=checkpoint.case_key,
        source_key=source_key,
    )
    cached = checkpoint.cached_hash(token, source_key)
    if cached:
        return cached

    def _verify_and_cache(value: str) -> str:
        if before_stat is not None:
            try:
                after_stat = Path(entry.path).stat()
            except OSError as exc:
                if isinstance(exc, DriveCaseSyncDeadline):
                    raise
                raise DriveCaseSyncError("local_source_changed_during_hash") from exc
            if (
                int(after_stat.st_size) != int(before_stat.st_size)
                or _local_stat_identity(after_stat)
                != _local_stat_identity(before_stat)
            ):
                raise DriveCaseSyncError("local_source_changed_during_hash")
        # Keep this call inside staged_upload_source: a failed durable write
        # must leave payload.part available for the next retry.
        checkpoint.cache_hash(token, source_key, value)
        return value

    if smb_source:
        # A cache miss is the only path allowed to read the SMB bytes.  Keep
        # that read in a resumable local spool; the md5 helper must never see
        # a /Volumes path and the spool is retained until cache_hash() is
        # durably journaled below.
        with staged_upload_source(Path(entry.path)) as staged:
            return _verify_and_cache(local_file_md5(str(staged)))
    else:
        value = local_file_md5(entry.path)
    return _verify_and_cache(value)


def _bind_file_plan_checkpoint(
    file_sync_plan: dict[str, Any],
    checkpoint: DriveFileCheckpoint | None,
) -> list[str]:
    """Attach only opaque progress identities to executable file actions."""

    if checkpoint is None:
        return []
    tokens: list[str] = []
    for case in file_sync_plan.get("cases") or []:
        for direction, key in (("download", "download_missing"), ("upload", "nas_only")):
            for action in case.get(key) or []:
                source_key = _file_action_source_fingerprint(
                    action,
                    direction=direction,
                    checkpoint=checkpoint,
                    require_live_stat=False,
                )
                token = drive_checkpoint_item_token(
                    direction=direction,
                    case_key=checkpoint.case_key,
                    source_key=source_key,
                )
                action["checkpoint_item_token"] = token
                action["checkpoint_source_fingerprint"] = source_key
                tokens.append(token)
    checkpoint.bind_snapshot(tokens)
    return tokens


def _suppress_case_write_actions(case_plan: dict[str, Any], summary: dict[str, Any]) -> int:
    """Clear every executable action after a hash proof failure.

    A duplicate-content lookup can happen after earlier Drive downloads were
    planned.  Once local proof fails, the case is no longer a safe dry-run:
    retain only opaque pending evidence and make the executor see no writes.
    """

    downloads = list(case_plan.get("download_missing") or [])
    uploads = list(case_plan.get("nas_only") or [])
    case_plan["download_missing"] = []
    case_plan["nas_only"] = []
    summary["drive_missing_in_nas_files"] = max(
        0,
        int(summary.get("drive_missing_in_nas_files") or 0) - len(downloads),
    )
    summary["drive_missing_in_nas_bytes"] = max(
        0,
        int(summary.get("drive_missing_in_nas_bytes") or 0)
        - sum(int(action.get("size") or 0) for action in downloads),
    )
    summary["nas_missing_in_drive_files"] = max(
        0,
        int(summary.get("nas_missing_in_drive_files") or 0) - len(uploads),
    )
    return len(downloads) + len(uploads)


def _file_action_source_fingerprint(
    action: dict[str, Any],
    *,
    direction: str,
    checkpoint: DriveFileCheckpoint,
    require_live_stat: bool,
) -> str:
    """Bind an upload proof to the current NAS object, not scan-time seconds."""

    locator = str(
        action.get("target_relative_path")
        or action.get("relative_path")
        or action.get("path")
        or ""
    )
    source_size = int(action.get("size") or 0)
    source_modified: Any = action.get("modified_time") or ""
    if direction == "upload" and action.get("path"):
        try:
            source_stat = Path(str(action["path"])).stat()
            source_size = int(source_stat.st_size)
            source_modified = _local_stat_identity(source_stat)
        except OSError:
            if require_live_stat:
                raise
            # The executor performs a bounded source probe and fails closed if
            # the NAS object is unavailable. Scan evidence remains sufficient
            # to identify the not-yet-executable plan item.
            pass
    return drive_checkpoint_source_fingerprint(
        direction=direction,
        case_key=checkpoint.case_key,
        locator=locator,
        size=source_size,
        modified=source_modified,
        checksum=str(action.get("md5") or ""),
        opaque_source_id=str(action.get("drive_id") or ""),
    )


def build_file_sync_plan(
    comparison: dict[str, Any],
    drive_service: Any,
    *,
    max_case_depth: int = 20,
    max_case_items: int = 10000,
    matched_case_limit: int = 0,
    matched_case_offset: int = 0,
    priority_case_numbers: Iterable[str] | None = None,
    checkpoint: DriveFileCheckpoint | None = None,
) -> dict[str, Any]:
    """Build a conservative per-file plan for uniquely matched cases.

    Drive and NAS can use different folder names for the same case document
    category.  The comparison uses semantic paths, while executable actions
    preserve the target side's native folder layout.
    """
    cases: list[dict[str, Any]] = []
    summary = {
        "matched_cases_scanned": 0,
        "drive_missing_in_nas_files": 0,
        "drive_missing_in_nas_bytes": 0,
        "nas_missing_in_drive_files": 0,
        "conflict_files": 0,
        "content_mismatch_files": 0,
        "skipped_existing_files": 0,
        "skipped_unmapped_drive_downloads": 0,
        "skipped_duplicate_content_downloads": 0,
        "skipped_duplicate_content_uploads": 0,
        "skipped_unmapped_nas_uploads": 0,
        "unverified_existing_files": 0,
        "pending_unverified_files": 0,
        "semantic_collision_files": 0,
        "semantic_collision_groups": 0,
        "semantic_collision_by_side": {
            "nas": {"groups": 0, "files": 0},
            "drive": {"groups": 0, "files": 0},
        },
        "semantic_alias_groups": 0,
        "semantic_alias_files": 0,
        "semantic_alias_by_side": {
            "nas": {"groups": 0, "files": 0},
            "drive": {"groups": 0, "files": 0},
        },
        "semantic_alias_by_fingerprint_state": {
            state: {"groups": 0, "files": 0}
            for state in (
                "local_hash_identical",
                "drive_checksum_identical",
                "drive_source_id_identical",
                "verified_different",
                "local_hash_unavailable",
                "drive_native_unverified",
                "drive_checksum_unavailable",
            )
        },
        "semantic_alias_by_side_and_fingerprint_state": {
            side: {
                state: {"groups": 0, "files": 0}
                for state in (
                    "local_hash_identical",
                    "drive_checksum_identical",
                    "drive_source_id_identical",
                    "verified_different",
                    "local_hash_unavailable",
                    "drive_native_unverified",
                    "drive_checksum_unavailable",
                )
            }
            for side in ("nas", "drive")
        },
        "semantic_fingerprint_deferred_groups": 0,
        "semantic_fingerprint_deferred_files": 0,
        "verified_alias_duplicate_groups": 0,
        "verified_alias_duplicate_files": 0,
        "storage_unavailable_case_scans": 0,
        "incomplete_case_scans": 0,
        "case_errors": 0,
        "matched_case_offset": max(0, int(matched_case_offset or 0)),
    }
    matched_items = list(comparison.get("matched", []) or [])
    priority_set = {str(x or "").strip() for x in (priority_case_numbers or []) if str(x or "").strip()}
    if priority_set:
        priority_items: list[dict[str, Any]] = []
        normal_items: list[dict[str, Any]] = []
        for item in matched_items:
            local: CaseFolder | None = item.get("local")
            drive: CaseFolder | None = item.get("drive")
            case_no = ""
            if local and local.meta:
                case_no = local.meta.case_number
            if not case_no and drive and drive.meta:
                case_no = drive.meta.case_number
            if case_no in priority_set:
                priority_items.append(item)
            else:
                normal_items.append(item)
        ordered_items = priority_items + normal_items[max(0, int(matched_case_offset or 0)) :]
    else:
        ordered_items = matched_items[max(0, int(matched_case_offset or 0)) :]

    for item in ordered_items:
        if matched_case_limit and summary["matched_cases_scanned"] >= matched_case_limit:
            break
        drive: CaseFolder = item.get("drive")
        local: CaseFolder = item.get("local")
        if not drive or not local or not drive.drive_id or not (local.local_path or local.path):
            continue
        case_plan = {
            "case_number": local.meta.case_number,
            "drive_path": drive.relative_path,
            "drive_id": drive.drive_id,
            "local_path": local.local_path or local.path,
            "download_missing": [],
            "download_skipped": [],
            "nas_only": [],
            "nas_only_skipped": [],
            "conflicts": [],
            "pending": [],
            "semantic_collisions": [],
            "verified_alias_duplicates": [],
            "skipped_existing": 0,
            "error": "",
        }
        matched_local_keys: set[str] = set()
        summary["matched_cases_scanned"] += 1
        try:
            drive_entries = drive_descendant_context(
                drive_service,
                drive.drive_id,
                max_depth=max_case_depth,
                max_items=max_case_items,
            )
            local_entries = local_descendant_context(
                local.local_path or local.path,
                max_depth=max_case_depth,
                max_items=max_case_items,
            )
        except Exception as exc:
            if isinstance(exc, DriveCaseSyncDeadline):
                raise
            case_plan["error"] = f"{type(exc).__name__}: {exc}"
            summary["case_errors"] += 1
            cases.append(case_plan)
            continue

        if bool(getattr(drive_entries, "truncated", False)) or bool(getattr(local_entries, "truncated", False)):
            case_plan["error"] = "incomplete_bounded_scan"
            case_plan["scan_truncated"] = {
                "drive": bool(getattr(drive_entries, "truncated", False)),
                "nas": bool(getattr(local_entries, "truncated", False)),
            }
            summary["incomplete_case_scans"] += 1
            summary["case_errors"] += 1
            cases.append(case_plan)
            continue

        local_semantic_diagnostics: dict[str, Any] = {}
        drive_semantic_diagnostics: dict[str, Any] = {}
        local_files, local_collisions, local_alias_duplicates = _semantic_file_index(
            local_entries,
            drive_side=False,
            checkpoint=checkpoint,
            diagnostics=local_semantic_diagnostics,
        )
        drive_files, drive_collisions, drive_alias_duplicates = _semantic_file_index(
            drive_entries,
            drive_side=True,
            checkpoint=checkpoint,
            diagnostics=drive_semantic_diagnostics,
        )

        for side, diagnostics, collisions in (
            ("nas", local_semantic_diagnostics, local_collisions),
            ("drive", drive_semantic_diagnostics, drive_collisions),
        ):
            buckets = diagnostics.get("bucket_entries") or {}
            states = diagnostics.get("fingerprint_state_by_key") or {}
            for key, state in states.items():
                entries = buckets.get(key) or []
                entry_count = len(entries)
                if entry_count <= 1:
                    continue
                summary["semantic_alias_groups"] += 1
                summary["semantic_alias_files"] += entry_count
                summary["semantic_alias_by_side"][side]["groups"] += 1
                summary["semantic_alias_by_side"][side]["files"] += entry_count
                state_counts = summary["semantic_alias_by_fingerprint_state"].get(state)
                if state_counts is not None:
                    state_counts["groups"] += 1
                    state_counts["files"] += entry_count
                side_state_counts = summary[
                    "semantic_alias_by_side_and_fingerprint_state"
                ][side].get(state)
                if side_state_counts is not None:
                    side_state_counts["groups"] += 1
                    side_state_counts["files"] += entry_count
                if key in collisions:
                    summary["semantic_collision_groups"] += 1
                    summary["semantic_collision_by_side"][side]["groups"] += 1
                    summary["semantic_collision_by_side"][side]["files"] += entry_count

        for side, duplicate_groups, indexed in (
            ("nas", local_alias_duplicates, local_files),
            ("drive", drive_alias_duplicates, drive_files),
        ):
            for key, entries in sorted(duplicate_groups.items()):
                representative = indexed[key]
                case_plan["verified_alias_duplicates"].append(
                    {
                        "side": side,
                        "semantic_key": key,
                        "representative": _entry_public_dict(representative),
                        "entries": [_entry_public_dict(entry) for entry in entries],
                    }
                )
                summary["verified_alias_duplicate_groups"] += 1
                summary["verified_alias_duplicate_files"] += len(entries) - 1

        local_fingerprint_failures = (
            local_semantic_diagnostics.get("fingerprint_failure_by_key") or {}
        )
        if local_fingerprint_failures:
            retryable_codes = LOCAL_HASH_RETRYABLE_FAILURE_CODES
            failure_codes: set[str] = set()
            deferred_files = 0
            local_buckets = local_semantic_diagnostics.get("bucket_entries") or {}
            for key, failure_code in local_fingerprint_failures.items():
                entries = local_buckets.get(key) or []
                failure_codes.add(str(failure_code or "hash_unavailable"))
                deferred_files += len(entries)
                for _entry in entries:
                    case_plan["pending"].append({
                        "side": "nas",
                        "reason": f"local_hash_failed:{failure_code}",
                        "status": "pending_unverified",
                        "semantic_alias_fingerprint": True,
                    })
            summary["semantic_fingerprint_deferred_groups"] += len(
                local_fingerprint_failures
            )
            summary["semantic_fingerprint_deferred_files"] += deferred_files
            summary["pending_unverified_files"] += deferred_files
            summary["unverified_existing_files"] += deferred_files
            if failure_codes and failure_codes.issubset(retryable_codes):
                case_plan["scan_deferred"] = {
                    "reason": "storage_unavailable_during_local_hash",
                    "failure_codes": sorted(failure_codes),
                    "writes_suppressed": 0,
                }
                summary["storage_unavailable_case_scans"] += 1
            else:
                case_plan["error"] = "semantic_alias_fingerprint_unavailable"
                summary["case_errors"] += 1
            cases.append(case_plan)
            continue

        if local_collisions or drive_collisions:
            case_plan["error"] = "semantic_path_collision"
            for side, collisions in (("nas", local_collisions), ("drive", drive_collisions)):
                for key, entries in sorted(collisions.items()):
                    case_plan["semantic_collisions"].append({
                        "side": side,
                        "semantic_key": key,
                        "entries": [_entry_public_dict(entry) for entry in entries],
                    })
                    summary["semantic_collision_files"] += len(entries)
            summary["case_errors"] += 1
            cases.append(case_plan)
            continue
        drive_existing_first_segments = _existing_drive_first_segments(drive_entries)
        local_existing_first_segments = _existing_local_first_segments(local_entries)
        local_duplicate_plan = build_local_duplicate_content_plan(
            local.local_path or local.path,
            local_entries,
            execute=False,
            checkpoint=checkpoint,
        )
        local_duplicate_by_key = {
            str(record.get("duplicate_semantic_key") or ""): record
            for record in local_duplicate_plan.get("records") or []
            if str(record.get("duplicate_semantic_key") or "")
        }
        storage_unavailable_scan = False
        for key, drive_entry in sorted(drive_files.items()):
            source_rel = export_relative_path(drive_entry)
            raw_target_rel = drive_to_nas_relative_path(
                source_rel,
                case_category=local.category,
                case_context_name=local.name,
                existing_nas_first_segments=local_existing_first_segments,
            )
            target_rel = nas_filesystem_relative_path(raw_target_rel)
            local_entry = local_files.get(key)
            local_key = key
            if not local_entry:
                target_key = normalized_relative_file_key(semantic_relative_path(target_rel))
                if target_key != key:
                    local_entry = local_files.get(target_key)
                    local_key = target_key
                if not local_entry:
                    duplicate_entry, duplicate_key, duplicate_verification = (
                        _find_same_content_local_entry(
                            drive_entry,
                            local_entries,
                            checkpoint=checkpoint,
                        )
                    )
                    if duplicate_entry and duplicate_verification == "verified_checksum":
                        action = _entry_public_dict(drive_entry)
                        action["source_relative_path"] = source_rel
                        action["target_relative_path"] = target_rel
                        action["reason"] = "same_content_elsewhere"
                        action["local_duplicate"] = _entry_public_dict(duplicate_entry)
                        action["hash_verification"] = "verified_checksum"
                        case_plan["download_skipped"].append(action)
                        if duplicate_key:
                            matched_local_keys.add(duplicate_key)
                        case_plan["skipped_existing"] += 1
                        summary["skipped_existing_files"] += 1
                        summary["skipped_duplicate_content_downloads"] += 1
                        continue
                    if duplicate_entry:
                        action = _entry_public_dict(drive_entry)
                        action["source_relative_path"] = source_rel
                        action["target_relative_path"] = target_rel
                        action["reason"] = duplicate_verification or "checksum_unverified_same_name_size"
                        action["status"] = "pending_unverified"
                        action["local_duplicate"] = _entry_public_dict(duplicate_entry)
                        case_plan["pending"].append(action)
                        if duplicate_key:
                            matched_local_keys.add(duplicate_key)
                        summary["pending_unverified_files"] += 1
                        summary["unverified_existing_files"] += 1
                        if str(duplicate_verification or "").startswith("local_hash_failed:"):
                            failure_code = str(duplicate_verification).split(":", 1)[-1]
                            suppressed = _suppress_case_write_actions(case_plan, summary)
                            if is_retryable_local_hash_failure(failure_code):
                                case_plan["scan_deferred"] = {
                                    "reason": "storage_unavailable_during_local_hash",
                                    "failure_code": failure_code,
                                    "writes_suppressed": suppressed,
                                }
                                summary["storage_unavailable_case_scans"] += 1
                            else:
                                case_plan["error"] = "local_hash_failed"
                                case_plan["hash_failure_code"] = failure_code
                                case_plan["writes_suppressed"] = suppressed
                                summary["case_errors"] += 1
                            storage_unavailable_scan = True
                            break
                        continue
                    action = _entry_public_dict(drive_entry)
                    action["source_relative_path"] = source_rel
                    action["target_relative_path"] = target_rel
                    skip_reason = drive_to_nas_download_skip_reason(
                        source_rel,
                        target_rel,
                        case_category=local.category,
                    )
                    if skip_reason:
                        action["reason"] = skip_reason
                        action["target_path"] = str(safe_child_path(Path(local.local_path or local.path), target_rel))
                        case_plan["download_skipped"].append(action)
                        summary["skipped_unmapped_drive_downloads"] += 1
                        continue
                    if target_rel != raw_target_rel:
                        action["original_target_relative_path"] = raw_target_rel
                        action["filename_shortened_for_nas"] = True
                    action["target_path"] = str(safe_child_path(Path(local.local_path or local.path), target_rel))
                    action["export_mime_type"] = (GOOGLE_EXPORT_MIME_MAP.get(drive_entry.mime_type) or [""])[0]
                    case_plan["download_missing"].append(action)
                    summary["drive_missing_in_nas_files"] += 1
                    summary["drive_missing_in_nas_bytes"] += int(drive_entry.size or 0)
                    continue
            matched_local_keys.add(local_key)
            if drive_entry.size is not None and local_entry.size is not None and int(drive_entry.size) != int(local_entry.size):
                case_plan["conflicts"].append({
                    "relative_path": target_rel,
                    "source_relative_path": source_rel,
                    "drive": _entry_public_dict(drive_entry),
                    "local": _entry_public_dict(local_entry),
                    "reason": "same_relative_path_size_differs",
                })
                summary["conflict_files"] += 1
            elif drive_entry.md5 and local_entry.path:
                try:
                    local_md5 = _checkpointed_local_md5(local_entry, checkpoint)
                except Exception as exc:
                    if isinstance(exc, DriveCaseSyncDeadline):
                        raise
                    failure_code = local_hash_failure_code(exc)
                    pending_record = {
                        "relative_path": target_rel,
                        "source_relative_path": source_rel,
                        "drive": _entry_public_dict(drive_entry),
                        "local": _entry_public_dict(local_entry),
                        "reason": f"local_hash_failed:{failure_code}",
                        "status": "pending_unverified",
                    }
                    case_plan["pending"].append(pending_record)
                    summary["pending_unverified_files"] += 1
                    summary["unverified_existing_files"] += 1
                    if is_retryable_local_hash_failure(failure_code):
                        # A single disconnected/blocked SMB file used to make the
                        # worker hash every remaining file in the same case, each
                        # with its own long timeout, while holding the global case
                        # mutation lock.  Stop this case immediately and retry it
                        # from the same checkpoint.  Suppress any downloads that
                        # were tentatively planned before the storage failure so
                        # an incomplete scan can never produce writes.
                        suppressed = _suppress_case_write_actions(case_plan, summary)
                        case_plan["scan_deferred"] = {
                            "reason": "storage_unavailable_during_local_hash",
                            "failure_code": failure_code,
                            "writes_suppressed": suppressed,
                        }
                        summary["storage_unavailable_case_scans"] += 1
                        storage_unavailable_scan = True
                        break
                    suppressed = _suppress_case_write_actions(case_plan, summary)
                    case_plan["error"] = "local_hash_failed"
                    case_plan["hash_failure_code"] = failure_code
                    case_plan["writes_suppressed"] = suppressed
                    summary["case_errors"] += 1
                    storage_unavailable_scan = True
                    break
                if normalize_text(local_md5) != normalize_text(drive_entry.md5):
                    local_data = _entry_public_dict(local_entry)
                    local_data["md5"] = local_md5
                    case_plan["conflicts"].append({
                        "relative_path": target_rel,
                        "source_relative_path": source_rel,
                        "drive": _entry_public_dict(drive_entry),
                        "local": local_data,
                        "reason": "same_relative_path_md5_differs",
                    })
                    summary["conflict_files"] += 1
                    summary["content_mismatch_files"] += 1
                else:
                    case_plan["skipped_existing"] += 1
                    summary["skipped_existing_files"] += 1
            elif GOOGLE_EXPORT_MIME_MAP.get(drive_entry.mime_type):
                case_plan["pending"].append({
                    "relative_path": target_rel,
                    "source_relative_path": source_rel,
                    "drive": _entry_public_dict(drive_entry),
                    "local": _entry_public_dict(local_entry),
                    "reason": "google_doc_export_checksum_unavailable",
                    "status": "pending_unverified",
                })
                summary["pending_unverified_files"] += 1
                summary["unverified_existing_files"] += 1
            else:
                case_plan["pending"].append({
                    "relative_path": target_rel,
                    "source_relative_path": source_rel,
                    "drive": _entry_public_dict(drive_entry),
                    "local": _entry_public_dict(local_entry),
                    "reason": "drive_checksum_missing",
                    "status": "pending_unverified",
                })
                summary["pending_unverified_files"] += 1
                summary["unverified_existing_files"] += 1
        if storage_unavailable_scan:
            cases.append(case_plan)
            continue
        for key, local_entry in sorted(local_files.items()):
            if key not in drive_files and key not in matched_local_keys:
                duplicate_record = local_duplicate_by_key.get(key)
                if duplicate_record:
                    action = _entry_public_dict(local_entry)
                    action["reason"] = "same_content_elsewhere_in_case_folder"
                    action["local_duplicate_canonical"] = duplicate_record.get("canonical") or {}
                    action["quarantine_candidate"] = duplicate_record.get("quarantine_path") or ""
                    case_plan["nas_only_skipped"].append(action)
                    summary["skipped_duplicate_content_uploads"] += 1
                    continue
                action = _entry_public_dict(local_entry)
                action["target_relative_path"] = nas_to_drive_relative_path(
                    local_entry.relative_path,
                    drive_existing_first_segments=drive_existing_first_segments,
                )
                skip_reason = nas_to_drive_upload_skip_reason(
                    local_entry.relative_path,
                    action["target_relative_path"],
                )
                if skip_reason:
                    action["reason"] = skip_reason
                    case_plan["nas_only_skipped"].append(action)
                    summary["skipped_unmapped_nas_uploads"] += 1
                    continue
                case_plan["nas_only"].append(action)
                summary["nas_missing_in_drive_files"] += 1
        cases.append(case_plan)
    plan = {
        "mode": "file_diff_dry_run",
        "write_actions_enabled": False,
        "direction": "bidirectional_missing_and_conflict_diff",
        "safety": "no_overwrite_no_delete_no_empty_folder_create",
        "summary": summary,
        "cases": cases,
    }
    _bind_file_plan_checkpoint(plan, checkpoint)
    return plan


def _cleanup_stale_drive_sync_tmp_files(
    directory: Path,
    *,
    now: float | None = None,
    protected: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Remove old MAGI download temp files left by interrupted sync runs."""
    try:
        max_age_sec = int(os.environ.get("MAGI_DRIVE_SYNC_TMP_MAX_AGE_SEC") or DEFAULT_STALE_TMP_MAX_AGE_SEC)
    except Exception as exc:
        if isinstance(exc, DriveCaseSyncDeadline):
            raise
        max_age_sec = DEFAULT_STALE_TMP_MAX_AGE_SEC
    if max_age_sec <= 0:
        return {"enabled": False, "removed": 0, "errors": 0}
    current_time = float(now if now is not None else time.time())
    removed = 0
    errors = 0
    protected_paths = {str(Path(path)) for path in (protected or [])}
    for tmp_path in directory.glob(f"{DRIVE_SYNC_TMP_PREFIX}*{DRIVE_SYNC_TMP_SUFFIX}"):
        try:
            if str(tmp_path) in protected_paths:
                continue
            if not tmp_path.is_file():
                continue
            age_sec = current_time - float(tmp_path.stat().st_mtime)
            if age_sec < max_age_sec:
                continue
            tmp_path.unlink()
            removed += 1
        except Exception as exc:
            if isinstance(exc, DriveCaseSyncDeadline):
                raise
            errors += 1
    return {"enabled": True, "removed": removed, "errors": errors}


def _download_drive_entry(
    service: Any,
    entry: FileEntry,
    target_path: Path,
    *,
    checkpoint: DriveFileCheckpoint | None = None,
    checkpoint_item_token: str = "",
    checkpoint_source_fingerprint: str = "",
) -> dict[str, Any]:
    from googleapiclient.http import MediaIoBaseDownload

    if target_path.exists():
        if entry.md5:
            try:
                local_md5 = local_file_md5(str(target_path))
            except Exception as exc:
                if isinstance(exc, DriveCaseSyncDeadline):
                    raise
                return {
                    "status": "pending_existing_unverified",
                    "target_path": str(target_path),
                    "bytes": 0,
                    "reason": f"local_hash_failed:{type(exc).__name__}",
                }
            if normalize_text(local_md5) == normalize_text(entry.md5):
                destination_proof = drive_checkpoint_proof_hash(
                    "download", int(target_path.stat().st_size), local_md5
                )
                if len(checkpoint_item_token) == 64:
                    # A prior worker can be terminated after the atomic final
                    # rename but before its checkpoint journal append.  Once
                    # the final file is independently verified, only the
                    # token-owned partial is stale and may be removed.
                    owned_partial = target_path.parent / (
                        f"{DRIVE_SYNC_TMP_PREFIX}{checkpoint_item_token}"
                        f"{DRIVE_SYNC_TMP_SUFFIX}"
                    )
                    try:
                        owned_partial.unlink(missing_ok=True)
                    except OSError:
                        # Cleanup is not part of the completion proof.
                        pass
                return {
                    "status": "skipped_existing",
                    "target_path": str(target_path),
                    "bytes": 0,
                    "hash_verification": "verified_checksum",
                    "destination_proof": destination_proof,
                }
            return {
                "status": "pending_existing_conflict",
                "target_path": str(target_path),
                "bytes": 0,
                "reason": "target_exists_checksum_differs",
                "local_md5": local_md5,
                "drive_md5": entry.md5,
            }
        completed = (
            checkpoint.completed(checkpoint_item_token, checkpoint_source_fingerprint)
            if checkpoint is not None
            and len(checkpoint_item_token) == 64
            and len(checkpoint_source_fingerprint) == 64
            else {}
        )
        if completed:
            try:
                local_md5 = local_file_md5(str(target_path))
                destination_proof = drive_checkpoint_proof_hash(
                    "download", int(target_path.stat().st_size), local_md5
                )
            except Exception as exc:
                if isinstance(exc, DriveCaseSyncDeadline):
                    raise
                destination_proof = ""
            if destination_proof and destination_proof == completed.get("destination_proof"):
                return {
                    "status": "skipped_existing",
                    "target_path": str(target_path),
                    "bytes": 0,
                    "hash_verification": "verified_checkpoint_proof",
                    "destination_proof": destination_proof,
                }
        return {
            "status": "pending_existing_unverified",
            "target_path": str(target_path),
            "bytes": 0,
            "reason": "drive_checksum_missing",
        }
    target_path.parent.mkdir(parents=True, exist_ok=True)
    stable_partial = bool(checkpoint is not None and len(checkpoint_item_token) == 64)
    stable_tmp_path = (
        target_path.parent
        / f"{DRIVE_SYNC_TMP_PREFIX}{checkpoint_item_token}{DRIVE_SYNC_TMP_SUFFIX}"
        if stable_partial
        else None
    )
    _cleanup_stale_drive_sync_tmp_files(
        target_path.parent,
        protected=[stable_tmp_path] if stable_tmp_path is not None else None,
    )
    request = None
    export = GOOGLE_EXPORT_MIME_MAP.get(entry.mime_type)
    if export:
        request = service.files().export_media(fileId=entry.drive_id, mimeType=export[0])
    else:
        request = service.files().get_media(fileId=entry.drive_id, supportsAllDrives=True)
    # Keep the temporary filename short.  Court PDFs often have intentionally
    # long descriptive names; reusing the target filename as the temp prefix can
    # exceed SMB/APFS filename limits even though the final target name is valid.
    if stable_tmp_path is not None:
        tmp_name = str(stable_tmp_path)
        try:
            existing_partial_size = int(stable_tmp_path.stat().st_size)
        except OSError:
            existing_partial_size = 0
        partial_row = (
            checkpoint.partial(
                checkpoint_item_token,
                checkpoint_source_fingerprint,
            )
            if checkpoint is not None
            and len(checkpoint_source_fingerprint) == 64
            else {}
        )
        trusted_partial = False
        if existing_partial_size > 0 and partial_row:
            try:
                trusted_partial = bool(
                    int(partial_row.get("bytes") or -1) == existing_partial_size
                    and str(partial_row.get("prefix_proof") or "")
                    == _download_partial_proof(stable_tmp_path)
                )
            except (OSError, ValueError):
                trusted_partial = False
        if entry.size is not None and existing_partial_size > int(entry.size):
            trusted_partial = False
        if existing_partial_size and not trusted_partial:
            # A token-shaped filename alone is not a resume proof. It can be a
            # stale pre-checkpoint artifact or contain bytes appended after a
            # crash but before the journal fsync. Restart only this MAGI-owned
            # partial and never touch an existing destination file.
            stable_tmp_path.unlink(missing_ok=True)
            existing_partial_size = 0
        fd = os.open(stable_tmp_path, os.O_RDWR | os.O_CREAT, 0o600)
    else:
        fd, tmp_name = tempfile.mkstemp(
            prefix=DRIVE_SYNC_TMP_PREFIX,
            suffix=DRIVE_SYNC_TMP_SUFFIX,
            dir=str(target_path.parent),
        )
    bytes_written = 0
    try:
        with os.fdopen(fd, "r+b") as fh:
            prefix_hasher = hashlib.md5()
            if existing_partial_size:
                fh.seek(0)
                remaining = existing_partial_size
                while remaining:
                    chunk = fh.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise DriveCaseSyncError("download_partial_short_read")
                    prefix_hasher.update(chunk)
                    remaining -= len(chunk)
            fh.seek(existing_partial_size, os.SEEK_SET)
            try:
                configured_chunk = int(
                    os.environ.get("MAGI_DRIVE_SYNC_DOWNLOAD_CHUNK_BYTES")
                    or DEFAULT_DRIVE_DOWNLOAD_CHUNK_BYTES
                )
            except Exception as exc:
                if isinstance(exc, DriveCaseSyncDeadline):
                    raise
                configured_chunk = DEFAULT_DRIVE_DOWNLOAD_CHUNK_BYTES
            # Avoid hundreds of HTTP round-trips for large court bundles while
            # keeping the per-transfer memory footprint bounded on the Mac.
            chunk_bytes = min(64 * 1024 * 1024, max(1024 * 1024, configured_chunk))
            downloader = MediaIoBaseDownload(fh, request, chunksize=chunk_bytes)
            if existing_partial_size:
                if not hasattr(downloader, "_progress"):
                    raise DriveCaseSyncError("download_resume_progress_unsupported")
                # googleapiclient initializes _progress=0 regardless of fd.tell().
                # Explicitly bind its Range header to the fsynced private proof.
                downloader._progress = existing_partial_size
            done = bool(
                entry.size is not None
                and existing_partial_size == int(entry.size)
                and trusted_partial
            )
            durable_position = existing_partial_size
            while not done:
                _status, done = downloader.next_chunk()
                bytes_written = int(fh.tell())
                fh.flush()
                os.fsync(fh.fileno())
                if bytes_written < durable_position:
                    raise DriveCaseSyncError("download_progress_regressed")
                fh.seek(durable_position)
                remaining = bytes_written - durable_position
                while remaining:
                    chunk = fh.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise DriveCaseSyncError("download_partial_short_read")
                    prefix_hasher.update(chunk)
                    remaining -= len(chunk)
                fh.seek(bytes_written)
                durable_position = bytes_written
                if stable_partial and checkpoint is not None:
                    checkpoint.record_partial(
                        checkpoint_item_token,
                        checkpoint_source_fingerprint,
                        byte_count=bytes_written,
                        prefix_proof=drive_checkpoint_proof_hash(
                            "download-partial-v1",
                            bytes_written,
                            prefix_hasher.hexdigest(),
                        ),
                    )
            bytes_written = int(fh.tell())
            fh.flush()
            os.fsync(fh.fileno())
        tmp_md5 = local_file_md5(tmp_name)
        if entry.md5 and normalize_text(tmp_md5) != normalize_text(entry.md5):
            os.unlink(tmp_name)
            raise DriveCaseSyncError("download_checksum_mismatch")
        destination_proof = drive_checkpoint_proof_hash(
            "download", bytes_written, tmp_md5
        )
        os.replace(tmp_name, target_path)
    except Exception:
        if not stable_partial:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise
    return {
        "status": "downloaded",
        "target_path": str(target_path),
        "bytes": bytes_written,
        "hash_verification": "verified_checksum" if entry.md5 else "verified_checkpoint_proof",
        "destination_proof": destination_proof,
    }


def _download_partial_proof(path: Path) -> str:
    candidate = Path(path)
    return drive_checkpoint_proof_hash(
        "download-partial-v1",
        int(candidate.stat().st_size),
        local_file_md5(str(candidate)),
    )


def execute_drive_to_nas_downloads(
    drive_service: Any,
    file_sync_plan: dict[str, Any],
    *,
    download_limit: int = 0,
    max_download_bytes: int = 0,
    checkpoint: DriveFileCheckpoint | None = None,
) -> dict[str, Any]:
    manifest: list[dict[str, Any]] = []
    summary = {
        "attempted": 0,
        "downloaded": 0,
        "skipped_existing": 0,
        "failed": 0,
        "pending_unverified": 0,
        "large_download_deferred": 0,
        "storage_unavailable": 0,
        "bytes": 0,
        "stopped_by_limit": False,
        "stopped_by_bytes": False,
    }
    max_single_download_bytes = max(
        0,
        int(os.environ.get("MAGI_DRIVE_SYNC_MAX_SINGLE_DOWNLOAD_BYTES") or "0"),
    )
    for case in file_sync_plan.get("cases") or []:
        for action in case.get("download_missing") or []:
            if download_limit and summary["attempted"] >= download_limit:
                summary["stopped_by_limit"] = True
                break
            size_hint = int(action.get("size") or 0)
            if max_download_bytes and size_hint and summary["bytes"] + size_hint > max_download_bytes:
                summary["stopped_by_bytes"] = True
                break
            entry = FileEntry(
                source="drive",
                path=str(action.get("relative_path") or ""),
                relative_path=str(action.get("relative_path") or ""),
                name=str(action.get("name") or ""),
                is_folder=False,
                modified_time=str(action.get("modified_time") or ""),
                size=int(action["size"]) if str(action.get("size") or "").isdigit() else None,
                md5=str(action.get("md5") or ""),
                drive_id=str(action.get("drive_id") or ""),
                web_url=str(action.get("web_url") or ""),
                mime_type=str(action.get("mime_type") or ""),
            )
            target_path = Path(str(action.get("target_path") or ""))
            summary["attempted"] += 1
            record = {
                "case_number": case.get("case_number"),
                "drive_path": case.get("drive_path"),
                "drive_relative_path": action.get("relative_path"),
                "target_path": str(target_path),
                "status": "",
                "bytes": 0,
                "error": "",
            }
            if max_single_download_bytes and size_hint and size_hint > max_single_download_bytes:
                record["status"] = "deferred_large_file"
                record["reason"] = f"large_download_deferred:{size_hint}>{max_single_download_bytes}"
                summary["large_download_deferred"] += 1
                manifest.append(record)
                continue
            try:
                checkpoint_item = str(action.get("checkpoint_item_token") or "")
                checkpoint_source = str(action.get("checkpoint_source_fingerprint") or "")
                result = _download_drive_entry(
                    drive_service,
                    entry,
                    target_path,
                    checkpoint=checkpoint,
                    checkpoint_item_token=checkpoint_item,
                    checkpoint_source_fingerprint=checkpoint_source,
                )
                record.update(result)
                if result["status"] == "downloaded":
                    summary["downloaded"] += 1
                    summary["bytes"] += int(result.get("bytes") or 0)
                elif result["status"] == "skipped_existing":
                    summary["skipped_existing"] += 1
                elif str(result.get("status") or "").startswith("pending_"):
                    summary["pending_unverified"] += 1
                if (
                    checkpoint is not None
                    and len(checkpoint_item) == 64
                    and len(checkpoint_source) == 64
                    and str(result.get("hash_verification") or "").startswith("verified_")
                    and len(str(result.get("destination_proof") or "")) == 64
                ):
                    checkpoint.mark_completed(
                        checkpoint_item,
                        checkpoint_source,
                        outcome=(
                            "downloaded_verified"
                            if result.get("status") == "downloaded"
                            else "verified_existing"
                        ),
                        destination_proof=str(result["destination_proof"]),
                        byte_count=int(result.get("bytes") or 0),
                        verified=True,
                    )
            except Exception as exc:
                if isinstance(exc, DriveCaseSyncDeadline):
                    raise
                record["error"] = f"{type(exc).__name__}: {exc}"
                if is_download_target_storage_unavailable_error(exc, target_path):
                    record["status"] = "deferred_storage_unavailable"
                    record["reason"] = "storage_unavailable"
                    summary["storage_unavailable"] += 1
                    manifest.append(record)
                    break
                record["status"] = "failed"
                summary["failed"] += 1
            manifest.append(record)
        if (
            summary["stopped_by_limit"]
            or summary["stopped_by_bytes"]
            or summary["storage_unavailable"]
        ):
            break
    return {
        "ok": (
            summary["failed"] == 0
            and summary["pending_unverified"] == 0
            and (_deferred_downloads_are_ok() or summary["large_download_deferred"] == 0)
            and not summary["stopped_by_limit"]
            and not summary["stopped_by_bytes"]
        ),
        "mode": "execute_drive_to_nas_missing_only",
        "write_actions_enabled": True,
        "safety": "no_overwrite_no_delete",
        "summary": summary,
        "manifest": manifest,
    }


def execute_nas_to_drive_uploads(
    drive_service: Any,
    file_sync_plan: dict[str, Any],
    *,
    upload_limit: int = 0,
    max_upload_bytes: int = 0,
    checkpoint: DriveFileCheckpoint | None = None,
) -> dict[str, Any]:
    manifest: list[dict[str, Any]] = []
    summary = {
        "attempted": 0,
        "uploaded": 0,
        "skipped_existing": 0,
        "failed": 0,
        "pending_unverified": 0,
        "bytes": 0,
        "folders_created": 0,
        "large_upload_deferred": 0,
        "storage_unavailable": 0,
        "source_unavailable_after_scan": 0,
        "stopped_by_limit": False,
        "stopped_by_bytes": False,
    }
    max_single_upload_bytes = max(
        0,
        int(os.environ.get("MAGI_DRIVE_SYNC_MAX_SINGLE_UPLOAD_BYTES") or DEFAULT_MAX_SINGLE_UPLOAD_BYTES),
    )
    for case in file_sync_plan.get("cases") or []:
        drive_case_folder_id = str(case.get("drive_id") or "")
        if not drive_case_folder_id:
            continue
        for action in case.get("nas_only") or []:
            if upload_limit and summary["attempted"] >= upload_limit:
                summary["stopped_by_limit"] = True
                break
            size_hint = int(action.get("size") or 0)
            local_path = Path(str(action.get("path") or ""))
            relative_path = str(action.get("target_relative_path") or action.get("relative_path") or "")
            record = {
                "case_number": case.get("case_number"),
                "drive_path": case.get("drive_path"),
                "local_path": str(local_path),
                "drive_relative_path": relative_path,
                "status": "",
                "bytes": 0,
                "drive_id": "",
                "web_url": "",
                "error": "",
                "created_folders": [],
            }
            # Classify a file which is larger than the single-file safety
            # envelope before applying the per-run byte budget.  Checking the
            # batch budget first made an oversized first item report
            # ``stopped_by_bytes`` with zero attempts forever, so every retry
            # revisited the same cursor without producing a durable reason.
            if max_single_upload_bytes and size_hint and size_hint > max_single_upload_bytes:
                record["status"] = "deferred_large_file"
                record["reason"] = f"large_upload_deferred:{size_hint}>{max_single_upload_bytes}"
                summary["large_upload_deferred"] += 1
                manifest.append(record)
                continue
            if max_upload_bytes and size_hint and summary["bytes"] + size_hint > max_upload_bytes:
                summary["stopped_by_bytes"] = True
                break
            summary["attempted"] += 1
            try:
                # smbfs can transiently answer ENOENT/False immediately after
                # a successful directory walk.  Recheck before opening; if
                # the source is still unavailable, retain the same sweep
                # position for an automatic retry instead of manufacturing a
                # hard per-file failure for every item in the case.
                source_ready = False
                source_probe_attempts = max(
                    1,
                    int(os.environ.get("MAGI_DRIVE_SYNC_SOURCE_PROBE_ATTEMPTS") or "12"),
                )
                for probe_index in range(source_probe_attempts):
                    try:
                        source_ready = local_path.exists() and local_path.is_file()
                    except OSError as exc:
                        if is_storage_unavailable_error(exc):
                            source_ready = False
                        else:
                            raise
                    if source_ready:
                        break
                    if probe_index + 1 < source_probe_attempts:
                        # smbfs can report a healthy file as absent for several
                        # seconds while reconnecting a tree/session.  Keep the
                        # retry bounded, but allow enough time for that normal
                        # reconnection instead of turning it into a failed
                        # all-files sweep that requires human intervention.
                        time.sleep(min(1.0, 0.25 * (probe_index + 1)))
                if not source_ready:
                    record["status"] = "deferred_storage_unavailable"
                    record["reason"] = "source_unavailable_after_scan"
                    summary["storage_unavailable"] += 1
                    summary["source_unavailable_after_scan"] += 1
                    manifest.append(record)
                    break
                with staged_upload_source(local_path) as upload_path:
                    checkpoint_item = str(action.get("checkpoint_item_token") or "")
                    checkpoint_source = str(action.get("checkpoint_source_fingerprint") or "")
                    result = upload_local_file_to_drive(
                        drive_service,
                        local_path=upload_path,
                        drive_case_folder_id=drive_case_folder_id,
                        relative_path=relative_path,
                        checkpoint=checkpoint,
                        checkpoint_item_token=checkpoint_item,
                        checkpoint_source_fingerprint=checkpoint_source,
                    )
                if (
                    checkpoint is not None
                    and len(checkpoint_item) == 64
                    and len(checkpoint_source) == 64
                    and _file_action_source_fingerprint(
                        action,
                        direction="upload",
                        checkpoint=checkpoint,
                        require_live_stat=True,
                    )
                    != checkpoint_source
                ):
                    # The staged bytes may have uploaded successfully, but a
                    # concurrently replaced NAS source is a new plan item. Do
                    # not journal completion or advance the case cursor.
                    result = {
                        **result,
                        "status": "pending_uploaded_source_changed",
                        "reason": "upload_source_changed_during_transfer",
                        "hash_verification": "",
                        "destination_proof": "",
                    }
                record.update(result)
                if result["status"] == "uploaded":
                    summary["uploaded"] += 1
                    summary["bytes"] += int(result.get("bytes") or 0)
                elif result["status"] == "skipped_existing":
                    summary["skipped_existing"] += 1
                elif str(result.get("status") or "").startswith("pending_"):
                    summary["pending_unverified"] += 1
                summary["folders_created"] += len(result.get("created_folders") or [])
                if (
                    checkpoint is not None
                    and len(checkpoint_item) == 64
                    and len(checkpoint_source) == 64
                    and result.get("hash_verification") == "verified_checksum"
                    and len(str(result.get("destination_proof") or "")) == 64
                ):
                    checkpoint.mark_completed(
                        checkpoint_item,
                        checkpoint_source,
                        outcome=(
                            "uploaded_verified"
                            if result.get("status") == "uploaded"
                            else "verified_existing"
                        ),
                        destination_proof=str(result["destination_proof"]),
                        byte_count=int(result.get("bytes") or 0),
                        verified=True,
                    )
            except Exception as exc:
                if isinstance(exc, DriveCaseSyncDeadline):
                    raise
                record["error"] = f"{type(exc).__name__}: {exc}"
                if is_storage_unavailable_error(exc):
                    # The NAS mount disappeared mid-run.  Do not inspect more
                    # paths (which would turn this single condition into false
                    # missing-file failures), and leave this item for retry.
                    record["status"] = "deferred_storage_unavailable"
                    record["reason"] = "storage_unavailable"
                    summary["storage_unavailable"] += 1
                    manifest.append(record)
                    break
                record["status"] = "failed"
                summary["failed"] += 1
            manifest.append(record)
        if (
            summary["stopped_by_limit"]
            or summary["stopped_by_bytes"]
            or summary["storage_unavailable"]
        ):
            break
    return {
        "ok": (
            summary["failed"] == 0
            and summary["pending_unverified"] == 0
            and (_deferred_uploads_are_ok() or summary["large_upload_deferred"] == 0)
            and not summary["stopped_by_limit"]
            and not summary["stopped_by_bytes"]
        ),
        "mode": "execute_nas_to_drive_missing_only",
        "write_actions_enabled": True,
        "safety": "no_overwrite_no_delete",
        "summary": summary,
        "manifest": manifest,
    }


def _deferred_uploads_are_ok() -> bool:
    return str(os.environ.get("MAGI_DRIVE_SYNC_DEFERRED_UPLOADS_ARE_OK") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _deferred_downloads_are_ok() -> bool:
    return str(os.environ.get("MAGI_DRIVE_SYNC_DEFERRED_DOWNLOADS_ARE_OK") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _unverified_existing_files_are_ok() -> bool:
    return str(os.environ.get("MAGI_DRIVE_SYNC_UNVERIFIED_EXISTING_ARE_OK") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def combine_execution_results(
    *,
    download_result: dict[str, Any] | None = None,
    upload_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if download_result and not upload_result:
        return download_result
    if upload_result and not download_result:
        return upload_result
    download_summary = (download_result or {}).get("summary") or {}
    upload_summary = (upload_result or {}).get("summary") or {}
    deferred_uploads_are_ok = _deferred_uploads_are_ok()
    deferred_downloads_are_ok = _deferred_downloads_are_ok()
    download_partial_keys = ["pending_unverified", "stopped_by_limit", "stopped_by_bytes"]
    if not deferred_downloads_are_ok:
        download_partial_keys.append("large_download_deferred")
    download_partial = any(
        bool(download_summary.get(key))
        for key in download_partial_keys
    )
    upload_partial_keys = ["pending_unverified", "stopped_by_limit", "stopped_by_bytes"]
    if not deferred_uploads_are_ok:
        upload_partial_keys.append("large_upload_deferred")
    upload_partial = any(
        bool(upload_summary.get(key))
        for key in upload_partial_keys
    )
    return {
        "ok": (
            (download_summary.get("failed", 0) or 0) == 0
            and (upload_summary.get("failed", 0) or 0) == 0
            and not download_partial
            and not upload_partial
        ),
        "mode": "execute_bidirectional_missing_only",
        "write_actions_enabled": True,
        "safety": "no_overwrite_no_delete_conflicts_blocked",
        "summary": {
            "download_attempted": download_summary.get("attempted", 0),
            "downloaded": download_summary.get("downloaded", 0),
            "download_skipped_existing": download_summary.get("skipped_existing", 0),
            "download_failed": download_summary.get("failed", 0),
            "download_pending_unverified": download_summary.get("pending_unverified", 0),
            "download_large_deferred": download_summary.get("large_download_deferred", 0),
            "download_storage_unavailable": download_summary.get("storage_unavailable", 0),
            "download_bytes": download_summary.get("bytes", 0),
            "upload_attempted": upload_summary.get("attempted", 0),
            "uploaded": upload_summary.get("uploaded", 0),
            "upload_skipped_existing": upload_summary.get("skipped_existing", 0),
            "upload_failed": upload_summary.get("failed", 0),
            "upload_pending_unverified": upload_summary.get("pending_unverified", 0),
            "upload_large_deferred": upload_summary.get("large_upload_deferred", 0),
            "upload_storage_unavailable": upload_summary.get("storage_unavailable", 0),
            "upload_bytes": upload_summary.get("bytes", 0),
            "upload_folders_created": upload_summary.get("folders_created", 0),
        },
        "download_result": download_result or {},
        "upload_result": upload_result or {},
    }


def report_has_partial_failures(report: dict[str, Any]) -> bool:
    """Return True when a Drive sync report contains failed write/repair work."""
    def _failed(summary: dict[str, Any], *keys: str) -> int:
        total = 0
        for key in keys:
            try:
                total += int(summary.get(key) or 0)
            except Exception:
                continue
        return total

    execution_summary = (report.get("execution_result") or {}).get("summary") or {}
    file_summary = (report.get("file_sync_plan") or {}).get("summary") or {}
    folder_summary = (report.get("drive_folder_result") or {}).get("summary") or {}
    repair_summary = (report.get("duplicate_repair_result") or {}).get("summary") or {}
    local_repair_summary = (report.get("local_duplicate_repair_result") or {}).get("summary") or {}
    imported_repair_summary = (report.get("drive_imported_folder_repair") or {}).get("summary") or {}
    pending_execution_keys = [
        "pending_unverified",
        "download_pending_unverified",
        "upload_pending_unverified",
    ]
    if not _deferred_downloads_are_ok():
        pending_execution_keys.extend(["download_large_deferred", "large_download_deferred"])
    if not _deferred_uploads_are_ok():
        pending_execution_keys.extend(["upload_large_deferred", "large_upload_deferred"])
    file_summary_failure_keys = ["case_errors"]
    if not _unverified_existing_files_are_ok():
        file_summary_failure_keys.append("pending_unverified_files")
    return any(
        (
            _failed(execution_summary, "failed", "download_failed", "upload_failed") > 0,
            _failed(execution_summary, *pending_execution_keys) > 0,
            bool(execution_summary.get("stopped_by_limit") or execution_summary.get("stopped_by_bytes")),
            _failed(file_summary, *file_summary_failure_keys) > 0,
            _failed(folder_summary, "failed") > 0,
            _failed(repair_summary, "failed") > 0,
            _failed(local_repair_summary, "failed", "case_errors") > 0,
            _failed(imported_repair_summary, "errors") > 0,
        )
    )


def report_write_actions_enabled(*sections: dict[str, Any] | None) -> bool:
    """Return True when any nested report section had write actions enabled."""
    return any(bool(section.get("write_actions_enabled")) for section in sections if isinstance(section, dict))


def _drive_case_from_local_case_result(local_case: CaseFolder, result: dict[str, Any]) -> CaseFolder | None:
    drive_id = str(result.get("drive_id") or "").strip()
    relative_path = str(result.get("relative_path") or "").strip()
    if not drive_id or not relative_path:
        return None
    if drive_case_sync_exclusion_reason(relative_path, require_canonical_layout=True):
        return None
    name = PurePosixPath(relative_path).name
    return CaseFolder(
        source="drive",
        path=relative_path,
        relative_path=relative_path,
        name=name,
        category=local_case.category,
        status=local_case.status,
        case_kind=local_case.case_kind,
        owner_bucket=drive_owner_bucket(),
        meta=CaseMeta(
            case_number=local_case.meta.case_number,
            laf_case_no=local_case.meta.laf_case_no,
            court_case_no=local_case.meta.court_case_no,
            client_hint=local_case.meta.client_hint,
            reason_hint=local_case.meta.reason_hint,
        ),
        drive_id=drive_id,
    )


def run_priority_case_sync(
    *,
    case_numbers: Iterable[str],
    root_id: str = "",
    root_name: str = DEFAULT_DRIVE_ROOT_NAME,
    output_dir: Path | None = None,
    interactive: bool = False,
    file_diff: bool = True,
    execute_downloads: bool = False,
    execute_uploads: bool = False,
    download_limit: int = 0,
    max_download_bytes: int = 0,
    upload_limit: int = 0,
    max_upload_bytes: int = 0,
    max_case_depth: int = 20,
    max_case_items: int = 10000,
    ensure_drive_case_folders: bool = False,
    drive_owner_bucket_name: str = "",
    repair_local_duplicates: bool = False,
    execute_local_duplicate_repair: bool = False,
    repair_local_duplicate_limit: int = 0,
    checkpoint: DriveFileCheckpoint | None = None,
) -> dict[str, Any]:
    """Synchronize explicit DB cases without full Drive/NAS inventory.

    This is the production path for upcoming todos.  It keeps Google Drive and
    NAS folder conventions independent: DB paths decide the NAS side, while
    ``drive_relative_path_for_local_case`` decides the visible Drive side.
    """
    clean_numbers = []
    seen_numbers: set[str] = set()
    for raw in case_numbers or []:
        value = str(raw or "").strip()
        if value and value not in seen_numbers:
            seen_numbers.add(value)
            clean_numbers.append(value)

    if checkpoint is not None:
        checkpoint.set_phase("resolve_case")
    load_local_env()
    write = execute_uploads or ensure_drive_case_folders
    service = build_drive_service(interactive=interactive, write=write)
    drive_root = find_drive_root(
        service,
        root_id=root_id or os.environ.get("MAGI_DRIVE_SYNC_ROOT_FOLDER_ID", ""),
        root_name=root_name,
    )
    owner_bucket = drive_owner_bucket_name or drive_owner_bucket()
    local_cases, skipped_db_cases = db_local_cases_for_numbers(
        clean_numbers,
        for_write=bool(execute_downloads),
        allow_missing_closed=bool(execute_downloads),
    )

    matched: list[dict[str, Any]] = []
    drive_cases: list[CaseFolder] = []
    folder_records: list[dict[str, Any]] = []
    for local_case in local_cases:
        try:
            if ensure_drive_case_folders:
                folder_result = ensure_drive_case_folder_for_local_case(
                    service,
                    str(drive_root.get("id") or ""),
                    local_case,
                    owner_bucket=owner_bucket,
                )
            else:
                folder_result = find_existing_drive_case_folder_for_local_case(
                    service,
                    str(drive_root.get("id") or ""),
                    local_case,
                    owner_bucket=owner_bucket,
                )
        except Exception as exc:
            if isinstance(exc, DriveCaseSyncDeadline):
                raise
            folder_result = {
                "ok": False,
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "case_number": local_case.meta.case_number,
                "case_name": local_case.name,
            }
        resolved_path = str(folder_result.get("relative_path") or "").strip()
        resolved_path_reason = (
            drive_case_sync_exclusion_reason(resolved_path, require_canonical_layout=True)
            if resolved_path
            else ""
        )
        if folder_result.get("ok") and resolved_path_reason:
            folder_result = {
                **folder_result,
                "ok": False,
                "skipped": True,
                "status": "blocked_forbidden_drive_case_result",
                "reason": resolved_path_reason,
                "created_folders": [],
                "created_count": 0,
                "message": (
                    "Drive 資料夾解析結果落在待整理/重複/衝突隔離或非 canonical 路徑；"
                    "direct DB sync 已 fail-closed，不掃描檔案也不執行雙向同步。"
                ),
            }
        folder_records.append(folder_result)
        drive_case = _drive_case_from_local_case_result(local_case, folder_result)
        if not drive_case:
            continue
        drive_cases.append(drive_case)
        matched.append({
            "drive": drive_case,
            "local": local_case,
            "match_keys": match_keys(local_case.meta),
            "context_resolution": {"status": "resolved_by_db_case_number_direct"},
        })

    comparison = {
        "matched": matched,
        "drive_only": [],
            "local_only": [],
            "ambiguous": [],
            "out_of_scope": [
            {
                "local": CaseFolder(
                    source="nas",
                    path=str(item.get("folder_path") or ""),
                    local_path="",
                    relative_path=str(item.get("folder_path") or item.get("case_number") or ""),
                    name=str(item.get("case_number") or ""),
                    meta=CaseMeta(case_number=str(item.get("case_number") or "")),
                ),
                "reason": str(item.get("reason") or ""),
            }
                for item in skipped_db_cases
            ],
            "drive_duplicates": [],
        }

    file_sync_plan: dict[str, Any] | None = None
    if file_diff or execute_downloads or execute_uploads:
        if checkpoint is not None:
            checkpoint.set_phase("scan_plan")
        file_sync_plan = build_file_sync_plan(
            comparison,
            service,
            max_case_depth=max_case_depth,
            max_case_items=max_case_items,
            matched_case_limit=0,
            matched_case_offset=0,
            priority_case_numbers=clean_numbers,
            checkpoint=checkpoint,
        )
        file_sync_plan["mode"] = "direct_db_case_file_diff"

    download_result: dict[str, Any] | None = None
    upload_result: dict[str, Any] | None = None
    if execute_downloads:
        if checkpoint is not None:
            checkpoint.set_phase("download")
        download_result = execute_drive_to_nas_downloads(
            service,
            file_sync_plan or {},
            download_limit=download_limit,
            max_download_bytes=max_download_bytes,
            checkpoint=checkpoint,
        )
    if execute_uploads:
        if checkpoint is not None:
            checkpoint.set_phase("upload")
        upload_result = execute_nas_to_drive_uploads(
            service,
            file_sync_plan or {},
            upload_limit=upload_limit,
            max_upload_bytes=max_upload_bytes,
            checkpoint=checkpoint,
        )
    if checkpoint is not None:
        checkpoint.set_phase("verify")
    local_duplicate_repair_result: dict[str, Any] | None = None
    if repair_local_duplicates or execute_local_duplicate_repair:
        local_duplicate_repair_result = repair_local_duplicate_content_for_cases(
            comparison,
            execute=execute_local_duplicate_repair,
            repair_limit=repair_local_duplicate_limit,
            max_case_depth=max_case_depth,
            max_case_items=max_case_items,
        )
    execution_result = None
    if download_result or upload_result:
        execution_result = combine_execution_results(
            download_result=download_result,
            upload_result=upload_result,
        )
    drive_folder_result = {
        "ok": sum(1 for item in folder_records if (not item.get("ok")) and not item.get("skipped")) == 0,
        "mode": "direct_db_case_drive_folder_resolution",
        "write_actions_enabled": bool(ensure_drive_case_folders or execute_uploads),
        "safety": "find_or_create_case_folder_only_no_file_delete",
        "summary": {
            "attempted": len(local_cases),
            "resolved": sum(1 for item in folder_records if item.get("ok")),
            "created_or_existing": sum(1 for item in folder_records if item.get("ok")),
            "created_folders": sum(int(item.get("created_count") or 0) for item in folder_records if item.get("ok")),
            "skipped": sum(1 for item in folder_records if item.get("skipped")),
            "failed": sum(1 for item in folder_records if (not item.get("ok")) and not item.get("skipped")),
        },
        "records": folder_records,
        "db_skipped_cases": skipped_db_cases,
    }
    report = build_report(
        drive_root=drive_root,
        drive_entries=[],
        drive_cases=drive_cases,
        local_entries=[],
        local_cases=local_cases,
        local_roots=[],
        comparison=comparison,
        file_sync_plan=file_sync_plan,
        execution_result=execution_result,
        drive_folder_result=drive_folder_result,
        local_duplicate_repair_result=local_duplicate_repair_result,
    )
    report["mode"] = "direct_db_case_sync"
    report["direct_case_numbers"] = clean_numbers
    paths = write_report_files(report, output_dir or runtime_dir())
    report["output_paths"] = paths
    return report


def _drive_duplicate_canonical_score(case: CaseFolder) -> tuple[int, str]:
    path = normalize_text(case.relative_path)
    owner = normalize_text(case.owner_bucket)
    score = 0
    if case.status == "closed":
        score += 400
    if owner.startswith("lumi"):
        score += 120
    if owner == "aaron":
        score -= 200
    if case.meta.case_number:
        score += 80
    if case.meta.laf_case_no:
        score += 40
    if "01.消債" in path:
        score += 10
    if "結案案件" in path:
        score += 20
    if path.endswith("等"):
        score -= 5
    return score, case.relative_path


def choose_drive_duplicate_canonical_case(group: dict[str, Any]) -> CaseFolder | None:
    cases = [c for c in (group.get("cases") or []) if isinstance(c, CaseFolder) and c.drive_id]
    if not cases:
        return None
    local = group.get("local") if isinstance(group.get("local"), CaseFolder) else None

    def score(case: CaseFolder) -> tuple[int, str]:
        base_score, path = _drive_duplicate_canonical_score(case)
        if local:
            if case.status == local.status:
                base_score += 1000
            if normalize_drive_case_category(case.category) == normalize_drive_case_category(local.category):
                base_score += 500
            if case.case_kind and local.case_kind and case.case_kind == local.case_kind:
                base_score += 100
        return base_score, path

    return sorted(cases, key=score, reverse=True)[0]


def _drive_duplicate_repair_groups(comparison: dict[str, Any]) -> list[dict[str, Any]]:
    """Combine strong duplicate groups with reverse many-to-one groups."""
    groups = list(comparison.get("drive_duplicates") or [])
    for item in comparison.get("drive_many_to_one") or []:
        local = item.get("local")
        drives = [c for c in item.get("drives") or [] if isinstance(c, CaseFolder) and c.drive_id]
        if not isinstance(local, CaseFolder) or len(drives) <= 1:
            continue
        local_key = hashlib.sha256(local.relative_path.encode("utf-8")).hexdigest()[:20]
        groups.append({
            "identity_key": f"nas_match:{local_key}",
            "identity_keys": [],
            "cases": drives,
            "local": local,
            "reason": item.get("reason", ""),
            "source": "drive_many_to_one",
        })
    return groups


def _drive_item_public(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "mime_type": str(item.get("mimeType") or ""),
        "parents": item.get("parents") or [],
        "size": int(item["size"]) if str(item.get("size") or "").isdigit() else None,
        "md5": str(item.get("md5Checksum") or ""),
        "web_url": str(item.get("webViewLink") or ""),
    }


def _drive_items_same_content(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("mimeType") == GOOGLE_FOLDER_MIME or b.get("mimeType") == GOOGLE_FOLDER_MIME:
        return False
    a_md5 = str(a.get("md5Checksum") or "")
    b_md5 = str(b.get("md5Checksum") or "")
    if a_md5 and b_md5:
        return a_md5 == b_md5
    # Equal size is not proof of equal content.  Google-native files and some
    # API responses have no MD5; treating them as duplicates could leave the
    # only copy inside a source folder that is subsequently trashed.
    return False


def _drive_conflict_name(name: str, source_id: str) -> str:
    text = str(name or "未命名")
    suffix = f"（Drive重複來源-{str(source_id or '')[:8]}）"
    if "." in text and not text.startswith("."):
        stem, ext = text.rsplit(".", 1)
        return f"{stem}{suffix}.{ext}"
    return f"{text}{suffix}"


def _move_or_plan_drive_item(
    service: Any,
    *,
    item: dict[str, Any],
    source_parent_id: str,
    target_parent_id: str,
    new_name: str = "",
    execute: bool,
) -> dict[str, Any]:
    record = {
        "item": _drive_item_public(item),
        "source_parent_id": source_parent_id,
        "target_parent_id": target_parent_id,
        "new_name": new_name,
        "status": "planned",
    }
    if not execute:
        return record
    moved = move_drive_item(
        service,
        str(item.get("id") or ""),
        add_parent_id=target_parent_id,
        remove_parent_ids=[source_parent_id],
        new_name=new_name,
    )
    record["status"] = "moved"
    record["result"] = _drive_item_public(moved)
    return record


def _merge_drive_folder_children(
    service: Any,
    *,
    source_folder_id: str,
    target_folder_id: str,
    execute: bool,
    max_depth: int,
    max_items: int,
    depth: int = 0,
    counter: dict[str, int] | None = None,
) -> dict[str, Any]:
    counter = counter if counter is not None else {"items": 0}
    result = {
        "moved": [],
        "renamed_conflicts": [],
        "merged_folders": [],
        "skipped_same_content": [],
        "skipped_limit": False,
    }
    if depth >= max_depth:
        result["skipped_limit"] = True
        return result
    source_children = _drive_list_children(service, source_folder_id)
    target_children = _drive_list_children(service, target_folder_id)
    target_name_groups: dict[str, list[dict[str, Any]]] = {}
    for item in target_children:
        target_name_groups.setdefault(str(item.get("name") or ""), []).append(item)
    duplicate_target_names = sorted(name for name, items in target_name_groups.items() if len(items) > 1)
    if duplicate_target_names:
        raise DriveCaseSyncError(
            f"duplicate_target_children:{target_folder_id}:{'|'.join(duplicate_target_names[:10])}"
        )
    target_by_name = {name: items[0] for name, items in target_name_groups.items()}
    for item in source_children:
        if max_items and counter["items"] >= max_items:
            result["skipped_limit"] = True
            break
        counter["items"] += 1
        name = str(item.get("name") or "")
        item_id = str(item.get("id") or "")
        target_item = target_by_name.get(name)
        is_folder = item.get("mimeType") == GOOGLE_FOLDER_MIME
        if target_item and is_folder and target_item.get("mimeType") == GOOGLE_FOLDER_MIME:
            child_result = _merge_drive_folder_children(
                service,
                source_folder_id=item_id,
                target_folder_id=str(target_item.get("id") or ""),
                execute=execute,
                max_depth=max_depth,
                max_items=max_items,
                depth=depth + 1,
                counter=counter,
            )
            result["merged_folders"].append({
                "source": _drive_item_public(item),
                "target": _drive_item_public(target_item),
                "result": child_result,
            })
            if child_result.get("skipped_limit"):
                result["skipped_limit"] = True
                break
            continue
        if target_item and _drive_items_same_content(item, target_item):
            result["skipped_same_content"].append({
                "source": _drive_item_public(item),
                "target": _drive_item_public(target_item),
                "reason": "same_name_same_content_kept_in_quarantine_copy",
            })
            continue
        new_name = ""
        if target_item:
            new_name = _drive_conflict_name(name, item_id)
            if new_name in target_by_name:
                raise DriveCaseSyncError(
                    f"drive_conflict_target_already_exists:{target_folder_id}:{new_name}"
                )
        record = _move_or_plan_drive_item(
            service,
            item=item,
            source_parent_id=source_folder_id,
            target_parent_id=target_folder_id,
            new_name=new_name,
            execute=execute,
        )
        if new_name:
            record["status"] = "renamed_conflict" if not execute else "moved_renamed_conflict"
            result["renamed_conflicts"].append(record)
        else:
            result["moved"].append(record)
        target_by_name[new_name or name] = (
            record.get("result")
            or {
                **item,
                "name": new_name or name,
                "parents": [target_folder_id],
            }
        )
    return result


def normalize_duplicate_drive_children(
    service: Any,
    *,
    folder_id: str,
    review_parent_id: str,
    execute: bool,
    max_depth: int = 8,
    max_items: int = 1000,
    depth: int = 0,
) -> dict[str, Any]:
    """Resolve duplicate sibling names without overwriting or deleting data."""
    result = {
        "duplicate_name_groups": 0,
        "folders_merged": 0,
        "items_renamed": 0,
        "same_content_moved_to_review": 0,
        "skipped_limit": False,
        "records": [],
    }
    if depth >= max_depth:
        result["skipped_limit"] = True
        return result
    children = _drive_list_children(service, folder_id)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in children:
        by_name.setdefault(str(item.get("name") or ""), []).append(item)
    existing_names = set(by_name)
    processed = 0
    for name, items in sorted(by_name.items()):
        if len(items) <= 1:
            continue
        result["duplicate_name_groups"] += 1
        if max_items and processed + len(items) > max_items:
            result["skipped_limit"] = True
            break
        processed += len(items)
        folders = [item for item in items if item.get("mimeType") == GOOGLE_FOLDER_MIME]
        record = {"name": name, "count": len(items), "actions": []}
        if len(folders) == len(items):
            scored: list[tuple[int, str, dict[str, Any]]] = []
            for folder in folders:
                folder_children = _drive_list_children(service, str(folder.get("id") or ""))
                scored.append((len(folder_children), str(folder.get("id") or ""), folder))
            canonical = sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)[0][2]
            canonical_id = str(canonical.get("id") or "")
            nested = normalize_duplicate_drive_children(
                service,
                folder_id=canonical_id,
                review_parent_id=review_parent_id,
                execute=execute,
                max_depth=max_depth,
                max_items=max_items,
                depth=depth + 1,
            )
            record["nested"] = nested
            if nested.get("skipped_limit"):
                result["skipped_limit"] = True
                result["records"].append(record)
                break
            for extra in sorted(folders, key=lambda item: str(item.get("id") or "")):
                extra_id = str(extra.get("id") or "")
                if extra_id == canonical_id:
                    continue
                merge = _merge_drive_folder_children(
                    service,
                    source_folder_id=extra_id,
                    target_folder_id=canonical_id,
                    execute=execute,
                    max_depth=max_depth,
                    max_items=max_items,
                    counter={"items": 0},
                )
                action = {"item_id": extra_id, "action": "merge_duplicate_folder", "merge": merge}
                if merge.get("skipped_limit"):
                    result["skipped_limit"] = True
                    record["actions"].append(action)
                    break
                review_name = f"{name}（同名資料夾-{extra_id[:8]}）"
                if execute:
                    moved = move_drive_item(
                        service,
                        extra_id,
                        add_parent_id=review_parent_id,
                        remove_parent_ids=[folder_id],
                        new_name=review_name,
                    )
                    action["result"] = _drive_item_public(moved)
                action["review_name"] = review_name
                result["folders_merged"] += 1
                record["actions"].append(action)
        else:
            canonical = sorted(
                items,
                key=lambda item: (
                    item.get("mimeType") == GOOGLE_FOLDER_MIME,
                    bool(item.get("md5Checksum")),
                    str(item.get("id") or ""),
                ),
                reverse=True,
            )[0]
            canonical_id = str(canonical.get("id") or "")
            for extra in sorted(items, key=lambda item: str(item.get("id") or "")):
                extra_id = str(extra.get("id") or "")
                if extra_id == canonical_id:
                    continue
                new_name = _drive_conflict_name(name, extra_id)
                if new_name in existing_names:
                    raise DriveCaseSyncError(f"drive_normalized_conflict_name_exists:{folder_id}:{new_name}")
                action = {"item_id": extra_id, "new_name": new_name}
                if _drive_items_same_content(canonical, extra) and review_parent_id:
                    action["action"] = "move_same_content_to_review"
                    if execute:
                        moved = move_drive_item(
                            service,
                            extra_id,
                            add_parent_id=review_parent_id,
                            remove_parent_ids=[folder_id],
                            new_name=new_name,
                        )
                        action["result"] = _drive_item_public(moved)
                    result["same_content_moved_to_review"] += 1
                else:
                    action["action"] = "rename_conflict_in_place"
                    if execute:
                        renamed = rename_drive_item(service, extra_id, new_name)
                        action["result"] = _drive_item_public(renamed)
                    result["items_renamed"] += 1
                existing_names.add(new_name)
                record["actions"].append(action)
        result["records"].append(record)
        if result["skipped_limit"]:
            break
    return result


def repair_drive_duplicate_case_folders(
    service: Any,
    drive_root_id: str,
    comparison: dict[str, Any],
    *,
    execute: bool = False,
    repair_limit: int = 0,
    quarantine_path: str = "MAGI待整理/Google Drive重複案件資料夾",
    max_depth: int = 8,
    max_items_per_group: int = 500,
    delete_resolved_duplicates: bool = False,
) -> dict[str, Any]:
    """Merge duplicate Drive case folders into one canonical folder.

    Non-canonical folders are resolved only after all non-conflicting children
    are moved into the canonical folder.  Same-name different-content files are
    renamed before moving.  By default the resolved source folder is moved to a
    MAGI review area.  When ``delete_resolved_duplicates`` is enabled, fully
    resolved source folders are moved to Google Drive trash instead; this is not
    a permanent delete and is skipped if the merge hit a traversal/item limit.
    """
    groups = _drive_duplicate_repair_groups(comparison)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_id = ""
    if execute:
        quarantine_root = ensure_drive_folder_path(service, drive_root_id, quarantine_path)
        batch = ensure_drive_folder_path(
            service,
            str(quarantine_root.get("drive_id") or ""),
            stamp,
        )
        batch_id = str(batch.get("drive_id") or "")
    summary = {
        "groups_total": len(groups),
        "groups_attempted": 0,
        "groups_repaired": 0,
        "source_folders_quarantined": 0,
        "source_folders_trashed": 0,
        "items_moved": 0,
        "renamed_conflicts": 0,
        "same_content_kept_in_quarantine": 0,
        "failed": 0,
        "stopped_by_limit": False,
        "execute": bool(execute),
        "delete_resolved_duplicates": bool(delete_resolved_duplicates),
    }
    records: list[dict[str, Any]] = []
    for group in groups:
        if repair_limit and summary["groups_attempted"] >= repair_limit:
            summary["stopped_by_limit"] = True
            break
        canonical = choose_drive_duplicate_canonical_case(group)
        cases = [c for c in (group.get("cases") or []) if isinstance(c, CaseFolder) and c.drive_id]
        sources = [c for c in cases if canonical and c.drive_id != canonical.drive_id]
        if not canonical or not sources:
            continue
        summary["groups_attempted"] += 1
        identity_key = str(group.get("identity_key") or "")
        safe_identity = re.sub(r"[^0-9A-Za-z._-]+", "_", identity_key).strip("_") or "unknown"
        group_bucket_id = ""
        if execute:
            group_bucket = ensure_drive_folder_path(
                service,
                batch_id,
                safe_identity,
            )
            group_bucket_id = str(group_bucket.get("drive_id") or "")
        else:
            group_bucket_id = f"dry-run:{safe_identity}"
        record = {
            "identity_key": identity_key,
            "identity_keys": group.get("identity_keys") or [],
            "canonical": _case_to_dict(canonical),
            "sources": [_case_to_dict(s) for s in sources],
            "execute": bool(execute),
            "source_results": [],
            "status": "planned" if not execute else "repaired",
            "error": "",
        }
        try:
            if execute:
                target_normalization = normalize_duplicate_drive_children(
                    service,
                    folder_id=str(canonical.drive_id),
                    review_parent_id=group_bucket_id,
                    execute=True,
                    max_depth=max_depth,
                    max_items=max_items_per_group,
                )
                record["target_duplicate_normalization"] = target_normalization
                if target_normalization.get("skipped_limit"):
                    raise DriveCaseSyncError("canonical_target_duplicate_normalization_hit_limit")
            for source in sources:
                source_result = {
                    "source": _case_to_dict(source),
                    "merge": {},
                    "quarantine": {},
                }
                merge = _merge_drive_folder_children(
                    service,
                    source_folder_id=str(source.drive_id),
                    target_folder_id=str(canonical.drive_id),
                    execute=execute,
                    max_depth=max_depth,
                    max_items=max_items_per_group,
                    counter={"items": 0},
                )
                source_result["merge"] = merge
                summary["items_moved"] += len(merge.get("moved") or [])
                summary["renamed_conflicts"] += len(merge.get("renamed_conflicts") or [])
                summary["same_content_kept_in_quarantine"] += len(merge.get("skipped_same_content") or [])
                merge_safe_to_delete = not bool(merge.get("skipped_limit"))
                source_meta = _drive_item_metadata(service, str(source.drive_id))
                source_parents = [str(x) for x in (source_meta.get("parents") or []) if str(x or "").strip()]
                if delete_resolved_duplicates and merge_safe_to_delete:
                    delete_record = {
                        "source_folder_id": source.drive_id,
                        "status": "planned_trash",
                        "reason": "merged_unique_children_and_only_resolved_duplicate_shell_remains",
                    }
                    if execute:
                        trashed_folder = trash_drive_item(service, str(source.drive_id))
                        delete_record["status"] = "trashed"
                        delete_record["result"] = _drive_item_public(trashed_folder)
                        summary["source_folders_trashed"] += 1
                    source_result["delete"] = delete_record
                else:
                    quarantine_name = f"{PurePosixPath(source.relative_path).name}（重複副本）"
                    quarantine_record = {
                        "source_folder_id": source.drive_id,
                        "target_parent_id": group_bucket_id,
                        "new_name": quarantine_name,
                        "status": "planned",
                    }
                    if delete_resolved_duplicates and not merge_safe_to_delete:
                        quarantine_record["reason"] = "merge_hit_limit_kept_for_review"
                    if execute:
                        moved_folder = move_drive_item(
                            service,
                            str(source.drive_id),
                            add_parent_id=group_bucket_id,
                            remove_parent_ids=source_parents,
                            new_name=quarantine_name,
                        )
                        quarantine_record["status"] = "quarantined"
                        quarantine_record["result"] = _drive_item_public(moved_folder)
                        summary["source_folders_quarantined"] += 1
                    source_result["quarantine"] = quarantine_record
                record["source_results"].append(source_result)
            summary["groups_repaired"] += 1 if execute else 0
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            summary["failed"] += 1
        records.append(record)
    return {
        "ok": summary["failed"] == 0,
        "mode": "drive_duplicate_case_folder_repair",
        "write_actions_enabled": bool(execute),
        "safety": (
            "merge_missing_children_then_trash_fully_resolved_duplicate_folders_no_permanent_delete_no_overwrite"
            if delete_resolved_duplicates
            else "merge_missing_children_then_move_duplicate_folders_to_review_area_no_delete_no_overwrite"
        ),
        "quarantine_path": quarantine_path,
        "quarantine_batch": stamp,
        "summary": summary,
        "records": records,
    }


def create_missing_drive_case_folders(
    service: Any,
    drive_root_id: str,
    comparison: dict[str, Any],
    *,
    create_limit: int = 0,
    max_age_hours: int = 0,
    owner_bucket: str | None = None,
) -> dict[str, Any]:
    """Create Google Drive case folders for NAS-only OSC folders.

    This only creates the case folder path. It does not upload files, overwrite
    content, delete content, or mutate NAS paths.
    """
    summary = {
        "attempted": 0,
        "created_or_existing": 0,
        "created_folders": 0,
        "skipped": 0,
        "failed": 0,
        "stopped_by_limit": False,
    }
    records: list[dict[str, Any]] = []
    for case in comparison.get("local_only") or []:
        if create_limit and summary["attempted"] >= create_limit:
            summary["stopped_by_limit"] = True
            break
        if not case_modified_within_hours(case, max_age_hours):
            summary["skipped"] += 1
            records.append({
                "status": "skipped",
                "reason": "outside_new_case_window",
                "case": _case_to_dict(case),
            })
            continue
        relative_path = drive_relative_path_for_local_case(case, owner_bucket=owner_bucket)
        if not relative_path:
            summary["skipped"] += 1
            records.append({
                "status": "skipped",
                "reason": "cannot_build_drive_case_path",
                "case": _case_to_dict(case),
            })
            continue
        summary["attempted"] += 1
        record = {
            "case_number": case.meta.case_number,
            "case_name": case.name,
            "local_path": case.local_path or case.path,
            "drive_relative_path": relative_path,
            "status": "",
            "created_folders": [],
            "error": "",
        }
        try:
            result = ensure_drive_case_folder_for_local_case(
                service,
                drive_root_id,
                case,
                owner_bucket=owner_bucket,
            )
            if not result.get("ok"):
                record["status"] = "skipped"
                record["reason"] = result.get("reason") or "drive_case_folder_not_resolved"
                summary["skipped"] += 1
                records.append(record)
                continue
            record["status"] = "created_or_existing"
            record["drive_id"] = result.get("drive_id", "")
            record["created_folders"] = result.get("created_folders", [])
            summary["created_or_existing"] += 1
            summary["created_folders"] += int(result.get("created_count") or 0)
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            summary["failed"] += 1
        records.append(record)
    return {
        "ok": summary["failed"] == 0,
        "mode": "ensure_google_drive_case_folders",
        "write_actions_enabled": True,
        "safety": "create_folders_only_no_file_write_no_delete",
        "summary": summary,
        "records": records,
    }


def build_report(
    *,
    drive_root: dict[str, Any],
    drive_entries: list[FileEntry],
    drive_cases: list[CaseFolder],
    local_entries: list[FileEntry],
    local_cases: list[CaseFolder],
    local_roots: list[Path],
    comparison: dict[str, Any] | None = None,
    file_sync_plan: dict[str, Any] | None = None,
    execution_result: dict[str, Any] | None = None,
    drive_folder_result: dict[str, Any] | None = None,
    duplicate_repair_result: dict[str, Any] | None = None,
    local_duplicate_repair_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    comparison = comparison or compare_case_folders(drive_cases, local_cases)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_inventory",
        "write_actions_enabled": report_write_actions_enabled(
            file_sync_plan,
            execution_result,
            drive_folder_result,
            duplicate_repair_result,
            local_duplicate_repair_result,
        ),
        "drive_root": {
            "id": drive_root.get("id"),
            "name": drive_root.get("name"),
            "web_url": drive_root.get("webViewLink"),
        },
        "local_roots": [str(p) for p in local_roots],
        "summary": {
            "drive_items": len(drive_entries),
            "drive_case_folders": len(drive_cases),
            "local_items": len(local_entries),
            "local_case_folders": len(local_cases),
            "matched_case_folders": len(comparison["matched"]),
            "drive_only_case_folders": len(comparison["drive_only"]),
            "local_only_case_folders": len(comparison["local_only"]),
            "ambiguous_case_folders": len(comparison["ambiguous"]),
            "out_of_scope_case_folders": len(comparison["out_of_scope"]),
            "drive_duplicate_groups": len(comparison.get("drive_duplicates") or []),
            "drive_duplicate_case_folders": sum(
                len(group.get("cases") or [])
                for group in comparison.get("drive_duplicates") or []
            ),
            "drive_potential_duplicate_groups": len(comparison.get("drive_potential_duplicates") or []),
            "drive_potential_duplicate_case_folders": sum(
                len(group.get("cases") or [])
                for group in comparison.get("drive_potential_duplicates") or []
            ),
            "drive_many_to_one_groups": len(comparison.get("drive_many_to_one") or []),
            "drive_many_to_one_case_folders": sum(
                len(group.get("drives") or [])
                for group in comparison.get("drive_many_to_one") or []
            ),
        },
        "matched": [
            {
                "drive": _case_to_dict(item["drive"]),
                "local": _case_to_dict(item["local"]),
                "match_keys": item["match_keys"],
                "context_resolution": item.get("context_resolution", {}),
            }
            for item in comparison["matched"]
        ],
        "drive_only": [_case_to_dict(c) for c in comparison["drive_only"]],
        "local_only": [_case_to_dict(c) for c in comparison["local_only"]],
        "out_of_scope": [
            {
                "drive": _case_to_dict(item["drive"]) if item.get("drive") else {},
                "local": _case_to_dict(item["local"]) if item.get("local") else {},
                "reason": item["reason"],
                "candidates": [_case_to_dict(c) for c in item.get("candidates", [])],
            }
            for item in comparison["out_of_scope"]
        ],
        "ambiguous": [
            {
                "drive": _case_to_dict(item["drive"]),
                "candidates": [_case_to_dict(c) for c in item["candidates"]],
                "match_keys": item["match_keys"],
                "context_resolution": item.get("context_resolution", {}),
            }
            for item in comparison["ambiguous"]
        ],
        "drive_duplicates": [
            _drive_duplicate_public_group(group)
            for group in comparison.get("drive_duplicates", [])
        ],
        "drive_potential_duplicates": [
            _drive_duplicate_public_group(group)
            for group in comparison.get("drive_potential_duplicates", [])
        ],
        "drive_many_to_one": [
            {
                "local": _case_to_dict(group["local"]),
                "drives": [_case_to_dict(case) for case in group.get("drives") or []],
                "reason": group.get("reason", ""),
            }
            for group in comparison.get("drive_many_to_one", [])
            if group.get("local")
        ],
        "sync_plan": build_sync_plan(comparison),
        "file_sync_plan": file_sync_plan or {},
        "execution_result": execution_result or {},
        "drive_folder_result": drive_folder_result or {},
        "duplicate_repair_result": duplicate_repair_result or {},
        "local_duplicate_repair_result": local_duplicate_repair_result or {},
    }


def write_report_files(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"drive_case_sync_report_{stamp}.json"
    md_path = output_dir / f"drive_case_sync_report_{stamp}.md"
    csv_path = output_dir / f"drive_case_sync_cases_{stamp}.csv"
    file_diff_csv_path = output_dir / f"drive_case_sync_file_diffs_{stamp}.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    rows: list[dict[str, Any]] = []
    for kind in ("drive_only", "local_only"):
        for case in report.get(kind, []):
            rows.append({
                "status": kind,
                "source": case.get("source"),
                "category": case.get("category"),
                "case_kind": case.get("case_kind"),
                "case_name": case.get("name"),
                "relative_path": case.get("relative_path"),
                "case_number": (case.get("meta") or {}).get("case_number"),
                "laf_case_no": (case.get("meta") or {}).get("laf_case_no"),
                "client_hint": (case.get("meta") or {}).get("client_hint"),
                "suggested_canonical_path": case.get("suggested_canonical_path", ""),
                "suggested_path_confidence": case.get("suggested_path_confidence", ""),
                "note": case.get("suggested_path_note", ""),
            })
    for item in report.get("out_of_scope", []):
        case = item.get("drive") or item.get("local") or {}
        rows.append({
            "status": "out_of_scope",
            "source": case.get("source"),
            "category": case.get("category"),
            "case_kind": case.get("case_kind"),
            "case_name": case.get("name"),
            "relative_path": case.get("relative_path"),
            "case_number": (case.get("meta") or {}).get("case_number"),
            "laf_case_no": (case.get("meta") or {}).get("laf_case_no"),
            "client_hint": (case.get("meta") or {}).get("client_hint"),
            "suggested_canonical_path": "",
            "suggested_path_confidence": "out_of_scope",
            "note": item.get("reason", ""),
        })
    for group in report.get("drive_duplicates", []):
        for case in group.get("cases") or []:
            rows.append({
                "status": "drive_duplicate",
                "source": case.get("source"),
                "category": case.get("category"),
                "case_kind": case.get("case_kind"),
                "case_name": case.get("name"),
                "relative_path": case.get("relative_path"),
                "case_number": (case.get("meta") or {}).get("case_number"),
                "laf_case_no": (case.get("meta") or {}).get("laf_case_no"),
                "client_hint": (case.get("meta") or {}).get("client_hint"),
                "suggested_canonical_path": "",
                "suggested_path_confidence": "blocked_duplicate",
                "note": f"{group.get('identity_key', '')}：{group.get('reason', '')}",
            })
    for group in report.get("drive_potential_duplicates", []):
        for case in group.get("cases") or []:
            rows.append({
                "status": "drive_potential_duplicate",
                "source": case.get("source"),
                "category": case.get("category"),
                "case_kind": case.get("case_kind"),
                "case_name": case.get("name"),
                "relative_path": case.get("relative_path"),
                "case_number": (case.get("meta") or {}).get("case_number"),
                "laf_case_no": (case.get("meta") or {}).get("laf_case_no"),
                "client_hint": (case.get("meta") or {}).get("client_hint"),
                "suggested_canonical_path": "",
                "suggested_path_confidence": "review_only",
                "note": f"{group.get('identity_key', '')}：{group.get('reason', '')}",
            })
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "status", "source", "category", "case_kind", "case_name", "relative_path",
            "case_number", "laf_case_no", "client_hint", "suggested_canonical_path",
            "suggested_path_confidence", "note",
        ])
        writer.writeheader()
        writer.writerows(rows)

    file_diff_rows: list[dict[str, Any]] = []
    for case_plan in (report.get("file_sync_plan") or {}).get("cases") or []:
        base = {
            "case_number": case_plan.get("case_number", ""),
            "drive_path": case_plan.get("drive_path", ""),
            "local_path": case_plan.get("local_path", ""),
        }
        for item in case_plan.get("download_missing") or []:
            file_diff_rows.append({
                **base,
                "diff_type": "drive_has_nas_missing",
                "relative_path": item.get("target_relative_path") or item.get("relative_path", ""),
                "drive_id": item.get("drive_id", ""),
                "drive_size": item.get("size", ""),
                "local_size": "",
                "reason": "Google Drive 有，NAS 缺少",
                "web_url": item.get("web_url", ""),
            })
        for item in case_plan.get("nas_only") or []:
            file_diff_rows.append({
                **base,
                "diff_type": "nas_has_drive_missing",
                "relative_path": item.get("target_relative_path") or item.get("relative_path", ""),
                "drive_id": "",
                "drive_size": "",
                "local_size": item.get("size", ""),
                "reason": "NAS 有，Google Drive 缺少",
                "web_url": "",
            })
        for item in case_plan.get("conflicts") or []:
            drive = item.get("drive") or {}
            local = item.get("local") or {}
            file_diff_rows.append({
                **base,
                "diff_type": "same_path_conflict",
                "relative_path": item.get("relative_path", ""),
                "drive_id": drive.get("drive_id", ""),
                "drive_size": drive.get("size", ""),
                "local_size": local.get("size", ""),
                "reason": item.get("reason", ""),
                "web_url": drive.get("web_url", ""),
            })
    if file_diff_rows:
        with file_diff_csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "case_number", "diff_type", "relative_path", "drive_path",
                "local_path", "drive_id", "drive_size", "local_size",
                "reason", "web_url",
            ])
            writer.writeheader()
            writer.writerows(file_diff_rows)

    s = report["summary"]
    lines = [
        "# MAGI 雲端/NAS 案件盤點報告",
        "",
        f"- 產生時間：{report['generated_at']}",
        f"- 模式：唯讀盤點，不會搬移、刪除、上傳或覆蓋檔案",
        f"- 雲端根目錄：{report['drive_root'].get('name')} ({report['drive_root'].get('id')})",
        f"- 雲端網址：{report['drive_root'].get('web_url')}",
        f"- 本機/NAS 根目錄：{', '.join(report.get('local_roots') or [])}",
        "",
        "## 摘要",
        "",
        f"- 雲端項目：{s['drive_items']}；雲端案件資料夾：{s['drive_case_folders']}",
        f"- NAS 項目：{s['local_items']}；NAS 案件資料夾：{s['local_case_folders']}",
        f"- 可比對案件：{s['matched_case_folders']}",
        f"- 雲端有、NAS 未明確找到：{s['drive_only_case_folders']}",
        f"- NAS 有、雲端未明確找到：{s['local_only_case_folders']}",
        f"- 需人工確認：{s['ambiguous_case_folders']}",
        f"- 不在同步範圍：{s.get('out_of_scope_case_folders', 0)}",
        f"- Google Drive 重複案件群組：{s.get('drive_duplicate_groups', 0)}（資料夾 {s.get('drive_duplicate_case_folders', 0)} 個）",
        f"- Google Drive 疑似重複案件群組：{s.get('drive_potential_duplicate_groups', 0)}（資料夾 {s.get('drive_potential_duplicate_case_folders', 0)} 個，僅列報不刪除）",
        f"- 同一 NAS 對到多個 Drive 案件群組：{s.get('drive_many_to_one_groups', 0)}（資料夾 {s.get('drive_many_to_one_case_folders', 0)} 個，整組阻斷同步）",
        "",
        "## 雲端有、NAS 未明確找到（前 80 筆）",
        "",
    ]
    for case in report.get("drive_only", [])[:80]:
        meta = case.get("meta") or {}
        lines.append(
            f"- `{case.get('relative_path')}` → 建議：`{case.get('suggested_canonical_path') or '需人工判斷'}` "
            f"（案號：{meta.get('case_number') or meta.get('laf_case_no') or '-'}；當事人：{meta.get('client_hint') or '-'}）"
        )
    lines.extend(["", "## NAS 有、雲端未明確找到（前 80 筆）", ""])
    for case in report.get("local_only", [])[:80]:
        meta = case.get("meta") or {}
        lines.append(
            f"- `{case.get('relative_path')}`（案號：{meta.get('case_number') or meta.get('laf_case_no') or '-'}；當事人：{meta.get('client_hint') or '-'}）"
        )
    if report.get("ambiguous"):
        lines.extend(["", "## 需人工確認（前 40 筆）", ""])
        for item in report.get("ambiguous", [])[:40]:
            drive = item.get("drive") or {}
            resolution = item.get("context_resolution") or {}
            reason = resolution.get("reason") or "找到多個候選案件"
            drive_terms = "、".join((resolution.get("drive_terms") or [])[:8])
            lines.append(
                f"- `{drive.get('relative_path')}` 候選 {len(item.get('candidates') or [])} 筆；"
                f"狀態：{resolution.get('status') or 'ambiguous'}；原因：{reason}"
            )
            if drive_terms:
                lines.append(f"  - 雲端線索：{drive_terms}")
            for score in (resolution.get("candidate_scores") or [])[:5]:
                matched_terms = "、".join(score.get("matched_terms") or []) or "-"
                lines.append(
                    f"  - 候選 `{score.get('relative_path')}`：分數 {score.get('score')}，命中 {matched_terms}"
                )
    if report.get("drive_duplicates"):
        lines.extend(["", "## Google Drive 重複案件資料夾（已阻斷同步）", ""])
        for group in report.get("drive_duplicates", [])[:80]:
            lines.append(f"- {group.get('identity_key')}: {group.get('reason')}")
            for case in (group.get("cases") or [])[:10]:
                lines.append(f"  - `{case.get('relative_path')}`（{case.get('drive_id') or '-'}）")
    if report.get("drive_potential_duplicates"):
        lines.extend(["", "## Google Drive 疑似重複案件資料夾（僅列報，不自動刪除）", ""])
        for group in report.get("drive_potential_duplicates", [])[:80]:
            lines.append(
                f"- {group.get('identity_key')}: {group.get('reason')} "
                f"（缺穩定編號 {group.get('weak_folder_count', 0)} 個）"
            )
            for case in (group.get("cases") or [])[:10]:
                meta = case.get("meta") or {}
                lines.append(
                    f"  - `{case.get('relative_path')}`"
                    f"（OSC：{meta.get('case_number') or '-'}；法扶：{meta.get('laf_case_no') or '-'}）"
                )
    if report.get("drive_many_to_one"):
        lines.extend(["", "## 同一 NAS 對到多個 Drive 案件資料夾（已阻斷同步）", ""])
        for group in report.get("drive_many_to_one", [])[:80]:
            local = group.get("local") or {}
            lines.append(f"- NAS `{local.get('relative_path')}`：{group.get('reason')}")
            for case in (group.get("drives") or [])[:10]:
                lines.append(f"  - Drive `{case.get('relative_path')}`（{case.get('drive_id') or '-'}）")
    if report.get("out_of_scope"):
        lines.extend(["", "## 不在同步範圍（前 80 筆）", ""])
        for item in report.get("out_of_scope", [])[:80]:
            case = item.get("drive") or item.get("local") or {}
            lines.append(f"- `{case.get('relative_path')}`：{item.get('reason')}")
    plan_summary = (report.get("sync_plan") or {}).get("summary") or {}
    lines.extend([
        "",
        "## 同步計畫（不會自動執行）",
        "",
        f"- 可進入檔案差異比對：{plan_summary.get('ready_for_file_diff', 0)}",
        f"- 需人工建立或指定：{plan_summary.get('needs_review', 0)}",
        f"- 同名多案需人工消歧義：{plan_summary.get('manual_disambiguation', 0)}",
        f"- 已排除同步範圍：{plan_summary.get('skipped', 0)}",
        f"- Google Drive 重複資料夾阻斷：{plan_summary.get('blocked_duplicate_drive_folders', 0)}",
        f"- Google Drive 疑似重複資料夾待確認：{plan_summary.get('potential_duplicate_drive_folders', 0)}",
        f"- 同一 NAS 對多個 Drive 群組阻斷：{plan_summary.get('blocked_drive_many_to_one_groups', 0)}",
    ])
    file_summary = (report.get("file_sync_plan") or {}).get("summary") or {}
    if file_summary:
        lines.extend([
            "",
            "## 逐檔差異（唯一匹配案件）",
            "",
            f"- 已掃描唯一匹配案件：{file_summary.get('matched_cases_scanned', 0)}",
            f"- 雲端有、NAS 缺少的檔案：{file_summary.get('drive_missing_in_nas_files', 0)}",
            f"- 預估下載位元組：{file_summary.get('drive_missing_in_nas_bytes', 0)}",
            f"- NAS 有、雲端缺少的檔案（目前僅列報，需寫入授權才可上傳）：{file_summary.get('nas_missing_in_drive_files', 0)}",
            f"- 同路徑內容不同或無法驗證，需人工確認：{file_summary.get('conflict_files', 0)}",
            f"- 其中雜湊不同：{file_summary.get('content_mismatch_files', 0)}",
            f"- 兩邊都有但 Google 文件匯出內容尚未逐字節驗證：{file_summary.get('unverified_existing_files', 0)}",
            f"- 已存在略過：{file_summary.get('skipped_existing_files', 0)}",
            f"- 同內容本機重複檔略過上傳：{file_summary.get('skipped_duplicate_content_uploads', 0)}",
            f"- 案件掃描錯誤：{file_summary.get('case_errors', 0)}",
        ])
        if file_diff_rows:
            lines.append(f"- 逐檔差異 CSV：`{file_diff_csv_path}`")
    folder_summary = (report.get("drive_folder_result") or {}).get("summary") or {}
    if folder_summary:
        lines.extend([
            "",
            "## 雲端案件資料夾建立",
            "",
            f"- 嘗試確認/建立：{folder_summary.get('attempted', 0)}",
            f"- 已存在或已建立：{folder_summary.get('created_or_existing', 0)}",
            f"- 本輪新增資料夾層級：{folder_summary.get('created_folders', 0)}",
            f"- 略過：{folder_summary.get('skipped', 0)}",
            f"- 失敗：{folder_summary.get('failed', 0)}",
        ])
    repair_summary = (report.get("duplicate_repair_result") or {}).get("summary") or {}
    if repair_summary:
        repair = report.get("duplicate_repair_result") or {}
        lines.extend([
            "",
            "## Google Drive 重複案件資料夾整理",
            "",
            f"- 模式：{'正式執行' if repair_summary.get('execute') else 'dry-run'}",
            f"- 重複群組總數：{repair_summary.get('groups_total', 0)}",
            f"- 本輪處理群組：{repair_summary.get('groups_attempted', 0)}",
            f"- 已修復群組：{repair_summary.get('groups_repaired', 0)}",
            f"- 副本資料夾移入待整理區：{repair_summary.get('source_folders_quarantined', 0)}",
            f"- 副本資料夾移到 Google Drive 垃圾桶：{repair_summary.get('source_folders_trashed', 0)}",
            f"- 搬入主資料夾項目：{repair_summary.get('items_moved', 0)}",
            f"- 同名不同內容改名保留：{repair_summary.get('renamed_conflicts', 0)}",
            f"- 同名同內容留在待整理副本：{repair_summary.get('same_content_kept_in_quarantine', 0)}",
            f"- 失敗：{repair_summary.get('failed', 0)}",
            f"- 待整理區：`{repair.get('quarantine_path')}/{repair.get('quarantine_batch')}`",
        ])
    local_repair_summary = (report.get("local_duplicate_repair_result") or {}).get("summary") or {}
    if local_repair_summary:
        local_repair = report.get("local_duplicate_repair_result") or {}
        lines.extend([
            "",
            "## NAS 本機同內容重複檔整理",
            "",
            f"- 模式：{'正式執行' if local_repair_summary.get('execute') else 'dry-run'}",
            f"- 已掃描案件：{local_repair_summary.get('cases_scanned', 0)}",
            f"- 有重複檔案件：{local_repair_summary.get('cases_with_duplicates', 0)}",
            f"- 重複群組：{local_repair_summary.get('groups', 0)}",
            f"- 規劃隔離檔案：{local_repair_summary.get('duplicates_planned', 0)}",
            f"- 已隔離檔案：{local_repair_summary.get('duplicates_quarantined', 0)}",
            f"- 隔離位元組：{local_repair_summary.get('duplicate_bytes', 0)}",
            f"- 失敗：{local_repair_summary.get('failed', 0)}",
            f"- 批次：`{local_repair.get('quarantine_batch') or '-'}`",
        ])
    exec_summary = (report.get("execution_result") or {}).get("summary") or {}
    if exec_summary:
        upload_result = (report.get("execution_result") or {}).get("upload_result") or {}
        download_result = (report.get("execution_result") or {}).get("download_result") or {}
        lines.extend([
            "",
            "## 本輪同步執行結果",
            "",
        ])
        if download_result:
            ds = download_result.get("summary") or {}
            lines.extend([
                f"- 下載嘗試：{ds.get('attempted', 0)}",
                f"- 已下載到 NAS：{ds.get('downloaded', 0)}",
                f"- 下載已存在略過：{ds.get('skipped_existing', 0)}",
                f"- 下載失敗：{ds.get('failed', 0)}",
                f"- 下載位元組：{ds.get('bytes', 0)}",
            ])
        elif "downloaded" in exec_summary:
            lines.extend([
                f"- 下載嘗試：{exec_summary.get('attempted', 0)}",
                f"- 已下載到 NAS：{exec_summary.get('downloaded', 0)}",
                f"- 下載已存在略過：{exec_summary.get('skipped_existing', 0)}",
                f"- 下載失敗：{exec_summary.get('failed', 0)}",
                f"- 下載位元組：{exec_summary.get('bytes', 0)}",
            ])
        if upload_result:
            us = upload_result.get("summary") or {}
            lines.extend([
                f"- 上傳嘗試：{us.get('attempted', 0)}",
                f"- 已上傳到 Google Drive：{us.get('uploaded', 0)}",
                f"- 上傳已存在略過：{us.get('skipped_existing', 0)}",
                f"- 上傳失敗：{us.get('failed', 0)}",
                f"- 上傳位元組：{us.get('bytes', 0)}",
                f"- 新增雲端資料夾：{us.get('folders_created', 0)}",
            ])
        elif "uploaded" in exec_summary:
            lines.extend([
                f"- 上傳嘗試：{exec_summary.get('attempted', 0)}",
                f"- 已上傳到 Google Drive：{exec_summary.get('uploaded', 0)}",
                f"- 上傳已存在略過：{exec_summary.get('skipped_existing', 0)}",
                f"- 上傳失敗：{exec_summary.get('failed', 0)}",
                f"- 上傳位元組：{exec_summary.get('bytes', 0)}",
                f"- 新增雲端資料夾：{exec_summary.get('folders_created', 0)}",
            ])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    paths = {"json": str(json_path), "markdown": str(md_path), "csv": str(csv_path)}
    if file_diff_rows:
        paths["file_diff_csv"] = str(file_diff_csv_path)
    return paths


def run_inventory(
    *,
    root_id: str = "",
    root_name: str = DEFAULT_DRIVE_ROOT_NAME,
    active_roots: list[Path] | None = None,
    closed_roots: list[Path] | None = None,
    max_depth: int = 7,
    max_items: int = 20000,
    output_dir: Path | None = None,
    interactive: bool = False,
    resolve_context: bool = True,
    file_diff: bool = False,
    execute_downloads: bool = False,
    execute_uploads: bool = False,
    download_limit: int = 0,
    max_download_bytes: int = 0,
    upload_limit: int = 0,
    max_upload_bytes: int = 0,
    max_case_depth: int = 20,
    max_case_items: int = 10000,
    matched_case_limit: int = 0,
    matched_case_offset: int = 0,
    priority_case_numbers: Iterable[str] | None = None,
    ensure_drive_case_folders: bool = False,
    create_drive_folder_limit: int = 0,
    create_drive_folder_max_age_hours: int = 0,
    drive_owner_bucket_name: str = "",
    drive_only: bool = False,
    repair_drive_duplicates: bool = False,
    execute_drive_duplicate_repair: bool = False,
    repair_drive_duplicate_limit: int = 0,
    repair_drive_duplicate_max_items: int = 500,
    repair_drive_duplicate_quarantine_path: str = "MAGI待整理/Google Drive重複案件資料夾",
    delete_resolved_drive_duplicates: bool = False,
    repair_local_duplicates: bool = False,
    execute_local_duplicate_repair: bool = False,
    repair_local_duplicate_limit: int = 0,
) -> dict[str, Any]:
    load_local_env()
    service = build_drive_service(
        interactive=interactive,
        write=execute_uploads or ensure_drive_case_folders or execute_drive_duplicate_repair,
    )
    drive_root = find_drive_root(service, root_id=root_id or os.environ.get("MAGI_DRIVE_SYNC_ROOT_FOLDER_ID", ""), root_name=root_name)
    drive_entries, drive_cases = drive_file_entries(service, drive_root["id"], max_depth=max_depth, max_items=max_items)
    inventory_truncated = bool(getattr(drive_entries, "truncated", False))

    active = [] if drive_only else (active_roots if active_roots is not None else default_active_case_roots())
    closed = [] if drive_only else (closed_roots if closed_roots is not None else default_closed_case_roots())
    local_entries: list[FileEntry] = []
    local_cases: list[CaseFolder] = []
    local_roots = active + closed
    for root in active:
        entries, cases = local_file_entries(root, status="active", max_depth=max_depth, max_items=max_items)
        inventory_truncated = inventory_truncated or bool(getattr(entries, "truncated", False))
        local_entries.extend(entries)
        local_cases.extend(cases)
    for root in closed:
        entries, cases = local_file_entries(root, status="closed", max_depth=max_depth, max_items=max_items)
        inventory_truncated = inventory_truncated or bool(getattr(entries, "truncated", False))
        local_entries.extend(entries)
        local_cases.extend(cases)

    if inventory_truncated and any(
        (
            execute_downloads,
            execute_uploads,
            ensure_drive_case_folders,
            execute_drive_duplicate_repair,
            execute_local_duplicate_repair,
        )
    ):
        raise DriveCaseSyncError("inventory_truncated_write_blocked")

    comparison = compare_case_folders(drive_cases, local_cases)
    if resolve_context:
        comparison = resolve_ambiguous_cases_with_context(
            comparison,
            drive_service=service,
        )
        comparison = resolve_drive_only_cases_with_context(
            comparison,
            drive_service=service,
        )
    comparison = enforce_reverse_unique_matches(comparison)

    file_sync_plan: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None
    if file_diff or execute_downloads or execute_uploads:
        file_sync_plan = build_file_sync_plan(
            comparison,
            service,
            max_case_depth=max_case_depth,
            max_case_items=max_case_items,
            matched_case_limit=matched_case_limit,
            matched_case_offset=matched_case_offset,
            priority_case_numbers=priority_case_numbers,
        )
    drive_folder_result: dict[str, Any] | None = None
    if ensure_drive_case_folders:
        drive_folder_result = create_missing_drive_case_folders(
            service,
            drive_root["id"],
            comparison,
            create_limit=create_drive_folder_limit,
            max_age_hours=create_drive_folder_max_age_hours,
            owner_bucket=drive_owner_bucket_name or drive_owner_bucket(),
        )
    duplicate_repair_result: dict[str, Any] | None = None
    if repair_drive_duplicates or execute_drive_duplicate_repair:
        duplicate_repair_result = repair_drive_duplicate_case_folders(
            service,
            str(drive_root.get("id") or ""),
            comparison,
            execute=execute_drive_duplicate_repair,
            repair_limit=repair_drive_duplicate_limit,
            quarantine_path=repair_drive_duplicate_quarantine_path,
            max_items_per_group=repair_drive_duplicate_max_items,
            delete_resolved_duplicates=delete_resolved_drive_duplicates,
        )
    local_duplicate_repair_result: dict[str, Any] | None = None
    if repair_local_duplicates or execute_local_duplicate_repair:
        local_duplicate_repair_result = repair_local_duplicate_content_for_cases(
            comparison,
            execute=execute_local_duplicate_repair,
            repair_limit=repair_local_duplicate_limit,
            max_case_depth=max_case_depth,
            max_case_items=max_case_items,
        )
    download_result: dict[str, Any] | None = None
    upload_result: dict[str, Any] | None = None
    if execute_downloads:
        download_result = execute_drive_to_nas_downloads(
            service,
            file_sync_plan or {},
            download_limit=download_limit,
            max_download_bytes=max_download_bytes,
        )
    if execute_uploads:
        upload_result = execute_nas_to_drive_uploads(
            service,
            file_sync_plan or {},
            upload_limit=upload_limit,
            max_upload_bytes=max_upload_bytes,
        )
    if download_result or upload_result:
        execution_result = combine_execution_results(
            download_result=download_result,
            upload_result=upload_result,
        )

    report = build_report(
        drive_root=drive_root,
        drive_entries=drive_entries,
        drive_cases=drive_cases,
        local_entries=local_entries,
        local_cases=local_cases,
        local_roots=local_roots,
        comparison=comparison,
        file_sync_plan=file_sync_plan,
        execution_result=execution_result,
        drive_folder_result=drive_folder_result,
        duplicate_repair_result=duplicate_repair_result,
        local_duplicate_repair_result=local_duplicate_repair_result,
    )
    paths = write_report_files(report, output_dir or runtime_dir())
    report["output_paths"] = paths
    return report


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    parser = argparse.ArgumentParser(description="MAGI Google Drive/NAS case inventory and conservative sync")
    parser.add_argument("--root-id", default="", help="Google Drive 案件辦理資料夾 ID；未指定則用名稱查找")
    parser.add_argument("--root-name", default=os.environ.get("MAGI_DRIVE_SYNC_ROOT_FOLDER_NAME", DEFAULT_DRIVE_ROOT_NAME))
    parser.add_argument("--active-root", action="append", default=[], help="NAS 進行中案件根目錄，可重複")
    parser.add_argument("--closed-root", action="append", default=[], help="NAS 結案案件根目錄，可重複")
    parser.add_argument("--max-depth", type=int, default=7)
    parser.add_argument("--max-items", type=int, default=20000)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--auth", action="store_true", help="必要時啟動 OAuth 授權")
    parser.add_argument("--auth-only", action="store_true", help="只建立/驗證 Google Drive OAuth token，不執行盤點或同步")
    parser.add_argument("--write-auth", action="store_true", help="搭配 --auth-only 建立寫入 scope token")
    parser.add_argument("--force-auth", action="store_true", help="搭配 --auth-only 強制重新建立 token")
    parser.add_argument("--no-context-resolve", action="store_true", help="不讀同名多案資料夾內容做深度消歧義")
    parser.add_argument("--file-diff", action="store_true", help="對唯一匹配案件做逐檔差異盤點")
    parser.add_argument("--execute-downloads", action="store_true", help="只把雲端有、NAS 缺少的檔案下載到 NAS；不覆蓋、不刪除")
    parser.add_argument("--execute-uploads", action="store_true", help="只把 NAS 有、雲端缺少的檔案上傳到 Google Drive；不覆蓋、不刪除")
    parser.add_argument("--download-limit", type=int, default=0, help="本輪最多下載幾個檔案，0 表示不限制")
    parser.add_argument("--max-download-bytes", type=int, default=0, help="本輪最多下載位元組，0 表示不限制")
    parser.add_argument("--upload-limit", type=int, default=0, help="本輪最多上傳幾個檔案，0 表示不限制")
    parser.add_argument("--max-upload-bytes", type=int, default=0, help="本輪最多上傳位元組，0 表示不限制")
    parser.add_argument("--max-case-depth", type=int, default=20, help="逐檔差異掃描單一案件的最大深度")
    parser.add_argument("--max-case-items", type=int, default=10000, help="逐檔差異掃描單一案件最多項目")
    parser.add_argument("--matched-case-limit", type=int, default=0, help="逐檔差異最多掃描幾個唯一匹配案件，0 表示不限制")
    parser.add_argument("--matched-case-offset", type=int, default=0, help="逐檔差異從第幾個唯一匹配案件開始，供背景批次輪轉使用")
    parser.add_argument("--ensure-drive-case-folders", action="store_true", help="為 NAS 已建立但雲端缺少的新案建立 Google Drive 對應資料夾")
    parser.add_argument("--create-drive-folder-limit", type=int, default=0, help="本輪最多確認/建立幾個雲端案件資料夾，0 表示不限制")
    parser.add_argument("--create-drive-folder-max-age-hours", type=int, default=0, help="只為最近幾小時異動的 NAS-only 案件建立雲端資料夾，0 表示不限")
    parser.add_argument("--drive-owner-bucket", default="", help="Google Drive 端 owner bucket，預設讀 MAGI_DRIVE_SYNC_OWNER_BUCKET 或 Lumi")
    parser.add_argument("--drive-only", action="store_true", help="只掃 Google Drive，不碰 NAS；用於授權、重複資料夾與雲端結構巡檢")
    parser.add_argument("--repair-drive-duplicates", action="store_true", help="產生 Google Drive 重複案件資料夾整理計畫；預設 dry-run")
    parser.add_argument("--execute-drive-duplicate-repair", action="store_true", help="正式整理 Google Drive 重複案件資料夾：合併缺檔後把副本移到待整理區")
    parser.add_argument("--delete-resolved-drive-duplicates", action="store_true", help="重複副本已合併且無待處理獨有檔案時，將副本移到 Google Drive 垃圾桶；需搭配 --execute-drive-duplicate-repair")
    parser.add_argument("--repair-drive-duplicate-limit", type=int, default=0, help="本輪最多整理幾組重複案件，0 表示不限制")
    parser.add_argument("--repair-drive-duplicate-max-items", type=int, default=500, help="每組最多搬移/檢查幾個雲端項目")
    parser.add_argument("--repair-drive-duplicate-quarantine-path", default="MAGI待整理/Google Drive重複案件資料夾", help="重複副本移入的 Google Drive 待整理區")
    parser.add_argument("--repair-local-duplicates", action="store_true", help="產生 NAS 案件內同內容重複檔整理計畫；預設 dry-run")
    parser.add_argument("--execute-local-duplicate-repair", action="store_true", help="正式整理 NAS 案件內同內容重複檔：把副本移入案件 .duplicates")
    parser.add_argument("--repair-local-duplicate-limit", type=int, default=0, help="本輪最多隔離幾個 NAS 同內容重複檔，0 表示不限制")
    parser.add_argument("--acquire-case-file-lock", action="store_true", help="直接執行寫入時取得共享案件檔案鎖；worker 已持鎖時不要使用")
    args = parser.parse_args(argv)

    if args.auth_only:
        write_scope = bool(args.write_auth or args.execute_uploads or args.ensure_drive_case_folders or args.execute_drive_duplicate_repair)
        creds = _load_google_credentials(
            interactive=True,
            force_auth=bool(args.force_auth),
            write=write_scope,
        )
        token_path = drive_sync_token_path(write=write_scope)
        print(json.dumps({
            "ok": True,
            "mode": "google_drive_auth_only",
            "token_path": str(token_path),
            "write_scope": write_scope,
            "expiry": getattr(creds, "expiry", None).isoformat() if getattr(creds, "expiry", None) else "",
            "scopes": sorted(getattr(creds, "scopes", []) or []),
        }, ensure_ascii=False, indent=2))
        return 0

    active = [Path(p).expanduser() for p in args.active_root] if args.active_root else None
    closed = [Path(p).expanduser() for p in args.closed_root] if args.closed_root else None
    case_lock: dict[str, Any] = {"acquired": True, "disabled": True}
    if args.acquire_case_file_lock:
        case_lock = acquire_case_file_operation_lock(owner="drive_case_sync_cli")
        if not case_lock.get("acquired"):
            print(json.dumps({
                "ok": False,
                "status": "case_file_operation_lock_busy",
                "lock": case_lock,
            }, ensure_ascii=False, indent=2))
            return 2
    try:
        report = run_inventory(
        root_id=args.root_id,
        root_name=args.root_name,
        active_roots=active,
        closed_roots=closed,
        max_depth=args.max_depth,
        max_items=args.max_items,
        output_dir=Path(args.output_dir).expanduser() if args.output_dir else None,
        interactive=args.auth,
        resolve_context=not args.no_context_resolve,
        file_diff=args.file_diff,
        execute_downloads=args.execute_downloads,
        execute_uploads=args.execute_uploads,
        download_limit=args.download_limit,
        max_download_bytes=args.max_download_bytes,
        upload_limit=args.upload_limit,
        max_upload_bytes=args.max_upload_bytes,
        max_case_depth=args.max_case_depth,
        max_case_items=args.max_case_items,
        matched_case_limit=args.matched_case_limit,
        matched_case_offset=args.matched_case_offset,
        ensure_drive_case_folders=args.ensure_drive_case_folders,
        create_drive_folder_limit=args.create_drive_folder_limit,
        create_drive_folder_max_age_hours=args.create_drive_folder_max_age_hours,
        drive_owner_bucket_name=args.drive_owner_bucket,
        drive_only=args.drive_only,
        repair_drive_duplicates=args.repair_drive_duplicates,
        execute_drive_duplicate_repair=args.execute_drive_duplicate_repair,
        repair_drive_duplicate_limit=args.repair_drive_duplicate_limit,
        repair_drive_duplicate_max_items=args.repair_drive_duplicate_max_items,
        repair_drive_duplicate_quarantine_path=args.repair_drive_duplicate_quarantine_path,
        delete_resolved_drive_duplicates=args.delete_resolved_drive_duplicates,
        repair_local_duplicates=args.repair_local_duplicates,
        execute_local_duplicate_repair=args.execute_local_duplicate_repair,
        repair_local_duplicate_limit=args.repair_local_duplicate_limit,
        )
    finally:
        if args.acquire_case_file_lock and case_lock.get("acquired"):
            release_case_file_operation_lock()
    has_partial_failures = report_has_partial_failures(report)
    print(json.dumps({
        "ok": not has_partial_failures,
        "mode": "conservative_drive_nas_sync",
        "summary": report["summary"],
        "sync_plan_summary": (report.get("sync_plan") or {}).get("summary", {}),
        "file_sync_summary": (report.get("file_sync_plan") or {}).get("summary", {}),
        "execution_summary": (report.get("execution_result") or {}).get("summary", {}),
        "drive_folder_summary": (report.get("drive_folder_result") or {}).get("summary", {}),
        "duplicate_repair_summary": (report.get("duplicate_repair_result") or {}).get("summary", {}),
        "local_duplicate_repair_summary": (report.get("local_duplicate_repair_result") or {}).get("summary", {}),
        "output_paths": report["output_paths"],
    }, ensure_ascii=False, indent=2))
    return 1 if has_partial_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
