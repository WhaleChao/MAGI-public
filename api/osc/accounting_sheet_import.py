"""Google Sheets accounting import for OSC/MAGI.

The importer is intentionally tolerant because the colleague-maintained sheet
may change column labels. It imports by month, skips rows marked as another
person's account, and keeps an import ledger so re-running a month is safe.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import re
import webbrowser
from dataclasses import asdict, dataclass
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SPREADSHEET_ID = os.environ.get("MAGI_ACCOUNTING_SHEET_ID", "").strip()
DEFAULT_GID = int(os.environ.get("MAGI_ACCOUNTING_SHEET_GID", "1995190935") or "1995190935")
DEFAULT_ACCOUNT_HINT = os.environ.get("MAGI_ACCOUNTING_GOOGLE_ACCOUNT_HINT", "primary").strip() or "primary"
SKIP_OWNER_MARKERS = ("俊儒",)
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_READ_SCOPES = [SHEETS_READONLY_SCOPE, DRIVE_READONLY_SCOPE]
API_ENABLE_URL_TEMPLATE = "https://console.developers.google.com/apis/api/{service}/overview?project={project}"


HEADER_ALIASES = {
    "date": {
        "日期",
        "日",
        "交易日",
        "時間",
        "入帳日",
        "支出日期",
        "收入日期",
        "付款日",
        "收款日",
        "date",
    },
    "amount": {"金額", "總額", "費用", "款項", "amount"},
    "income": {"收入", "收入金額", "收款", "入帳", "貸方"},
    "expense": {"支出", "支出金額", "付款", "費用支出", "借方"},
    "type": {"收支", "類型", "帳務類型", "收入支出", "type"},
    "category": {"分類", "項目", "科目", "帳務項目", "類別", "category"},
    "sub_type": {"細項", "子類型", "支付方式", "付款方式", "收款方式", "sub_type"},
    "description": {"說明", "摘要", "內容", "用途", "description"},
    "memo": {"備註", "memo", "remark"},
    "name": {"姓名", "當事人", "客戶", "client", "name"},
    "case_ref": {
        "案件",
        "案件編號",
        "案號",
        "OSC案號",
        "MAGI案號",
        "法院案號",
        "case",
        "case_id",
        "case_number",
    },
    "owner": {"標識", "標記", "歸屬", "負責人", "人員", "律師", "記帳人", "owner"},
}


@dataclass
class AccountingSheetRow:
    source_row: int
    date: str
    type: str
    amount: float
    category: str | None = None
    sub_type: str | None = None
    description: str | None = None
    case_ref: str | None = None
    owner: str | None = None
    fingerprint: str | None = None


class AccountingImportError(RuntimeError):
    pass


class SheetsAuthorizationRequired(AccountingImportError):
    pass


FIXED_EXPENSE_SKIP_CATEGORIES = {"薪資", "保險", "房租", "租金支出", "人事費"}
FIXED_EXPENSE_SKIP_KEYWORDS = (
    "主持律師薪資",
    "法務專員薪資",
    "薪資",
    "薪水",
    "勞工保險",
    "勞保",
    "全民健康保險",
    "健保",
    "勞工退休金",
    "勞退",
    "房租",
    "辦公室租金",
    "台北事務所",
    "臺北事務所",
    "花蓮事務所",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_token_path() -> Path:
    return Path(os.environ.get("MAGI_GOOGLE_SHEETS_TOKEN") or "~/.magi/google/sheets_token.json").expanduser()


def _default_credentials_path() -> Path:
    env = os.environ.get("MAGI_GOOGLE_CREDENTIALS_PATH")
    if env:
        return Path(env).expanduser()
    return _repo_root() / "json" / "credentials.json"


def _google_cloud_project(credentials_path: Path | None = None) -> str:
    path = credentials_path or _default_credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cfg = data.get("installed") or data.get("web") or {}
        return str(cfg.get("project_id") or "").strip()
    except Exception:
        return ""


def sheets_api_enable_url(credentials_path: Path | None = None) -> str:
    return google_api_enable_url("sheets.googleapis.com", credentials_path)


def google_api_enable_url(service: str, credentials_path: Path | None = None) -> str:
    project = _google_cloud_project(credentials_path) or "671795481149"
    return API_ENABLE_URL_TEMPLATE.format(service=service, project=project)


def month_window(month: str | None = None, today: date | None = None) -> tuple[date, date, str]:
    base = today or date.today()
    if not month:
        year, mon = base.year, base.month
    else:
        m = str(month).strip()
        if m.lower() in {"previous", "prev", "last"}:
            year, mon = base.year, base.month - 1
            if mon == 0:
                year -= 1
                mon = 12
        else:
            match = re.fullmatch(r"(\d{4})[-/](\d{1,2})", m)
            if not match:
                raise ValueError("month must be YYYY-MM")
            year, mon = int(match.group(1)), int(match.group(2))
    last_day = calendar.monthrange(year, mon)[1]
    return date(year, mon, 1), date(year, mon, last_day), f"{year:04d}-{mon:02d}"


def normalize_header(value: Any) -> str:
    s = str(value or "").strip().lower()
    s = re.sub(r"\s+", "", s)
    return s.replace("　", "")


def header_map(headers: Iterable[Any]) -> dict[str, int]:
    normalized_aliases = {
        field: {normalize_header(alias) for alias in aliases}
        for field, aliases in HEADER_ALIASES.items()
    }
    mapping: dict[str, int] = {}
    for idx, raw in enumerate(headers):
        key = normalize_header(raw)
        if not key:
            continue
        for field, aliases in normalized_aliases.items():
            if field not in mapping and key in aliases:
                mapping[field] = idx
                break
    return mapping


def _cell(row: list[Any], mapping: dict[str, int], key: str) -> str:
    idx = mapping.get(key)
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx] if row[idx] is not None else "").strip()


def _is_header_row(row: list[Any]) -> bool:
    mapping = header_map(row)
    return "date" in mapping and ("amount" in mapping or "income" in mapping or "expense" in mapping)


def _clean_category(value: str, fallback: str | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return fallback
    if text in {"總額", "小計", "合計"}:
        return None
    return text


def _case_ref_from_name(value: str) -> str | None:
    text = str(value or "").strip()
    match = re.search(r"\b\d{6,7}-[A-Z]-\d{3}\b", text)
    if match:
        return match.group(0)
    match = re.search(r"\b20\d{2}-\d{4}\b", text)
    if match:
        return match.group(0)
    return None


def _join_description(name: str, memo: str) -> str | None:
    name = str(name or "").strip()
    memo = str(memo or "").strip()
    if name and memo:
        return f"{name}｜{memo}"
    return name or memo or None


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("年", "/").replace("月", "/").replace("日", "")
    text = text.replace(".", "/").replace("-", "/")
    text = re.sub(r"\s+", "", text)
    m = re.fullmatch(r"(\d{2,4})/(\d{1,2})/(\d{1,2})", text)
    if m:
        year, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 1911:
            year += 1911
        try:
            return date(year, mon, day)
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text)
    if m:
        try:
            return date(date.today().year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except Exception:
        return None


def parse_amount(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(",", "").replace("元", "").replace("$", "").strip()
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.startswith("-"):
        negative = True
        text = text[1:]
    text = re.sub(r"[^\d.]", "", text)
    if not text:
        return None
    try:
        amount = float(text)
    except ValueError:
        return None
    return -amount if negative else amount


def _infer_type(type_text: str, amount: float) -> str:
    t = str(type_text or "").strip()
    if "支" in t or "付" in t or "費" in t:
        return "支出"
    if "收" in t or "入" in t:
        return "收入"
    return "支出" if amount < 0 else "收入"


def _fingerprint(row: AccountingSheetRow, spreadsheet_id: str, gid: int) -> str:
    payload = {
        "spreadsheet_id": spreadsheet_id,
        "gid": gid,
        "source_row": row.source_row,
        "date": row.date,
        "type": row.type,
        "amount": round(float(row.amount), 2),
        "category": row.category or "",
        "sub_type": row.sub_type or "",
        "description": row.description or "",
        "case_ref": row.case_ref or "",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_sheet_values(
    values: list[list[Any]],
    *,
    month: str | None = None,
    spreadsheet_id: str = DEFAULT_SPREADSHEET_ID,
    gid: int = DEFAULT_GID,
    source_label: str = "",
    allow_no_header: bool = False,
) -> tuple[list[AccountingSheetRow], dict[str, Any]]:
    start, end, month_key = month_window(month)
    if source_label and any(marker in source_label for marker in SKIP_OWNER_MARKERS):
        return [], {
            "month": month_key,
            "source_label": source_label,
            "parsed": 0,
            "skipped_owner": 0,
            "skipped_sheet_owner": True,
            "skipped_outside_month": 0,
            "skipped_invalid": 0,
            "header_rows": [],
        }

    header_rows: list[int] = []
    if not any(_is_header_row(row) for row in values):
        if allow_no_header:
            return [], {
                "month": month_key,
                "source_label": source_label,
                "parsed": 0,
                "skipped_owner": 0,
                "skipped_outside_month": 0,
                "skipped_invalid": 0,
                "header_rows": [],
                "no_header": True,
            }
        raise AccountingImportError("找不到日期與金額欄位，請確認試算表標題列")

    parsed: list[AccountingSheetRow] = []
    skipped_owner = 0
    skipped_month = 0
    skipped_invalid = 0
    mapping: dict[str, int] = {}
    current_category: str | None = None
    current_type_hint: str | None = None
    for source_row, raw in enumerate(values, start=1):
        if not any(str(c or "").strip() for c in raw):
            continue
        candidate = header_map(raw)
        if "date" in candidate and ("amount" in candidate or "income" in candidate or "expense" in candidate):
            mapping = candidate
            header_rows.append(source_row)
            current_type_hint = None
            if "expense" in mapping and "income" not in mapping and "amount" not in mapping:
                current_type_hint = "支出"
            elif "income" in mapping and "expense" not in mapping and "amount" not in mapping:
                current_type_hint = "收入"
            current_category = None
            continue
        if not mapping:
            continue
        owner = _cell(raw, mapping, "owner")
        if owner and any(marker in owner for marker in SKIP_OWNER_MARKERS):
            skipped_owner += 1
            continue
        category = _clean_category(_cell(raw, mapping, "category"), current_category)
        if category is None:
            continue
        if _cell(raw, mapping, "category").strip() and _cell(raw, mapping, "category").strip() != "總額":
            current_category = category
        tx_date = parse_date(_cell(raw, mapping, "date"))
        if not tx_date:
            skipped_invalid += 1
            continue
        if tx_date < start or tx_date > end:
            skipped_month += 1
            continue

        amount = parse_amount(_cell(raw, mapping, "amount"))
        income = parse_amount(_cell(raw, mapping, "income"))
        expense = parse_amount(_cell(raw, mapping, "expense"))
        if amount is None:
            if income is not None and income != 0:
                amount = abs(income)
            elif expense is not None and expense != 0:
                amount = -abs(expense)
        if amount is None or amount == 0:
            skipped_invalid += 1
            continue
        tx_type = current_type_hint or _infer_type(_cell(raw, mapping, "type"), amount)
        name = _cell(raw, mapping, "name") or _cell(raw, mapping, "description")
        memo = _cell(raw, mapping, "memo")
        case_ref = _cell(raw, mapping, "case_ref") or _case_ref_from_name(name)
        item = AccountingSheetRow(
            source_row=source_row,
            date=tx_date.isoformat(),
            type=tx_type,
            amount=abs(float(amount)),
            category=category,
            sub_type=_cell(raw, mapping, "sub_type") or None,
            description=_join_description(name, memo),
            case_ref=case_ref,
            owner=owner or None,
        )
        item.fingerprint = _fingerprint(item, spreadsheet_id, gid)
        parsed.append(item)

    return parsed, {
        "month": month_key,
        "source_label": source_label,
        "header_rows": header_rows,
        "parsed": len(parsed),
        "skipped_owner": skipped_owner,
        "skipped_outside_month": skipped_month,
        "skipped_invalid": skipped_invalid,
    }


def _load_google_credentials(token_path: Path, credentials_path: Path, *, account_hint: str, interactive: bool):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception as exc:
        raise AccountingImportError(f"Google API 套件未安裝：{exc}") from exc

    creds = None
    token_has_requested_scopes = False
    if token_path.exists():
        try:
            token_data = json.loads(token_path.read_text(encoding="utf-8"))
            token_scopes = set(token_data.get("scopes") or [])
            token_has_requested_scopes = set(GOOGLE_READ_SCOPES).issubset(token_scopes)
        except Exception:
            token_has_requested_scopes = False
        creds = Credentials.from_authorized_user_file(str(token_path), GOOGLE_READ_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    has_scopes = bool(creds and creds.valid and token_has_requested_scopes and creds.has_scopes(GOOGLE_READ_SCOPES))
    if not creds or not creds.valid or not has_scopes:
        if not interactive:
            raise SheetsAuthorizationRequired(
                f"尚未授權 Google Sheets/Drive 讀取。請執行 scripts/import_accounting_sheet.py --auth，並用 {account_hint} 登入。"
            )
        if not credentials_path.exists():
            raise AccountingImportError(f"找不到 Google OAuth credentials：{credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), GOOGLE_READ_SCOPES)
        creds = flow.run_local_server(
            port=0,
            prompt="consent",
            login_hint=account_hint,
            authorization_prompt_message=(
                f"請用 {account_hint} 授權 MAGI 讀取同事帳務 Google Sheet：{{url}}"
            ),
        )
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            token_path.chmod(0o600)
        except Exception:
            pass
    return creds


def fetch_sheet_values(
    *,
    spreadsheet_id: str = DEFAULT_SPREADSHEET_ID,
    gid: int = DEFAULT_GID,
    token_path: Path | None = None,
    credentials_path: Path | None = None,
    account_hint: str = DEFAULT_ACCOUNT_HINT,
    interactive: bool = False,
    month: str | None = None,
) -> list[list[Any]]:
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except Exception as exc:
        raise AccountingImportError(f"Google Sheets API 套件未安裝：{exc}") from exc
    if not spreadsheet_id:
        raise AccountingImportError("尚未設定 MAGI_ACCOUNTING_SHEET_ID，無法讀取同事帳務表")

    creds = _load_google_credentials(
        token_path or _default_token_path(),
        credentials_path or _default_credentials_path(),
        account_hint=account_hint,
        interactive=interactive,
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        ).execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        message = str(exc)
        if status == 403 and "sheets.googleapis.com" in message and "SERVICE_DISABLED" in message:
            enable_url = sheets_api_enable_url(credentials_path)
            if interactive:
                try:
                    webbrowser.open(enable_url)
                except Exception:
                    pass
            raise AccountingImportError(
                "Google OAuth 專案尚未啟用 Google Sheets API；請先在 Cloud Console 啟用 sheets.googleapis.com，"
                f"啟用網址：{enable_url}。啟用後重新執行 scripts/import_accounting_sheet.py --auth --month YYYY-MM。"
            ) from exc
        if status in {401, 403}:
            raise AccountingImportError("Google Sheet 讀取權限不足；請確認已用指定 Google 帳號授權且該帳號可讀此表。") from exc
        if status == 400 and "Office file" in message:
            return fetch_office_spreadsheet_values(
                spreadsheet_id=spreadsheet_id,
                creds=creds,
                credentials_path=credentials_path or _default_credentials_path(),
                account_hint=account_hint,
                interactive=interactive,
                month=month,
            )
        raise
    title = None
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties") or {}
        if int(props.get("sheetId") or -1) == int(gid):
            title = props.get("title")
            break
    if not title:
        raise AccountingImportError(f"找不到 gid={gid} 的工作表")
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{title}'!A:AZ",
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="FORMATTED_STRING",
        ).execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in {401, 403}:
            raise AccountingImportError("Google Sheet 讀取權限不足；請確認已用指定 Google 帳號授權且該帳號可讀此表。") from exc
        raise
    return result.get("values", [])


def fetch_office_spreadsheet_values(
    *,
    spreadsheet_id: str,
    creds: Any,
    credentials_path: Path,
    account_hint: str,
    interactive: bool,
    month: str | None = None,
) -> list[list[Any]]:
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        import openpyxl
    except Exception as exc:
        raise AccountingImportError(f"讀取 Office 帳務檔需要 Google Drive API 與 openpyxl：{exc}") from exc

    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    try:
        content = drive.files().get_media(fileId=spreadsheet_id).execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        message = str(exc)
        if status == 403 and "drive.googleapis.com" in message and (
            "SERVICE_DISABLED" in message or "accessNotConfigured" in message or "has not been used" in message
        ):
            enable_url = google_api_enable_url("drive.googleapis.com", credentials_path)
            if interactive:
                try:
                    webbrowser.open(enable_url)
                except Exception:
                    pass
            raise AccountingImportError(
                "這份帳務是 Office/Excel 檔，MAGI 需要 Google Drive API 下載原檔；"
                f"請先啟用 drive.googleapis.com：{enable_url}。"
            ) from exc
        if status in {401, 403}:
            raise AccountingImportError(f"Google Drive 讀取權限不足；請確認用 {account_hint} 授權且該帳號可讀此表。") from exc
        raise

    workbook = openpyxl.load_workbook(BytesIO(content), data_only=True, read_only=True)
    target_month = None
    if month:
        _, _, target_month = month_window(month)
    best_values: list[list[Any]] | None = None
    best_score = -1
    for worksheet in workbook.worksheets:
        title = str(worksheet.title or "")
        if any(marker in title for marker in SKIP_OWNER_MARKERS):
            continue
        if target_month:
            year, mon = target_month.split("-")
            month_tokens = {f"{year}年{int(mon)}月", f"{year}年{mon}月"}
            if not any(token in title for token in month_tokens):
                continue
        values = [[cell for cell in row] for row in worksheet.iter_rows(values_only=True)]
        score = 0
        for raw in values:
            mapping = header_map(raw or [])
            if "date" in mapping:
                score += 2
            if "amount" in mapping or "income" in mapping or "expense" in mapping:
                score += 2
            if "owner" in mapping:
                score += 1
            if score >= 4:
                break
        if score > best_score:
            best_values = values
            best_score = score
    if best_values is None:
        raise AccountingImportError("Excel 帳務檔內找不到可辨識的日期/金額標題列")
    return best_values


def _get_osc_helpers():
    from api.osc.utils import _osc_exec, _osc_resolve_case_id

    return _osc_exec, _osc_resolve_case_id


def ensure_import_schema() -> None:
    _osc_exec, _ = _get_osc_helpers()
    _osc_exec(
        """
        CREATE TABLE IF NOT EXISTS accounting_import_records (
          fingerprint CHAR(64) NOT NULL,
          source VARCHAR(120) NOT NULL,
          source_row INT NULL,
          source_month VARCHAR(7) NOT NULL,
          transaction_id INT NULL,
          payload_json TEXT NULL,
          imported_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (fingerprint),
          KEY idx_accounting_import_month (source_month)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        fetch="none",
    )


