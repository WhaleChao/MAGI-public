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
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
SYNC_IGNORE_NAMES = {
    ".DS_Store",
    "@eaDir",
    "#recycle",
    ".SynologyWorkingDirectory",
    ".TemporaryItems",
    ".Trashes",
}
SYNC_IGNORE_PREFIXES = ("~$", "._")
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
    if force_auth and interactive:
        token_path.unlink(missing_ok=True)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid or not creds.has_scopes(scopes):
        if not interactive:
            scope_text = "Google Drive 寫入" if write else "Google Drive 唯讀"
            raise DriveCaseSyncError(
                f"尚未授權 {scope_text}。請先以 --auth 建立 "
                f"{'MAGI_DRIVE_SYNC_WRITE_TOKEN' if write else 'MAGI_DRIVE_SYNC_TOKEN'}。"
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


def default_active_case_roots() -> list[Path]:
    env = os.environ.get("MAGI_DRIVE_SYNC_ACTIVE_CASE_ROOT") or os.environ.get("MAGI_ACTIVE_CASE_ROOT")
    if env:
        p = Path(env).expanduser()
        return [p] if p.exists() else []
    real_nas = Path("/Volumes/homes/lumi63181107/01_案件")
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


def canonical_base_for_status(status: str) -> str:
    if status == "closed":
        return (os.environ.get("MAGI_CANONICAL_CLOSED_CASE_PREFIX") or "Y:/lumi/03_工作資料/10_結案").replace("\\", "/").rstrip("/")
    return (os.environ.get("MAGI_CANONICAL_ACTIVE_CASE_PREFIX") or "Z:/lumi63181107/01_案件").replace("\\", "/").rstrip("/")


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
        "webViewLink,driveId,shortcutDetails)"
    )
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            corpora="allDrives",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=1000,
            pageToken=token,
            fields=fields,
        ).execute()
        out.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            return sorted(out, key=lambda x: str(x.get("name") or ""))


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


def local_descendant_context(
    root: str,
    *,
    max_depth: int = 3,
    max_items: int = 300,
) -> list[FileEntry]:
    entries: list[FileEntry] = []
    base = Path(root)
    if not base.exists():
        return entries
    stack: list[Path] = [base]
    while stack and len(entries) < max_items:
        cur = stack.pop()
        try:
            children = sorted(list(os.scandir(cur)), key=lambda e: e.name)
        except OSError:
            continue
        for child in children:
            if _ignore_name(child.name):
                continue
            try:
                rel = Path(child.path).relative_to(base).as_posix()
                depth = len(Path(rel).parts)
                st = child.stat()
                is_dir = child.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if depth > max_depth:
                continue
            entries.append(FileEntry(
                source="nas",
                path=child.path,
                relative_path=rel,
                name=child.name,
                is_folder=is_dir,
                modified_time=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                size=None if is_dir else int(st.st_size),
            ))
            if is_dir and depth < max_depth:
                stack.append(Path(child.path))
            if len(entries) >= max_items:
                break
    return entries


