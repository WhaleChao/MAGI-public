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

from api.legal_workflow import (
    LEGAL_WORKFLOW_AGENTS,
    PRACTICE_AREA_PROFILES,
    detect_legal_workflow,
    workflow_review,
)
from api.osc.draft_learning import draft_learning_summary, recent_draft_feedback

ROOT = Path(__file__).resolve().parents[2]
INTAKE_PATH = ROOT / ".runtime" / "osc_saas_intake_events.jsonl"
ONBOARDING_PATH = ROOT / ".runtime" / "osc_saas_onboarding.json"
NOTIFICATION_PREFS_PATH = ROOT / ".runtime" / "osc_saas_notification_prefs.json"
WORKFLOW_TEMPLATES_PATH = ROOT / ".runtime" / "osc_saas_workflow_templates.json"
MAX_TEXT = 60000
CLOSED_CASE_STATUSES = ("已結案", "已結案，待報結", "已結案待報結", "已結案，待送出")
NOT_NEEDED_FOR_SINGLE_HOST = ("多租戶", "電子簽章", "公開上傳入口")
TASK_REFRESH_INTERVAL_HOURS = 6

ExecFn = Callable[..., tuple[Any, Any]]

CAPABILITIES = [
    {
        "key": "learning_center",
        "title": "修正學習中心",
        "status": "enabled",
        "owner": "AI 草擬",
        "tab": "drafts",
        "primary_action": {"act": "saas-section-jump", "section": "saasLearningSection", "label": "查看學習紀錄"},
        "source": "draft_learning JSONL",
        "role": "彙整既有人工改正，不另建第二套學習庫",
    },
    {
        "key": "quality_gate",
        "title": "品質/幻覺審核層",
        "status": "enabled",
        "owner": "AI 草擬",
        "tab": "drafts",
        "primary_action": {"act": "saas-section-jump", "section": "saasQualitySection", "label": "檢查文字"},
        "source": "書狀輸出文字與來源文件",
        "role": "作為草擬輸出前的審核層",
    },
    {
        "key": "risk_dashboard",
        "title": "期限與風險",
        "status": "enabled",
        "owner": "待辦事項 / 行事曆 / 法扶管理",
        "tab": "todos",
        "primary_action": {"act": "saas-section-jump", "section": "saasRiskSection", "label": "查看風險"},
        "source": "case_todos、calendar_events、cases",
        "role": "只聚合既有待辦、行程與法扶案件狀態",
    },
    {
        "key": "document_timeline",
        "title": "文件證據時間線",
        "status": "enabled",
        "owner": "管理工具",
        "tab": "",
        "primary_action": {"act": "saas-section-jump", "section": "saasTimelineSection", "label": "查看時間線"},
        "secondary_actions": [{"act": "tab-jump", "tab": "documents", "label": "開書狀索引"}],
        "source": "document_index",
        "role": "用既有書狀索引整理案件文件時間線，不等同書狀索引頁",
    },
    {
        "key": "nerv_status_page",
        "title": "NERV 上線狀態",
        "status": "enabled",
        "owner": "NERV",
        "tab": "",
        "primary_action": {"act": "open-url", "url": "/dashboard/nerv", "label": "開啟 NERV"},
        "secondary_actions": [{"act": "open-url", "url": "/dashboard/nerv/api/health", "label": "健康檢查"}],
        "source": "NERV health API",
        "role": "作為正式上線狀態頁，顯示推理、OCR、DB、NAS 與背景服務健康度",
    },
    {
        "key": "external_packet",
        "title": "對外資料",
        "status": "enabled",
        "owner": "案件列表 / 應備事項表",
        "tab": "cases",
        "primary_action": {"act": "saas-section-jump", "section": "saasPacketSection", "label": "產生對外資料"},
        "source": "cases 與既有 checklist 邏輯",
        "role": "輸出可複製文字，不取代正式案件資料",
    },
    {
        "key": "intake_funnel",
        "title": "諮詢／接案追蹤",
        "status": "enabled",
        "owner": "業務概覽",
        "tab": "dashboard",
        "primary_action": {"act": "saas-section-jump", "section": "saasIntakeSection", "label": "建立紀錄"},
        "source": "runtime intake JSONL",
        "role": "原本沒有正式接案前入口，僅保留諮詢紀錄；轉正式案件後仍進 cases",
    },
    {
        "key": "conflict_check",
        "title": "利益衝突檢查",
        "status": "enabled",
        "owner": "當事人 / 對造 / 案件列表",
        "tab": "clients",
        "primary_action": {"act": "saas-section-jump", "section": "saasConflictSection", "label": "進行檢查"},
        "source": "clients、opponents、cases",
        "role": "查既有當事人、對造與案件，不另建名冊",
    },
    {
        "key": "light_audit",
        "title": "輕量權限與高風險操作紀錄",
        "status": "high_risk_only",
        "owner": "系統設定",
        "tab": "admin",
        "primary_action": {"act": "saas-section-jump", "section": "saasOpsSection", "label": "查看紀錄"},
        "source": "activity_logs",
        "role": "只顯示刪除、歸檔、搬移、匯出、分享等高風險紀錄",
    },
    {
        "key": "operations_report",
        "title": "事務統計",
        "status": "enabled",
        "owner": "業務概覽",
        "tab": "dashboard",
        "primary_action": {"act": "saas-section-jump", "section": "saasOpsSection", "label": "查看統計"},
        "source": "OSC 既有資料表彙總",
        "role": "補強概覽，不取代各模組明細頁",
    },
    {
        "key": "onboarding_checklist",
        "title": "導入檢查",
        "status": "enabled",
        "owner": "管理工具",
        "tab": "",
        "primary_action": {"act": "saas-section-jump", "section": "saasOnboardingSection", "label": "勾選檢查"},
        "source": "runtime onboarding state",
        "role": "把交付前檢查做成可勾選清單，避免只靠文件記憶",
    },
    {
        "key": "notification_preferences",
        "title": "通知偏好",
        "status": "enabled",
        "owner": "系統通知",
        "tab": "",
        "primary_action": {"act": "saas-section-jump", "section": "saasNotificationSection", "label": "調整通知"},
        "source": "notification preference JSON",
        "role": "明確區分業務、法扶與系統通知，降低誤送頻道風險",
    },
    {
        "key": "workflow_templates",
        "title": "流程樣板",
        "status": "enabled",
        "owner": "案件 / 法扶 / 書狀",
        "tab": "",
        "primary_action": {"act": "saas-section-jump", "section": "saasWorkflowSection", "label": "查看樣板"},
        "source": "workflow templates",
        "role": "將常見流程列成可複製步驟，仍使用既有案件與法扶功能落地",
    },
    {
        "key": "diagnostics_export",
        "title": "維運診斷匯出",
        "status": "enabled",
        "owner": "NERV / 管理工具",
        "tab": "",
        "primary_action": {"act": "download-url", "url": "/api/osc/saas/diagnostic-pack", "label": "下載診斷"},
        "source": "readiness, operations, audit",
        "role": "一鍵輸出不含金鑰的診斷 JSON，方便檢查上線狀態",
    },
]