def resolve_accounting_case_ref(ref: str | None) -> str | None:
    text = str(ref or "").strip()
    if not text:
        return None
    _osc_exec, _osc_resolve_case_id = _get_osc_helpers()
    direct = _osc_resolve_case_id(text)
    if direct and direct != text:
        return str(direct)
    row, _ = _osc_exec(
        """
        SELECT id FROM cases
         WHERE case_number=%s
            OR legal_aid_number=%s
            OR laf_case_no=%s
            OR application_no=%s
            OR court_case_number=%s
            OR court_case_no=%s
         LIMIT 1
        """,
        (text, text, text, text, text, text),
        fetch="one",
    )
    if row and row.get("id"):
        return str(row.get("id"))
    return text


def _fixed_expense_family(haystack: str) -> str | None:
    if any(k in haystack for k in ("薪資", "薪水", "主持律師", "法務專員")):
        return "薪資"
    if any(k in haystack for k in ("勞工退休金", "勞退")):
        return "勞退"
    if any(k in haystack for k in ("勞工保險", "勞保")):
        return "勞保"
    if any(k in haystack for k in ("全民健康保險", "健保")):
        return "健保"
    if any(k in haystack for k in ("房租", "租金", "台北事務所", "臺北事務所", "花蓮事務所")):
        return "辦公室租金"
    return None