def find_drive_root(service: Any, *, root_id: str = "", root_name: str = DEFAULT_DRIVE_ROOT_NAME) -> dict[str, Any]:
    if root_id:
        return service.files().get(
            fileId=root_id,
            supportsAllDrives=True,
            fields="id,name,mimeType,parents,modifiedTime,webViewLink,driveId",
        ).execute()
    resp = service.files().list(
        q=f"name = '{root_name}' and mimeType = '{GOOGLE_FOLDER_MIME}' and trashed = false",
        spaces="drive",
        corpora="allDrives",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=10,
        fields="files(id,name,mimeType,parents,modifiedTime,webViewLink,driveId)",
    ).execute()
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
                    case = CaseFolder(
                        source="drive",
                        path=rel,
                        relative_path=rel,
                        name=name,
                        category=cls["category"],
                        status=cls["status"],
                        case_kind=cls["case_kind"],
                        owner_bucket=cls["owner_bucket"],
                        meta=extract_case_meta(name),
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
                   notes, folder_path, status
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
    strong_local = _index_cases(local_cases)
    weak_local = _index_cases(local_cases, include_name_only=True)
    matched: list[dict[str, Any]] = []
    drive_only: list[CaseFolder] = []
    out_of_scope: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    matched_local_ids: set[str] = set()

    for d in drive_cases:
        scope_reason = sync_scope_exclusion_reason(d)
        if scope_reason:
            out_of_scope.append({"drive": d, "reason": scope_reason})
            continue
        keys = match_keys(d.meta)
        candidates: dict[str, CaseFolder] = {}
        for key in keys:
            if key.startswith("name:"):
                continue
            for c in strong_local.get(key, []):
                candidates[c.relative_path] = c
        if not candidates and keys:
            for key in keys:
                for c in weak_local.get(key, []):
                    candidates[c.relative_path] = c
        if len(candidates) == 1:
            local = next(iter(candidates.values()))
            matched_local_ids.add(local.relative_path)
            matched.append({"drive": d, "local": local, "match_keys": keys})
        elif len(candidates) > 1:
            ambiguous.append({"drive": d, "candidates": list(candidates.values()), "match_keys": keys})
        else:
            if is_aaron_drive_bucket(d):
                out_of_scope.append({
                    "drive": d,
                    "reason": "Aaron 雲端資料夾沒有 NAS 唯一對應；依規則不建立 NAS 資料夾、不進同步佇列",
                })
            else:
                drive_only.append(d)

    drive_strong = _index_cases(drive_cases)
    local_only: list[CaseFolder] = []
    for local in local_cases:
        if local.relative_path in matched_local_ids:
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
        drive = item.get("drive")
        if not drive:
            continue
        actions.append({
            "action": "skip",
            "safety": "out_of_scope",
            "drive_path": drive.relative_path,
            "drive_id": drive.drive_id,
            "reason": item.get("reason", ""),
            "status": "skipped",
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
DRIVE_TO_NAS_FIRST_SEGMENT = {
    "法扶資料": "01_法扶資料",
    "開辦資料": "02_開辦資料",
    "結案資料": "03_結案資料",
    "結案酬金領款單": "03_結案資料",
    "我方書狀": "04_我方歷次書狀",
    "我方歷次書狀": "04_我方歷次書狀",
    "對造書狀": "05_對方歷次書狀",
    "對方歷次書狀": "05_對方歷次書狀",
    "閱卷資料": "06_閱卷資料",
    "證據資料": "07_證據資料",
    "筆錄": "08_筆錄",
    "法院通知": "09_法院通知或程序裁定",
    "法院通知與程序裁定": "09_法院通知或程序裁定",
    "程序裁定": "09_法院通知或程序裁定",
    "法院判決": "10_判決書",
    "判決書": "10_判決書",
    "回執": "11_回執",
    "信件往返": "12_信件往返",
}
DRIVE_TO_NAS_PREFIXES = {
    ("閱卷資料", "筆錄"): ("08_筆錄",),
}
SEMANTIC_FIRST_SEGMENT = {
    "01_法扶資料": "法扶資料",
    "法扶資料": "法扶資料",
    "02_開辦資料": "開辦資料",
    "開辦資料": "開辦資料",
    "03_結案資料": "結案資料",
    "結案資料": "結案資料",
    "結案酬金領款單": "結案酬金領款單",
    "04_我方歷次書狀": "我方書狀",
    "我方書狀": "我方書狀",
    "我方歷次書狀": "我方書狀",
    "05_對方歷次書狀": "對造書狀",
    "對造書狀": "對造書狀",
    "對方歷次書狀": "對造書狀",
    "06_閱卷資料": "閱卷資料",
    "閱卷資料": "閱卷資料",
    "07_證據資料": "證據資料",
    "證據資料": "證據資料",
    "08_筆錄": "筆錄",
    "筆錄": "筆錄",
    "09_法院通知或程序裁定": "法院通知",
    "法院通知": "法院通知",
    "法院通知與程序裁定": "法院通知",
    "程序裁定": "法院通知",
    "10_判決書": "法院判決",
    "法院判決": "法院判決",
    "判決書": "法院判決",
    "11_回執": "回執",
    "回執": "回執",
    "12_信件往返": "信件往返",
    "信件往返": "信件往返",
}
SEMANTIC_PREFIXES = {
    ("08_筆錄",): ("筆錄",),
    ("閱卷資料", "筆錄"): ("筆錄",),
}


def split_relative_parts(value: str) -> list[str]:
    text = str(value or "").replace("\\", "/").strip("/")
    return [p for p in PurePosixPath(text).parts if p and p not in {"."}]


def closing_drive_folder_for_nas_path(parts: list[str]) -> str:
    filename = parts[-1] if parts else ""
    if any(term in filename for term in ("結案酬金", "結案審查", "變動審查")):
        return "結案酬金領款單"
    return "結案資料"


def drive_to_nas_relative_path(relative_path: str) -> str:
    parts = split_relative_parts(relative_path)
    if not parts:
        return ""
    for source, target in sorted(DRIVE_TO_NAS_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if tuple(parts[: len(source)]) == source:
            return PurePosixPath(*(list(target) + parts[len(source) :])).as_posix()
    parts[0] = DRIVE_TO_NAS_FIRST_SEGMENT.get(parts[0], parts[0])
    return PurePosixPath(*parts).as_posix()


def nas_to_drive_relative_path(relative_path: str) -> str:
    parts = split_relative_parts(relative_path)
    if not parts:
        return ""
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
    for source, target in sorted(SEMANTIC_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if tuple(parts[: len(source)]) == source:
            return PurePosixPath(*(list(target) + parts[len(source) :])).as_posix()
    if parts[0] == "03_結案資料" and closing_drive_folder_for_nas_path(parts) == "結案酬金領款單":
        parts[0] = "結案酬金領款單"
    else:
        parts[0] = SEMANTIC_FIRST_SEGMENT.get(parts[0], parts[0])
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


def create_drive_folder(service: Any, parent_id: str, name: str) -> str:
    body = {"name": name, "mimeType": GOOGLE_FOLDER_MIME, "parents": [parent_id]}
    created = service.files().create(
        body=body,
        supportsAllDrives=True,
        fields="id,name,webViewLink",
    ).execute()
    return str(created.get("id") or "")


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
    created = service.files().create(
        body={"name": name, "parents": [parent_id]},
        media_body=media,
        supportsAllDrives=True,
        fields="id,name,size,md5Checksum,webViewLink",
    ).execute()
    return {
        "status": "uploaded",
        "drive_id": str(created.get("id") or ""),
        "web_url": str(created.get("webViewLink") or ""),
        "bytes": int(created["size"]) if str(created.get("size") or "").isdigit() else int(local_path.stat().st_size),
        "created_folders": created_folders,
        "md5": str(created.get("md5Checksum") or ""),
    }


def local_file_md5(path: str, *, chunk_size: int = 1024 * 1024) -> str:
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
        "unverified_existing_files": 0,
        "case_errors": 0,
    }
    for item in comparison.get("matched", []):
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
            "nas_only": [],
            "conflicts": [],
            "skipped_existing": 0,
            "error": "",
        }
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
            if not e.is_folder
        }
        for key, drive_entry in sorted(drive_files.items()):
            source_rel = export_relative_path(drive_entry)
            target_rel = drive_to_nas_relative_path(source_rel)
            local_entry = local_files.get(key)
            if not local_entry:
                action = _entry_public_dict(drive_entry)
                action["source_relative_path"] = source_rel
                action["target_relative_path"] = target_rel
                action["target_path"] = str(safe_child_path(Path(local.local_path or local.path), target_rel))
                action["export_mime_type"] = (GOOGLE_EXPORT_MIME_MAP.get(drive_entry.mime_type) or [""])[0]
                case_plan["download_missing"].append(action)
                summary["drive_missing_in_nas_files"] += 1
                summary["drive_missing_in_nas_bytes"] += int(drive_entry.size or 0)
                continue
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
            if key not in drive_files:
                action = _entry_public_dict(local_entry)
                action["target_relative_path"] = nas_to_drive_relative_path(local_entry.relative_path)
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
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".magi-drive-sync-{target_path.name}-",
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
                "drive": _case_to_dict(item["drive"]),
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
        "sync_plan": build_sync_plan(comparison),
        "file_sync_plan": file_sync_plan or {},
        "execution_result": execution_result or {},
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
        case = item.get("drive") or {}
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
    if report.get("out_of_scope"):
        lines.extend(["", "## 不在同步範圍（前 80 筆）", ""])
        for item in report.get("out_of_scope", [])[:80]:
            drive = item.get("drive") or {}
            lines.append(f"- `{drive.get('relative_path')}`：{item.get('reason')}")
    plan_summary = (report.get("sync_plan") or {}).get("summary") or {}
    lines.extend([
        "",
        "## 同步計畫（不會自動執行）",
        "",
        f"- 可進入檔案差異比對：{plan_summary.get('ready_for_file_diff', 0)}",
        f"- 需人工建立或指定：{plan_summary.get('needs_review', 0)}",
        f"- 同名多案需人工消歧義：{plan_summary.get('manual_disambiguation', 0)}",
        f"- 已排除同步範圍：{plan_summary.get('skipped', 0)}",
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
) -> dict[str, Any]:
    load_local_env()
    service = build_drive_service(interactive=interactive, write=execute_uploads)
    drive_root = find_drive_root(service, root_id=root_id or os.environ.get("MAGI_DRIVE_SYNC_ROOT_FOLDER_ID", ""), root_name=root_name)
    drive_entries, drive_cases = drive_file_entries(service, drive_root["id"], max_depth=max_depth, max_items=max_items)

    active = active_roots if active_roots is not None else default_active_case_roots()
    closed = closed_roots if closed_roots is not None else default_closed_case_roots()
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
    )
    print(json.dumps({
        "ok": True,
        "mode": "read_only_inventory",
        "summary": report["summary"],
        "sync_plan_summary": (report.get("sync_plan") or {}).get("summary", {}),
        "file_sync_summary": (report.get("file_sync_plan") or {}).get("summary", {}),
        "execution_summary": (report.get("execution_result") or {}).get("summary", {}),
        "output_paths": report["output_paths"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
