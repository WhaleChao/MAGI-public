"""Read-only Google Drive/NAS case inventory for MAGI.

This module deliberately stops at inventory and diff planning.  It does not
create, move, delete, upload, or overwrite files.  The goal is to build a
stable bridge between the legacy Google Drive layout and OSC's NAS-first case
layout before any synchronisation action is allowed.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
READONLY_SCOPES = [DRIVE_READONLY_SCOPE, SHEETS_READONLY_SCOPE]
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"

DEFAULT_DRIVE_ROOT_NAME = "案件辦理"
DEFAULT_OWNER_BUCKETS = {"Aaron", "Lumi", "Aaron&Lumi", "Lumi-2"}
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


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_dir() -> Path:
    root = Path(os.environ.get("MAGI_RUNTIME_DIR") or repo_root() / ".runtime")
    return root / "drive_sync"


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


def drive_sync_token_path() -> Path:
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


def _load_google_credentials(*, interactive: bool = False, force_auth: bool = False):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception as exc:  # pragma: no cover - import depends on runtime extras
        raise DriveCaseSyncError(f"Google API 套件未安裝：{exc}") from exc

    token_path = drive_sync_token_path()
    credentials_path = drive_sync_credentials_path()
    account_hint = drive_sync_account_hint()
    creds = None
    if force_auth and interactive:
        token_path.unlink(missing_ok=True)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), READONLY_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid or not creds.has_scopes(READONLY_SCOPES):
        if not interactive:
            raise DriveCaseSyncError(
                "尚未授權 Google Drive 唯讀盤點。請先以已授權帳號建立 "
                "MAGI_DRIVE_SYNC_TOKEN，或暫用帳務 token。"
            )
        if not credentials_path.exists():
            raise DriveCaseSyncError(f"找不到 Google OAuth credentials：{credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), READONLY_SCOPES)
        creds = flow.run_local_server(
            port=0,
            prompt="consent",
            login_hint=account_hint,
            authorization_prompt_message=(
                f"請用 {account_hint} 授權 MAGI 唯讀盤點雲端案件資料夾：{{url}}"
            ),
        )
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            token_path.chmod(0o600)
        except Exception:
            pass
    return creds


def build_drive_service(*, interactive: bool = False, force_auth: bool = False):
    try:
        from googleapiclient.discovery import build
    except Exception as exc:  # pragma: no cover - import depends on runtime extras
        raise DriveCaseSyncError(f"Google Drive API 套件未安裝：{exc}") from exc
    return build("drive", "v3", credentials=_load_google_credentials(interactive=interactive, force_auth=force_auth), cache_discovery=False)


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


def _clean_folder_token(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\d+[.．、_ ]+", "", text)
    return text.strip(" -_－—")


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
    name = normalize_text(meta.client_hint)
    reason = normalize_text(meta.reason_hint)
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
        case_idx = 2
    elif top == "結案案件" and len(parts) >= 3:
        status = "closed"
        category = parts[1]
        if len(parts) >= 4 and parts[2] in DEFAULT_OWNER_BUCKETS:
            owner = parts[2]
            case_idx = 3
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


def compare_case_folders(drive_cases: list[CaseFolder], local_cases: list[CaseFolder]) -> dict[str, Any]:
    strong_local = _index_cases(local_cases)
    weak_local = _index_cases(local_cases, include_name_only=True)
    matched: list[dict[str, Any]] = []
    drive_only: list[CaseFolder] = []
    ambiguous: list[dict[str, Any]] = []
    matched_local_ids: set[str] = set()

    for d in drive_cases:
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
    }


def _case_to_dict(case: CaseFolder) -> dict[str, Any]:
    data = asdict(case)
    data["match_keys"] = match_keys(case.meta)
    return data


def build_report(
    *,
    drive_root: dict[str, Any],
    drive_entries: list[FileEntry],
    drive_cases: list[CaseFolder],
    local_entries: list[FileEntry],
    local_cases: list[CaseFolder],
    local_roots: list[Path],
) -> dict[str, Any]:
    comparison = compare_case_folders(drive_cases, local_cases)
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
        },
        "matched": [
            {
                "drive": _case_to_dict(item["drive"]),
                "local": _case_to_dict(item["local"]),
                "match_keys": item["match_keys"],
            }
            for item in comparison["matched"]
        ],
        "drive_only": [_case_to_dict(c) for c in comparison["drive_only"]],
        "local_only": [_case_to_dict(c) for c in comparison["local_only"]],
        "ambiguous": [
            {
                "drive": _case_to_dict(item["drive"]),
                "candidates": [_case_to_dict(c) for c in item["candidates"]],
                "match_keys": item["match_keys"],
            }
            for item in comparison["ambiguous"]
        ],
    }


def write_report_files(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"drive_case_sync_report_{stamp}.json"
    md_path = output_dir / f"drive_case_sync_report_{stamp}.md"
    csv_path = output_dir / f"drive_case_sync_cases_{stamp}.csv"
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
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "status", "source", "category", "case_kind", "case_name", "relative_path",
            "case_number", "laf_case_no", "client_hint", "suggested_canonical_path",
            "suggested_path_confidence", "note",
        ])
        writer.writeheader()
        writer.writerows(rows)

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
            lines.append(f"- `{drive.get('relative_path')}` 候選 {len(item.get('candidates') or [])} 筆")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path), "csv": str(csv_path)}


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
) -> dict[str, Any]:
    load_local_env()
    service = build_drive_service(interactive=interactive)
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

    report = build_report(
        drive_root=drive_root,
        drive_entries=drive_entries,
        drive_cases=drive_cases,
        local_entries=local_entries,
        local_cases=local_cases,
        local_roots=local_roots,
    )
    paths = write_report_files(report, output_dir or runtime_dir())
    report["output_paths"] = paths
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI Google Drive/NAS case inventory (read-only)")
    parser.add_argument("--root-id", default="", help="Google Drive 案件辦理資料夾 ID；未指定則用名稱查找")
    parser.add_argument("--root-name", default=os.environ.get("MAGI_DRIVE_SYNC_ROOT_FOLDER_NAME", DEFAULT_DRIVE_ROOT_NAME))
    parser.add_argument("--active-root", action="append", default=[], help="NAS 進行中案件根目錄，可重複")
    parser.add_argument("--closed-root", action="append", default=[], help="NAS 結案案件根目錄，可重複")
    parser.add_argument("--max-depth", type=int, default=7)
    parser.add_argument("--max-items", type=int, default=20000)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--auth", action="store_true", help="必要時啟動 OAuth 授權")
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
    )
    print(json.dumps({
        "ok": True,
        "mode": "read_only_inventory",
        "summary": report["summary"],
        "output_paths": report["output_paths"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
