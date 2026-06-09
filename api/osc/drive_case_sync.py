"""Google Drive/NAS case inventory and conservative sync for MAGI.

This module keeps OSC's NAS-first folder layout and the legacy Google Drive
layout separate.  Sync actions use a boundary mapping layer so each side keeps
its own naming rules; no action overwrites existing files or deletes content.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


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
    "@eaDir",
    "#recycle",
    ".SynologyWorkingDirectory",
    ".TemporaryItems",
    ".Trashes",
}
SYNC_IGNORE_PREFIXES = ("~$", "._")
DEFAULT_LOCAL_HASH_MAX_BYTES = 25_000_000
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
        token_path.unlink(missing_ok=True)
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        except Exception as exc:
            if not interactive and write:
                raise DriveCaseSyncAuthRequired(
                    f"Google Drive 授權檔無法讀取，請重新授權：{token_path}",
                    token_path=token_path,
                    write=write,
                ) from exc
            deferred_auth_error = exc
            if interactive:
                token_path.unlink(missing_ok=True)
            creds = None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            if not interactive and write:
                raise DriveCaseSyncAuthRequired(
                    f"Google Drive 授權已失效，請重新授權：{token_path}",
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
                fallback = Credentials.from_authorized_user_file(str(fallback_path), WRITE_SCOPES)
                if fallback.expired and fallback.refresh_token:
                    fallback.refresh(Request())
                if fallback and fallback.valid and fallback.has_scopes(WRITE_SCOPES):
                    return fallback
            except Exception as exc:
                deferred_auth_error = deferred_auth_error or exc
    if not creds or not creds.valid or not creds.has_scopes(scopes):
        if not interactive:
            if deferred_auth_error:
                raise DriveCaseSyncAuthRequired(
                    f"Google Drive 授權已失效，請重新授權：{token_path}",
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
        creds = flow.run_local_server(
            port=0,
            prompt="consent",
            login_hint=account_hint,
            authorization_prompt_message=(
                f"請用 {account_hint} 授權 MAGI {'上傳缺檔到' if write else '唯讀盤點'}雲端案件資料夾：{{url}}"
            ),
        )
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            token_path.chmod(0o600)
        except Exception:
            pass
    return creds


def build_drive_service(*, interactive: bool = False, force_auth: bool = False, write: bool = False):
    try:
        from googleapiclient.discovery import build
        import google_auth_httplib2
        import httplib2
    except Exception as exc:  # pragma: no cover - import depends on runtime extras
        raise DriveCaseSyncError(f"Google Drive API 套件未安裝：{exc}") from exc
    timeout = int(os.environ.get("MAGI_DRIVE_SYNC_HTTP_TIMEOUT") or "30")
    creds = _load_google_credentials(interactive=interactive, force_auth=force_auth, write=write)
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))
    return build("drive", "v3", http=http, cache_discovery=False)


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("臺", "台")
    text = re.sub(r"\s+", "", text)
    return text.lower()


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
    exclusions: set[str] = set()
    raw_sources: list[Any] = []
    env_raw = os.environ.get("MAGI_DRIVE_SYNC_CASE_EXCLUSIONS_JSON", "").strip()
    if env_raw:
        try:
            raw_sources.append(json.loads(env_raw))
        except Exception:
            pass
    exclusion_path = case_exclusion_file_path()
    if exclusion_path.exists():
        try:
            raw_sources.append(json.loads(exclusion_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    for raw in raw_sources:
        if isinstance(raw, list):
            values = raw
        elif isinstance(raw, dict):
            values = raw.get("relative_paths") or raw.get("paths") or raw.get("drive_paths") or []
        else:
            continue
        for value in values:
            normalized = normalize_text(value)
            if normalized:
                exclusions.add(normalized)
    return exclusions


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


def classify_local_case_folder(relative_path: str, *, status: str) -> dict[str, str] | None:
    parts = [p for p in Path(relative_path).parts if p not in {"", "."}]
    if len(parts) < 3:
        return None
    category = parts[0]
    if category not in {"一般案件", "法扶案件", "無償案件", "指定辯護案件"}:
        return None
    if len(parts) != 3:
        return None
    return {
        "category": category,
        "status": status,
        "owner_bucket": "",
        "case_kind": parts[1],
    }


def _ignore_name(name: str) -> bool:
    return name in SYNC_IGNORE_NAMES or any(name.startswith(p) for p in SYNC_IGNORE_PREFIXES)


def _first_existing_homes_case_root() -> Path:
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
    real_nas = Path("/Volumes/lumi/lumi/03_工作資料/10_結案")
    if real_nas.exists():
        return [real_nas]
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
        return (os.environ.get("MAGI_CANONICAL_CLOSED_CASE_PREFIX") or "Y:/lumi/03_工作資料/10_結案").replace("\\", "/").rstrip("/")
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
    entries: list[FileEntry] = []
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
            if len(entries) >= max_items:
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
    timeout = float(os.environ.get("MAGI_DRIVE_SYNC_DRIVE_LIST_TIMEOUT_SEC") or "20")
    result: dict[str, Any] = {"done": False, "value": None, "error": None}

    def _execute() -> None:
        try:
            result["value"] = request.execute()
        except Exception as exc:
            result["error"] = exc
        result["done"] = True

    worker = threading.Thread(target=_execute, daemon=True)
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
    entries: list[FileEntry] = []
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
            if len(entries) >= max_items:
                break
    return entries


def _safe_local_is_dir(path: str, *, timeout_sec: float | None = None) -> bool:
    timeout = float(
        timeout_sec
        if timeout_sec is not None
        else os.environ.get("MAGI_DRIVE_SYNC_LOCAL_SCAN_TIMEOUT_SEC") or "5"
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
        else os.environ.get("MAGI_DRIVE_SYNC_LOCAL_SCAN_TIMEOUT_SEC") or "5"
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
    entries: list[FileEntry] = []
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
            if len(entries) >= max_items:
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
    entries: list[FileEntry] = []
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
                    stack.append((str(item["id"]), rel, depth + 1))
            if len(entries) >= max_items:
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


def _db_row_to_local_case(row: dict[str, Any]) -> CaseFolder | None:
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


def db_local_cases_for_numbers(case_numbers: Iterable[str]) -> tuple[list[CaseFolder], list[dict[str, Any]]]:
    contexts = lookup_db_case_contexts(case_numbers)
    local_cases: list[CaseFolder] = []
    skipped: list[dict[str, Any]] = []
    for case_number in [str(x or "").strip() for x in case_numbers if str(x or "").strip()]:
        row = contexts.get(case_number)
        if not row:
            skipped.append({"case_number": case_number, "reason": "db_case_not_found"})
            continue
        case = _db_row_to_local_case(row)
        if not case:
            skipped.append({
                "case_number": case_number,
                "reason": "local_case_folder_not_accessible",
                "folder_path": str(row.get("folder_path") or ""),
            })
            continue
        local_cases.append(case)
    return local_cases, skipped


def _closed_status_text(value: str) -> bool:
    text = normalize_text(value)
    if "待報結" in text or "結案中" in text:
        return False
    return any(term in text for term in ("已結案", "已報結", "待送出", "已轉入"))


def _closed_canonical_path(value: str) -> bool:
    text = str(value or "").replace("\\", "/")
    return text.upper().startswith("Y:/") or "/10_結案/" in text or text.endswith("/10_結案")


def _db_context_marks_closed(db_context: dict[str, Any] | None) -> bool:
    if not db_context:
        return False
    status_closed = _closed_status_text(str(db_context.get("status") or ""))
    laf_closed = _closed_status_text(str(db_context.get("legal_aid_status") or ""))
    try:
        locked = int(db_context.get("manual_status_lock") or 0) == 1
    except Exception:
        locked = False
    path_closed = _closed_canonical_path(str(db_context.get("folder_path") or ""))
    return status_closed or laf_closed or (locked and path_closed)


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
    return comparison


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
    return comparison


def compare_case_folders(drive_cases: list[CaseFolder], local_cases: list[CaseFolder]) -> dict[str, Any]:
    drive_duplicate_groups = detect_drive_duplicate_case_groups(drive_cases)
    drive_potential_duplicate_groups = detect_drive_potential_duplicate_case_groups(
        drive_cases,
        confirmed_duplicate_groups=drive_duplicate_groups,
    )
    duplicate_identity_keys = {
        str(key or "")
        for group in drive_duplicate_groups
        for key in (group.get("identity_keys") or [group.get("identity_key")])
        if str(key or "")
    }
    duplicate_drive_refs: set[str] = {
        c.drive_id or c.relative_path
        for group in drive_duplicate_groups
        for c in group.get("cases", [])
    }
    active_drive_cases = [
        c for c in drive_cases
        if (c.drive_id or c.relative_path) not in duplicate_drive_refs
    ]
    strong_local = _index_cases(local_cases)
    weak_local = _index_cases(local_cases, include_name_only=True)
    db_contexts = lookup_db_case_contexts(
        c.meta.case_number for c in local_cases if c.meta.case_number
    )
    matched: list[dict[str, Any]] = []
    drive_only: list[CaseFolder] = []
    out_of_scope: list[dict[str, Any]] = []
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

    drive_strong = _index_cases(active_drive_cases)
    local_only: list[CaseFolder] = []
    for local in local_cases:
        if local.relative_path in matched_local_ids:
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
    }


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
    "10_判決書": "法院判決",
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
        "判決書",
        "起訴書",
        "地檢署起訴書",
        "調解不成立證明書",
    ),
    "回執": ("回執", "自行收納款項收據", "法院收據", "律師酬金收據"),
    "信件往返": ("信件", "信件往返"),
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
    "起訴書": "10_判決書",
    "地檢署起訴書": "10_判決書",
    "另案起訴書": "10_判決書",
    "法院判決": "10_判決書",
    "法院裁判": "10_判決書",
    "判決書": "10_判決書",
    "調解不成立證明書": "10_判決書",
    "回執": "11_回執",
    "自行收納款項收據": "11_回執",
    "法院收據": "11_回執",
    "律師酬金收據": "11_回執",
    "信件": "12_信件往返",
    "信件往返": "12_信件往返",
}
DRIVE_TO_NAS_PREFIXES = {
    ("閱卷資料", "筆錄"): ("08_筆錄",),
    ("法院資料", "法院判決"): ("10_判決書",),
    ("法院資料", "判決書"): ("10_判決書",),
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
    r"(?:裁定).{0,24}(?:駁回|准許|許可|認可|免責|不免責|復權|終結|開始更生|開始清算|廢棄|撤銷|移送|確定)"
)
PLEADING_FILENAME_RE = re.compile(
    r"(?:書狀|(?<!證)狀|上訴理由|上訴|抗告|聲請|陳報|補正|答辯|準備|意見|更生方案)"
)
EVIDENCE_FILENAME_RE = re.compile(r"(?:證據|附件|照片|截圖|錄音|錄影|鑑定|診斷證明|病歷)")
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
    "10_判決書": "法院判決",
    "法院判決": "法院判決",
    "法院裁判": "法院判決",
    "法院裁定": "法院判決",
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
    ("法院資料", "判決書"): ("法院判決",),
    ("法院資料", "法院通知"): ("法院通知",),
    ("法院資料", "程序裁定"): ("法院通知",),
}


def split_relative_parts(value: str) -> list[str]:
    text = str(value or "").replace("\\", "/").strip("/")
    return [p for p in PurePosixPath(text).parts if p and p not in {"."}]


def looks_like_drive_case_folder_segment(segment: str) -> bool:
    text = str(segment or "").strip()
    if not text or text in DRIVE_TO_NAS_FIRST_SEGMENT or text in NAS_TO_DRIVE_FIRST_SEGMENT:
        return False
    if OSC_CASE_RE.search(text) or LAF_CASE_RE.search(text) or ROC_COURT_NO_RE.search(text):
        return True
    # Drive-side LAF folders often omit OSC case numbers but include
    # "client-lafNo-stage-reason"; the LAF number is enough to prove this is an
    # outer case folder, not a document category.
    return False


def strip_embedded_drive_case_folder(relative_path: str) -> str:
    parts = split_relative_parts(relative_path)
    if len(parts) <= 1:
        return PurePosixPath(*parts).as_posix() if parts else ""
    if looks_like_drive_case_folder_segment(parts[0]):
        return PurePosixPath(*parts[1:]).as_posix()
    return PurePosixPath(*parts).as_posix()


def infer_nas_folder_for_drive_root_file(filename: str) -> str:
    name = PurePosixPath(str(filename or "")).name
    if not name:
        return ""
    if COURT_PROCEDURAL_FORM_RE.search(name) or COURT_FINAL_DOC_RE.search(name) or "裁定" in name:
        return court_document_target_segment(name)
    if "筆錄" in name:
        return "08_筆錄"
    if PLEADING_FILENAME_RE.search(name) and not re.search(r"(?:對造|對方|被告|原告).{0,12}(?:書狀|答辯|陳報)", name):
        return "04_我方歷次書狀"
    if EVIDENCE_FILENAME_RE.search(name):
        return "07_證據資料"
    if "回執" in name or "收據" in name:
        return "11_回執"
    return ""


def closing_drive_folder_for_nas_path(parts: list[str]) -> str:
    filename = parts[-1] if parts else ""
    if any(term in filename for term in ("結案酬金", "結案審查", "變動審查")):
        return "結案酬金領款單"
    return "結案資料"


def drive_to_nas_relative_path(relative_path: str) -> str:
    parts = split_relative_parts(relative_path)
    if not parts:
        return ""
    stripped = strip_embedded_drive_case_folder(relative_path)
    if stripped and stripped != PurePosixPath(*parts).as_posix():
        return drive_to_nas_relative_path(stripped)
    if len(parts) == 1:
        inferred = infer_nas_folder_for_drive_root_file(parts[0])
        if inferred:
            return PurePosixPath(inferred, parts[0]).as_posix()
    for source, target in sorted(DRIVE_TO_NAS_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if tuple(parts[: len(source)]) == source:
            return PurePosixPath(*(list(target) + parts[len(source) :])).as_posix()
    if parts[0] in {"法院裁判", "法院裁定"} or tuple(parts[:2]) in {("法院資料", "法院裁判"), ("法院資料", "法院裁定")}:
        rest = parts[1:] if parts[0] in {"法院裁判", "法院裁定"} else parts[2:]
        probe = PurePosixPath(parts[0] if parts[0] in {"法院裁判", "法院裁定"} else parts[1], *rest).as_posix()
        target_first = court_document_target_segment(probe)
        return PurePosixPath(*([target_first] + rest)).as_posix()
    if parts[0] == "起訴書" or tuple(parts[:2]) == ("法院資料", "起訴書"):
        rest = parts[1:] if parts[0] == "起訴書" else parts[2:]
        probe = PurePosixPath("起訴書", *rest).as_posix()
        target_first = court_document_target_segment(probe)
        return PurePosixPath(*([target_first] + rest)).as_posix()
    parts[0] = DRIVE_TO_NAS_FIRST_SEGMENT.get(parts[0], parts[0])
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
        return "10_判決書"
    if "裁定" in text:
        return "10_判決書" if COURT_FINAL_RULING_RE.search(text) else "09_法院通知或程序裁定"
    return "09_法院通知或程序裁定"


def _existing_drive_first_segments(entries: Iterable[FileEntry]) -> set[str]:
    out: set[str] = set()
    for entry in entries or []:
        parts = split_relative_parts(entry.relative_path)
        if parts:
            out.add(parts[0])
    return out


def _prefer_existing_drive_alias(semantic_segment: str, existing_first_segments: set[str]) -> str:
    for alias in DRIVE_EXISTING_ALIAS_PRIORITY.get(semantic_segment, ()):
        if alias in existing_first_segments:
            return alias
    return ""


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
    for source, target in sorted(NAS_TO_DRIVE_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if tuple(parts[: len(source)]) == source:
            return PurePosixPath(*(list(target) + parts[len(source) :])).as_posix()
    if parts[0] == "03_結案資料":
        parts[0] = closing_drive_folder_for_nas_path(parts)
    else:
        parts[0] = NAS_TO_DRIVE_FIRST_SEGMENT.get(parts[0], parts[0])
    return PurePosixPath(*parts).as_posix()


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
    if parts[0] == "03_結案資料" and closing_drive_folder_for_nas_path(parts) == "結案酬金領款單":
        parts[0] = "結案酬金領款單"
    else:
        parts[0] = SEMANTIC_FIRST_SEGMENT.get(parts[0], parts[0])
    return PurePosixPath(*parts).as_posix()


def drive_to_nas_download_skip_reason(source_relative_path: str, target_relative_path: str) -> str:
    """Return a reason when a Drive file would be copied as a raw Drive folder.

    Google Drive case folders intentionally use different first-level buckets from
    OSC/NAS.  A file under an unknown Drive bucket must not be downloaded with
    that bucket name unchanged, because it creates mixed layouts such as
    `開庭通知/` next to `09_法院通知或程序裁定/`.  Unknown buckets are reported for
    rule review instead of silently polluting the case folder.
    """
    source_parts = split_relative_parts(source_relative_path)
    target_parts = split_relative_parts(target_relative_path)
    if len(source_parts) <= 1 or not target_parts:
        return ""
    source_first = source_parts[0]
    target_first = target_parts[0]
    if source_first.startswith("."):
        return "drive_admin_folder"
    if target_first != source_first:
        return ""
    if re.match(r"^\d{2}_", target_first):
        return ""
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


def _find_same_content_local_entry(drive_entry: FileEntry, local_entries: Iterable[FileEntry]) -> tuple[FileEntry | None, str]:
    """Find an already archived NAS file with the same name, size and hash.

    This is intentionally conservative: MAGI only treats cross-folder content as
    duplicate when Drive supplies an MD5 and the local file with the same visible
    filename has identical size and MD5.  It prevents duplicate downloads caused
    by Drive/NAS folder vocabulary differences without deleting or overwriting
    anything.
    """
    if not drive_entry.md5 or drive_entry.size is None:
        return None, ""
    drive_name_key = normalized_relative_file_key(PurePosixPath(export_relative_path(drive_entry)).name)
    max_hash_bytes = int(os.environ.get("MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES") or DEFAULT_LOCAL_HASH_MAX_BYTES)
    for local_entry in local_entries:
        if local_entry.is_folder or not local_entry.path or local_entry.size is None:
            continue
        if int(local_entry.size) != int(drive_entry.size):
            continue
        if normalized_relative_file_key(PurePosixPath(local_entry.relative_path).name) != drive_name_key:
            continue
        key = normalized_relative_file_key(semantic_relative_path(local_entry.relative_path))
        if max_hash_bytes > 0 and int(local_entry.size) > max_hash_bytes:
            # Large court files and OCR PDFs can block SMB while hashing.  Same
            # visible filename + identical size is strong enough to skip a
            # duplicate download, but the manifest marks it as unverified.
            return local_entry, key
        try:
            local_md5 = local_file_md5(local_entry.path)
        except Exception:
            continue
        if normalize_text(local_md5) == normalize_text(drive_entry.md5):
            return local_entry, key
    return None, ""


def _drive_entry_downloadable(entry: FileEntry) -> bool:
    if entry.is_folder:
        return False
    mime = str(entry.mime_type or "")
    if mime.startswith("application/vnd.google-apps.") and mime not in GOOGLE_EXPORT_MIME_MAP:
        return False
    return True


def find_drive_child_folder(service: Any, parent_id: str, name: str) -> str:
    for item in _drive_list_children(service, parent_id):
        if item.get("mimeType") == GOOGLE_FOLDER_MIME and str(item.get("name") or "") == name:
            return str(item.get("id") or "")
    return ""


def find_drive_child_file(service: Any, parent_id: str, name: str) -> str:
    for item in _drive_list_children(service, parent_id):
        if item.get("mimeType") != GOOGLE_FOLDER_MIME and str(item.get("name") or "") == name:
            return str(item.get("id") or "")
    return ""


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


def _score_drive_search_candidate(candidate: CaseFolder, local_case: CaseFolder) -> tuple[int, list[str]]:
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
    for item in _search_drive_folders_by_name_tokens(service, tokens):
        rel = _drive_folder_relative_path_to_root(service, str(item.get("id") or ""), drive_root_id)
        if not rel:
            continue
        drive_case = _drive_case_from_search_candidate(item, rel, local_case=case)
        score, matched_terms = _score_drive_search_candidate(drive_case, case)
        if score < min_score:
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
            "reason": "drive_case_folder_missing_after_broad_search",
            "searched_tokens": tokens,
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
    }


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
    return sorted(found.values(), key=lambda c: c.relative_path)


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
) -> dict[str, Any]:
    from googleapiclient.http import MediaFileUpload

    if not local_path.exists() or not local_path.is_file():
        raise DriveCaseSyncError(f"找不到可上傳檔案：{local_path}")
    parent_id, created_folders = ensure_drive_parent_folder(service, drive_case_folder_id, relative_path)
    name = PurePosixPath(str(relative_path).replace("\\", "/")).name
    if find_drive_child_file(service, parent_id, name):
        return {
            "status": "skipped_existing",
            "drive_id": "",
            "web_url": "",
            "bytes": 0,
            "created_folders": created_folders,
        }
    media = MediaFileUpload(str(local_path), resumable=True)
    created = _drive_execute_with_timeout(service.files().create(
        body={"name": name, "parents": [parent_id]},
        media_body=media,
        supportsAllDrives=True,
        fields="id,name,size,md5Checksum,webViewLink",
    ), context=f"upload_file:{parent_id}:{name}")
    return {
        "status": "uploaded",
        "drive_id": str(created.get("id") or ""),
        "web_url": str(created.get("webViewLink") or ""),
        "bytes": int(created["size"]) if str(created.get("size") or "").isdigit() else int(local_path.stat().st_size),
        "created_folders": created_folders,
        "md5": str(created.get("md5Checksum") or ""),
    }


def local_file_md5(path: str, *, chunk_size: int = 1024 * 1024, max_bytes: int | None = None) -> str:
    if max_bytes is None:
        max_bytes = int(os.environ.get("MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES") or DEFAULT_LOCAL_HASH_MAX_BYTES)
    if max_bytes > 0:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size > max_bytes:
            raise DriveCaseSyncError(f"local_hash_skipped_large_file:{size}>{max_bytes}:{path}")
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_file_sync_plan(
    comparison: dict[str, Any],
    drive_service: Any,
    *,
    max_case_depth: int = 20,
    max_case_items: int = 10000,
    matched_case_limit: int = 0,
    matched_case_offset: int = 0,
    priority_case_numbers: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a conservative per-file plan for uniquely matched cases.

    Drive and NAS can use different folder names for the same case document
    category.  The comparison uses semantic paths, while executable actions
    preserve the target side's native folder layout.
    """
    compare_md5 = os.environ.get("MAGI_DRIVE_SYNC_COMPARE_MD5", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
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
        "unverified_existing_files": 0,
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
            "conflicts": [],
            "skipped_existing": 0,
            "error": "",
        }
        matched_local_keys: set[str] = set()
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
            case_plan["error"] = f"{type(exc).__name__}: {exc}"
            summary["case_errors"] += 1
            cases.append(case_plan)
            continue

        local_files = {
            normalized_relative_file_key(semantic_relative_path(e.relative_path)): e
            for e in local_entries
            if not e.is_folder
        }
        drive_files = {
            normalized_relative_file_key(semantic_relative_path(export_relative_path(e))): e
            for e in drive_entries
            if _drive_entry_downloadable(e)
        }
        drive_existing_first_segments = _existing_drive_first_segments(drive_entries)
        for key, drive_entry in sorted(drive_files.items()):
            source_rel = export_relative_path(drive_entry)
            raw_target_rel = drive_to_nas_relative_path(source_rel)
            target_rel = nas_filesystem_relative_path(raw_target_rel)
            local_entry = local_files.get(key)
            local_key = key
            if not local_entry:
                target_key = normalized_relative_file_key(semantic_relative_path(target_rel))
                if target_key != key:
                    local_entry = local_files.get(target_key)
                    local_key = target_key
                if not local_entry:
                    duplicate_entry, duplicate_key = _find_same_content_local_entry(drive_entry, local_entries)
                    if duplicate_entry:
                        action = _entry_public_dict(drive_entry)
                        action["source_relative_path"] = source_rel
                        action["target_relative_path"] = target_rel
                        action["reason"] = "same_content_elsewhere"
                        action["local_duplicate"] = _entry_public_dict(duplicate_entry)
                        max_hash_bytes = int(os.environ.get("MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES") or DEFAULT_LOCAL_HASH_MAX_BYTES)
                        if (
                            max_hash_bytes > 0
                            and duplicate_entry.size is not None
                            and int(duplicate_entry.size) > max_hash_bytes
                        ):
                            action["hash_verification"] = "skipped_large_same_name_size"
                        case_plan["download_skipped"].append(action)
                        if duplicate_key:
                            matched_local_keys.add(duplicate_key)
                        case_plan["skipped_existing"] += 1
                        summary["skipped_existing_files"] += 1
                        summary["skipped_duplicate_content_downloads"] += 1
                        continue
                    action = _entry_public_dict(drive_entry)
                    action["source_relative_path"] = source_rel
                    action["target_relative_path"] = target_rel
                    skip_reason = drive_to_nas_download_skip_reason(source_rel, target_rel)
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
            elif compare_md5 and drive_entry.md5 and local_entry.path:
                try:
                    local_md5 = local_file_md5(local_entry.path)
                except Exception as exc:
                    case_plan["conflicts"].append({
                        "relative_path": target_rel,
                        "source_relative_path": source_rel,
                        "drive": _entry_public_dict(drive_entry),
                        "local": _entry_public_dict(local_entry),
                        "reason": f"local_hash_failed:{type(exc).__name__}",
                    })
                    summary["conflict_files"] += 1
                    continue
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
                case_plan["skipped_existing"] += 1
                summary["skipped_existing_files"] += 1
                summary["unverified_existing_files"] += 1
            else:
                case_plan["skipped_existing"] += 1
                summary["skipped_existing_files"] += 1
        for key, local_entry in sorted(local_files.items()):
            if key not in drive_files and key not in matched_local_keys:
                action = _entry_public_dict(local_entry)
                action["target_relative_path"] = nas_to_drive_relative_path(
                    local_entry.relative_path,
                    drive_existing_first_segments=drive_existing_first_segments,
                )
                case_plan["nas_only"].append(action)
                summary["nas_missing_in_drive_files"] += 1
        summary["matched_cases_scanned"] += 1
        cases.append(case_plan)
    return {
        "mode": "file_diff_dry_run",
        "write_actions_enabled": False,
        "direction": "bidirectional_missing_and_conflict_diff",
        "safety": "no_overwrite_no_delete_no_empty_folder_create",
        "summary": summary,
        "cases": cases,
    }