READINESS_CHECKS = [
    {
        "key": "single_host_boundary",
        "title": "部署邊界",
        "status": "ready",
        "detail": "每台主機是一個獨立 MAGI；不做共用多租戶資料庫。",
        "actions": [{"act": "open-url", "url": "/dashboard/nerv", "label": "NERV 狀態"}],
    },
    {
        "key": "nerv_status",
        "title": "上線狀態頁",
        "status": "ready",
        "detail": "NERV 已提供健康檢查與服務狀態，作為正式上線狀態頁。",
        "actions": [
            {"act": "open-url", "url": "/dashboard/nerv", "label": "開啟 NERV"},
            {"act": "open-url", "url": "/dashboard/nerv/api/health", "label": "健康 API"},
        ],
    },
    {
        "key": "high_risk_controls",
        "title": "高風險操作",
        "status": "guarded",
        "detail": "送出、還原、結案搬移、批次清理與大量匯入保留人工確認與高風險紀錄。",
        "actions": [{"act": "tab-jump", "tab": "admin", "label": "系統設定"}],
    },
    {
        "key": "workflow_templates",
        "title": "流程樣板",
        "status": "ready",
        "detail": "案件、法扶、消債、書狀、帳務與行事曆使用同一套既有資料表，不再疊第二套。",
        "actions": [{"act": "tab-jump", "tab": "cases", "label": "案件列表"}],
    },
    {
        "key": "notification_routing",
        "title": "通知路由",
        "status": "ready",
        "detail": "業務通知與系統通知分流；法扶巡檢應走法扶一般或系統通知，不送派案頻道。",
        "actions": [{"act": "tab-jump", "tab": "admin", "label": "通知設定"}],
    },
    {
        "key": "import_export",
        "title": "匯入 / 匯出",
        "status": "ready",
        "detail": "案件、當事人、帳務、書狀與法扶活動已保留匯入匯出或可複製文字出口。",
        "actions": [{"act": "tab-jump", "tab": "cases", "label": "案件匯入匯出"}],
    },
    {
        "key": "ai_provenance",
        "title": "AI 來源標示",
        "status": "guarded",
        "detail": "回答、記憶、實務見解與書狀草擬保留來源標示；引用前仍提示核對全文。",
        "actions": [{"act": "tab-jump", "tab": "drafts", "label": "AI 草擬"}],
    },
    {
        "key": "support_diagnostics",
        "title": "維運診斷",
        "status": "ready",
        "detail": "public audit、smoke50、production-live 與 commercial-release 作為交付前檢查。",
        "actions": [{"act": "open-url", "url": "/dashboard/nerv", "label": "查看狀態"}],
    },
    {
        "key": "not_needed_scope",
        "title": "本版不啟用",
        "status": "not_needed",
        "detail": "多租戶、電子簽章、公開上傳入口暫不納入；對外聯絡維持私人 LINE 與可複製文字。",
        "actions": [{"act": "saas-section-jump", "section": "saasPacketSection", "label": "對外資料"}],
    },
]

