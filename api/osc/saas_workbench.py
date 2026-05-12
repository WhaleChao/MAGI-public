"""Office operations control-plane helpers for OSC/Paperclip.

The workbench deliberately reuses existing OSC tables and runtime JSONL files.
It adds a unified operating layer without forcing a DB migration on the user's
live office database.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from api.osc.draft_learning import draft_learning_summary, recent_draft_feedback

ROOT = Path(__file__).resolve().parents[2]
INTAKE_PATH = ROOT / ".runtime" / "osc_saas_intake_events.jsonl"
MAX_TEXT = 60000
CLOSED_CASE_STATUSES = ("已結案", "已結案，待報結", "已結案待報結", "已結案，待送出")

ExecFn = Callable[..., tuple[Any, Any]]

CAPABILITIES = [
    {
        "key": "learning_center",
        "title": "修正學習中心",
        "status": "enabled",
        "owner": "AI 草擬",
        "tab": "drafts",
        "source": "draft_learning JSONL",
        "role": "彙整既有人工改正，不另建第二套學習庫",
    },
    {
        "key": "quality_gate",
        "title": "品質/幻覺審核層",
        "status": "enabled",
        "owner": "AI 草擬",
        "tab": "drafts",
        "source": "書狀輸出文字與來源文件",
        "role": "作為草擬輸出前的審核層",
    },
    {
        "key": "risk_dashboard",
        "title": "期限與風險儀表板",
        "status": "enabled",
        "owner": "待辦事項 / 行事曆 / 法扶管理",
        "tab": "todos",
        "source": "case_todos、calendar_events、cases",
        "role": "只聚合既有待辦、行程與法扶案件狀態",
    },
    {
        "key": "document_timeline",
        "title": "文件證據索引 / 事實時間線",
        "status": "enabled",
        "owner": "書狀索引",
        "tab": "documents",
        "source": "document_index",
        "role": "沿用書狀索引，不另掃資料夾",
    },
    {
        "key": "external_packet",
        "title": "對外文件產生包",
        "status": "enabled",
        "owner": "案件列表 / 應備事項表",
        "tab": "cases",
        "source": "cases 與既有 checklist 邏輯",
        "role": "輸出可複製文字，不取代正式案件資料",
    },
    {
        "key": "intake_funnel",
        "title": "諮詢／接案追蹤",
        "status": "enabled",
        "owner": "所務管理",
        "tab": "saasWorkbench",
        "source": "runtime intake JSONL",
        "role": "原本沒有正式收案前入口，僅保留諮詢紀錄；轉正式案件後仍進 cases",
    },
    {
        "key": "conflict_check",
        "title": "利益衝突檢查",
        "status": "enabled",
        "owner": "當事人 / 對造 / 案件列表",
        "tab": "clients",
        "source": "clients、opponents、cases",
        "role": "查既有當事人、對造與案件，不另建名冊",
    },
    {
        "key": "client_portal",
        "title": "客戶 / 當事人入口",
        "status": "packet_mode",
        "owner": "案件列表 / 應備事項表",
        "tab": "cases",
        "source": "cases 與 checklist",
        "role": "目前只開資料包模式，公開上傳入口保留但不啟用",
    },
    {
        "key": "light_audit",
        "title": "輕量權限與高風險操作紀錄",
        "status": "high_risk_only",
        "owner": "系統設定",
        "tab": "admin",
        "source": "activity_logs",
        "role": "只顯示刪除、歸檔、搬移、匯出、分享等高風險紀錄",
    },
    {
        "key": "operations_report",
        "title": "所務統計",
        "status": "enabled",
        "owner": "業務概覽",
        "tab": "dashboard",
        "source": "OSC 既有資料表彙總",
        "role": "補強概覽，不取代各模組明細頁",
    },
]

INTEGRATION_MATRIX = [
    {"area": "期限與風險", "source": "待辦事項、行事曆、法扶管理", "target_tab": "todos", "mode": "聚合顯示"},
    {"area": "利益衝突", "source": "當事人、對造、案件列表", "target_tab": "clients", "mode": "查詢既有資料"},
    {"area": "文件時間線", "source": "書狀索引", "target_tab": "documents", "mode": "引用索引"},
    {"area": "修正學習", "source": "AI 草擬的人工改正紀錄", "target_tab": "drafts", "mode": "彙整顯示"},
    {"area": "對外資料包", "source": "案件資料與應備事項", "target_tab": "cases", "mode": "產生可複製文字"},
    {"area": "高風險紀錄", "source": "系統活動紀錄", "target_tab": "admin", "mode": "只開高風險稽核"},
]


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:limit]


def _norm(value: Any) -> str:
    return re.sub(r"[\s　,，、。．.／/\\|｜:：;；()（）【】\[\]「」『』\"']", "", str(value or ""))


def _one_line(value: Any, limit: int = 140) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _action(label: str, act: str, **attrs: Any) -> dict:
    out = {"label": label, "act": act}
    for key, value in attrs.items():
        if value not in (None, ""):
            out[key] = value
    return out


def _rows(exec_fn: ExecFn, sql: str, params: tuple = ()) -> list[dict]:
    try:
        rows, _ = exec_fn(sql, params, fetch="all") if params else exec_fn(sql, fetch="all")
        return rows or []
    except Exception:
        return []


def _row(exec_fn: ExecFn, sql: str, params: tuple = ()) -> dict:
    try:
        row, _ = exec_fn(sql, params, fetch="one") if params else exec_fn(sql, fetch="one")
        return row or {}
    except Exception:
        return {}


def _count(exec_fn: ExecFn, sql: str, params: tuple = ()) -> int:
    return _safe_int(_row(exec_fn, sql, params).get("c"))


def _status_open_sql(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    closed = "','".join(CLOSED_CASE_STATUSES + ("完成", "completed", "done", "cancelled", "canceled"))
    return f"({p}status IS NULL OR {p}status='' OR {p}status NOT IN ('{closed}'))"


def _risk_from_todo(row: dict, today: date) -> dict:
    raw_date = str(row.get("todo_date") or "").strip()
    due = None
    try:
        due = date.fromisoformat(raw_date[:10])
    except Exception:
        pass
    severity = "medium"
    reason = "未排日期"
    if due:
        delta = (due - today).days
        if delta < 0:
            severity, reason = "critical", f"逾期 {-delta} 天"
        elif delta == 0:
            severity, reason = "high", "今日到期"
        elif delta <= 3:
            severity, reason = "high", f"{delta} 天內到期"
        else:
            severity, reason = "medium", f"{delta} 天後"
    return {
        "type": "todo",
        "severity": severity,
        "owner": "待辦事項",
        "target_tab": "todos",
        "reason": reason,
        "case_number": row.get("case_number") or "",
        "client_name": row.get("client_name") or "",
        "title": row.get("todo_type") or "待辦",
        "date": raw_date,
        "detail": row.get("description") or "",
        "id": row.get("id"),
        "actions": [
            _action("編輯待辦", "saas-todo-edit", id=row.get("id")),
            _action("標記完成", "saas-todo-complete", id=row.get("id")),
        ],
    }


def _risk_from_calendar(row: dict, today: date) -> dict:
    raw_date = str(row.get("start_date") or "").strip()
    start = None
    try:
        start = date.fromisoformat(raw_date[:10])
    except Exception:
        pass
    severity = "medium"
    reason = "近期行程"
    if start:
        delta = (start - today).days
        if delta < 0:
            severity, reason = "medium", f"已過期 {-delta} 天，需確認是否結清"
        elif delta == 0:
            severity, reason = "high", "今日行程"
        elif delta <= 7:
            severity, reason = "medium", f"{delta} 天內行程"
    return {
        "type": "calendar",
        "severity": severity,
        "owner": "行事曆",
        "target_tab": "calendar",
        "reason": reason,
        "case_number": row.get("case_number") or "",
        "client_name": "",
        "title": row.get("title") or "行事曆",
        "date": raw_date,
        "detail": row.get("location") or row.get("description") or "",
        "id": row.get("id"),
        "actions": [_action("編輯行程", "saas-cal-edit", id=row.get("id"))],
    }


def build_risk_dashboard(exec_fn: ExecFn, *, limit: int = 30) -> dict:
    today = date.today()
    todos = _rows(
        exec_fn,
        f"""
        SELECT id, case_number, client_name, todo_type, todo_date, todo_time, description, status
        FROM case_todos
        WHERE {_status_open_sql()}
        ORDER BY COALESCE(todo_date, CURDATE()) ASC, id DESC
        LIMIT %s
        """,
        (limit,),
    )
    events = _rows(
        exec_fn,
        """
        SELECT id, case_number, title, start_date, end_date, description, location
        FROM calendar_events
        WHERE start_date >= %s
        ORDER BY start_date ASC, id ASC
        LIMIT %s
        """,
        (today - timedelta(days=1), limit),
    )
    laf_long = _rows(
        exec_fn,
        """
        SELECT id, case_number, client_name, case_reason, status, created_date
        FROM cases
        WHERE (case_category='法律扶助案件' OR case_reason LIKE '%法扶%' OR case_reason LIKE '%法律扶助%')
          AND (status IS NULL OR status='' OR status NOT IN ('已結案','已結案，待報結'))
        ORDER BY created_date ASC
        LIMIT %s
        """,
        (limit,),
    )
    risks = [_risk_from_todo(x, today) for x in todos] + [_risk_from_calendar(x, today) for x in events]
    for row in laf_long:
        created = str(row.get("created_date") or "").strip()
        age = 0
        try:
            age = (today - date.fromisoformat(created[:10])).days
        except Exception:
            pass
        if age >= 540:
            risks.append({
                "type": "laf",
                "severity": "high",
                "owner": "法扶管理",
                "target_tab": "laf",
                "reason": f"法扶進行中 {age} 天，需確認進度/報結",
                "case_number": row.get("case_number") or "",
                "client_name": row.get("client_name") or "",
                "title": row.get("case_reason") or "法扶案件",
                "date": created,
                "detail": row.get("status") or "",
                "id": row.get("id"),
                "actions": [
                    _action("法扶明細", "saas-laf-detail", id=row.get("id")),
                    _action("更新狀態", "saas-laf-status", id=row.get("id")),
                ],
            })
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    risks.sort(key=lambda x: (order.get(x.get("severity"), 9), x.get("date") or "9999"))
    return {
        "items": risks[:limit],
        "counts": {
            "critical": sum(1 for x in risks if x.get("severity") == "critical"),
            "high": sum(1 for x in risks if x.get("severity") == "high"),
            "medium": sum(1 for x in risks if x.get("severity") == "medium"),
        },
    }


def build_document_timeline(exec_fn: ExecFn, *, case_number: str = "", limit: int = 40) -> dict:
    params: tuple = (limit,)
    where = ""
    if case_number:
        where = "WHERE case_number=%s OR file_path LIKE %s OR file_name LIKE %s"
        params = (case_number, f"%{case_number}%", f"%{case_number}%", limit)
    docs = _rows(
        exec_fn,
        f"""
        SELECT id, case_number, file_name, file_path, subfolder_name, reason, party, modified_date
        FROM document_index
        {where}
        ORDER BY modified_date DESC, id DESC
        LIMIT %s
        """,
        params,
    )
    items = []
    for row in docs:
        when = str(row.get("modified_date") or "").strip()
        kind = str(row.get("subfolder_name") or row.get("reason") or "").strip() or guess_document_kind(row.get("file_name") or "")
        items.append({
            "date": when[:10],
            "case_number": row.get("case_number") or case_number,
            "kind": kind,
            "title": row.get("file_name") or "文件",
            "path": row.get("file_path") or "",
            "evidence_hint": evidence_hint(row.get("file_name") or "", kind),
            "actions": [
                _action("開啟", "doc-open", path=row.get("file_path") or ""),
                _action("複製路徑", "doc-copy", path=row.get("file_path") or ""),
            ],
        })
    return {"items": items, "count": len(items)}


def guess_document_kind(name: str) -> str:
    text = str(name or "")
    if any(k in text for k in ["判決", "裁定"]):
        return "裁判"
    if any(k in text for k in ["通知", "函"]):
        return "法院通知"
    if any(k in text for k in ["書狀", "聲請", "答辯", "準備"]):
        return "書狀"
    if any(k in text for k in ["證據", "收據", "診斷", "照片", "契約"]):
        return "證據"
    return "文件"


def evidence_hint(name: str, kind: str) -> str:
    if kind in {"裁判", "法院通知"}:
        return "可作為程序狀態或期限判斷來源"
    if kind == "書狀":
        return "可作為書狀格式、爭點與對造主張來源"
    if kind == "證據":
        return "可作為事實時間線與附件清單來源"
    return "需人工確認用途"


def conflict_check(exec_fn: ExecFn, payload: dict) -> dict:
    names = []
    for key in ("client_name", "opponent_name", "related_names", "company_name", "email", "phone"):
        raw = payload.get(key)
        if isinstance(raw, list):
            names.extend(raw)
        else:
            names.extend(re.split(r"[,，、\n]+", str(raw or "")))
    terms = []
    seen = set()
    for item in names:
        item = _text(item, 80)
        if not item:
            continue
        key = _norm(item).lower()
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        terms.append(item)

    matches = []
    for term in terms[:12]:
        like = f"%{term}%"
        case_rows = _rows(
            exec_fn,
            """
            SELECT id, case_number, client_name, case_reason, status, notes
            FROM cases
            WHERE client_name LIKE %s OR notes LIKE %s OR case_number LIKE %s OR court_case_no LIKE %s
            ORDER BY updated_at DESC, created_date DESC
            LIMIT 20
            """,
            (like, like, like, like),
        )
        for row in case_rows:
            side = "unknown"
            if term and term in str(row.get("client_name") or ""):
                side = "client"
            if term and term in str(row.get("notes") or ""):
                side = "opponent"
            matches.append({
                "term": term,
                "source": "cases",
                "side": side,
                "actions": [
                    _action("編輯案件", "saas-case-edit", id=row.get("id")),
                    _action("案件工作台", "case-workbench", id=row.get("id")),
                ],
                **row,
            })
        client_rows = _rows(
            exec_fn,
            """
            SELECT id, name AS client_name, phone, email, status
            FROM clients
            WHERE name LIKE %s OR contact_person LIKE %s OR phone LIKE %s OR email LIKE %s
            ORDER BY updated_at DESC, id DESC
            LIMIT 10
            """,
            (like, like, like, like),
        )
        for row in client_rows:
            matches.append({
                "term": term,
                "source": "clients",
                "side": "client_record",
                "actions": [_action("編輯當事人", "saas-client-edit", id=row.get("id"))],
                **row,
            })
        opponent_rows = _rows(
            exec_fn,
            """
            SELECT id, case_number, name AS opponent_name, address AS notes
            FROM opponents
            WHERE name LIKE %s OR address LIKE %s
            ORDER BY updated_date DESC, id DESC
            LIMIT 10
            """,
            (like, like),
        )
        for row in opponent_rows:
            matches.append({
                "term": term,
                "source": "opponents",
                "side": "opponent",
                "actions": [_action("編輯對造", "saas-opponent-edit", id=row.get("id"))],
                **row,
            })

    risk = "clear"
    if any(x.get("side") == "opponent" for x in matches):
        risk = "high"
    elif matches:
        risk = "review"
    return {
        "ok": True,
        "terms": terms,
        "risk": risk,
        "matches": matches[:80],
        "summary": conflict_summary(risk, matches),
    }


def conflict_summary(risk: str, matches: list[dict]) -> str:
    if risk == "clear":
        return "未找到明顯衝突候選。"
    if risk == "high":
        return f"找到 {len(matches)} 筆候選，其中包含對造/相對人紀錄，請人工確認。"
    return f"找到 {len(matches)} 筆候選，多為既有當事人或相關紀錄，請確認是否可承接。"


def quality_check(payload: dict) -> dict:
    text = _text(payload.get("text") or payload.get("draft_text") or "")
    case_number = _text(payload.get("case_number") or "", 120)
    reason = _text(payload.get("reason") or "", 120)
    source_paths = payload.get("source_paths") or []
    issues = []
    if not text:
        issues.append({"severity": "high", "code": "empty", "message": "沒有可檢查的文字。"})
    if re.search(r"\bOSC[-_ ]?\d{3,}\b|20\d{2}-\d{3,}", text) and not re.search(r"年度.+字", text):
        issues.append({"severity": "high", "code": "internal_case_number", "message": "可能把內部 OSC 案號當法院案號。"})
    if "<|channel>" in text or "Here's a thinking process" in text or "作為MAGI" in text:
        issues.append({"severity": "critical", "code": "prompt_or_reasoning_leak", "message": "疑似模型思考標記或提示詞外洩。"})
    citations = re.findall(r"\d{2,3}年度[^\s，。、；;]{1,16}字第?\d{1,6}號", text)
    if citations and not source_paths:
        issues.append({"severity": "medium", "code": "citation_needs_source", "message": f"偵測到 {len(citations)} 個裁判/案號引用，需確認來源文件。"})
    if case_number and case_number not in text and re.search(r"案號|年度.+字", text):
        issues.append({"severity": "medium", "code": "case_number_mismatch", "message": "提供的案號未出現在文本中，需確認狀頭。"})
    if reason and reason not in text and len(text) > 500:
        issues.append({"severity": "low", "code": "reason_not_visible", "message": "案由未明顯出現在文本中，可確認是否需要補入。"})
    if re.search(r"（待確認）|待確認|TODO|FIXME", text, re.I):
        issues.append({"severity": "medium", "code": "placeholder", "message": "仍有待確認欄位。"})
    severity_order = {"critical": 3, "high": 2, "medium": 1, "low": 0}
    max_sev = max((severity_order.get(x["severity"], 0) for x in issues), default=-1)
    return {
        "ok": True,
        "pass": max_sev < 2,
        "score": max(0, 100 - sum({"critical": 35, "high": 25, "medium": 10, "low": 4}.get(x["severity"], 5) for x in issues)),
        "issues": issues,
        "stats": {"chars": len(text), "citations": len(citations), "sources": len(source_paths) if isinstance(source_paths, list) else 0},
    }


def build_client_packet(exec_fn: ExecFn, payload: dict) -> dict:
    case_number = _text(payload.get("case_number") or "", 120)
    case_row = {}
    if case_number:
        case_row = _row(
            exec_fn,
            """
            SELECT case_number, client_name, case_reason, case_type, case_stage, court_name, court_case_no, laf_case_no
            FROM cases
            WHERE case_number=%s OR court_case_no=%s
            ORDER BY updated_at DESC, created_date DESC
            LIMIT 1
            """,
            (case_number, case_number),
        )
    client_name = _text(payload.get("client_name") or case_row.get("client_name") or "", 120)
    reason = _text(payload.get("reason") or case_row.get("case_reason") or "", 120)
    checklist = default_client_checklist(reason)
    lines = [
        f"{client_name or '您好'}，您好：",
        "",
        f"關於{reason or '本案'}，請協助補充以下資料：",
    ]
    lines += [f"{i}. {item}" for i, item in enumerate(checklist, 1)]
    lines += ["", "資料可拍照或掃描上傳；若不確定是否相關，也可以先提供，我們再協助判斷。"]
    return {
        "ok": True,
        "case": case_row,
        "checklist": checklist,
        "copy_text": "\n".join(lines),
        "portal_mode": "packet_only",
        "portal_note": "目前先產生可複製的當事人資料包；公開上傳入口保留設計但預設不開啟。",
    }


def default_client_checklist(reason: str) -> list[str]:
    key = _norm(reason)
    if "消債" in key or "更生" in key or "清算" in key:
        return ["身分證正反面", "戶籍謄本", "財產清單", "所得資料", "債權人清冊", "近三個月收支明細", "債務相關通知或催收文件"]
    if "損害賠償" in key:
        return ["事故或爭議發生時序", "契約或往來文件", "付款或損害證明", "照片/錄音/對話紀錄", "對方資料", "已寄送或收到的存證信函/通知"]
    if "刑" in key:
        return ["傳票或通知書", "筆錄或起訴書", "相關對話紀錄", "證人或共犯資訊", "有利證據資料", "前科或案件進度說明"]
    return ["案件時序整理", "相關契約或通知", "付款/收據/交易紀錄", "對話紀錄", "照片或其他證據", "對方姓名與聯絡方式"]


def record_intake(exec_fn: ExecFn, payload: dict, *, actor: str = "") -> dict:
    conflict = conflict_check(exec_fn, payload)
    event = {
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "actor": _text(actor, 120),
        "client_name": _text(payload.get("client_name"), 120),
        "opponent_name": _text(payload.get("opponent_name"), 120),
        "case_reason": _text(payload.get("case_reason") or payload.get("reason"), 160),
        "contact": _text(payload.get("contact") or payload.get("phone") or payload.get("email"), 200),
        "summary": _text(payload.get("summary") or payload.get("notes"), 3000),
        "next_step": _text(payload.get("next_step"), 500),
        "conflict_risk": conflict.get("risk"),
        "conflict_terms": conflict.get("terms") or [],
        "payload_hash": _sha(json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    }
    INTAKE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with INTAKE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return {"ok": True, "event": event, "conflict": conflict}


def recent_intakes(limit: int = 20) -> list[dict]:
    if not INTAKE_PATH.exists():
        return []
    out = []
    for line in reversed(INTAKE_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit * 3:]):
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
        if len(out) >= limit:
            break
    return out


def build_operations_report(exec_fn: ExecFn) -> dict:
    total = _count(exec_fn, "SELECT COUNT(*) AS c FROM cases")
    active = _count(exec_fn, f"SELECT COUNT(*) AS c FROM cases WHERE {_status_open_sql()}")
    closing_pending = _count(exec_fn, "SELECT COUNT(*) AS c FROM cases WHERE status IN ('已結案，待報結','已結案待報結','已結案，待送出')")
    closed = _count(exec_fn, "SELECT COUNT(*) AS c FROM cases WHERE status='已結案'")
    pending = _count(exec_fn, f"SELECT COUNT(*) AS c FROM case_todos WHERE {_status_open_sql()}")
    overdue = _count(exec_fn, f"SELECT COUNT(*) AS c FROM case_todos WHERE {_status_open_sql()} AND todo_date < CURDATE()")
    docs = _count(exec_fn, "SELECT COUNT(*) AS c FROM document_index")
    insights = _count(exec_fn, "SELECT COUNT(*) AS c FROM legal_insights")
    laf = _count(exec_fn, "SELECT COUNT(*) AS c FROM cases WHERE case_category='法律扶助案件' OR case_reason LIKE '%法扶%' OR case_reason LIKE '%法律扶助%'")
    return {
        "total_cases": total,
        "active_cases": active,
        "closed_cases": closed,
        "closing_pending_cases": closing_pending,
        "pending_todos": pending,
        "overdue_todos": overdue,
        "documents": docs,
        "legal_insights": insights,
        "legal_aid_cases": laf,
        "automation": {
            "learning_events": draft_learning_summary().get("count", 0),
            "intake_events": len(recent_intakes(200)),
        },
    }


def high_risk_activity(exec_fn: ExecFn, *, limit: int = 30) -> dict:
    rows = _rows(
        exec_fn,
        """
        SELECT id, action, entity_type, entity_id, details, user, timestamp
        FROM activity_logs
        WHERE action LIKE '%delete%' OR action LIKE '%刪%' OR action LIKE '%archive%' OR action LIKE '%move%'
           OR action LIKE '%share%' OR action LIKE '%export%' OR action LIKE '%feedback%'
        ORDER BY timestamp DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return {
        "mode": "high_risk_only",
        "full_access_audit_enabled": False,
        "items": rows,
    }


def build_saas_overview(exec_fn: ExecFn, *, case_number: str = "") -> dict:
    risk = build_risk_dashboard(exec_fn)
    timeline = build_document_timeline(exec_fn, case_number=case_number)
    ops = build_operations_report(exec_fn)
    learning = {"summary": draft_learning_summary(), "recent": recent_draft_feedback(8)}
    return {
        "ok": True,
        "capabilities": CAPABILITIES,
        "integration": {
            "principle": "這裡集中顯示常用資訊；實際新增與修改仍在各對應頁籤完成。",
            "items": INTEGRATION_MATRIX,
        },
        "risk": risk,
        "timeline": timeline,
        "operations": ops,
        "learning": learning,
        "intake": {"recent": recent_intakes(8)},
        "audit": high_risk_activity(exec_fn, limit=12),
    }