def fixed_expense_overlap_details(row: AccountingSheetRow) -> dict[str, Any] | None:
    """Return recurring-expense match details for colleague-sheet fixed expenses."""
    if row.type != "支出":
        return None
    haystack = " ".join(
        str(v or "")
        for v in [row.category, row.sub_type, row.description, row.case_ref]
    )
    if row.category not in FIXED_EXPENSE_SKIP_CATEGORIES and not any(k in haystack for k in FIXED_EXPENSE_SKIP_KEYWORDS):
        return None
    _osc_exec, _ = _get_osc_helpers()
    active, _ = _osc_exec(
        """
        SELECT id, category, sub_type, description, amount
        FROM recurring_expenses
        WHERE is_active=1
          AND (
               category IN ('人事費','租金支出')
            OR sub_type IN ('薪資','勞保','勞退','健保','辦公室租金')
            OR description LIKE '%薪水%'
            OR description LIKE '%健保%'
            OR description LIKE '%勞保%'
            OR description LIKE '%勞退%'
            OR description LIKE '%辦公室%'
          )
        """,
        fetch="all",
    )
    if not active:
        return None
    family = _fixed_expense_family(haystack)
    matched = [r for r in active if not family or str(r.get("sub_type") or "") == family]
    if not matched:
        return None
    amounts = []
    compact_rows = []
    for r in matched:
        try:
            amt = abs(float(r.get("amount") or 0))
        except Exception:
            amt = 0.0
        amounts.append(amt)
        compact_rows.append(
            {
                "id": r.get("id"),
                "category": r.get("category"),
                "sub_type": r.get("sub_type"),
                "description": r.get("description"),
                "amount": amt,
            }
        )
    row_amount = abs(float(row.amount or 0))
    amount_matches = any(abs(row_amount - amt) < 0.01 for amt in amounts)
    total_matches = abs(row_amount - sum(amounts)) < 0.01 if len(amounts) > 1 else False
    amount_conflict = bool(row_amount and not amount_matches and not total_matches)
    return {
        "family": family or "固定支出",
        "recurring": compact_rows,
        "recurring_amount_total": sum(amounts),
        "amount_conflict": amount_conflict,
    }