INTEGRATION_MATRIX = [
    {"area": "期限與風險", "source": "待辦事項、行事曆、法扶管理", "target_tab": "todos", "mode": "聚合顯示"},
    {"area": "利益衝突", "source": "當事人、對造、案件列表", "target_tab": "clients", "mode": "查詢既有資料"},
    {
        "area": "文件資料",
        "source": "書狀索引 document_index",
        "target_tabs": [
            {"tab": "saasTimelineSection", "label": "查看時間線", "act": "saas-section-jump"},
            {"tab": "documents", "label": "開書狀索引", "act": "tab-jump"},
        ],
        "mode": "時間線彙整 / 索引明細分流",
    },
    {"area": "修正學習", "source": "AI 草擬的人工改正紀錄", "target_tab": "drafts", "mode": "彙整顯示"},
    {"area": "對外資料", "source": "案件資料與應備事項", "target_tab": "cases", "mode": "產生可複製文字"},
    {"area": "高風險紀錄", "source": "系統活動紀錄", "target_tab": "admin", "mode": "只開高風險稽核"},
    {"area": "上線檢查", "source": "NERV、導入檢查、診斷匯出", "target_tab": "saasOnboardingSection", "mode": "狀態確認 / JSON 匯出"},
]

DEFAULT_ONBOARDING_ITEMS = [
    {"key": "public_audit", "title": "public audit strict 通過", "category": "交付", "required": True},
    {"key": "daemon_health", "title": "MAGI daemon、NERV、Tools API 正常", "category": "服務", "required": True},
    {"key": "db_backup", "title": "本機 DB 備份可讀取且還原需確認", "category": "資料", "required": True},
    {"key": "nas_mounts", "title": "NAS 掛載名稱正確，未掛成 -1", "category": "資料", "required": True},
    {"key": "calendar_scope", "title": "Google Calendar 匯入只抓 OSC 與法扶計數行程", "category": "行事曆", "required": True},
    {"key": "channel_routes", "title": "法扶一般、法扶派案、系統通知分流確認", "category": "通知", "required": True},
    {"key": "laf_guard", "title": "法扶送出、報結與批次搬移仍需人工確認", "category": "法扶", "required": True},
    {"key": "ai_sources", "title": "AI 回答與書狀草擬保留來源核對提示", "category": "AI", "required": True},
]