def _download_drive_entry(service: Any, entry: FileEntry, target_path: Path) -> dict[str, Any]:
    from googleapiclient.http import MediaIoBaseDownload

    if target_path.exists():
        return {"status": "skipped_existing", "target_path": str(target_path), "bytes": 0}
    target_path.parent.mkdir(parents=True, exist_ok=True)
    request = None
    export = GOOGLE_EXPORT_MIME_MAP.get(entry.mime_type)
    if export:
        request = service.files().export_media(fileId=entry.drive_id, mimeType=export[0])
    else:
        request = service.files().get_media(fileId=entry.drive_id, supportsAllDrives=True)
    # Keep the temporary filename short.  Court PDFs often have intentionally
    # long descriptive names; reusing the target filename as the temp prefix can
    # exceed SMB/APFS filename limits even though the final target name is valid.
    fd, tmp_name = tempfile.mkstemp(
        prefix=".magi-drive-sync-",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    bytes_written = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=1024 * 1024)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
            bytes_written = int(fh.tell())
        os.replace(tmp_name, target_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return {"status": "downloaded", "target_path": str(target_path), "bytes": bytes_written}


def execute_drive_to_nas_downloads(
    drive_service: Any,
    file_sync_plan: dict[str, Any],
    *,
    download_limit: int = 0,
    max_download_bytes: int = 0,
) -> dict[str, Any]:
    manifest: list[dict[str, Any]] = []
    summary = {
        "attempted": 0,
        "downloaded": 0,
        "skipped_existing": 0,
        "failed": 0,
        "bytes": 0,
        "stopped_by_limit": False,
        "stopped_by_bytes": False,
    }
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
            try:
                result = _download_drive_entry(drive_service, entry, target_path)
                record.update(result)
                if result["status"] == "downloaded":
                    summary["downloaded"] += 1
                    summary["bytes"] += int(result.get("bytes") or 0)
                elif result["status"] == "skipped_existing":
                    summary["skipped_existing"] += 1
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = f"{type(exc).__name__}: {exc}"
                summary["failed"] += 1
            manifest.append(record)
        if summary["stopped_by_limit"] or summary["stopped_by_bytes"]:
            break
    return {
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
) -> dict[str, Any]:
    manifest: list[dict[str, Any]] = []
    summary = {
        "attempted": 0,
        "uploaded": 0,
        "skipped_existing": 0,
        "failed": 0,
        "bytes": 0,
        "folders_created": 0,
        "stopped_by_limit": False,
        "stopped_by_bytes": False,
    }
    for case in file_sync_plan.get("cases") or []:
        drive_case_folder_id = str(case.get("drive_id") or "")
        if not drive_case_folder_id:
            continue
        for action in case.get("nas_only") or []:
            if upload_limit and summary["attempted"] >= upload_limit:
                summary["stopped_by_limit"] = True
                break
            size_hint = int(action.get("size") or 0)
            if max_upload_bytes and size_hint and summary["bytes"] + size_hint > max_upload_bytes:
                summary["stopped_by_bytes"] = True
                break
            local_path = Path(str(action.get("path") or ""))
            relative_path = str(action.get("target_relative_path") or action.get("relative_path") or "")
            summary["attempted"] += 1
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
            try:
                result = upload_local_file_to_drive(
                    drive_service,
                    local_path=local_path,
                    drive_case_folder_id=drive_case_folder_id,
                    relative_path=relative_path,
                )
                record.update(result)
                if result["status"] == "uploaded":
                    summary["uploaded"] += 1
                    summary["bytes"] += int(result.get("bytes") or 0)
                elif result["status"] == "skipped_existing":
                    summary["skipped_existing"] += 1
                summary["folders_created"] += len(result.get("created_folders") or [])
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = f"{type(exc).__name__}: {exc}"
                summary["failed"] += 1
            manifest.append(record)
        if summary["stopped_by_limit"] or summary["stopped_by_bytes"]:
            break
    return {
        "mode": "execute_nas_to_drive_missing_only",
        "write_actions_enabled": True,
        "safety": "no_overwrite_no_delete",
        "summary": summary,
        "manifest": manifest,
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
    return {
        "mode": "execute_bidirectional_missing_only",
        "write_actions_enabled": True,
        "safety": "no_overwrite_no_delete_conflicts_blocked",
        "summary": {
            "download_attempted": download_summary.get("attempted", 0),
            "downloaded": download_summary.get("downloaded", 0),
            "download_skipped_existing": download_summary.get("skipped_existing", 0),
            "download_failed": download_summary.get("failed", 0),
            "download_bytes": download_summary.get("bytes", 0),
            "upload_attempted": upload_summary.get("attempted", 0),
            "uploaded": upload_summary.get("uploaded", 0),
            "upload_skipped_existing": upload_summary.get("skipped_existing", 0),
            "upload_failed": upload_summary.get("failed", 0),
            "upload_bytes": upload_summary.get("bytes", 0),
            "upload_folders_created": upload_summary.get("folders_created", 0),
        },
        "download_result": download_result or {},
        "upload_result": upload_result or {},
    }


def _drive_case_from_local_case_result(local_case: CaseFolder, result: dict[str, Any]) -> CaseFolder | None:
    drive_id = str(result.get("drive_id") or "").strip()
    relative_path = str(result.get("relative_path") or "").strip()
    if not drive_id or not relative_path:
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

    load_local_env()
    write = execute_uploads or ensure_drive_case_folders
    service = build_drive_service(interactive=interactive, write=write)
    drive_root = find_drive_root(
        service,
        root_id=root_id or os.environ.get("MAGI_DRIVE_SYNC_ROOT_FOLDER_ID", ""),
        root_name=root_name,
    )
    owner_bucket = drive_owner_bucket_name or drive_owner_bucket()
    local_cases, skipped_db_cases = db_local_cases_for_numbers(clean_numbers)

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
            folder_result = {
                "ok": False,
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "case_number": local_case.meta.case_number,
                "case_name": local_case.name,
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
        file_sync_plan = build_file_sync_plan(
            comparison,
            service,
            max_case_depth=max_case_depth,
            max_case_items=max_case_items,
            matched_case_limit=0,
            matched_case_offset=0,
            priority_case_numbers=clean_numbers,
        )
        file_sync_plan["mode"] = "direct_db_case_file_diff"

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
    execution_result = None
    if download_result or upload_result:
        execution_result = combine_execution_results(
            download_result=download_result,
            upload_result=upload_result,
        )
    drive_folder_result = {
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
    return sorted(cases, key=lambda c: _drive_duplicate_canonical_score(c), reverse=True)[0]


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
    a_size = str(a.get("size") or "")
    b_size = str(b.get("size") or "")
    return bool(a_size and b_size and a_size == b_size)


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
    target_by_name = {str(item.get("name") or ""): item for item in target_children}
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
    groups = comparison.get("drive_duplicates") or []
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
            result = ensure_drive_folder_path(service, drive_root_id, relative_path)
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
) -> dict[str, Any]:
    comparison = comparison or compare_case_folders(drive_cases, local_cases)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_inventory",
        "write_actions_enabled": False,
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
        "sync_plan": build_sync_plan(comparison),
        "file_sync_plan": file_sync_plan or {},
        "execution_result": execution_result or {},
        "drive_folder_result": drive_folder_result or {},
        "duplicate_repair_result": duplicate_repair_result or {},
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
) -> dict[str, Any]:
    load_local_env()
    service = build_drive_service(
        interactive=interactive,
        write=execute_uploads or ensure_drive_case_folders or execute_drive_duplicate_repair,
    )
    drive_root = find_drive_root(service, root_id=root_id or os.environ.get("MAGI_DRIVE_SYNC_ROOT_FOLDER_ID", ""), root_name=root_name)
    drive_entries, drive_cases = drive_file_entries(service, drive_root["id"], max_depth=max_depth, max_items=max_items)

    active = [] if drive_only else (active_roots if active_roots is not None else default_active_case_roots())
    closed = [] if drive_only else (closed_roots if closed_roots is not None else default_closed_case_roots())
    local_entries: list[FileEntry] = []
    local_cases: list[CaseFolder] = []
    local_roots = active + closed
    for root in active:
        entries, cases = local_file_entries(root, status="active", max_depth=max_depth, max_items=max_items)
        local_entries.extend(entries)
        local_cases.extend(cases)
    for root in closed:
        entries, cases = local_file_entries(root, status="closed", max_depth=max_depth, max_items=max_items)
        local_entries.extend(entries)
        local_cases.extend(cases)

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
    )
    paths = write_report_files(report, output_dir or runtime_dir())
    report["output_paths"] = paths
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI Google Drive/NAS case inventory and conservative sync")
    parser.add_argument("--root-id", default="", help="Google Drive 案件辦理資料夾 ID；未指定則用名稱查找")
    parser.add_argument("--root-name", default=os.environ.get("MAGI_DRIVE_SYNC_ROOT_FOLDER_NAME", DEFAULT_DRIVE_ROOT_NAME))
    parser.add_argument("--active-root", action="append", default=[], help="NAS 進行中案件根目錄，可重複")
    parser.add_argument("--closed-root", action="append", default=[], help="NAS 結案案件根目錄，可重複")
    parser.add_argument("--max-depth", type=int, default=7)
    parser.add_argument("--max-items", type=int, default=20000)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--auth", action="store_true", help="必要時啟動 OAuth 授權")
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
    args = parser.parse_args(argv)

    active = [Path(p).expanduser() for p in args.active_root] if args.active_root else None
    closed = [Path(p).expanduser() for p in args.closed_root] if args.closed_root else None
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
    )
    print(json.dumps({
        "ok": True,
        "mode": "conservative_drive_nas_sync",
        "summary": report["summary"],
        "sync_plan_summary": (report.get("sync_plan") or {}).get("summary", {}),
        "file_sync_summary": (report.get("file_sync_plan") or {}).get("summary", {}),
        "execution_summary": (report.get("execution_result") or {}).get("summary", {}),
        "drive_folder_summary": (report.get("drive_folder_result") or {}).get("summary", {}),
        "duplicate_repair_summary": (report.get("duplicate_repair_result") or {}).get("summary", {}),
        "output_paths": report["output_paths"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