def is_fixed_expense_overlap(row: AccountingSheetRow) -> bool:
    """Return True when a colleague-sheet row is already covered by recurring expenses."""
    return fixed_expense_overlap_details(row) is not None


def import_rows(rows: list[AccountingSheetRow], *, month: str, dry_run: bool = True) -> dict[str, Any]:
    _osc_exec, _osc_resolve_case_id = _get_osc_helpers()
    ensure_import_schema()
    imported: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    existing_matches: list[dict[str, Any]] = []
    fixed_expense_skips: list[dict[str, Any]] = []
    fixed_expense_conflicts: list[dict[str, Any]] = []
    for row in rows:
        exists, _ = _osc_exec(
            "SELECT fingerprint, transaction_id FROM accounting_import_records WHERE fingerprint=%s",
            (row.fingerprint,),
            fetch="one",
        )
        row_dict = asdict(row)
        if exists:
            duplicates.append(row_dict)
            continue
        fixed_overlap = fixed_expense_overlap_details(row)
        if fixed_overlap:
            row_dict["skip_reason"] = "covered_by_recurring_expense"
            row_dict["fixed_expense_match"] = fixed_overlap
            fixed_expense_skips.append(row_dict)
            if fixed_overlap.get("amount_conflict"):
                row_dict["warning"] = "colleague_sheet_amount_differs_from_recurring_expense"
                fixed_expense_conflicts.append(row_dict)
            continue
        case_id = resolve_accounting_case_ref(row.case_ref)
        existing_tx, _ = _osc_exec(
            """
            SELECT id FROM case_transactions
             WHERE date=%s
               AND type=%s
               AND ABS(amount)=%s
               AND COALESCE(category,'')=%s
               AND COALESCE(description,'')=%s
             LIMIT 1
            """,
            (
                row.date,
                row.type,
                row.amount,
                row.category or "",
                row.description or "",
            ),
            fetch="one",
        )
        if existing_tx and existing_tx.get("id"):
            row_dict["transaction_id"] = existing_tx.get("id")
            existing_matches.append(row_dict)
            if not dry_run:
                _osc_exec(
                    """
                    INSERT INTO accounting_import_records
                      (fingerprint, source, source_row, source_month, transaction_id, payload_json)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        row.fingerprint,
                        "colleague_google_sheet",
                        row.source_row,
                        month,
                        existing_tx.get("id"),
                        json.dumps(row_dict, ensure_ascii=False, sort_keys=True),
                    ),
                    fetch="none",
                )
            continue
        if dry_run:
            imported.append(row_dict)
            continue
        result, _ = _osc_exec(
            """
            INSERT INTO case_transactions (case_id, date, type, sub_type, category, description, amount)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (case_id, row.date, row.type, row.sub_type, row.category, row.description, row.amount),
            fetch="none",
        )
        tx_id = (result or {}).get("lastrowid")
        _osc_exec(
            """
            INSERT INTO accounting_import_records
              (fingerprint, source, source_row, source_month, transaction_id, payload_json)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (
                row.fingerprint,
                "colleague_google_sheet",
                row.source_row,
                month,
                tx_id,
                json.dumps(row_dict, ensure_ascii=False, sort_keys=True),
            ),
            fetch="none",
        )
        row_dict["transaction_id"] = tx_id
        imported.append(row_dict)
    return {
        "ok": True,
        "dry_run": dry_run,
        "month": month,
        "importable_count": len(imported),
        "duplicate_count": len(duplicates),
        "existing_count": len(existing_matches),
        "fixed_expense_skip_count": len(fixed_expense_skips),
        "fixed_expense_conflict_count": len(fixed_expense_conflicts),
        "items": imported,
        "duplicates": duplicates[:20],
        "existing_matches": existing_matches[:20],
        "fixed_expense_skips": fixed_expense_skips[:20],
        "fixed_expense_conflicts": fixed_expense_conflicts[:20],
    }


def run_import(
    *,
    month: str | None = None,
    dry_run: bool = True,
    spreadsheet_id: str = DEFAULT_SPREADSHEET_ID,
    gid: int = DEFAULT_GID,
    interactive: bool = False,
    account_hint: str = DEFAULT_ACCOUNT_HINT,
) -> dict[str, Any]:
    _, _, month_key = month_window(month)
    if not spreadsheet_id:
        raise AccountingImportError("尚未設定 MAGI_ACCOUNTING_SHEET_ID，請在 .env 設定同事帳務表檔案 ID")
    values = fetch_sheet_values(
        spreadsheet_id=spreadsheet_id,
        gid=gid,
        interactive=interactive,
        account_hint=account_hint,
        month=month_key,
    )
    rows, stats = parse_sheet_values(
        values,
        month=month_key,
        spreadsheet_id=spreadsheet_id,
        gid=gid,
        allow_no_header=True,
    )
    result = import_rows(rows, month=month_key, dry_run=dry_run)
    result["sheet_stats"] = stats
    result["spreadsheet_id"] = spreadsheet_id
    result["gid"] = gid
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import colleague accounting Google Sheet into MAGI.")
    parser.add_argument("--month", default=None, help="匯入月份 YYYY-MM；預設本月；也可用 previous")
    parser.add_argument("--include-previous", action="store_true", help="同時檢查上一個月，補抓月底後登資料")
    parser.add_argument("--commit", action="store_true", help="實際寫入資料庫；預設只預覽")
    parser.add_argument("--auth", action="store_true", help="需要時開啟瀏覽器授權 Google Sheets")
    parser.add_argument("--spreadsheet-id", default=DEFAULT_SPREADSHEET_ID)
    parser.add_argument("--gid", type=int, default=DEFAULT_GID)
    parser.add_argument("--account-hint", default=DEFAULT_ACCOUNT_HINT)
    args = parser.parse_args(argv)
    try:
        months: list[str | None] = [args.month]
        if args.include_previous and not args.month:
            months = ["previous", None]
        results = [
            run_import(
                month=target_month,
                dry_run=not args.commit,
                spreadsheet_id=args.spreadsheet_id,
                gid=args.gid,
                interactive=args.auth,
                account_hint=args.account_hint,
            )
            for target_month in months
        ]
        result = results[0] if len(results) == 1 else {
            "ok": all(r.get("ok") for r in results),
            "dry_run": not args.commit,
            "months": [r.get("month") for r in results],
            "results": results,
            "importable_count": sum(int(r.get("importable_count") or 0) for r in results),
            "duplicate_count": sum(int(r.get("duplicate_count") or 0) for r in results),
            "existing_count": sum(int(r.get("existing_count") or 0) for r in results),
            "fixed_expense_skip_count": sum(int(r.get("fixed_expense_skip_count") or 0) for r in results),
            "fixed_expense_conflict_count": sum(int(r.get("fixed_expense_conflict_count") or 0) for r in results),
        }
    except SheetsAuthorizationRequired as exc:
        print(json.dumps({"ok": False, "error": "auth_required", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