DEFAULT_NOTIFICATION_PREFS = {
    "business": "enabled",
    "laf_general": "enabled",
    "laf_dispatch": "enabled",
    "system_health": "system_only",
    "nightly_report": "system_only",
    "live_check": "system_only",
    "discord_business_channels": "business_only",
}

DEFAULT_WORKFLOW_TEMPLATES = [
    {
        "key": "laf_new_case",
        "title": "法扶新案",
        "scope": "法律扶助案件",
        "steps": ["建立案件", "下載官網附件", "補齊法扶案號", "確認開辦資料", "人工確認後送出開辦"],
        "entry_actions": [{"act": "tab-jump", "tab": "laf", "label": "法扶管理"}],
    },
    {
        "key": "debt_case",
        "title": "消債更生 / 清算",
        "scope": "消債事件",
        "steps": ["建立應備事項表", "產生可複製補件文字", "追蹤所得與財產清單年度", "整理債權人清冊", "產生書狀草稿"],
        "entry_actions": [{"act": "tab-jump", "tab": "laf", "label": "法扶管理"}, {"act": "tab-jump", "tab": "drafts", "label": "AI 草擬"}],
    },
    {
        "key": "pleading_final",
        "title": "書狀定稿",
        "scope": "書狀製作",
        "steps": ["選定案件", "載入同案由學習紀錄", "核對引用來源", "匯出 DOCX/PDF", "人工比對完稿"],
        "entry_actions": [{"act": "tab-jump", "tab": "drafts", "label": "AI 草擬"}, {"act": "tab-jump", "tab": "documents", "label": "書狀索引"}],
    },
    {
        "key": "closing_archive",
        "title": "結案歸檔",
        "scope": "案件資料夾",
        "steps": ["確認案件狀態", "比對同名不同案", "預覽搬移", "人工確認執行", "驗證原路徑已清乾淨且結案區可開啟"],
        "entry_actions": [{"act": "tab-jump", "tab": "archive", "label": "結案歸檔"}],
    },
]

APPROVAL_MATRIX = [
    {"operation": "法扶送出 / 報結", "level": "manual_confirm", "reason": "會對外提交資料"},
    {"operation": "DB 還原", "level": "manual_confirm", "reason": "可能覆蓋正式資料"},
    {"operation": "結案批次搬移", "level": "preview_then_confirm", "reason": "涉及 NAS 檔案搬移"},
    {"operation": "大量匯入 / 清理", "level": "dry_run_then_confirm", "reason": "可能造成重複或刪除"},
    {"operation": "AI 書狀引用", "level": "source_required", "reason": "引用前需核對裁判全文與來源文件"},
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


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if data is not None else default
    except Exception:
        pass
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


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
    source_file = str(row.get("source_file") or "").strip()
    is_calendar_import = source_file.startswith("gcal_import")
    return {
        "type": "todo",
        "severity": severity,
        "owner": "行事曆匯入" if is_calendar_import else "OSC 建立待辦",
        "target_tab": "calendar" if is_calendar_import else "todos",
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
        SELECT id, case_number, client_name, todo_type, todo_date, todo_time, description, status, source_file
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


def _todo_board_item(row: dict, *, imported_calendar: bool = False) -> dict:
    date_part = str(row.get("todo_date") or "").strip()
    time_part = str(row.get("todo_time") or "").strip()
    return {
        "id": row.get("id"),
        "source": "gcal_import" if imported_calendar else "case_todos",
        "source_label": "行事曆匯入" if imported_calendar else "OSC 建立",
        "case_number": row.get("case_number") or "",
        "client_name": row.get("client_name") or "",
        "date": date_part,
        "time": time_part,
        "title": row.get("todo_type") or ("行事曆事件" if imported_calendar else "待辦"),
        "detail": row.get("description") or row.get("source_file") or "",
        "status": row.get("status") or "",
        "sort_key": f"{date_part or '9999-99-99'} {time_part or '99:99'}",
        "actions": [
            _action("編輯", "saas-todo-edit", id=row.get("id")),
            _action("完成", "saas-todo-complete", id=row.get("id")),
        ],
    }


def _calendar_board_item(row: dict) -> dict:
    raw_date = str(row.get("start_date") or "").strip()
    return {
        "id": row.get("id"),
        "source": "calendar_events",
        "source_label": "行事曆事件",
        "case_number": row.get("case_number") or "",
        "client_name": "",
        "date": raw_date[:10],
        "time": raw_date[11:16] if len(raw_date) >= 16 else "",
        "title": row.get("title") or row.get("summary") or "行事曆事件",
        "detail": row.get("location") or row.get("description") or "",
        "status": "",
        "sort_key": raw_date or "9999-99-99 99:99",
        "actions": [_action("編輯", "saas-cal-edit", id=row.get("id"))],
    }


def build_task_boards(exec_fn: ExecFn, *, case_number: str = "", limit: int = 20) -> dict:
    """Return separate to-do boards for OSC-created items and calendar items.

    Google Calendar imports are stored in ``case_todos`` with source_file
    ``gcal_import``.  They must stay on the calendar board so the operations
    page does not misread imported/manual calendar work as OSC-created work.
    """

    case_clause = ""
    case_params: list[Any] = []
    if case_number:
        case_clause = " AND (case_number=%s OR case_number LIKE %s OR description LIKE %s)"
        like = f"%{case_number}%"
        case_params = [case_number, like, like]

    osc_todos = _rows(
        exec_fn,
        f"""
        SELECT id, case_number, client_name, todo_type, todo_date, todo_time, description, status, source_file, created_date
        FROM case_todos
        WHERE {_status_open_sql()}
          AND (source_file IS NULL OR source_file='' OR source_file NOT LIKE 'gcal_import%%')
          {case_clause}
        ORDER BY COALESCE(todo_date, CURDATE()) ASC, COALESCE(todo_time, '23:59') ASC, id DESC
        LIMIT %s
        """,
        tuple(case_params + [limit]),
    )
    imported_calendar_todos = _rows(
        exec_fn,
        f"""
        SELECT id, case_number, client_name, todo_type, todo_date, todo_time, description, status, source_file, created_date
        FROM case_todos
        WHERE {_status_open_sql()}
          AND source_file LIKE 'gcal_import%%'
          {case_clause}
        ORDER BY COALESCE(todo_date, CURDATE()) ASC, COALESCE(todo_time, '23:59') ASC, id DESC
        LIMIT %s
        """,
        tuple(case_params + [limit]),
    )
    cal_clause = ""
    cal_params: list[Any] = []
    if case_number:
        cal_clause = " AND (case_number=%s OR title LIKE %s OR summary LIKE %s OR description LIKE %s)"
        like = f"%{case_number}%"
        cal_params = [case_number, like, like, like]
    calendar_events = _rows(
        exec_fn,
        f"""
        SELECT id, case_number, title, summary, start_date, end_date, description, location, is_all_day
        FROM calendar_events
        WHERE start_date >= %s
          {cal_clause}
        ORDER BY start_date ASC, id ASC
        LIMIT %s
        """,
        tuple([date.today() - timedelta(days=1)] + cal_params + [limit]),
    )
    calendar_items = [_calendar_board_item(x) for x in calendar_events] + [
        _todo_board_item(x, imported_calendar=True) for x in imported_calendar_todos
    ]
    calendar_items.sort(key=lambda x: x.get("sort_key") or "")
    return {
        "ok": True,
        "refresh": {
            "interval_hours": TASK_REFRESH_INTERVAL_HOURS,
            "cron_id": "job_osc_events_refresh",
            "policy": "每 6 小時更新 OSC 建立待辦與行事曆事件；頁面重新整理時只讀已更新資料。",
        },
        "osc_todos": {
            "title": "OSC 建立待辦",
            "source": "case_todos（排除 gcal_import）",
            "items": [_todo_board_item(x) for x in osc_todos],
            "count": len(osc_todos),
            "entry_actions": [{"act": "tab-jump", "tab": "todos", "label": "待辦事項"}],
        },
        "calendar_events": {
            "title": "行事曆事件",
            "source": "calendar_events + case_todos.source_file=gcal_import",
            "items": calendar_items[:limit],
            "count": len(calendar_items),
            "source_counts": {
                "calendar_events": len(calendar_events),
                "gcal_import": len(imported_calendar_todos),
            },
            "entry_actions": [{"act": "tab-jump", "tab": "calendar", "label": "行事曆"}],
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
                    _action("案件處理", "case-workbench", id=row.get("id")),
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
    selected_insights = payload.get("selected_insights") or []
    selected_documents = payload.get("selected_documents") or []
    workflow = detect_legal_workflow(
        text=text,
        reason=reason,
        doc_type=str(payload.get("doc_type") or payload.get("document_type") or ""),
        mode=str(payload.get("mode") or "draft"),
    )
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
    review = workflow_review(
        text,
        workflow,
        source_count=len(source_paths) if isinstance(source_paths, list) else 0,
        selected_insights=len(selected_insights) if isinstance(selected_insights, list) else 0,
        selected_documents=len(selected_documents) if isinstance(selected_documents, list) else 0,
    )
    issues.extend(review.get("issues") or [])
    severity_order = {"critical": 3, "high": 2, "medium": 1, "low": 0}
    max_sev = max((severity_order.get(x["severity"], 0) for x in issues), default=-1)
    return {
        "ok": True,
        "pass": max_sev < 2,
        "score": max(0, 100 - sum({"critical": 35, "high": 25, "medium": 10, "low": 4}.get(x["severity"], 5) for x in issues)),
        "issues": issues,
        "stats": {"chars": len(text), "citations": len(citations), "sources": len(source_paths) if isinstance(source_paths, list) else 0},
        "legal_workflow": workflow,
        "workflow_review": review,
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
        "portal_note": "目前先產生可複製的當事人資料；公開上傳入口保留設計但預設不開啟。",
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


def build_onboarding_status() -> dict:
    state = _read_json(ONBOARDING_PATH, {})
    done = state.get("done") if isinstance(state, dict) else {}
    if not isinstance(done, dict):
        done = {}
    items = []
    for item in DEFAULT_ONBOARDING_ITEMS:
        key = item["key"]
        row = dict(item)
        row["done"] = bool(done.get(key))
        row["updated_at"] = (done.get(key) or {}).get("updated_at") if isinstance(done.get(key), dict) else ""
        items.append(row)
    required = [x for x in items if x.get("required")]
    done_required = [x for x in required if x.get("done")]
    return {
        "ok": True,
        "items": items,
        "summary": {
            "total": len(items),
            "required": len(required),
            "done": sum(1 for x in items if x.get("done")),
            "required_done": len(done_required),
            "ready": len(required) == len(done_required),
        },
    }


def update_onboarding_status(payload: dict, *, actor: str = "") -> dict:
    key = _text(payload.get("key"), 80)
    valid = {x["key"] for x in DEFAULT_ONBOARDING_ITEMS}
    if key not in valid:
        return {"ok": False, "error": "unknown_onboarding_item"}
    state = _read_json(ONBOARDING_PATH, {})
    if not isinstance(state, dict):
        state = {}
    done = state.get("done")
    if not isinstance(done, dict):
        done = {}
    if bool(payload.get("done")):
        done[key] = {"actor": _text(actor, 120), "updated_at": datetime.now(timezone.utc).isoformat()}
    else:
        done.pop(key, None)
    state["done"] = done
    _write_json(ONBOARDING_PATH, state)
    return build_onboarding_status()


def build_notification_preferences() -> dict:
    prefs = _read_json(NOTIFICATION_PREFS_PATH, {})
    if not isinstance(prefs, dict):
        prefs = {}
    merged = dict(DEFAULT_NOTIFICATION_PREFS)
    merged.update({str(k): str(v) for k, v in prefs.items() if k in DEFAULT_NOTIFICATION_PREFS})
    items = [
        {"key": "business", "title": "一般業務通知", "value": merged["business"], "options": ["enabled", "silent"]},
        {"key": "laf_general", "title": "法扶一般 / 巡檢", "value": merged["laf_general"], "options": ["enabled", "system_only", "silent"]},
        {"key": "laf_dispatch", "title": "法扶派案", "value": merged["laf_dispatch"], "options": ["enabled", "silent"]},
        {"key": "system_health", "title": "系統健康 / live 檢查", "value": merged["system_health"], "options": ["system_only", "silent"]},
        {"key": "nightly_report", "title": "夜間報告", "value": merged["nightly_report"], "options": ["system_only", "enabled", "silent"]},
        {"key": "live_check", "title": "三模組 live 檢查", "value": merged["live_check"], "options": ["system_only", "silent"]},
    ]
    return {"ok": True, "prefs": merged, "items": items, "policy": "system_only 不送業務 Discord 頻道；enabled 依路由器分流；silent 不主動推播。"}


def save_notification_preferences(payload: dict) -> dict:
    incoming = payload.get("prefs") if isinstance(payload.get("prefs"), dict) else payload
    current = build_notification_preferences()["prefs"]
    allowed_values = {"enabled", "system_only", "business_only", "silent"}
    for key, value in (incoming or {}).items():
        if key not in DEFAULT_NOTIFICATION_PREFS:
            continue
        value = str(value or "").strip()
        if value in allowed_values:
            current[key] = value
    _write_json(NOTIFICATION_PREFS_PATH, current)
    return build_notification_preferences()


def build_workflow_templates() -> dict:
    overrides = _read_json(WORKFLOW_TEMPLATES_PATH, {})
    custom = overrides.get("templates") if isinstance(overrides, dict) else []
    templates = [dict(x) for x in DEFAULT_WORKFLOW_TEMPLATES]
    if isinstance(custom, list):
        by_key = {x["key"]: x for x in templates}
        for item in custom:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            base = by_key.get(str(item["key"]), {})
            merged = {**base, **item}
            by_key[str(item["key"])] = merged
        templates = list(by_key.values())
    return {
        "ok": True,
        "templates": templates,
        "legal_workflow_agents": [dict(x) for x in LEGAL_WORKFLOW_AGENTS],
        "practice_profiles": [dict(x) for x in PRACTICE_AREA_PROFILES],
        "count": len(templates),
        "reference": {
            "name": "claude-for-legal",
            "url": "https://github.com/anthropics/claude-for-legal",
            "import_mode": "conceptual_patterns_only",
            "note": "移植其法律工作流代理、案由設定檔、來源與覆核護欄的設計；不複製外部程式碼。",
        },
    }


def build_ai_governance() -> dict:
    provenance_files = [
        {"path": "api/session/provenance.py", "ready": _file_ready("api/session/provenance.py")},
        {"path": "api/answer_provenance.py", "ready": _file_ready("api/answer_provenance.py")},
        {"path": "api/verification/answer_verifier.py", "ready": _file_ready("api/verification/answer_verifier.py")},
    ]
    return {
        "ok": True,
        "policies": [
            "法律回答需標示來源；沒有來源時回覆查不到或請使用者補資料。",
            "書狀引用裁判前需核對全文，不可只依摘要或片段生成。",
            "人工修正學習限同案由使用，不同案由不可混記。",
            "疑似提示詞、思考標記、內部案號外洩會被品質檢查攔下。",
        ],
        "provenance_files": provenance_files,
        "ready": all(x["ready"] for x in provenance_files),
    }


def render_operations_report_text(exec_fn: ExecFn) -> dict:
    ops = build_operations_report(exec_fn)
    lines = [
        "MAGI 事務統計",
        f"產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"案件總數：{ops['total_cases']}",
        f"進行中案件：{ops['active_cases']}",
        f"已結案：{ops['closed_cases']}",
        f"報結/送出中：{ops['closing_pending_cases']}",
        f"待辦：{ops['pending_todos']}",
        f"逾期待辦：{ops['overdue_todos']}",
        f"文件索引：{ops['documents']}",
        f"實務見解：{ops['legal_insights']}",
        f"法扶案件：{ops['legal_aid_cases']}",
        f"學習紀錄：{ops.get('automation', {}).get('learning_events', 0)}",
    ]
    return {"ok": True, "text": "\n".join(lines), "operations": ops}


def build_diagnostic_pack(exec_fn: ExecFn) -> dict:
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "single_host_magi",
        "readiness": build_product_readiness(exec_fn),
        "operations": build_operations_report(exec_fn),
        "task_boards": build_task_boards(exec_fn, limit=8),
        "onboarding": build_onboarding_status(),
        "notification_preferences": build_notification_preferences(),
        "workflow_templates": build_workflow_templates(),
        "ai_governance": build_ai_governance(),
        "audit": high_risk_activity(exec_fn, limit=20),
        "redaction": "No secrets, tokens, DB dumps, or case file contents are included.",
    }


def _file_ready(path: str) -> bool:
    return (ROOT / path).exists()


def build_product_readiness(exec_fn: ExecFn) -> dict:
    """Return the single-host product-readiness map used by the OSC page.

    This is intentionally lightweight: it only checks local files/routes that
    should exist in every checkout and DB-backed counts that are safe to read.
    The live health of services remains NERV's job.
    """

    checks = [dict(item) for item in READINESS_CHECKS]
    file_checks = {
        "nerv_status": _file_ready("templates/dashboard_nerv.html") and _file_ready("api/blueprints/admin_runtime.py"),
        "workflow_templates": _file_ready("api/osc/saas_workbench.py") and _file_ready("api/osc/laf_activity_stats.py"),
        "notification_routing": _file_ready("api/discord_channel_router.py"),
        "import_export": _file_ready("api/blueprints/osc_cases.py") and _file_ready("api/osc/accounting_sheet_import.py"),
        "ai_provenance": _file_ready("api/session/provenance.py") and _file_ready("api/answer_provenance.py"),
        "support_diagnostics": _file_ready("scripts/ops/run_test_suite.py") and _file_ready("scripts/ops/commercial_readiness_live.py"),
    }
    for item in checks:
        key = item.get("key")
        if key in file_checks and not file_checks[key]:
            item["status"] = "needs_attention"
            item["detail"] = f"{item['detail']}（本機缺少必要檔案，請先檢查安裝。）"

    high_risk_count = len(high_risk_activity(exec_fn, limit=5).get("items") or [])
    operations = build_operations_report(exec_fn)
    return {
        "mode": "single_host",
        "mode_label": "單主機 MAGI",
        "not_needed": list(NOT_NEEDED_FOR_SINGLE_HOST),
        "status_page": {"label": "NERV 上線狀態", "url": "/dashboard/nerv", "health_api": "/dashboard/nerv/api/health"},
        "summary": {
            "ready": sum(1 for x in checks if x.get("status") == "ready"),
            "guarded": sum(1 for x in checks if x.get("status") == "guarded"),
            "not_needed": sum(1 for x in checks if x.get("status") == "not_needed"),
            "needs_attention": sum(1 for x in checks if x.get("status") == "needs_attention"),
            "high_risk_recent": high_risk_count,
            "total_cases": operations.get("total_cases", 0),
            "pending_todos": operations.get("pending_todos", 0),
            "task_refresh_interval_hours": TASK_REFRESH_INTERVAL_HOURS,
        },
        "checks": checks,
        "approval_matrix": APPROVAL_MATRIX,
    }


def build_saas_overview(exec_fn: ExecFn, *, case_number: str = "") -> dict:
    risk = build_risk_dashboard(exec_fn)
    task_boards = build_task_boards(exec_fn, case_number=case_number)
    timeline = build_document_timeline(exec_fn, case_number=case_number)
    ops = build_operations_report(exec_fn)
    learning = {"summary": draft_learning_summary(), "recent": recent_draft_feedback(8)}
    return {
        "ok": True,
        "capabilities": CAPABILITIES,
        "readiness": build_product_readiness(exec_fn),
        "integration": {
            "principle": "這裡集中顯示常用資訊；實際新增與修改仍在各對應頁籤完成。",
            "items": INTEGRATION_MATRIX,
        },
        "task_boards": task_boards,
        "risk": risk,
        "timeline": timeline,
        "operations": ops,
        "operations_text": render_operations_report_text(exec_fn)["text"],
        "learning": learning,
        "intake": {"recent": recent_intakes(8)},
        "audit": high_risk_activity(exec_fn, limit=12),
        "onboarding": build_onboarding_status(),
        "notification_preferences": build_notification_preferences(),
        "workflow_templates": build_workflow_templates(),
        "ai_governance": build_ai_governance(),
    }
