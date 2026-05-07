"""OSC (案件管理) API routes blueprint.

Extracted from server.py to reduce its size.
"""
from __future__ import annotations

import json
import logging
import base64
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import csv
import html as html_lib
import io

from flask import Blueprint, request, jsonify, send_file, Response
from flask_login import login_required, current_user

from api.osc.utils import (
    _osc_exec, _osc_web_connect, _osc_row_json, _osc_json_value,
    _osc_norm_case_category, _osc_resolve_case_id, _osc_safe_int,
    _osc_truthy, _osc_text, _osc_current_actor, _osc_log_activity,
    _osc_accounting_window, _osc_get_setting_value, _osc_unique_strings,
    _osc_norm_path, _osc_local_path_candidates, _osc_is_safe_local_path,
    _osc_resolve_existing_local_path, _osc_try_open_path,
    _osc_case_folder_from_doc_path, _osc_guess_case_folder,
    _osc_folder_entries, _osc_human_size, _osc_relpath_under,
    _osc_is_editable_text_path, _osc_read_text_file, _osc_smb_candidates,
    _osc_windows_unc_candidates, _osc_windows_synology_candidates,
    _osc_path_to_smb, _osc_parse_dt, _osc_read_reference_document,
    _osc_read_plain_text, _osc_read_docx_text, _osc_read_pdf_text,
    _osc_allowed_local_roots,
)
from api.osc.drafts import (
    _osc_template_data_json_or_wrap, _osc_json_or_wrap,
)
from api.osc.judicial import (
    _osc_collect_insights, _osc_fetch_fulltext_from_judicial,
    _osc_summarize_legal_insight, _osc_doc_kind_match, _osc_doc_kind_label,
)
from api.osc.insight_filters import (
    is_non_extractable_legal_insight,
    non_extractable_legal_insight_sql_where,
)
from api.osc.drafts import (
    _osc_get_case_identity_by_payload, _osc_build_form_preview,
    _osc_build_draft_context, _osc_generate_draft_with_casper,
    _osc_generate_draft_with_ollama, _osc_generate_draft_with_gemini,
    _osc_clean_draft_output, _osc_draft_enabled_flag,
    _osc_import_laf_orchestrator, _osc_map_laf_action,
    _osc_prepare_laf_identity, _osc_enrich_portal_preview,
    _osc_get_closed_archive_base, _osc_build_archive_preview,
)

_log = logging.getLogger(__name__)
logger = _log  # alias used by some routes

osc_bp = Blueprint("osc_cases", __name__)

# ---------------------------------------------------------------------------
# Lazy imports for server globals
# ---------------------------------------------------------------------------

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_OSC_RESOURCE_PHOTO_DIR = os.path.join(_MAGI_ROOT, "resources", "osc", "photo")


def _osc_photo_path(filename: str) -> str:
    return os.path.join(_OSC_RESOURCE_PHOTO_DIR, filename)


def _osc_existing_resource_path(setting_key: str, filename: str, fallback: str = "") -> str:
    configured = str(_osc_get_setting_value(setting_key, "") or "").strip()
    candidates = [configured, _osc_photo_path(filename), fallback]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return configured or _osc_photo_path(filename) or fallback


def _get_orchestrator():
    from api.server import orchestrator
    return orchestrator


def _get_normalize_output_text():
    try:
        from api.tw_output_guard import normalize_output_text
        return normalize_output_text
    except Exception:
        return None


def _format_web_reply_html(reply: str) -> str:
    try:
        from api.blueprints.web_runtime import format_web_reply_html

        return format_web_reply_html(reply)
    except Exception:
        return f'<div class="web-reply"><p>{html_lib.escape(str(reply or ""))}</p></div>'


def _current_user_context() -> tuple[str, str]:
    try:
        user_id = str(getattr(current_user, "id", "") or "web")
    except Exception:
        user_id = "web"
    try:
        role = str(getattr(current_user, "role", "") or "user")
    except Exception:
        role = "user"
    return user_id, role


def _quick_action_rows(sql: str, params: tuple = (), fetch: str = "all"):
    try:
        rows, _ = _osc_exec(sql, params, fetch=fetch)
        return rows or ([] if fetch == "all" else {})
    except Exception:
        _log.debug("quick action context query failed", exc_info=True)
        return [] if fetch == "all" else {}


def _osc_todo_done_statuses() -> tuple[str, ...]:
    return ("completed", "done", "已完成", "完成", "cancelled", "canceled", "取消")


def _osc_is_todo_done_status(status: str) -> bool:
    text = str(status or "").strip().lower()
    return text in {s.lower() for s in _osc_todo_done_statuses()}


def _build_quick_action_native_reply(action: str, case: dict) -> str:
    case_number = str(case.get("case_number") or "").strip()
    client_name = str(case.get("client_name") or "").strip()
    laf_no = str(case.get("laf_case_no") or "").strip()
    doc_terms = {
        "laf_closing_status": ["結案", "酬金", "領款", "判決", "裁定", "調解不成立", "收據"],
        "laf_progress_summary": ["接案", "開辦", "應備", "補件", "回報", "法扶"],
        "closing_overview": ["結案", "判決", "裁定", "收據", "繳費"],
        "generate_power_of_attorney": ["委任狀", "委任契約"],
        "generate_receipt": ["收據", "領款"],
    }.get(action, [])

    docs = []
    if case_number:
        docs = _quick_action_rows(
            """
            SELECT title, file_name, file_path, doc_type, created_at
            FROM document_index
            WHERE case_number=%s
            ORDER BY created_at DESC, id DESC
            LIMIT 80
            """,
            (case_number,),
        )
    matched_docs = []
    for doc in docs or []:
        blob = " ".join(str(doc.get(k) or "") for k in ("title", "file_name", "file_path", "doc_type"))
        if not doc_terms or any(term in blob for term in doc_terms):
            matched_docs.append(doc)

    todos = []
    if case_number:
        todos = _quick_action_rows(
            """
            SELECT todo_type, todo_date, description, status
            FROM case_todos
            WHERE case_number=%s
            ORDER BY todo_date DESC, id DESC
            LIMIT 20
            """,
            (case_number,),
        )

    pending_todos = [t for t in (todos or []) if not _osc_is_todo_done_status(t.get("status") or "")]
    action_title = {
        "laf_closing_status": "法扶結案狀況盤點",
        "laf_progress_summary": "法扶進度盤點",
        "closing_overview": "結案資料彙整",
        "generate_power_of_attorney": "委任狀草稿檢查",
        "generate_receipt": "收據草稿檢查",
    }.get(action, "案件盤點")

    missing = []
    if action == "laf_closing_status":
        if not laf_no:
            missing.append("法扶案號未填，報結前請先補入。")
        if not matched_docs:
            missing.append("尚未在索引中找到結案酬金領款單、判決/裁定或結案相關文件。")
        if pending_todos:
            missing.append(f"尚有 {len(pending_todos)} 筆待辦未完成，報結前請確認。")
    elif action == "laf_progress_summary":
        if not laf_no:
            missing.append("法扶案號未填。")
        if not matched_docs:
            missing.append("尚未在索引中找到開辦/應備/補件相關文件。")
    elif action == "closing_overview" and not matched_docs:
        missing.append("尚未在索引中找到判決、裁定、收據或結案相關文件。")

    lines = [
        f"# {action_title}",
        "",
        "## 案件",
        f"- 案件編號：{case_number or '-'}",
        f"- 當事人：{client_name or '-'}",
        f"- 案件種類：{case.get('case_category') or '-'}",
        f"- 案由：{case.get('case_reason') or '-'}",
        f"- 法扶案號：{laf_no or '-'}",
        "",
        "## 已找到的相關文件",
    ]
    if matched_docs:
        for doc in matched_docs[:10]:
            label = doc.get("title") or doc.get("file_name") or doc.get("file_path") or "未命名文件"
            lines.append(f"- {label}")
    else:
        lines.append("- 尚未找到符合本次盤點關鍵字的文件。")

    lines.extend(["", "## 待辦狀況"])
    if pending_todos:
        for todo in pending_todos[:8]:
            lines.append(f"- {todo.get('todo_date') or '-'}｜{todo.get('todo_type') or '待辦'}｜{todo.get('description') or ''}".rstrip("｜"))
    else:
        lines.append("- 目前沒有查到未完成待辦。")

    lines.extend(["", "## 建議下一步"])
    if missing:
        lines.extend(f"- {item}" for item in missing)
    else:
        lines.append("- 目前沒有明顯缺漏；請人工確認文件內容與法扶系統狀態後再送出。")
    return "\n".join(lines)


def _get_text_primary_model():
    from api.model_config import TEXT_PRIMARY_MODEL
    return TEXT_PRIMARY_MODEL


def _get_preferred_case_roots():
    from api.case_path_mapper import preferred_case_roots
    return preferred_case_roots()


def _get_translate_local_path_to_canonical():
    from api.case_path_mapper import translate_local_path_to_canonical
    return translate_local_path_to_canonical


def _osc_fetch_url_text(url: str, timeout: int = 20) -> dict:
    from api.osc.utils import _osc_fetch_url_text as _impl
    return _impl(url, timeout=timeout)


def _osc_lookup_fulltext_fallback(title: str = "", case_number: str = "", url: str = "") -> dict:
    from api.osc.utils import _osc_lookup_fulltext_fallback as _impl
    return _impl(title=title, case_number=case_number, url=url)


def _export_osc_form_files(title: str, preview_text: str, suggested_filename: str = "") -> dict:
    from api.startup import _export_osc_form_files as _impl
    return _impl(title, preview_text, suggested_filename)


def _export_file_meta(path: str) -> dict:
    from api.startup import _export_file_meta as _impl
    return _impl(path)


def _record_last_public_base_url():
    from api.server import _record_last_public_base_url as _impl
    return _impl()


def _get_public_base_url() -> str:
    from api.server import _load_public_base_url as _impl
    return _impl()


# ---------------------------------------------------------------------------
# Constants (mirrored from server.py)
# ---------------------------------------------------------------------------

_OSC_DRAFT_DOC_TYPES = [
    "民事起訴狀",
    "民事答辯狀",
    "民事準備書狀",
    "民事上訴狀",
    "民事聲請狀",
    "刑事告訴狀",
    "刑事答辯狀",
    "刑事上訴狀",
    "刑事聲請狀",
    "刑事陳報狀",
    "行政起訴狀",
    "行政答辯狀",
    "抗告狀",
    "聲明異議狀",
    "強制執行聲請狀",
    "假扣押聲請狀",
    "假處分聲請狀",
    "支付命令聲請狀",
    "本票裁定聲請狀",
]

_OSC_DRAFT_PROMPT_TEMPLATE = """你是一位專業的台灣律師助理，請根據以下資料協助草擬法律文書。

## 書狀類型
{doc_type}

## 案件基本資訊
- 案號：{case_number}
- 股別：{division}
- 法院/地檢署：{court_name}
- 案由：{reason}
- 原告/聲請人：{plaintiff}
- 被告/相對人：{defendant}

## 案件事實
{case_facts}

## 書寫風格參考（以下為過往類似書狀的格式範例）
{reference_style}

## 要求
1. 請按照上述參考風格撰寫完整的{doc_type}
2. 格式需符合台灣法院規範
3. 確保案號、股別、法院名稱正確填入狀頭
4. 論述需有邏輯、條理分明
5. 請加入常見的法律用語和格式

請直接輸出完整書狀內容：
"""


# ── Helper (auto-create folder for new case) ──────────────────────────────


def _osc_auto_create_folder_for_case(row_id: str, payload: dict, case_category: str) -> dict:
    """建立案件資料夾並更新 DB，回傳結果 dict。供 POST /api/osc/cases 使用。"""
    from casper_ecosystem.law_firm_orchestrators.osc.folder_utils import (
        build_full_case_path,
        create_folder_structure,
    )

    translate_local_path_to_canonical = _get_translate_local_path_to_canonical()
    case_roots = _get_preferred_case_roots()
    if not case_roots or not os.path.isdir(case_roots[0]):
        return {"ok": False, "error": "no_case_root"}

    case_number = (payload.get("case_number") or payload.get("case_no") or payload.get("caseNumber") or "").strip()
    client_name = (payload.get("client_name") or payload.get("name") or payload.get("client") or "").strip()
    case_type = (payload.get("case_type") or payload.get("type") or "").strip()
    case_stage = (payload.get("case_stage") or "").strip()
    case_reason = (payload.get("case_reason") or "").strip()

    if not case_number or not client_name:
        return {"ok": False, "error": "missing_case_number_or_client_name"}

    full_path = build_full_case_path(
        case_roots[0], case_number, client_name,
        case_type=case_type, case_category=case_category or "一般案件",
        case_stage=case_stage, case_reason=case_reason,
    )
    result = create_folder_structure(full_path, case_category or "一般案件")
    if not result.get("ok"):
        return result

    canonical = translate_local_path_to_canonical(full_path)
    try:
        _osc_exec("UPDATE cases SET folder_path=%s, updated_at=NOW() WHERE id=%s", (canonical, row_id), fetch="none")
    except Exception as e:
        return {"ok": True, "path": full_path, "canonical": canonical, "db_update_error": str(e)}
    return {"ok": True, "path": full_path, "canonical": canonical, "subfolders": result.get("subfolders", [])}


def _osc_removed_research_normalized_expr() -> str:
    return "''"


def _osc_cleanup_removed_research_records() -> int:
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# OSC Meta
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/meta", methods=["GET"])
@login_required
def osc_meta_api():
    # Gather failover status regardless of DB connectivity.
    failover_info = {}
    try:
        from api.db_failover import get_failover_status
        fs = get_failover_status()
        failover_info = {
            "failover_active": fs.get("failover_active", False),
            "syncing": fs.get("syncing", False),
            "remote_ok": fs.get("remote_ok"),
            "active_host": fs.get("active_host", ""),
            "active_port": fs.get("active_port", 0),
        }
    except Exception:
        pass

    try:
        conn, cfg = _osc_web_connect()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT CURRENT_USER() AS current_user_name")
            who = cur.fetchone() or {}
            counts = {}
            for tbl in [
                "cases",
                "clients",
                "meetings",
                "case_todos",
                "case_transactions",
                "document_index",
                "document_templates",
                "document_keywords",
                "document_replacements",
                "expense_defaults",
                "recurring_expenses",
                "quotations",
                "quotation_templates",
                "calendar_events",
                "legal_aid_checklists",
                "laf_lifecycle_log",
                "laf_email_records",
            ]:
                try:
                    cur.execute(f"SELECT COUNT(*) AS c FROM `{tbl}`")
                    counts[tbl] = int((cur.fetchone() or {}).get("c") or 0)
                except Exception:
                    counts[tbl] = None
            return jsonify(
                {
                    "ok": True,
                    "db": {
                        "host": cfg["host"],
                        "port": int(cfg["port"]),
                        "database": cfg["database"],
                        "user": cfg["user"],
                        "current_user": who.get("current_user_name") or "",
                    },
                    "failover": failover_info,
                    "counts": counts,
                }
            )
        finally:
            try:
                cur.close()
            except Exception:
                _log.debug("silent-catch cur.close()", exc_info=True)
            try:
                conn.close()
            except Exception:
                _log.debug("silent-catch conn.close()", exc_info=True)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "failover": failover_info}), 500


# ══════════════════════════════════════════════════════════════════════════════
# Cases CRUD
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/cases", methods=["GET", "POST"])
@login_required
def osc_cases_api():
    translate_local_path_to_canonical = _get_translate_local_path_to_canonical()
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        category = (request.args.get("category") or "").strip()
        case_type = (request.args.get("case_type") or "").strip()
        case_kind = (request.args.get("case_kind") or "").strip()
        status_scope = (request.args.get("status_scope") or "all").strip().lower()
        limit = max(1, min(500, int(request.args.get("limit") or "200")))
        case_type_values = {"刑事", "民事", "行政", "非訟", "消費者債務清理"}
        case_kind_values = {"一般", "一般案件", "法扶", "法律扶助案件", "消費者債務清理", "指定辯護", "指定辯護案件", "無償", "無償案件"}
        if category and category not in {"全部", "all", "ALL"} and not case_type and not case_kind:
            if category in case_type_values:
                case_type = category
            elif category in case_kind_values:
                case_kind = category
            else:
                case_kind = category
        where = []
        params = []
        if q:
            like = f"%{q}%"
            where.append(
                """
                (
                    case_number LIKE %s
                    OR client_name LIKE %s
                    OR court_name LIKE %s
                    OR court_case_no LIKE %s
                    OR laf_case_no LIKE %s
                    OR application_no LIKE %s
                )
                """
            )
            params.extend([like, like, like, like, like, like])
        if case_type and case_type not in {"全部", "all", "ALL"}:
            if case_type == "消費者債務清理":
                where.append("(case_type = %s OR case_category = %s)")
                params.extend([case_type, case_type])
            else:
                where.append("case_type = %s")
                params.append(case_type)
        if case_kind and case_kind not in {"全部", "all", "ALL"}:
            kind_map = {
                "一般": "一般案件",
                "法扶": "法律扶助案件",
                "指定辯護": "指定辯護案件",
                "無償": "無償案件",
            }
            normalized_kind = kind_map.get(case_kind, case_kind)
            if normalized_kind == "消費者債務清理":
                where.append("(case_category = %s OR case_type = %s)")
                params.extend([normalized_kind, normalized_kind])
            elif normalized_kind == "法律扶助案件":
                where.append(
                    """
                    (
                        case_category = %s
                        OR case_reason LIKE %s
                        OR case_reason LIKE %s
                    )
                    """
                )
                params.extend([normalized_kind, "%法扶%", "%法律扶助%"])
            else:
                where.append("case_category = %s")
                params.append(normalized_kind)
        if status_scope in {"working", "default", "open"}:
            where.append(
                """
                (
                    status IS NULL OR status = ''
                    OR status LIKE %s
                    OR status LIKE %s
                    OR status LIKE %s
                    OR LOWER(status) IN ('active', 'open', 'ongoing', 'pending')
                )
                """
            )
            params.extend(["%進行%", "%結案中%", "%待報結%"])
        elif status_scope in {"active", "ongoing"}:
            where.append(
                """
                (
                    status IS NULL OR status = ''
                    OR status LIKE %s
                    OR LOWER(status) IN ('active', 'open', 'ongoing', 'pending')
                )
                """
            )
            params.append("%進行%")
        elif status_scope in {"closing", "closing_case"}:
            where.append("(status LIKE %s OR status LIKE %s)")
            params.extend(["%結案中%", "%待報結%"])
        elif status_scope in {"closed", "archived"}:
            where.append(
                """
                (
                    status LIKE %s
                    OR LOWER(status) IN ('closed', 'close', 'done')
                )
                """
            )
            params.append("%已結案%")
        sql = """
            SELECT id, case_number, client_name, case_category, case_type, case_stage, case_reason,
                   laf_case_no, application_no, court_name, court_case_no, status, notes, folder_path, updated_at, created_date
            FROM cases
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, created_date DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows})

    payload = request.get_json() or {}
    row_id = (payload.get("id") or f"web-{uuid.uuid4().hex[:12]}").strip()
    case_number = (
        payload.get("case_number")
        or payload.get("case_no")
        or payload.get("caseNumber")
        or ""
    ).strip()
    client_name = (
        payload.get("client_name")
        or payload.get("name")
        or payload.get("client")
        or ""
    ).strip()
    if not client_name:
        return jsonify({"ok": False, "error": "client_name required"}), 400
    case_category = _osc_norm_case_category(payload.get("case_category") or payload.get("category") or "")
    cols = [
        "id", "case_number", "client_name", "client_phone", "client_email", "client_id_number",
        "case_category", "case_type", "case_stage", "case_reason",
        "laf_case_no", "application_no", "court_name", "court_case_no", "status", "notes", "folder_path"
    ]
    status_value = (payload.get("status") or "進行中").strip() or "進行中"
    vals = [
        row_id,
        case_number or None,
        client_name,
        (payload.get("client_phone") or "").strip() or None,
        (payload.get("client_email") or "").strip() or None,
        (payload.get("client_id_number") or "").strip() or None,
        case_category or None,
        (payload.get("case_type") or payload.get("type") or "").strip() or None,
        (payload.get("case_stage") or "").strip() or None,
        (payload.get("case_reason") or "").strip() or None,
        (payload.get("laf_case_no") or payload.get("legal_aid_number") or "").strip() or None,
        (payload.get("application_no") or "").strip() or None,
        (payload.get("court_name") or payload.get("court") or "").strip() or None,
        (payload.get("court_case_no") or payload.get("court_case_number") or "").strip() or None,
        status_value,
        (payload.get("notes") or "").strip() or None,
        translate_local_path_to_canonical((payload.get("folder_path") or "").strip()) or None,
    ]
    auto_create_folder = str(payload.get("auto_create_folder") or "").strip().lower() in {"1", "true", "yes", "on"}
    sql_insert = f"INSERT INTO cases ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})"
    try:
        result, _ = _osc_exec(sql_insert, tuple(vals), fetch="none")
        resp = {"ok": True, "result": result, "id": row_id, "mode": "insert"}
        if auto_create_folder:
            folder_resp = _osc_auto_create_folder_for_case(row_id, payload, case_category)
            resp["folder"] = folder_resp
        if _osc_is_closed_case_status(status_value):
            resp["archive"] = _osc_auto_archive_closed_case(row_id)
        return jsonify(resp)
    except Exception as e:
        msg = str(e)
        is_dup = ("1062" in msg) or ("Duplicate entry" in msg)
        if not is_dup:
            return jsonify({"ok": False, "error": msg}), 500

        target = None
        if case_number:
            target, _ = _osc_exec("SELECT id FROM cases WHERE case_number=%s LIMIT 1", (case_number,), fetch="one")
        if not target and row_id:
            target, _ = _osc_exec("SELECT id FROM cases WHERE id=%s LIMIT 1", (row_id,), fetch="one")
        if not target:
            return jsonify({"ok": False, "error": msg}), 500

        update_payload = {
            "client_name": client_name,
            "case_category": case_category or None,
            "case_type": (payload.get("case_type") or payload.get("type") or "").strip() or None,
            "case_stage": (payload.get("case_stage") or "").strip() or None,
            "case_reason": (payload.get("case_reason") or "").strip() or None,
            "laf_case_no": (payload.get("laf_case_no") or payload.get("legal_aid_number") or "").strip() or None,
            "application_no": (payload.get("application_no") or "").strip() or None,
            "court_name": (payload.get("court_name") or payload.get("court") or "").strip() or None,
            "court_case_no": (payload.get("court_case_no") or payload.get("court_case_number") or "").strip() or None,
            "status": (payload.get("status") or "進行中").strip() or "進行中",
            "notes": (payload.get("notes") or "").strip() or None,
            "folder_path": translate_local_path_to_canonical((payload.get("folder_path") or "").strip()) or None,
        }
        if case_number:
            update_payload["case_number"] = case_number
        sets = []
        vals2 = []
        for k, v in update_payload.items():
            sets.append(f"{k}=%s")
            vals2.append(v)
        sets.append("updated_at=NOW()")
        vals2.append(target.get("id"))
        result, _ = _osc_exec(f"UPDATE cases SET {','.join(sets)} WHERE id=%s", tuple(vals2), fetch="none")
        resp = {"ok": True, "result": result, "id": target.get("id"), "mode": "upsert"}
        if _osc_is_closed_case_status(update_payload.get("status") or ""):
            resp["archive"] = _osc_auto_archive_closed_case(str(target.get("id") or ""))
        return jsonify(resp)


@osc_bp.route("/api/osc/cases/<row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_case_detail_api(row_id):
    translate_local_path_to_canonical = _get_translate_local_path_to_canonical()
    row_id = (row_id or "").strip()
    if not row_id:
        return jsonify({"ok": False, "error": "invalid id"}), 400
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM cases WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM cases WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    allowed = [
        "case_number", "client_name", "client_name_en", "client_phone", "client_email", "client_id_number",
        "case_category", "case_type", "case_stage", "case_reason",
        "laf_case_no", "application_no", "court_case_no", "status", "notes", "folder_path",
        "legal_aid_status", "court_case_number", "court_name",
    ]
    sets = []
    vals = []
    for k in allowed:
        if k in payload:
            sets.append(f"{k}=%s")
            v = (payload.get(k) or "").strip() or None
            if k == "case_category":
                v = _osc_norm_case_category(v or "")
            if k == "court_case_no" and not v:
                v = (payload.get("court_case_number") or "").strip() or None
            if k == "laf_case_no" and not v:
                v = (payload.get("legal_aid_number") or "").strip() or None
            if k == "folder_path" and v:
                v = translate_local_path_to_canonical(v) or v
            vals.append(v)
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    sets.append("updated_at=NOW()")
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE cases SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    resp = {"ok": True, "result": result}
    if "status" in payload and _osc_is_closed_case_status(payload.get("status") or ""):
        resp["archive"] = _osc_auto_archive_closed_case(row_id)
    return jsonify(resp)


# ══════════════════════════════════════════════════════════════════════════════
# Case open-folder / create-folder / folder-browser
# ══════════════════════════════════════════════════════════════════════════════


def _check_nas_mount_status() -> bool:
    """檢查 NAS SMB mount 是否還在線（輕量，最多 1s）。"""
    try:
        from api.nas_mount_guard import _is_mounted, _SHARES
        for _share_name, volume_path in _SHARES:
            if _is_mounted(volume_path):
                return True
        return False
    except Exception:
        return False


def _osc_synology_drive_base() -> str:
    """回傳 Synology Drive homes 本機路徑（若未安裝則空字串）。"""
    base = os.path.expanduser("~/Library/CloudStorage/SynologyDrive-homes")
    return base if os.path.isdir(base) else ""


def _osc_is_closed_case_status(status: str) -> bool:
    text = str(status or "").strip()
    lower = text.lower()
    return bool(text and ("結案" in text or "報結" in text or lower in {"closed", "close", "done"}))


def _osc_archive_local_base() -> tuple[str, str]:
    archive_base = _osc_get_closed_archive_base()
    archive_local_candidates = _osc_local_path_candidates(_osc_norm_path(archive_base))
    archive_local = ""
    for candidate in archive_local_candidates:
        if candidate and os.path.exists(candidate):
            archive_local = candidate
            break
    if not archive_local:
        try:
            from api.nas_mount_guard import ensure_nas_mounts
            ensure_nas_mounts()
            for candidate in archive_local_candidates:
                if candidate and os.path.exists(candidate):
                    archive_local = candidate
                    break
        except Exception:
            _log.debug("silent-catch archive mount retry", exc_info=True)
    if (not archive_local) and archive_local_candidates:
        archive_local = archive_local_candidates[0]
    return archive_base, archive_local


def _osc_archive_item_for_row(row: dict) -> dict:
    archive_base, archive_local = _osc_archive_local_base()
    source_raw = (row.get("folder_path") or "").strip() or _osc_guess_case_folder(row.get("case_number") or "")
    source_norm = _osc_norm_path(source_raw)
    local_candidates = _osc_local_path_candidates(source_norm)
    source_local = ""
    for candidate in local_candidates:
        if candidate and os.path.exists(candidate):
            source_local = candidate
            break
    folder_name = os.path.basename(source_local.rstrip("/")) if source_local else os.path.basename(source_norm.rstrip("/"))
    target_local = os.path.join(archive_local, folder_name) if archive_local and folder_name else ""
    target_exists = bool(target_local and os.path.exists(target_local))
    source_exists = bool(source_local and os.path.exists(source_local))
    return {
        "id": row.get("id"),
        "case_number": row.get("case_number") or "",
        "client_name": row.get("client_name") or "",
        "status": row.get("status") or "",
        "archive_base": archive_base,
        "archive_local": archive_local,
        "source_path": source_norm,
        "source_local": source_local,
        "source_exists": source_exists,
        "target_local": target_local,
        "target_exists": target_exists,
        "ready": bool(source_exists and target_local and (not target_exists)),
    }


def _osc_tree_signature(path: str) -> dict:
    files = 0
    dirs = 0
    size = 0
    if not path or not os.path.exists(path):
        return {"exists": False, "files": 0, "dirs": 0, "size": 0}
    if os.path.isfile(path):
        try:
            return {"exists": True, "files": 1, "dirs": 0, "size": int(os.path.getsize(path))}
        except OSError:
            return {"exists": True, "files": 1, "dirs": 0, "size": 0}
    for root, dirnames, filenames in os.walk(path):
        dirs += len(dirnames)
        files += len(filenames)
        for filename in filenames:
            try:
                size += int(os.path.getsize(os.path.join(root, filename)))
            except OSError:
                pass
    return {"exists": True, "files": files, "dirs": dirs, "size": size}


def _osc_copy_to_temp_and_swap(src: str, dst: str, *, force: bool) -> dict:
    """Copy to a hidden temp dir first, then atomically swap the destination.

    This avoids the dangerous sequence "move existing target away, then fail while
    copying source" on Synology Drive / SMB cloud-backed folders.
    """
    parent = os.path.dirname(dst)
    os.makedirs(parent, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    incoming_root = os.path.join(parent, ".archive_incoming")
    os.makedirs(incoming_root, exist_ok=True)
    tmp_dst = os.path.join(incoming_root, f"{os.path.basename(dst.rstrip(os.sep))}_{stamp}_{uuid.uuid4().hex[:8]}")
    src_sig = _osc_tree_signature(src)
    if not src_sig.get("exists"):
        return {"ok": False, "reason": "source_missing"}
    try:
        if os.path.isdir(src):
            copy_timeout = max(30, int(os.environ.get("MAGI_ARCHIVE_COPY_TIMEOUT_SEC", "300") or "300"))
            ditto = shutil.which("ditto")
            if ditto:
                cp = subprocess.run(
                    [ditto, src, tmp_dst],
                    capture_output=True,
                    text=True,
                    timeout=copy_timeout,
                )
                if cp.returncode != 0:
                    shutil.rmtree(tmp_dst, ignore_errors=True)
                    detail = (cp.stderr or cp.stdout or "").strip()[-800:]
                    return {
                        "ok": False,
                        "reason": "copy_failed",
                        "error": detail or f"ditto_exit_{cp.returncode}",
                        "source_signature": src_sig,
                    }
            else:
                shutil.copytree(src, tmp_dst, symlinks=True)
        else:
            os.makedirs(tmp_dst, exist_ok=True)
            tmp_file = os.path.join(tmp_dst, os.path.basename(dst))
            shutil.copy2(src, tmp_file)
            tmp_dst = tmp_file
    except subprocess.TimeoutExpired as e:
        shutil.rmtree(tmp_dst, ignore_errors=True)
        detail = ((e.stderr or "") if isinstance(e.stderr, str) else "")[-800:]
        return {
            "ok": False,
            "reason": "copy_timeout",
            "error": detail or f"copy exceeded {copy_timeout}s",
            "source_signature": src_sig,
        }
    except Exception as e:
        shutil.rmtree(tmp_dst, ignore_errors=True)
        return {"ok": False, "reason": "copy_failed", "error": str(e), "source_signature": src_sig}

    tmp_sig = _osc_tree_signature(tmp_dst)
    if tmp_sig.get("files") != src_sig.get("files") or tmp_sig.get("dirs") != src_sig.get("dirs") or tmp_sig.get("size") != src_sig.get("size"):
        shutil.rmtree(tmp_dst, ignore_errors=True)
        return {
            "ok": False,
            "reason": "copy_verify_failed",
            "source_signature": src_sig,
            "temp_signature": tmp_sig,
        }

    replaced_backup = ""
    try:
        if os.path.exists(dst):
            if not force:
                shutil.rmtree(tmp_dst, ignore_errors=True)
                return {"ok": False, "reason": "target_exists", "target": dst}
            backup_root = os.path.join(parent, ".archive_replaced")
            os.makedirs(backup_root, exist_ok=True)
            base_name = os.path.basename(dst.rstrip(os.sep))
            replaced_backup = os.path.join(backup_root, f"{base_name}_{stamp}")
            shutil.move(dst, replaced_backup)
        shutil.move(tmp_dst, dst)
    except Exception as e:
        if replaced_backup and os.path.exists(replaced_backup) and not os.path.exists(dst):
            try:
                shutil.move(replaced_backup, dst)
                replaced_backup = ""
            except Exception:
                pass
        shutil.rmtree(tmp_dst, ignore_errors=True)
        return {"ok": False, "reason": "swap_failed", "error": str(e), "replaced_backup": replaced_backup}

    cleanup_error = ""
    try:
        if os.path.isdir(src):
            shutil.rmtree(src)
        elif os.path.exists(src):
            os.remove(src)
    except Exception as e:
        cleanup_error = str(e)
    return {
        "ok": True,
        "reason": "replaced" if replaced_backup else "moved",
        "replaced_backup": replaced_backup,
        "source_signature": src_sig,
        "target_signature": _osc_tree_signature(dst),
        "source_cleanup_error": cleanup_error,
    }


def _osc_move_archive_item(it: dict, *, force: bool = False) -> dict:
    cid = str(it.get("id") or "").strip()
    src = str(it.get("source_local") or "").strip()
    dst = str(it.get("target_local") or "").strip()
    case_number = it.get("case_number")
    if not src or not os.path.exists(src):
        return {"ok": False, "id": cid, "case_number": case_number, "reason": "source_missing"}
    if not dst:
        return {"ok": False, "id": cid, "case_number": case_number, "reason": "target_missing"}
    src_abs = os.path.abspath(src)
    dst_abs = os.path.abspath(dst)
    if src_abs == dst_abs:
        return {"ok": True, "id": cid, "case_number": case_number, "from": src, "to": dst, "reason": "already_archived"}
    archive_local = str(it.get("archive_local") or "").strip()
    try:
        already_under_archive = bool(
            archive_local
            and os.path.commonpath([os.path.abspath(archive_local), src_abs]) == os.path.abspath(archive_local)
        )
    except ValueError:
        already_under_archive = False
    if already_under_archive:
        _osc_exec("UPDATE cases SET folder_path=%s, updated_at=NOW() WHERE id=%s", (src, cid), fetch="none")
        return {"ok": True, "id": cid, "case_number": case_number, "from": src, "to": src, "reason": "already_in_archive_base"}
    if os.path.exists(dst) and not force:
        return {"ok": False, "id": cid, "case_number": case_number, "reason": "target_exists", "target": dst}
    try:
        moved = _osc_copy_to_temp_and_swap(src, dst, force=force)
        if not moved.get("ok"):
            return {
                "ok": False,
                "id": cid,
                "case_number": case_number,
                "reason": moved.get("reason") or "move_failed",
                "error": moved.get("error") or "",
                "from": src,
                "to": dst,
                "detail": moved,
            }
        _osc_exec("UPDATE cases SET folder_path=%s, updated_at=NOW() WHERE id=%s", (dst, cid), fetch="none")
        return {
            "ok": True,
            "id": cid,
            "case_number": case_number,
            "from": src,
            "to": dst,
            "reason": moved.get("reason") or "moved",
            "replaced_backup": moved.get("replaced_backup") or "",
            "source_cleanup_error": moved.get("source_cleanup_error") or "",
        }
    except Exception as e:
        return {
            "ok": False,
            "id": cid,
            "case_number": case_number,
            "reason": "move_failed",
            "error": str(e),
            "from": src,
            "to": dst,
        }


def _osc_auto_archive_closed_case(row_id: str, *, force: bool = False) -> dict:
    row, _ = _osc_exec(
        "SELECT id, case_number, client_name, status, folder_path, updated_at FROM cases WHERE id=%s",
        (row_id,),
        fetch="one",
    )
    if not row:
        return {"ok": False, "reason": "case_not_found"}
    if not _osc_is_closed_case_status(row.get("status") or ""):
        return {"ok": True, "skipped": True, "reason": "status_not_closed", "status": row.get("status") or ""}
    item = _osc_archive_item_for_row(row)
    moved = _osc_move_archive_item(item, force=force)
    moved["archive_base"] = item.get("archive_base")
    moved["archive_local"] = item.get("archive_local")
    moved["source_path"] = item.get("source_path")
    return moved


@osc_bp.route("/api/osc/cases/<row_id>/open-folder", methods=["POST"])
@login_required
def osc_case_open_folder_api(row_id):
    """開啟案件資料夾（cross-platform 多候選路徑）。

    2026-05-03 改寫（UX v3 P0）：
    - 不再 server-side `open` / `xdg-open`（在 Tailscale / 遠端瀏覽器情境下
      只會在伺服器電腦開 Finder，律師看不到）
    - 改回多種 candidate 路徑（smb_url / mac_synology / win_unc /
      win_synology），由前端依 navigator.platform 觸發 smb:// /
      file:/// / 複製對話框
    - 仍保留 error_kind（folder_path_empty / case_not_found 等）給前端
      決定是否彈警告
    """
    row_id = (row_id or "").strip()
    row, _ = _osc_exec("SELECT id, case_number, client_name, folder_path FROM cases WHERE id=%s", (row_id,), fetch="one")
    if not row:
        return jsonify({"ok": False, "error_kind": "case_not_found", "message": "找不到案件"}), 404
    folder_path = (row.get("folder_path") or "").strip()
    if not folder_path:
        folder_path = _osc_guess_case_folder(row.get("case_number") or "")
    if not folder_path:
        return jsonify({
            "ok": False,
            "error_kind": "folder_path_empty",
            "message": "案件未設定資料夾路徑，請先用「建立資料夾」按鈕建立預設結構。",
            "client_name": row.get("client_name"),
        }), 200
    norm = _osc_norm_path(folder_path)
    case_info = {"id": row.get("id"), "case_number": row.get("case_number"), "client_name": row.get("client_name")}

    smb_candidates = _osc_smb_candidates(norm)
    mac_synology = _osc_local_path_candidates(norm)  # /Users/.../SynologyDrive-homes/... + /Volumes/...
    win_unc = _osc_windows_unc_candidates(norm)
    win_synology = _osc_windows_synology_candidates(norm)

    return jsonify({
        "ok": True,
        "case": case_info,
        "folder_path": norm,
        # 主要候選（前端依 platform 選用）
        "candidates": {
            "smb_url": smb_candidates,
            "mac_synology": mac_synology,
            "win_unc": win_unc,
            "win_synology": win_synology,
        },
        # 既有 callers 回傳：smb_url / smb_candidates / local_candidates 仍保留以相容
        "smb_url": smb_candidates[0] if smb_candidates else "",
        "smb_candidates": smb_candidates,
        "local_candidates": mac_synology,
        "browser_supported": True,
        "browser_url": f"/api/osc/cases/{row_id}/folder-browser",
    })


@osc_bp.route("/api/osc/cases/<row_id>/create-folder", methods=["POST"])
@login_required
def osc_case_create_folder_api(row_id):
    """建立案件資料夾結構並更新 DB folder_path。"""
    from casper_ecosystem.law_firm_orchestrators.osc.folder_utils import (
        build_full_case_path,
        create_folder_structure,
    )

    translate_local_path_to_canonical = _get_translate_local_path_to_canonical()
    row_id = (row_id or "").strip()
    row, _ = _osc_exec(
        "SELECT id, case_number, client_name, case_category, case_type, case_stage, case_reason, folder_path FROM cases WHERE id=%s",
        (row_id,),
        fetch="one",
    )
    if not row:
        return jsonify({"ok": False, "error": "case_not_found"}), 404

    case_roots = _get_preferred_case_roots()
    if not case_roots:
        return jsonify({"ok": False, "error": "no_case_root_configured"}), 500
    base_path = case_roots[0]
    if not os.path.isdir(base_path):
        return jsonify({"ok": False, "error": f"base_path_not_found: {base_path}"}), 500

    case_number = row.get("case_number") or ""
    client_name = row.get("client_name") or ""
    case_category = row.get("case_category") or "一般案件"
    case_type = row.get("case_type") or ""
    case_stage = row.get("case_stage") or ""
    case_reason = row.get("case_reason") or ""

    if not case_number or not client_name:
        return jsonify({"ok": False, "error": "case_number and client_name are required"}), 400

    full_path = build_full_case_path(
        base_path, case_number, client_name,
        case_type=case_type, case_category=case_category,
        case_stage=case_stage, case_reason=case_reason,
    )

    result = create_folder_structure(full_path, case_category)
    if not result.get("ok"):
        return jsonify(result), 500

    canonical = translate_local_path_to_canonical(full_path)
    _osc_exec("UPDATE cases SET folder_path=%s, updated_at=NOW() WHERE id=%s", (canonical, row_id), fetch="none")

    return jsonify({
        "ok": True,
        "folder_path": full_path,
        "canonical_path": canonical,
        "subfolders": result.get("subfolders", []),
    })


@osc_bp.route("/api/osc/cases/<row_id>/folder-browser", methods=["GET"])
@login_required
def osc_case_folder_browser_api(row_id):
    row_id = (row_id or "").strip()
    row, _ = _osc_exec("SELECT id, case_number, client_name, folder_path FROM cases WHERE id=%s", (row_id,), fetch="one")
    if not row:
        return jsonify({"ok": False, "error": "case_not_found"}), 404
    folder_path = (row.get("folder_path") or "").strip()
    if not folder_path:
        folder_path = _osc_guess_case_folder(row.get("case_number") or "")
    if not folder_path:
        return jsonify({"ok": False, "error": "folder_path_empty"}), 400
    norm = _osc_norm_path(folder_path)
    smb_candidates = _osc_smb_candidates(norm)
    local_candidates = _osc_local_path_candidates(norm)
    local_folder = _osc_resolve_existing_local_path(norm, prefer_dir=True)
    rel = (request.args.get("path") or "").strip().strip("/")
    payload = {
        "ok": True,
        "case": {"id": row.get("id"), "case_number": row.get("case_number"), "client_name": row.get("client_name")},
        "folder_path": norm,
        "local_candidates": local_candidates,
        "smb_candidates": smb_candidates,
        "local_folder": local_folder,
        "folder_exists": bool(local_folder),
    }
    if not local_folder:
        payload["entries"] = []
        payload["current_relative_path"] = ""
        payload["parent_relative_path"] = ""
        payload["error"] = "folder_not_synced"
        return jsonify(payload)
    listing = _osc_folder_entries(local_folder, rel)
    if not listing.get("ok"):
        return jsonify({**payload, **listing}), 400
    payload.update(listing)
    return jsonify(payload)


def _osc_doc_search_terms(keyword: str) -> list[str]:
    raw = str(keyword or "").strip()
    base = [x for x in re.split(r"[\s,，、/|]+", raw) if x]
    variants = {
        "接案通知書": ["接案通知書", "接案通知", "開辦通知", "通知書"],
        "開辦通知書": ["開辦通知書", "接案通知書", "開辦通知", "接案通知"],
        "委任狀": ["委任狀", "委任", "委任契約"],
        "預付酬金領款單": ["預付酬金領款單", "預付酬金", "領款單"],
        "結案酬金領款單": ["結案酬金領款單", "結案酬金", "領款單"],
        "應備資料": ["應備資料", "應備事項", "應備事項表", "補件"],
    }
    terms: list[str] = []
    for token in base or [raw]:
        terms.extend(variants.get(token, [token]))
    return _osc_unique_strings([x.strip().lower() for x in terms if x and x.strip()])


@osc_bp.route("/api/osc/cases/<row_id>/file-search", methods=["GET"])
@login_required
def osc_case_file_search_api(row_id):
    """在案件資料夾內搜尋文件；補足 document_index 尚未索引到的 PDF/文件。"""
    row_id = (row_id or "").strip()
    keyword = (request.args.get("q") or request.args.get("keyword") or "").strip()
    try:
        limit = max(1, min(80, int(request.args.get("limit") or "30")))
    except (TypeError, ValueError):
        limit = 30
    row, _ = _osc_exec(
        "SELECT id, case_number, client_name, folder_path FROM cases WHERE id=%s",
        (row_id,),
        fetch="one",
    )
    if not row:
        return jsonify({"ok": False, "error": "case_not_found", "items": []}), 404

    folder_path = (row.get("folder_path") or "").strip() or _osc_guess_case_folder(row.get("case_number") or "")
    if not folder_path:
        return jsonify({"ok": False, "error": "folder_path_empty", "items": [], "case": row}), 200

    norm = _osc_norm_path(folder_path)
    local_folder = _osc_resolve_existing_local_path(norm, prefer_dir=True)
    local_candidates = _osc_local_path_candidates(norm)
    if not local_folder:
        return jsonify({
            "ok": False,
            "error": "folder_not_synced",
            "items": [],
            "folder_path": norm,
            "local_candidates": local_candidates,
            "case": {"id": row.get("id"), "case_number": row.get("case_number"), "client_name": row.get("client_name")},
        }), 200

    terms = _osc_doc_search_terms(keyword)
    extensions_rank = {
        ".pdf": 0,
        ".docx": 1,
        ".doc": 2,
        ".xlsx": 3,
        ".xls": 4,
        ".jpg": 5,
        ".jpeg": 5,
        ".png": 5,
    }
    skip_names = {".ds_store", "thumbs.db"}
    items: list[dict] = []
    scanned = 0
    max_scan = 5000
    for root, dirs, files in os.walk(local_folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", "node_modules", ".git"}]
        for file_name in files:
            scanned += 1
            if scanned > max_scan:
                break
            if not file_name or file_name.startswith(".") or file_name.lower() in skip_names:
                continue
            hay = f"{file_name} {os.path.relpath(root, local_folder)}".lower()
            if terms and not any(term in hay for term in terms):
                continue
            local_path = os.path.join(root, file_name)
            try:
                st = os.stat(local_path)
            except OSError:
                continue
            rel_path = os.path.relpath(local_path, local_folder)
            ext = os.path.splitext(file_name)[1].lower()
            canonical_path = _osc_norm_path(local_path)
            items.append({
                "file_name": file_name,
                "file_path": canonical_path,
                "relative_path": rel_path,
                "extension": ext,
                "is_pdf": ext == ".pdf",
                "size": st.st_size,
                "size_label": _osc_human_size(st.st_size),
                "modified_date": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "rank": extensions_rank.get(ext, 20),
            })
            if len(items) >= limit * 4:
                break
        if scanned > max_scan or len(items) >= limit * 4:
            break

    items.sort(key=lambda x: (x.get("rank", 20), str(x.get("relative_path") or "").lower()))
    out = items[:limit]
    for it in out:
        it.pop("rank", None)
    return jsonify({
        "ok": True,
        "items": out,
        "query": keyword,
        "terms": terms,
        "scanned": scanned,
        "folder_path": norm,
        "local_folder": local_folder,
        "local_candidates": local_candidates,
        "case": {"id": row.get("id"), "case_number": row.get("case_number"), "client_name": row.get("client_name")},
    })


# ══════════════════════════════════════════════════════════════════════════════
# Case quick-action
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/cases/<row_id>/quick-action", methods=["POST"])
@login_required
def osc_case_quick_action_api(row_id):
    row_id = (row_id or "").strip()
    if not row_id:
        return jsonify({"ok": False, "error": "invalid_id"}), 400
    case, _ = _osc_exec(
        """
        SELECT id, case_number, client_name, case_category, case_reason, case_stage, court_case_no, laf_case_no
        FROM cases
        WHERE id=%s
        """,
        (row_id,),
        fetch="one",
    )
    if not case:
        return jsonify({"ok": False, "error": "case_not_found"}), 404
    payload = request.get_json() or {}
    action = (payload.get("action") or "").strip()
    action_map = {
        "generate_power_of_attorney": "請針對此案件產生委任狀草稿，並列出欄位缺漏供人工確認。",
        "generate_receipt": "請針對此案件產生收據草稿，並列出必填欄位。",
        "closing_overview": "請彙整此案件結案回報需要的進度、文件與風險缺漏，輸出待辦清單。",
        "laf_progress_summary": "請整理此案件目前法扶進度、補件狀態與卡點，輸出下一步建議。",
        "laf_closing_status": "請整理此案件結案狀況（已完成/待補/風險），並列出缺漏文件。",
    }
    if action not in action_map:
        return jsonify({"ok": False, "error": "unsupported_action"}), 400
    native_actions = {"closing_overview", "laf_progress_summary", "laf_closing_status"}
    if action in native_actions:
        reply_text = _build_quick_action_native_reply(action, case)
        return jsonify(
            {
                "ok": True,
                "action": action,
                "case": case,
                "reply": reply_text,
                "reply_html": _format_web_reply_html(reply_text),
                "native": True,
                "message": "已完成案件盤點。",
            }
        )
    prompt = (
        f"{action_map[action]}\n\n"
        f"案件編號: {case.get('case_number') or ''}\n"
        f"當事人: {case.get('client_name') or ''}\n"
        f"案件種類: {case.get('case_category') or ''}\n"
        f"案由: {case.get('case_reason') or ''}\n"
        f"審級/階段: {case.get('case_stage') or ''}\n"
        f"法院案號: {case.get('court_case_no') or ''}\n"
        f"法扶案號: {case.get('laf_case_no') or ''}\n"
    )
    try:
        orchestrator = _get_orchestrator()
        _normalize_output_text = _get_normalize_output_text()
        user_id, role = _current_user_context()
        reply = orchestrator.process_message(
            user_id=user_id,
            message=prompt,
            platform="WEB",
            role=role,
        )
        if _normalize_output_text:
            reply = _normalize_output_text(str(reply or ""), platform="WEB")
        reply_text = str(reply or "")
        return jsonify(
            {
                "ok": True,
                "action": action,
                "case": case,
                "reply": reply_text,
                "reply_html": _format_web_reply_html(reply_text),
            }
        )
    except Exception as e:
        _log.warning("quick action MAGI fallback used: action=%s case=%s error=%s", action, row_id, e)
        reply_text = _build_quick_action_native_reply(action, case)
        return jsonify(
            {
                "ok": True,
                "action": action,
                "case": case,
                "reply": reply_text,
                "reply_html": _format_web_reply_html(reply_text),
                "fallback": True,
                "warning": "MAGI 推論暫時不可用，已改用本機案件資料盤點。",
                "message": "已完成案件盤點（本機資料模式）。",
            }
        )


# ══════════════════════════════════════════════════════════════════════════════
# Client workbench
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/clients/<row_id>/workbench", methods=["GET"])
@login_required
def osc_client_workbench_api(row_id):
    row_id = (row_id or "").strip()
    client, _ = _osc_exec("SELECT * FROM clients WHERE id=%s", (row_id,), fetch="one")
    if not client:
        return jsonify({"ok": False, "error": "client_not_found"}), 404
    name = (client.get("name") or "").strip()
    like = f"%{name}%"
    cases, _ = _osc_exec(
        """
        SELECT id, case_number, client_name, case_category, case_type, case_stage, case_reason, status, folder_path,
               laf_case_no, application_no, court_case_no, legal_aid_status, updated_at
        FROM cases
        WHERE client_name LIKE %s
        ORDER BY updated_at DESC, created_date DESC
        LIMIT 200
        """,
        (like,),
        fetch="all",
    )
    case_numbers = [str(c.get("case_number") or "").strip() for c in cases if (c.get("case_number") or "").strip()]
    todos = []
    meetings = []
    legal_aid_checklist = []
    case_checklist = []
    lifecycle = []
    opponents = []
    pdf_generation_log = []
    if case_numbers:
        ph = ",".join(["%s"] * len(case_numbers))
        todos, _ = _osc_exec(
            f"""
            SELECT id, case_number, client_name, todo_type, todo_date, todo_time, description, status, source_file, created_date
            FROM case_todos
            WHERE case_number IN ({ph})
            ORDER BY todo_date DESC, id DESC
            LIMIT 500
            """,
            tuple(case_numbers),
            fetch="all",
        )
        meetings, _ = _osc_exec(
            f"""
            SELECT id, case_number, client_name, type, datetime, duration, location, notes, status, reminder, reminder_time
            FROM meetings
            WHERE case_number IN ({ph})
            ORDER BY datetime DESC, id DESC
            LIMIT 500
            """,
            tuple(case_numbers),
            fetch="all",
        )
        legal_aid_checklist, _ = _osc_exec(
            f"""
            SELECT id, case_number, item_key, item_label, status, notes, last_updated
            FROM legal_aid_checklists
            WHERE case_number IN ({ph})
            ORDER BY last_updated DESC, id DESC
            LIMIT 500
            """,
            tuple(case_numbers),
            fetch="all",
        )
        case_checklist, _ = _osc_exec(
            f"""
            SELECT id, case_number, item_label, status, notes, is_active
            FROM case_checklists
            WHERE case_number IN ({ph})
            ORDER BY id DESC
            LIMIT 500
            """,
            tuple(case_numbers),
            fetch="all",
        )
        lifecycle, _ = _osc_exec(
            f"""
            SELECT id, case_number, event_type, status, created_at, completed_at, event_data
            FROM laf_lifecycle_log
            WHERE case_number IN ({ph})
            ORDER BY created_at DESC, id DESC
            LIMIT 500
            """,
            tuple(case_numbers),
            fetch="all",
        )
        opponents, _ = _osc_exec(
            f"""
            SELECT id, case_number, name, address, created_date, updated_date, is_active
            FROM opponents
            WHERE case_number IN ({ph})
            ORDER BY updated_date DESC, id DESC
            LIMIT 500
            """,
            tuple(case_numbers),
            fetch="all",
        )
        pdf_generation_log, _ = _osc_exec(
            f"""
            SELECT id, case_number, file_name, log_timestamp, status, error_message
            FROM pdf_generation_log
            WHERE case_number IN ({ph})
            ORDER BY log_timestamp DESC, id DESC
            LIMIT 500
            """,
            tuple(case_numbers),
            fetch="all",
        )
    return jsonify(
        {
            "ok": True,
            "client": client,
            "cases": cases,
            "todos": todos,
            "meetings": meetings,
            "legal_aid_checklist": legal_aid_checklist,
            "case_checklist": case_checklist,
            "laf_progress": lifecycle,
            "opponents": opponents,
            "pdf_generation_log": pdf_generation_log,
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# Case workbench
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/cases/<row_id>/workbench", methods=["GET"])
@login_required
def osc_case_workbench_api(row_id):
    case, _ = _osc_exec("SELECT * FROM cases WHERE id=%s", ((row_id or "").strip(),), fetch="one")
    if not case:
        return jsonify({"ok": False, "error": "case_not_found"}), 404
    case_number = (case.get("case_number") or "").strip()
    todos, _ = _osc_exec(
        """
        SELECT id, case_number, client_name, todo_type, todo_date, todo_time, description, status, source_file, created_date, completed_date
        FROM case_todos WHERE case_number=%s ORDER BY todo_date DESC, id DESC LIMIT 800
        """,
        (case_number,),
        fetch="all",
    )
    meetings, _ = _osc_exec(
        """
        SELECT id, case_number, client_name, type, datetime, duration, location, notes, status
        FROM meetings WHERE case_number=%s ORDER BY datetime DESC, id DESC LIMIT 800
        """,
        (case_number,),
        fetch="all",
    )
    legal_aid, _ = _osc_exec(
        """
        SELECT id, case_number, item_key, item_label, status, notes, last_updated
        FROM legal_aid_checklists WHERE case_number=%s ORDER BY last_updated DESC, id DESC LIMIT 1000
        """,
        (case_number,),
        fetch="all",
    )
    lifecycle, _ = _osc_exec(
        """
        SELECT id, case_number, event_type, status, created_at, completed_at, event_data
        FROM laf_lifecycle_log WHERE case_number=%s ORDER BY created_at DESC, id DESC LIMIT 1000
        """,
        (case_number,),
        fetch="all",
    )
    docs, _ = _osc_exec(
        """
        SELECT id, case_number, file_name, file_path, subfolder_name, party, reason, modified_date
        FROM document_index WHERE case_number=%s ORDER BY modified_date DESC, id DESC LIMIT 1000
        """,
        (case_number,),
        fetch="all",
    )
    opponents, _ = _osc_exec(
        """
        SELECT id, case_number, name, address, created_date, updated_date, is_active
        FROM opponents WHERE case_number=%s ORDER BY updated_date DESC, id DESC LIMIT 300
        """,
        (case_number,),
        fetch="all",
    )
    pdf_generation_log, _ = _osc_exec(
        """
        SELECT id, case_number, file_name, log_timestamp, status, error_message
        FROM pdf_generation_log WHERE case_number=%s ORDER BY log_timestamp DESC, id DESC LIMIT 300
        """,
        (case_number,),
        fetch="all",
    )
    stats = {
        "todo_total": len(todos),
        "todo_pending": len([t for t in todos if not _osc_is_todo_done_status(t.get("status") or "")]),
        "todo_completed": len([t for t in todos if _osc_is_todo_done_status(t.get("status") or "")]),
        "meeting_total": len(meetings),
        "laf_items": len(legal_aid),
        "docs_indexed": len(docs),
        "opponents_total": len(opponents),
        "pdf_logs_total": len(pdf_generation_log),
    }
    return jsonify(
        {
            "ok": True,
            "case": case,
            "stats": stats,
            "todos": todos,
            "meetings": meetings,
            "legal_aid_checklist": legal_aid,
            "laf_progress": lifecycle,
            "documents": docs,
            "opponents": opponents,
            "pdf_generation_log": pdf_generation_log,
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/dashboard", methods=["GET"])
@login_required
def osc_dashboard_api():
    start_date, end_date = _osc_accounting_window()
    active_cases_row, _ = _osc_exec(
        """
        SELECT COUNT(*) AS c FROM cases
        WHERE status NOT IN ('已結案', '已結案，待報結') OR status IS NULL OR status=''
        """,
        fetch="one",
    )
    legal_aid_cases_row, _ = _osc_exec(
        """
        SELECT COUNT(*) AS c FROM cases
        WHERE (case_category='法律扶助案件' OR case_reason LIKE '%法扶%' OR case_reason LIKE '%法律扶助%')
          AND (status NOT IN ('已結案', '已結案，待報結') OR status IS NULL OR status='')
        """,
        fetch="one",
    )
    monthly_revenue_row, _ = _osc_exec(
        "SELECT COALESCE(SUM(amount),0) AS total FROM case_transactions WHERE date >= %s AND date <= %s AND type='收入'",
        (start_date, end_date),
        fetch="one",
    )
    monthly_expense_row, _ = _osc_exec(
        "SELECT COALESCE(SUM(amount),0) AS total FROM case_transactions WHERE date >= %s AND date <= %s AND type='支出'",
        (start_date, end_date),
        fetch="one",
    )
    closed_regular_row, _ = _osc_exec(
        """
        SELECT COUNT(*) AS c FROM cases
        WHERE status IN ('已結案', '已結案，待報結')
          AND NOT (case_category='法律扶助案件' OR case_reason LIKE '%法扶%' OR case_reason LIKE '%法律扶助%')
        """,
        fetch="one",
    )
    closed_laf_row, _ = _osc_exec(
        """
        SELECT COUNT(*) AS c FROM cases
        WHERE status IN ('已結案', '已結案，待報結')
          AND (case_category='法律扶助案件' OR case_reason LIKE '%法扶%' OR case_reason LIKE '%法律扶助%')
        """,
        fetch="one",
    )
    recent_cases, _ = _osc_exec(
        """
        SELECT id, case_number, client_name, case_category, case_type, case_stage, case_reason, status, updated_at, created_date
        FROM cases
        ORDER BY updated_at DESC, created_date DESC
        LIMIT 12
        """,
        fetch="all",
    )
    pending_todos, _ = _osc_exec(
        """
        SELECT id, case_number, client_name, todo_type, todo_date, todo_time, description, status
        FROM case_todos
        WHERE status IS NULL OR status='' OR LOWER(status) NOT IN ('completed', 'done', '已完成', '完成', 'cancelled', 'canceled', '取消')
        ORDER BY COALESCE(todo_date, CURDATE()) ASC, id DESC
        LIMIT 20
        """,
        fetch="all",
    )
    upcoming_calendar, _ = _osc_exec(
        """
        SELECT id, case_number, title, start_date, end_date, description, location, color, is_all_day
        FROM calendar_events
        WHERE start_date >= %s
        ORDER BY start_date ASC, id ASC
        LIMIT 20
        """,
        (date.today(),),
        fetch="all",
    )
    recent_activity, _ = _osc_exec(
        """
        SELECT id, action, entity_type, entity_id, details, user, timestamp
        FROM activity_logs
        ORDER BY timestamp DESC, id DESC
        LIMIT 20
        """,
        fetch="all",
    )
    recent_pdf_logs, _ = _osc_exec(
        """
        SELECT id, case_number, file_name, log_timestamp, status, error_message
        FROM pdf_generation_log
        ORDER BY log_timestamp DESC, id DESC
        LIMIT 20
        """,
        fetch="all",
    )
    return jsonify(
        {
            "ok": True,
            "window": {"start_date": str(start_date), "end_date": str(end_date)},
            "stats": {
                "active_cases": int((active_cases_row or {}).get("c") or 0),
                "legal_aid_cases": int((legal_aid_cases_row or {}).get("c") or 0),
                "monthly_revenue": float((monthly_revenue_row or {}).get("total") or 0),
                "monthly_expense": float((monthly_expense_row or {}).get("total") or 0),
                "closed_regular": int((closed_regular_row or {}).get("c") or 0),
                "closed_legal_aid": int((closed_laf_row or {}).get("c") or 0),
            },
            "recent_cases": recent_cases or [],
            "pending_todos": pending_todos or [],
            "upcoming_calendar": upcoming_calendar or [],
            "recent_activity": recent_activity or [],
            "recent_pdf_logs": recent_pdf_logs or [],
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# Case reason templates
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/case-reason-templates", methods=["GET", "POST"])
@login_required
def osc_case_reason_templates_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        case_type = (request.args.get("case_type") or "").strip()
        common_only = _osc_truthy(request.args.get("common_only"))
        limit = max(1, min(2000, int(request.args.get("limit") or "300")))
        where = ["1=1"]
        params = []
        if case_type:
            where.append("case_type=%s")
            params.append(case_type)
        if common_only:
            where.append("is_common=1")
        if q:
            like = f"%{q}%"
            where.append("(case_type LIKE %s OR reason LIKE %s)")
            params.extend([like, like])
        params.append(limit)
        rows, _ = _osc_exec(
            f"""
            SELECT id, case_type, reason, is_common, created_date
            FROM case_reason_templates
            WHERE {' AND '.join(where)}
            ORDER BY is_common DESC, case_type ASC, id DESC
            LIMIT %s
            """,
            tuple(params),
            fetch="all",
        )
        return jsonify({"ok": True, "items": rows or []})
    payload = request.get_json() or {}
    case_type = _osc_text(payload.get("case_type"))
    reason = _osc_text(payload.get("reason"))
    if not case_type or not reason:
        return jsonify({"ok": False, "error": "case_type/reason required"}), 400
    is_common = 1 if _osc_truthy(payload.get("is_common")) else 0
    result, _ = _osc_exec(
        """
        INSERT INTO case_reason_templates (case_type, reason, is_common)
        VALUES (%s,%s,%s)
        ON DUPLICATE KEY UPDATE is_common=VALUES(is_common)
        """,
        (case_type, reason, is_common),
        fetch="none",
    )
    _osc_log_activity("case_reason_template:save", "case_reason_templates", f"{case_type}:{reason}", payload)
    return jsonify({"ok": True, "result": result})


@osc_bp.route("/api/osc/case-reason-templates/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_case_reason_template_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM case_reason_templates WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM case_reason_templates WHERE id=%s", (row_id,), fetch="none")
        _osc_log_activity("case_reason_template:delete", "case_reason_templates", str(row_id))
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    sets, vals = [], []
    for key in ["case_type", "reason", "is_common"]:
        if key not in payload:
            continue
        sets.append(f"{key}=%s")
        if key == "is_common":
            vals.append(1 if _osc_truthy(payload.get(key)) else 0)
        else:
            vals.append(_osc_text(payload.get(key)))
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE case_reason_templates SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    _osc_log_activity("case_reason_template:update", "case_reason_templates", str(row_id), payload)
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Activity logs
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/activity-logs", methods=["GET", "POST"])
@login_required
def osc_activity_logs_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        entity_type = (request.args.get("entity_type") or "").strip()
        user_name = (request.args.get("user") or "").strip()
        limit = max(1, min(2000, int(request.args.get("limit") or "300")))
        sql = "SELECT id, action, entity_type, entity_id, details, user, timestamp FROM activity_logs WHERE 1=1 "
        params = []
        if entity_type:
            sql += "AND entity_type=%s "
            params.append(entity_type)
        if user_name:
            sql += "AND user=%s "
            params.append(user_name)
        if q:
            like = f"%{q}%"
            sql += "AND (action LIKE %s OR entity_type LIKE %s OR entity_id LIKE %s OR details LIKE %s OR user LIKE %s) "
            params.extend([like, like, like, like, like])
        sql += "ORDER BY timestamp DESC, id DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows or []})
    payload = request.get_json() or {}
    action = _osc_text(payload.get("action"))
    if not action:
        return jsonify({"ok": False, "error": "action required"}), 400
    result, _ = _osc_exec(
        "INSERT INTO activity_logs (action, entity_type, entity_id, details, user) VALUES (%s,%s,%s,%s,%s)",
        (
            action,
            _osc_text(payload.get("entity_type")),
            _osc_text(payload.get("entity_id")),
            _osc_text(payload.get("details")),
            _osc_text(payload.get("user")) or _osc_current_actor(),
        ),
        fetch="none",
    )
    return jsonify({"ok": True, "result": result})


@osc_bp.route("/api/osc/activity-logs/<int:row_id>", methods=["GET", "DELETE"])
@login_required
def osc_activity_log_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM activity_logs WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    result, _ = _osc_exec("DELETE FROM activity_logs WHERE id=%s", (row_id,), fetch="none")
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# User settings
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/user-settings", methods=["GET", "POST"])
@login_required
def osc_user_settings_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        hostname = (request.args.get("hostname") or "").strip()
        limit = max(1, min(2000, int(request.args.get("limit") or "300")))
        sql = "SELECT id, hostname, setting_key, setting_value, last_updated FROM user_settings WHERE 1=1 "
        params = []
        if hostname:
            sql += "AND hostname=%s "
            params.append(hostname)
        if q:
            like = f"%{q}%"
            sql += "AND (hostname LIKE %s OR setting_key LIKE %s OR setting_value LIKE %s) "
            params.extend([like, like, like])
        sql += "ORDER BY hostname ASC, setting_key ASC, id DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows or []})
    payload = request.get_json() or {}
    hostname = _osc_text(payload.get("hostname"))
    setting_key = _osc_text(payload.get("setting_key"))
    if not hostname or not setting_key:
        return jsonify({"ok": False, "error": "hostname/setting_key required"}), 400
    result, _ = _osc_exec(
        """
        INSERT INTO user_settings (hostname, setting_key, setting_value)
        VALUES (%s,%s,%s)
        ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)
        """,
        (hostname, setting_key, _osc_text(payload.get("setting_value"))),
        fetch="none",
    )
    _osc_log_activity("user_setting:save", "user_settings", f"{hostname}:{setting_key}", payload)
    return jsonify({"ok": True, "result": result})


@osc_bp.route("/api/osc/user-settings/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_user_setting_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM user_settings WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM user_settings WHERE id=%s", (row_id,), fetch="none")
        _osc_log_activity("user_setting:delete", "user_settings", str(row_id))
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    sets, vals = [], []
    for key in ["hostname", "setting_key", "setting_value"]:
        if key not in payload:
            continue
        sets.append(f"{key}=%s")
        vals.append(_osc_text(payload.get(key)))
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE user_settings SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    _osc_log_activity("user_setting:update", "user_settings", str(row_id), payload)
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Memory keywords
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/memory-keywords", methods=["GET", "POST"])
@login_required
def osc_memory_keywords_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        case_number = (request.args.get("case_number") or "").strip()
        limit = max(1, min(2000, int(request.args.get("limit") or "300")))
        sql = "SELECT case_number, hotkey, name, value FROM memory_keywords WHERE 1=1 "
        params = []
        if case_number:
            sql += "AND case_number=%s "
            params.append(case_number)
        if q:
            like = f"%{q}%"
            sql += "AND (case_number LIKE %s OR hotkey LIKE %s OR name LIKE %s OR value LIKE %s) "
            params.extend([like, like, like, like])
        sql += "ORDER BY case_number ASC, hotkey ASC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows or []})
    payload = request.get_json() or {}
    case_number = _osc_text(payload.get("case_number"))
    hotkey = _osc_text(payload.get("hotkey"))
    if not case_number or not hotkey:
        return jsonify({"ok": False, "error": "case_number/hotkey required"}), 400
    result, _ = _osc_exec(
        """
        INSERT INTO memory_keywords (case_number, hotkey, name, value)
        VALUES (%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE name=VALUES(name), value=VALUES(value)
        """,
        (case_number, hotkey, _osc_text(payload.get("name")), _osc_text(payload.get("value"))),
        fetch="none",
    )
    _osc_log_activity("memory_keyword:save", "memory_keywords", f"{case_number}:{hotkey}", payload)
    return jsonify({"ok": True, "result": result})


@osc_bp.route("/api/osc/memory-keywords/<path:case_number>/<path:hotkey>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_memory_keyword_detail_api(case_number, hotkey):
    if request.method == "GET":
        row, _ = _osc_exec(
            "SELECT case_number, hotkey, name, value FROM memory_keywords WHERE case_number=%s AND hotkey=%s",
            (case_number, hotkey),
            fetch="one",
        )
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM memory_keywords WHERE case_number=%s AND hotkey=%s", (case_number, hotkey), fetch="none")
        _osc_log_activity("memory_keyword:delete", "memory_keywords", f"{case_number}:{hotkey}")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    sets, vals = [], []
    for key in ["name", "value"]:
        if key not in payload:
            continue
        sets.append(f"{key}=%s")
        vals.append(_osc_text(payload.get(key)))
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.extend([case_number, hotkey])
    result, _ = _osc_exec(
        f"UPDATE memory_keywords SET {','.join(sets)} WHERE case_number=%s AND hotkey=%s",
        tuple(vals),
        fetch="none",
    )
    _osc_log_activity("memory_keyword:update", "memory_keywords", f"{case_number}:{hotkey}", payload)
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Opponents
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/opponents", methods=["GET", "POST"])
@login_required
def osc_opponents_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        case_number = (request.args.get("case_number") or "").strip()
        active_only = _osc_truthy(request.args.get("active_only"))
        limit = max(1, min(2000, int(request.args.get("limit") or "300")))
        sql = "SELECT id, case_number, name, address, created_date, updated_date, is_active FROM opponents WHERE 1=1 "
        params = []
        if case_number:
            sql += "AND case_number=%s "
            params.append(case_number)
        if active_only:
            sql += "AND is_active=1 "
        if q:
            like = f"%{q}%"
            sql += "AND (case_number LIKE %s OR name LIKE %s OR address LIKE %s) "
            params.extend([like, like, like])
        sql += "ORDER BY updated_date DESC, id DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows or []})
    payload = request.get_json() or {}
    case_number = _osc_text(payload.get("case_number"))
    name = _osc_text(payload.get("name"))
    if not case_number or not name:
        return jsonify({"ok": False, "error": "case_number/name required"}), 400
    result, _ = _osc_exec(
        """
        INSERT INTO opponents (case_number, name, address, is_active)
        VALUES (%s,%s,%s,%s)
        """,
        (case_number, name, _osc_text(payload.get("address")), 1 if _osc_truthy(payload.get("is_active", 1)) else 0),
        fetch="none",
    )
    _osc_log_activity("opponent:create", "opponents", case_number, payload)
    return jsonify({"ok": True, "result": result})


@osc_bp.route("/api/osc/opponents/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_opponent_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM opponents WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM opponents WHERE id=%s", (row_id,), fetch="none")
        _osc_log_activity("opponent:delete", "opponents", str(row_id))
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    sets, vals = [], []
    for key in ["case_number", "name", "address", "is_active"]:
        if key not in payload:
            continue
        sets.append(f"{key}=%s")
        if key == "is_active":
            vals.append(1 if _osc_truthy(payload.get(key)) else 0)
        else:
            vals.append(_osc_text(payload.get(key)))
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE opponents SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    _osc_log_activity("opponent:update", "opponents", str(row_id), payload)
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# PDF generation log
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/pdf-generation-log", methods=["GET"])
@login_required
def osc_pdf_generation_log_api():
    q = (request.args.get("q") or "").strip()
    case_number = (request.args.get("case_number") or "").strip()
    status = (request.args.get("status") or "").strip()
    limit = max(1, min(2000, int(request.args.get("limit") or "300")))
    sql = "SELECT id, case_number, file_name, log_timestamp, status, error_message FROM pdf_generation_log WHERE 1=1 "
    params = []
    if case_number:
        sql += "AND case_number=%s "
        params.append(case_number)
    if status:
        sql += "AND status=%s "
        params.append(status)
    if q:
        like = f"%{q}%"
        sql += "AND (case_number LIKE %s OR file_name LIKE %s OR status LIKE %s OR error_message LIKE %s) "
        params.extend([like, like, like, like])
    sql += "ORDER BY log_timestamp DESC, id DESC LIMIT %s"
    params.append(limit)
    rows, _ = _osc_exec(sql, tuple(params), fetch="all")
    return jsonify({"ok": True, "items": rows or []})


@osc_bp.route("/api/osc/pdf-generation-log/<int:row_id>", methods=["GET", "DELETE"])
@login_required
def osc_pdf_generation_log_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM pdf_generation_log WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    result, _ = _osc_exec("DELETE FROM pdf_generation_log WHERE id=%s", (row_id,), fetch="none")
    _osc_log_activity("pdf_log:delete", "pdf_generation_log", str(row_id))
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Drafts
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/drafts/meta", methods=["GET"])
@login_required
def osc_drafts_meta_api():
    TEXT_PRIMARY_MODEL = _get_text_primary_model()
    provider = _osc_get_setting_value("ai_draft_provider", "casper") or "casper"
    model = _osc_get_setting_value("ai_draft_ollama_model", TEXT_PRIMARY_MODEL) or TEXT_PRIMARY_MODEL
    _default_chat_url = os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:11434")
    ollama_url = _osc_get_setting_value("ollama_url", _default_chat_url) or _default_chat_url
    custom_template = _osc_get_setting_value("draft_prompt_template", "")
    allow_cloud_models = str(os.environ.get("MAGI_ALLOW_CLOUD_MODELS", "0") or "").strip().lower() in {"1", "true", "yes", "on"}
    effective_provider = provider
    if provider == "gemini" and not allow_cloud_models:
        effective_provider = "casper"
    return jsonify(
        {
            "ok": True,
            "meta": {
                "enabled": _osc_draft_enabled_flag(),
                "provider": provider,
                "effective_provider": effective_provider,
                "ollama_model": model,
                "ollama_url": ollama_url,
                "allow_cloud_models": allow_cloud_models,
                "template_source": "custom" if custom_template.strip() else "default",
                "has_custom_template": bool(custom_template.strip()),
                "template_length": len(custom_template.strip() or _OSC_DRAFT_PROMPT_TEMPLATE),
            },
            "doc_types": _OSC_DRAFT_DOC_TYPES,
        }
    )


@osc_bp.route("/api/osc/drafts/generate", methods=["POST"])
@login_required
def osc_drafts_generate_api():
    TEXT_PRIMARY_MODEL = _get_text_primary_model()
    payload = request.get_json() or {}
    ctx = _osc_build_draft_context(payload)
    doc_type = str(ctx.get("doc_type") or "").strip()
    case_facts = str(ctx.get("case_facts") or "").strip()
    if not doc_type:
        return jsonify({"ok": False, "error": "doc_type required"}), 400
    if not case_facts:
        return jsonify({"ok": False, "error": "case_facts required"}), 400

    prompt = str(ctx.get("prompt") or "").strip()
    provider = str(payload.get("provider") or _osc_get_setting_value("ai_draft_provider", "casper") or "casper").strip().lower()
    ollama_model = str(payload.get("ollama_model") or _osc_get_setting_value("ai_draft_ollama_model", TEXT_PRIMARY_MODEL) or TEXT_PRIMARY_MODEL).strip()
    _default_chat = os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:11434")
    ollama_url = str(payload.get("ollama_url") or _osc_get_setting_value("ollama_url", _default_chat) or _default_chat).strip()
    dry_run = _osc_truthy(payload.get("dry_run") or payload.get("preview_only"))

    if dry_run:
        return jsonify(
            {
                "ok": True,
                "dry_run": True,
                "provider": provider,
                "ollama_model": ollama_model,
                "prompt_preview": prompt,
                "warnings": ctx.get("warnings") or [],
                "case": ctx.get("case") or {},
                "selected_documents": ctx.get("selected_documents") or [],
                "selected_insights": ctx.get("selected_insights") or [],
                "suggested_filename": ctx.get("suggested_filename") or "",
            }
        )

    try:
        actual_provider = provider
        actual_model = ""
        if provider == "ollama":
            draft_text = _osc_generate_draft_with_ollama(prompt, ollama_model, ollama_url)
            actual_model = ollama_model
        elif provider == "gemini":
            draft_text, actual_model = _osc_generate_draft_with_gemini(prompt)
            if actual_model == "casper":
                actual_provider = "casper"
                actual_model = ""
        else:
            draft_text = _osc_generate_draft_with_casper(prompt)
        cleaned = _osc_clean_draft_output(draft_text)
        _osc_log_activity(
            "draft:generate",
            "drafts",
            str((ctx.get("case") or {}).get("id") or ctx.get("case_number") or ""),
            {
                "provider": actual_provider,
                "model": actual_model,
                "doc_type": ctx.get("doc_type"),
                "case_number": ctx.get("case_number"),
                "documents": len(ctx.get("selected_documents") or []),
                "insights": len(ctx.get("selected_insights") or []),
            },
        )
        return jsonify(
            {
                "ok": True,
                "provider": actual_provider,
                "model": actual_model,
                "draft_text": cleaned,
                "prompt_preview": prompt,
                "warnings": ctx.get("warnings") or [],
                "case": ctx.get("case") or {},
                "selected_documents": ctx.get("selected_documents") or [],
                "selected_insights": ctx.get("selected_insights") or [],
                "suggested_filename": ctx.get("suggested_filename") or "",
                "export_title": ctx.get("export_title") or "書狀草稿",
            }
        )
    except Exception as e:
        _osc_log_activity(
            "draft:generate_error",
            "drafts",
            str((ctx.get("case") or {}).get("id") or ctx.get("case_number") or ""),
            {"provider": provider, "error": str(e)},
        )
        return jsonify({"ok": False, "error": str(e), "prompt_preview": prompt, "warnings": ctx.get("warnings") or []}), 500


@osc_bp.route("/api/osc/drafts/export", methods=["POST"])
@login_required
def osc_drafts_export_api():
    payload = request.get_json() or {}
    text = str(payload.get("draft_text") or payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "draft_text required"}), 400
    title = str(payload.get("title") or payload.get("doc_type") or "書狀草稿").strip() or "書狀草稿"
    case_number = str(payload.get("case_number") or "").strip()
    suggested = str(payload.get("suggested_filename") or "").strip()
    if not suggested:
        pieces = [title, case_number or "未命名"]
        suggested = "_".join(p for p in pieces if p)
    exported = _export_osc_form_files(title, text, suggested)
    status = "success" if exported.get("success") else "failed"
    if exported.get("success") and exported.get("errors"):
        status = "partial_success"
    preferred = exported.get("export") or {}
    error_text = ""
    if exported.get("errors"):
        error_text = "; ".join(str(x.get("error") or "") for x in exported.get("errors") or [] if str(x.get("error") or "").strip())
    try:
        _osc_exec(
            "INSERT INTO pdf_generation_log (case_number, file_name, status, error_message) VALUES (%s,%s,%s,%s)",
            (
                case_number or "draft",
                str(preferred.get("filename") or suggested or title),
                status,
                error_text or None,
            ),
            fetch="none",
        )
    except Exception as e:
        logger.warning("draft export log write failed: %s", e)
    _osc_log_activity(
        "draft:export",
        "drafts",
        case_number or "draft",
        {"title": title, "status": status, "filename": str(preferred.get("filename") or suggested or title)},
    )
    http_status = 200 if exported.get("success") else 500
    return jsonify({"ok": bool(exported.get("success")), **exported, "status": status}), http_status


# ══════════════════════════════════════════════════════════════════════════════
# Documents
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/documents", methods=["GET"])
@login_required
def osc_documents_api():
    q = (request.args.get("q") or "").strip().lower()
    case_number = (request.args.get("case_number") or "").strip()
    kind = (request.args.get("kind") or "all").strip()
    limit = max(1, min(1000, int(request.args.get("limit") or "300")))

    items = []
    di_limit = max(200, limit * 3)
    cd_limit = max(200, limit * 3)

    di_where = []
    di_params = []
    if case_number:
        di_where.append("case_number = %s")
        di_params.append(case_number)
    if q:
        like = f"%{q}%"
        di_where.append("(file_name LIKE %s OR file_path LIKE %s OR reason LIKE %s OR party LIKE %s OR subfolder_name LIKE %s)")
        di_params.extend([like, like, like, like, like])
    di_sql = """
        SELECT id, case_number, file_name, file_path, subfolder_name, reason, party, modified_date
        FROM document_index
    """
    if di_where:
        di_sql += " WHERE " + " AND ".join(di_where)
    di_sql += " ORDER BY modified_date DESC, id DESC LIMIT %s"
    di_params.append(di_limit)
    di_rows, _ = _osc_exec(di_sql, tuple(di_params), fetch="all")
    for r in di_rows:
        blob = " ".join(
            [
                str(r.get("file_name") or ""),
                str(r.get("subfolder_name") or ""),
                str(r.get("reason") or ""),
                str(r.get("party") or ""),
            ]
        )
        if not _osc_doc_kind_match(kind, blob):
            continue
        ts = r.get("modified_date")
        items.append(
            {
                "id": f"di-{r.get('id')}",
                "source": "document_index",
                "case_number": r.get("case_number") or "",
                "file_name": r.get("file_name") or "",
                "file_path": r.get("file_path") or "",
                "subfolder_name": r.get("subfolder_name") or "",
                "reason": r.get("reason") or "",
                "party": r.get("party") or "",
                "kind_label": _osc_doc_kind_label(blob),
                "timestamp": _osc_json_value(ts) if ts else "",
                "sort_ts": _osc_parse_dt(ts).timestamp() if _osc_parse_dt(ts) else 0,
            }
        )

    cd_where = []
    cd_params = []
    if case_number:
        cd_where.append("(cd.case_id = %s OR cd.case_id IN (SELECT id FROM cases WHERE case_number=%s))")
        cd_params.extend([case_number, case_number])
    if q:
        like = f"%{q}%"
        cd_where.append("(cd.file_name LIKE %s OR cd.file_path LIKE %s OR cd.document_type LIKE %s OR cd.description LIKE %s)")
        cd_params.extend([like, like, like, like])
    cd_sql = """
        SELECT cd.id, cd.case_id, c.case_number AS case_number_ref, cd.document_type, cd.file_name, cd.file_path, cd.description, cd.upload_date
        FROM case_documents cd
        LEFT JOIN cases c ON c.id = cd.case_id
    """
    if cd_where:
        cd_sql += " WHERE " + " AND ".join(cd_where)
    cd_sql += " ORDER BY upload_date DESC, id DESC LIMIT %s"
    cd_params.append(cd_limit)
    cd_rows, _ = _osc_exec(cd_sql, tuple(cd_params), fetch="all")
    for r in cd_rows:
        blob = " ".join(
            [
                str(r.get("document_type") or ""),
                str(r.get("file_name") or ""),
                str(r.get("description") or ""),
            ]
        )
        if not _osc_doc_kind_match(kind, blob):
            continue
        ts = r.get("upload_date")
        items.append(
            {
                "id": f"cd-{r.get('id')}",
                "source": "case_documents",
                "case_number": r.get("case_number_ref") or r.get("case_id") or "",
                "file_name": r.get("file_name") or "",
                "file_path": r.get("file_path") or "",
                "subfolder_name": r.get("document_type") or "",
                "reason": r.get("description") or "",
                "party": "",
                "kind_label": _osc_doc_kind_label(blob),
                "timestamp": _osc_json_value(ts) if ts else "",
                "sort_ts": _osc_parse_dt(ts).timestamp() if _osc_parse_dt(ts) else 0,
            }
        )

    items.sort(key=lambda x: x.get("sort_ts") or 0, reverse=True)
    out = items[:limit]
    for it in out:
        it.pop("sort_ts", None)
    return jsonify({"ok": True, "items": out})


@osc_bp.route("/api/osc/documents/open", methods=["POST"])
@login_required
def osc_documents_open_api():
    payload = request.get_json() or {}
    raw = str(payload.get("path") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "path required"}), 400
    norm = _osc_norm_path(raw)
    local_candidates = _osc_local_path_candidates(norm)
    smb_candidates = _osc_smb_candidates(norm)
    chosen_open_path = ""
    open_result = {"ok": False, "error": "open_failed"}

    for lp in local_candidates:
        try:
            if lp and os.path.exists(lp):
                r = _osc_try_open_path(lp)
                chosen_open_path = lp
                open_result = r
                if r.get("ok"):
                    break
        except Exception:
            continue
    if not open_result.get("ok"):
        for sp in smb_candidates:
            r = _osc_try_open_path(sp)
            chosen_open_path = sp
            open_result = r
            if r.get("ok"):
                break
    return jsonify(
        {
            "ok": True,
            "path": norm,
            "local_candidates": local_candidates,
            "smb_candidates": smb_candidates,
            "chosen_open_path": chosen_open_path,
            "open_result": open_result,
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# Files (content / text / upload)
# ══════════════════════════════════════════════════════════════════════════════


def _osc_read_file_with_retry(local_file: str, *, max_attempts: int = 4) -> bytes:
    """讀檔並對 SMB EDEADLK (errno 11) / EAGAIN 做指數 backoff 重試。

    macOS SMB-over-Tailscale-relay 在多 client 同時開檔（Word / Preview / 其他律師）會偶發
    `[Errno 11] Resource deadlock avoided`。此函式會 retry 4 次（0.25s/0.5s/1s backoff）。
    """
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with open(local_file, "rb") as f:
                return f.read()
        except OSError as e:
            last_exc = e
            # errno 11 = EDEADLK on macOS, also EAGAIN on some platforms
            if e.errno in (11, 35) and attempt < max_attempts - 1:
                _time.sleep(0.25 * (2 ** attempt))
                continue
            raise
    # unreachable but defensive
    if last_exc:
        raise last_exc
    return b""


def _osc_wants_json_response() -> bool:
    accept = str(request.headers.get("Accept") or "")
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    if "application/json" in accept:
        return True
    if "text/html" in accept:
        return False
    return True


def _osc_download_error_response(message: str, status: int = 400):
    """Return JSON for API calls, but a readable page for direct browser downloads."""
    if _osc_wants_json_response():
        return jsonify({"ok": False, "error": message}), status
    safe_message = html_lib.escape(str(message or "操作失敗"))
    body = f"""<!doctype html>
<html lang="zh-TW">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Paperclip 檔案操作失敗</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f5f7fb; color:#1f2937; margin:0; }}
    main {{ max-width: 640px; margin: 14vh auto; padding: 28px; background:white; border:1px solid #d8dee9; border-radius:12px; box-shadow:0 12px 30px rgba(15,23,42,.08); }}
    h1 {{ font-size: 22px; margin:0 0 12px; }}
    p {{ line-height:1.7; margin:0 0 18px; }}
    button {{ border:1px solid #0ea5e9; background:#0ea5e9; color:white; border-radius:8px; padding:9px 14px; cursor:pointer; }}
  </style>
</head>
<body>
  <main>
    <h1>檔案操作沒有完成</h1>
    <p>{safe_message}</p>
    <button onclick="history.length > 1 ? history.back() : window.close()">返回上一頁</button>
  </main>
</body>
</html>"""
    return Response(body, status=status, content_type="text/html; charset=utf-8")


def _osc_copy_with_system_cp(local_file: str, tmp_path: str) -> bool:
    """Use macOS /bin/cp as a fallback when Python's SMB read hits EDEADLK."""
    cp_bin = shutil.which("cp") or "/bin/cp"
    try:
        expected_size = os.path.getsize(local_file)
        result = subprocess.run(
            [cp_bin, "-p", local_file, tmp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(os.environ.get("PAPERCLIP_FILE_CP_TIMEOUT_SEC", "120") or "120"),
        )
        return result.returncode == 0 and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) == expected_size
    except Exception:
        _log.debug("silent-catch system cp fallback failed", exc_info=True)
        return False


def _osc_is_dataless_file(path: str) -> bool:
    try:
        return bool(getattr(os.stat(path), "st_flags", 0) & 0x40000000)
    except Exception:
        return False


def _osc_stage_file_with_retry(local_file: str, *, max_attempts: int | None = None) -> str:
    """Copy a NAS-backed large file to local temp storage before Flask sends it.

    Direct `send_file("/Volumes/...")` can hit macOS SMB EDEADLK while Werkzeug
    streams the file. Staging keeps the browser response on a local file handle.
    """
    if max_attempts is None:
        max_attempts = max(4, int(os.environ.get("PAPERCLIP_FILE_STAGE_MAX_ATTEMPTS", "8") or "8"))
    last_exc: Exception | None = None
    expected_size = os.path.getsize(local_file)
    tmp_dir = os.path.join(tempfile.gettempdir(), "paperclip-downloads")
    os.makedirs(tmp_dir, exist_ok=True)
    suffix = os.path.splitext(local_file)[1] or ".bin"
    for attempt in range(max_attempts):
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(prefix="osc-download-", suffix=suffix, dir=tmp_dir)
            try:
                with os.fdopen(fd, "wb") as out, open(local_file, "rb") as src:
                    while True:
                        try:
                            chunk = src.read(4 * 1024 * 1024)
                        except TypeError:
                            chunk = src.read()
                        if not chunk:
                            break
                        out.write(chunk)
            except OSError as e:
                try:
                    os.close(fd)
                except OSError:
                    pass
                if e.errno in (11, 35) and _osc_copy_with_system_cp(local_file, tmp_path):
                    return tmp_path
                raise
            if os.path.getsize(tmp_path) != expected_size:
                raise OSError(
                    f"staged copy incomplete: expected {expected_size} bytes, got {os.path.getsize(tmp_path)} bytes"
                )
            return tmp_path
        except OSError as e:
            last_exc = e
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            if e.errno in (11, 35) and attempt < max_attempts - 1:
                time.sleep(0.25 * (2 ** attempt))
                continue
            raise
        except Exception as e:
            last_exc = e
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
    if last_exc:
        raise last_exc
    raise OSError("stage_file_failed")


def _osc_cleanup_file_once(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _osc_content_disposition(filename: str, *, inline: bool) -> str:
    disposition = "inline" if inline else "attachment"
    suffix = Path(filename or "").suffix
    if not suffix or not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix):
        suffix = ".bin"
    ascii_raw = filename.encode("ascii", "ignore").decode("ascii").replace("/", "_").replace("\\", "_")
    ascii_raw = re.sub(r"[^A-Za-z0-9._ -]+", "_", ascii_raw)
    ascii_suffix = Path(ascii_raw).suffix
    if not ascii_suffix or not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", ascii_suffix):
        ascii_suffix = suffix
    raw_stem = ascii_raw[:-len(ascii_suffix)] if ascii_raw.lower().endswith(ascii_suffix.lower()) else Path(ascii_raw).stem
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw_stem).strip(" .-_")
    ascii_name = (stem or "paperclip") + ascii_suffix
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'


def _osc_stream_staged_file(staged_file: str, *, download_name: str, mime: str | None, inline: bool):
    """Stream a local staged file without Werkzeug send_file.

    macOS SMB/Tailscale backed files can surface EDEADLK through send_file even
    after staging. A small explicit streamer keeps PDF preview/download paths on
    a normal local file descriptor and still supports browser byte ranges.
    """
    try:
        size = os.path.getsize(staged_file)
    except OSError as e:
        _osc_cleanup_file_once(staged_file)
        return _osc_download_error_response(f"檔案暫存讀取失敗：{e}", 500)

    start = 0
    end = max(0, size - 1)
    status = 200
    range_header = str(request.headers.get("Range") or "").strip()
    if range_header.startswith("bytes=") and size > 0:
        spec = range_header[6:].split(",", 1)[0].strip()
        try:
            left, _, right = spec.partition("-")
            if left == "":
                suffix_len = int(right)
                if suffix_len <= 0:
                    raise ValueError("invalid suffix range")
                start = max(0, size - suffix_len)
            else:
                start = int(left)
                if right:
                    end = min(size - 1, int(right))
            if start < 0 or start >= size or end < start:
                raise ValueError("invalid byte range")
            status = 206
        except Exception:
            _osc_cleanup_file_once(staged_file)
            resp = Response(status=416)
            resp.headers["Content-Range"] = f"bytes */{size}"
            resp.headers["Accept-Ranges"] = "bytes"
            return resp

    length = 0 if size == 0 else end - start + 1

    if request.method == "HEAD":
        _osc_cleanup_file_once(staged_file)
        resp = Response(status=status, mimetype=mime or "application/octet-stream")
    else:
        def _iter_file():
            try:
                with open(staged_file, "rb") as fh:
                    fh.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = fh.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk
            finally:
                _osc_cleanup_file_once(staged_file)

        resp = Response(_iter_file(), status=status, mimetype=mime or "application/octet-stream")
        resp.call_on_close(lambda: _osc_cleanup_file_once(staged_file))

    resp.headers["Content-Disposition"] = _osc_content_disposition(download_name, inline=inline)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    resp.headers["Cache-Control"] = "private, max-age=300"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@osc_bp.route("/api/osc/files/content", methods=["GET"])
@login_required
def osc_file_content_api():
    raw = str(request.args.get("path") or "").strip()
    if not raw:
        return _osc_download_error_response("缺少檔案路徑。", 400)
    # 改為枚舉所有 candidate，遇 EDEADLK / 開檔失敗時 fallback 到下一個（NAS↔Synology 雙向）
    candidates = _osc_local_path_candidates(raw)
    norm = _osc_norm_path(raw).replace("\\", "/")
    if norm and norm not in candidates:
        candidates.append(norm)
    def _existing_file_candidates(candidate_paths: list[str]) -> list[str]:
        found: list[str] = []
        for cand in candidate_paths:
            try:
                real = os.path.realpath(cand)
                if _osc_is_safe_local_path(real) and os.path.isfile(real):
                    if real not in found:
                        found.append(real)
            except Exception:
                continue
        return found

    existing_candidates = _existing_file_candidates(candidates)
    if existing_candidates and all(_osc_is_dataless_file(p) for p in existing_candidates):
        try:
            from api.nas_mount_guard import ensure_nas_mounts
            ensure_nas_mounts()
            remapped = _osc_local_path_candidates(raw)
            if norm and norm not in remapped:
                remapped.append(norm)
            existing_candidates = _existing_file_candidates(remapped) or existing_candidates
        except Exception:
            _log.debug("silent-catch dataless file NAS remap failed", exc_info=True)
    if not existing_candidates:
        return _osc_download_error_response("找不到檔案，可能已移動、刪除，或 NAS 尚未完成同步。", 404)
    existing_candidates.sort(key=lambda p: (1 if _osc_is_dataless_file(p) else 0, 0 if p.startswith("/Volumes/") else 1))

    inline = str(request.args.get("inline") or "").strip() in {"1", "true", "yes"}
    if request.method == "HEAD":
        chosen = existing_candidates[0]
        mime, _ = mimetypes.guess_type(chosen)
        try:
            st = os.stat(chosen)
        except OSError as e:
            return _osc_download_error_response(f"檔案讀取失敗：{e}", 500)
        resp = Response(status=200, mimetype=mime or "application/octet-stream")
        resp.headers["Content-Disposition"] = _osc_content_disposition(os.path.basename(chosen), inline=inline)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Length"] = str(int(st.st_size))
        resp.headers["Cache-Control"] = "private, max-age=300"
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["ETag"] = f'"{int(st.st_mtime)}-{st.st_size}"'
        return resp

    last_err: Exception | None = None
    chosen: str = ""
    staged_file: str = ""
    for local_file in existing_candidates:
        try:
            staged_file = _osc_stage_file_with_retry(local_file)
            chosen = local_file
            break
        except OSError as e:
            last_err = e
            _log.warning("osc_file_content_api stage failed (errno=%s) on %s, trying next candidate", e.errno, local_file)
            continue
    if not chosen:
        _log.error("osc_file_content_api: all candidates failed; last_err=%s; tried=%s", last_err, existing_candidates)
        return _osc_download_error_response(f"檔案讀取失敗：{last_err}", 500)

    mime, _ = mimetypes.guess_type(chosen)
    try:
        resp = _osc_stream_staged_file(
            staged_file,
            mime=mime or "application/octet-stream",
            inline=inline,
            download_name=os.path.basename(chosen),
        )
        try:
            st = os.stat(chosen)
            resp.headers["ETag"] = f'"{int(st.st_mtime)}-{st.st_size}"'
        except OSError:
            pass
        return resp
    except Exception as e:
        if staged_file:
            _osc_cleanup_file_once(staged_file)
        _log.error("osc_file_content_api send_file error: %s - file=%s", e, chosen)
        return _osc_download_error_response(f"檔案傳送失敗：{e}", 500)


@osc_bp.route("/api/osc/files/text", methods=["GET", "PUT"])
@login_required
def osc_file_text_api():
    if request.method == "GET":
        raw = str(request.args.get("path") or "").strip()
        if not raw:
            return jsonify({"ok": False, "error": "path required"}), 400
        if not _osc_is_editable_text_path(raw):
            return jsonify({"ok": False, "error": "not_editable_text"}), 400
        local_file = _osc_resolve_existing_local_path(raw, prefer_dir=False)
        if not local_file:
            return jsonify({"ok": False, "error": "file_not_found"}), 404
        if not _osc_is_safe_local_path(local_file):
            return jsonify({"ok": False, "error": "path_not_allowed"}), 403
        _MAX_TEXT_SIZE = 10 * 1024 * 1024  # 10 MB
        file_size = os.path.getsize(local_file)
        if file_size > _MAX_TEXT_SIZE:
            return jsonify({"ok": False, "error": f"file too large ({file_size} bytes, max {_MAX_TEXT_SIZE})"}), 413
        try:
            content, encoding = _osc_read_text_file(local_file)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify(
            {
                "ok": True,
                "path": raw,
                "local_path": local_file,
                "content": content,
                "encoding": encoding,
                "size": os.path.getsize(local_file),
            }
        )

    payload = request.get_json() or {}
    raw = str(payload.get("path") or "").strip()
    content = payload.get("content")
    if not raw:
        return jsonify({"ok": False, "error": "path required"}), 400
    if content is None:
        return jsonify({"ok": False, "error": "content required"}), 400
    if not _osc_is_editable_text_path(raw):
        return jsonify({"ok": False, "error": "not_editable_text"}), 400
    local_file = _osc_resolve_existing_local_path(raw, prefer_dir=False)
    if not local_file:
        return jsonify({"ok": False, "error": "file_not_found"}), 404
    if not _osc_is_safe_local_path(local_file):
        return jsonify({"ok": False, "error": "path_not_allowed"}), 403
    text = str(content)
    _MAX_TEXT_SIZE = 10 * 1024 * 1024  # 10 MB
    if len(text.encode("utf-8")) > _MAX_TEXT_SIZE:
        return jsonify({"ok": False, "error": f"content too large (max {_MAX_TEXT_SIZE} bytes)"}), 413
    Path(local_file).write_text(text, encoding="utf-8")
    return jsonify({"ok": True, "path": raw, "local_path": local_file, "size": len(text.encode('utf-8'))})


@osc_bp.route("/api/osc/files/upload", methods=["POST"])
@login_required
def osc_file_upload_api():
    folder_path = str(request.form.get("folder_path") or request.args.get("folder_path") or "").strip()
    relative_path = str(request.form.get("relative_path") or request.args.get("relative_path") or "").strip().strip("/")
    overwrite = str(request.form.get("overwrite") or request.args.get("overwrite") or "").strip().lower() in {"1", "true", "yes", "on"}
    if not folder_path:
        return jsonify({"ok": False, "error": "folder_path required"}), 400
    base_folder = _osc_resolve_existing_local_path(folder_path, prefer_dir=True)
    if not base_folder:
        return jsonify({"ok": False, "error": "folder_not_found"}), 404
    target_dir = os.path.realpath(os.path.join(base_folder, relative_path or ""))
    base_real = os.path.realpath(base_folder)
    if target_dir != base_real and not target_dir.startswith(base_real + os.sep):
        return jsonify({"ok": False, "error": "path_escape"}), 400
    if not os.path.isdir(target_dir):
        return jsonify({"ok": False, "error": "target_dir_not_found"}), 404
    uploads = request.files.getlist("file") or request.files.getlist("files")
    if not uploads:
        return jsonify({"ok": False, "error": "file required"}), 400

    # --- upload size limits ---
    # Keep this aligned with the file manager chunked uploader. Legal PDFs can easily exceed 50 MB.
    max_per_file_mb = max(1, int(os.environ.get("PAPERCLIP_UPLOAD_MAX_PER_FILE_MB", "1024") or "1024"))
    max_total_mb = max(max_per_file_mb, int(os.environ.get("PAPERCLIP_UPLOAD_MAX_TOTAL_MB", "1024") or "1024"))
    _MAX_PER_FILE = max_per_file_mb * 1024 * 1024
    _MAX_TOTAL = max_total_mb * 1024 * 1024

    saved = []
    total_saved = 0
    for uploaded in uploads:
        name = os.path.basename(str(uploaded.filename or "").strip())
        if not name:
            continue
        dest = os.path.join(target_dir, name)
        if os.path.exists(dest) and not overwrite:
            return jsonify({"ok": False, "error": "file_exists", "file_name": name, "target_path": dest}), 409
        uploaded.save(dest)
        fsize = os.path.getsize(dest)
        if fsize > _MAX_PER_FILE:
            os.remove(dest)
            return jsonify({"ok": False, "error": "檔案過大", "code": "file_too_large", "file_name": name,
                            "size_mb": round(fsize / 1024 / 1024, 1), "limit_mb": max_per_file_mb}), 413
        total_saved += fsize
        if total_saved > _MAX_TOTAL:
            os.remove(dest)
            return jsonify({"ok": False, "error": "上傳總量過大", "code": "total_upload_too_large",
                            "total_mb": round(total_saved / 1024 / 1024, 1), "limit_mb": max_total_mb}), 413
        saved.append(
            {
                "file_name": name,
                "target_path": dest,
                "size": fsize,
            }
        )
    if not saved:
        return jsonify({"ok": False, "error": "no_valid_files"}), 400
    return jsonify({"ok": True, "saved": saved, "target_dir": target_dir, "overwrite": overwrite})


# ══════════════════════════════════════════════════════════════════════════════
# Document templates
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/document-templates", methods=["GET", "POST"])
@login_required
def osc_document_templates_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        case_number = (request.args.get("case_number") or "").strip()
        doc_type = (request.args.get("doc_type") or "").strip()
        limit = max(1, min(1000, int(request.args.get("limit") or "300")))
        sql = (
            "SELECT id, doc_type, party_name, case_number, division, template_data, created_date, last_used, use_count "
            "FROM document_templates WHERE 1=1 "
        )
        params = []
        if case_number:
            sql += "AND case_number=%s "
            params.append(case_number)
        if doc_type:
            sql += "AND doc_type=%s "
            params.append(doc_type)
        if q:
            like = f"%{q}%"
            sql += "AND (doc_type LIKE %s OR party_name LIKE %s OR case_number LIKE %s OR division LIKE %s OR template_data LIKE %s) "
            params.extend([like, like, like, like, like])
        sql += "ORDER BY COALESCE(last_used, created_date) DESC, id DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows})

    payload = request.get_json() or {}
    row_id = str(payload.get("id") or "").strip()
    body = {
        "doc_type": (payload.get("doc_type") or "").strip() or None,
        "party_name": (payload.get("party_name") or "").strip() or None,
        "case_number": (payload.get("case_number") or "").strip() or None,
        "division": (payload.get("division") or "").strip() or None,
        "template_data": _osc_template_data_json_or_wrap(payload.get("template_data")),
        "use_count": _osc_safe_int(payload.get("use_count"), 0),
    }
    if row_id:
        sets = [f"{k}=%s" for k in body.keys()]
        vals = list(body.values()) + [row_id]
        result, _ = _osc_exec(f"UPDATE document_templates SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
        return jsonify({"ok": True, "mode": "update", "id": row_id, "result": result})

    cols = list(body.keys())
    vals = [body[c] for c in cols]
    result, _ = _osc_exec(
        f"INSERT INTO document_templates ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
        tuple(vals),
        fetch="none",
    )
    return jsonify({"ok": True, "mode": "insert", "id": result.get("lastrowid"), "result": result})


@osc_bp.route("/api/osc/document-templates/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_document_template_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM document_templates WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM document_templates WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    allowed = ["doc_type", "party_name", "case_number", "division", "template_data", "last_used", "use_count"]
    sets, vals = [], []
    for k in allowed:
        if k in payload:
            sets.append(f"{k}=%s")
            if k == "use_count":
                vals.append(_osc_safe_int(payload.get(k), 0))
            elif k == "template_data":
                vals.append(_osc_template_data_json_or_wrap(payload.get(k)))
            else:
                vals.append((payload.get(k) or "").strip() or None)
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE document_templates SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Document keywords
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/document-keywords", methods=["GET", "POST"])
@login_required
def osc_document_keywords_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        case_number = (request.args.get("case_number") or "").strip()
        category = (request.args.get("category") or "").strip()
        limit = max(1, min(1000, int(request.args.get("limit") or "300")))
        sql = (
            "SELECT id, case_number, keyword_name, keyword_content, category, hotkey, is_case_specific, usage_count, created_date, modified_date "
            "FROM document_keywords WHERE 1=1 "
        )
        params = []
        if case_number:
            sql += "AND case_number=%s "
            params.append(case_number)
        if category:
            sql += "AND category=%s "
            params.append(category)
        if q:
            like = f"%{q}%"
            sql += "AND (case_number LIKE %s OR keyword_name LIKE %s OR keyword_content LIKE %s OR category LIKE %s OR hotkey LIKE %s) "
            params.extend([like, like, like, like, like])
        sql += "ORDER BY modified_date DESC, created_date DESC, id DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows})

    payload = request.get_json() or {}
    row_id = str(payload.get("id") or "").strip()
    body = {
        "case_number": (payload.get("case_number") or "").strip() or None,
        "keyword_name": (payload.get("keyword_name") or "").strip() or None,
        "keyword_content": (payload.get("keyword_content") or "").strip() or None,
        "category": (payload.get("category") or "").strip() or None,
        "hotkey": (payload.get("hotkey") or "").strip() or None,
        "is_case_specific": 1 if str(payload.get("is_case_specific") or "").strip().lower() in {"1", "true", "yes", "on"} else 0,
        "usage_count": _osc_safe_int(payload.get("usage_count"), 0),
    }
    if not body["keyword_name"]:
        return jsonify({"ok": False, "error": "keyword_name required"}), 400
    if row_id:
        sets = [f"{k}=%s" for k in body.keys()]
        vals = list(body.values()) + [row_id]
        result, _ = _osc_exec(
            f"UPDATE document_keywords SET {','.join(sets)}, modified_date=NOW() WHERE id=%s",
            tuple(vals),
            fetch="none",
        )
        return jsonify({"ok": True, "mode": "update", "id": row_id, "result": result})

    cols = list(body.keys())
    vals = [body[c] for c in cols]
    result, _ = _osc_exec(
        f"INSERT INTO document_keywords ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
        tuple(vals),
        fetch="none",
    )
    return jsonify({"ok": True, "mode": "insert", "result": result})


@osc_bp.route("/api/osc/document-keywords/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_document_keyword_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM document_keywords WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM document_keywords WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    allowed = ["case_number", "keyword_name", "keyword_content", "category", "hotkey", "is_case_specific", "usage_count"]
    sets, vals = [], []
    for k in allowed:
        if k not in payload:
            continue
        sets.append(f"{k}=%s")
        if k in {"usage_count", "is_case_specific"}:
            vals.append(_osc_safe_int(payload.get(k), 0))
        else:
            vals.append((payload.get(k) or "").strip() or None)
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(
        f"UPDATE document_keywords SET {','.join(sets)}, modified_date=NOW() WHERE id=%s",
        tuple(vals),
        fetch="none",
    )
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Document replacements
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/document-replacements", methods=["GET", "POST"])
@login_required
def osc_document_replacements_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        case_number = (request.args.get("case_number") or "").strip()
        limit = max(1, min(1000, int(request.args.get("limit") or "300")))
        sql = (
            "SELECT id, template_file, new_case_number, old_client_name, new_client_name, old_data, new_data, replaced_date "
            "FROM document_replacements WHERE 1=1 "
        )
        params = []
        if case_number:
            sql += "AND new_case_number=%s "
            params.append(case_number)
        if q:
            like = f"%{q}%"
            sql += "AND (template_file LIKE %s OR new_case_number LIKE %s OR old_client_name LIKE %s OR new_client_name LIKE %s OR old_data LIKE %s OR new_data LIKE %s) "
            params.extend([like, like, like, like, like, like])
        sql += "ORDER BY replaced_date DESC, id DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows})

    payload = request.get_json() or {}
    body = {
        "template_file": (payload.get("template_file") or "").strip() or None,
        "new_case_number": (payload.get("new_case_number") or payload.get("case_number") or "").strip() or None,
        "old_client_name": (payload.get("old_client_name") or "").strip() or None,
        "new_client_name": (payload.get("new_client_name") or "").strip() or None,
        "old_data": (payload.get("old_data") or "").strip() or None,
        "new_data": (payload.get("new_data") or "").strip() or None,
    }
    cols = list(body.keys())
    vals = [body[c] for c in cols]
    result, _ = _osc_exec(
        f"INSERT INTO document_replacements ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
        tuple(vals),
        fetch="none",
    )
    return jsonify({"ok": True, "result": result})


@osc_bp.route("/api/osc/document-replacements/<int:row_id>", methods=["GET", "DELETE"])
@login_required
def osc_document_replacement_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM document_replacements WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    result, _ = _osc_exec("DELETE FROM document_replacements WHERE id=%s", (row_id,), fetch="none")
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# LAF (Legal Aid Foundation)
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/laf", methods=["GET"])
@login_required
def osc_laf_api():
    q = (request.args.get("q") or "").strip()
    case_number = (request.args.get("case_number") or "").strip()
    limit = max(1, min(1000, int(request.args.get("limit") or "300")))

    like = f"%{q}%"
    where_case = "case_number=%s" if case_number else "1=1"
    case_params = [case_number] if case_number else []

    checklist_sql = (
        "SELECT id, case_number, item_key, item_label, status, notes, last_updated "
        "FROM legal_aid_checklists "
        f"WHERE {where_case} "
    )
    checklist_params = list(case_params)
    if q:
        checklist_sql += "AND (case_number LIKE %s OR item_key LIKE %s OR item_label LIKE %s OR status LIKE %s OR notes LIKE %s) "
        checklist_params.extend([like, like, like, like, like])
    checklist_sql += "ORDER BY last_updated DESC, id DESC LIMIT %s"
    checklist_params.append(limit)
    checklist, _ = _osc_exec(checklist_sql, tuple(checklist_params), fetch="all")

    lifecycle_sql = (
        "SELECT id, case_number, event_type, status, created_at, completed_at, event_data "
        "FROM laf_lifecycle_log "
        f"WHERE {where_case} "
    )
    lifecycle_params = list(case_params)
    if q:
        lifecycle_sql += "AND (case_number LIKE %s OR event_type LIKE %s OR status LIKE %s OR event_data LIKE %s) "
        lifecycle_params.extend([like, like, like, like])
    lifecycle_sql += "ORDER BY created_at DESC, id DESC LIMIT %s"
    lifecycle_params.append(limit)
    lifecycle, _ = _osc_exec(lifecycle_sql, tuple(lifecycle_params), fetch="all")

    email_sql = "SELECT id, gmail_message_id, subject, sender, received_at, processed_at, status, case_number, created_case_id, error_message FROM laf_email_records WHERE 1=1 "
    email_params = []
    if case_number:
        email_sql += "AND case_number=%s "
        email_params.append(case_number)
    if q:
        email_sql += "AND (subject LIKE %s OR sender LIKE %s OR case_number LIKE %s OR status LIKE %s OR error_message LIKE %s) "
        email_params.extend([like, like, like, like, like])
    email_sql += "ORDER BY received_at DESC, id DESC LIMIT %s"
    email_params.append(limit)
    emails, _ = _osc_exec(email_sql, tuple(email_params), fetch="all")

    return jsonify(
        {
            "ok": True,
            "items": {
                "checklist": checklist or [],
                "lifecycle": lifecycle or [],
                "emails": emails or [],
            },
            "counts": {
                "checklist": len(checklist or []),
                "lifecycle": len(lifecycle or []),
                "emails": len(emails or []),
            },
        }
    )


@osc_bp.route("/api/osc/laf/cases", methods=["GET"])
@login_required
def osc_laf_cases_api():
    """Return the legal-aid case master list used by the Paperclip LAF workbench."""
    q = (request.args.get("q") or "").strip()
    limit = max(1, min(1000, int(request.args.get("limit") or "500")))
    status_scope = (request.args.get("status_scope") or "all").strip().lower()

    where = [
        """
        (
            case_category = '法律扶助案件'
            OR case_reason LIKE '%法扶%'
            OR case_reason LIKE '%法律扶助%'
        )
        """
    ]
    params = []
    if q:
        like = f"%{q}%"
        where.append(
            """
            (
                case_number LIKE %s
                OR client_name LIKE %s
                OR case_type LIKE %s
                OR case_reason LIKE %s
                OR laf_case_no LIKE %s
                OR legal_aid_status LIKE %s
                OR status LIKE %s
            )
            """
        )
        params.extend([like, like, like, like, like, like, like])
    if status_scope in {"working", "active", "open"}:
        where.append(
            """
            (
                status IS NULL OR status = ''
                OR status LIKE '%進行%'
                OR status LIKE '%結案中%'
                OR status LIKE '%待報結%'
                OR legal_aid_status IS NULL OR legal_aid_status = ''
                OR legal_aid_status IN ('未開辦', '進行中', '已結案，待報結')
            )
            """
        )
    elif status_scope in {"closed", "archived"}:
        where.append("(status LIKE '%已結案%' OR legal_aid_status='已結案')")

    sql = f"""
        SELECT
            id, case_number, client_name, case_category, case_type, case_reason,
            laf_case_no, legal_aid_status, status, folder_path, updated_at, created_date,
            (
                SELECT COUNT(*)
                FROM legal_aid_checklists lac
                WHERE lac.case_number = cases.case_number
                  AND COALESCE(lac.status, '') NOT IN ('已備齊', '不適用', '完成', '已完成', '已繳', '免附')
            ) AS pending_laf_items
        FROM cases
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE COALESCE(legal_aid_status, '')
                WHEN '未開辦' THEN 0
                WHEN '進行中' THEN 1
                WHEN '已結案，待報結' THEN 2
                WHEN '已結案' THEN 4
                ELSE 3
            END,
            updated_at DESC,
            created_date DESC
        LIMIT %s
    """
    params.append(limit)
    rows, _ = _osc_exec(sql, tuple(params), fetch="all")
    return jsonify({"ok": True, "items": rows or []})


@osc_bp.route("/api/osc/laf/batch-status", methods=["POST"])
@login_required
def osc_laf_batch_status_api():
    """Mirror OSC standalone's '全部改為進行中' button for not-yet-started LAF cases."""
    payload = request.get_json() or {}
    target = (payload.get("legal_aid_status") or "進行中").strip() or "進行中"
    if target != "進行中":
        return jsonify({"ok": False, "error": "目前僅支援批次改為進行中"}), 400
    result, _ = _osc_exec(
        """
        UPDATE cases
        SET legal_aid_status=%s, status=%s, updated_at=NOW()
        WHERE (
            case_category = '法律扶助案件'
            OR case_reason LIKE '%法扶%'
            OR case_reason LIKE '%法律扶助%'
        )
          AND (legal_aid_status IS NULL OR legal_aid_status='' OR legal_aid_status='未開辦')
        """,
        (target, "進行中"),
        fetch="none",
    )
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Quotations
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/quotations", methods=["GET", "POST"])
@login_required
def osc_quotations_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        status = (request.args.get("status") or "").strip()
        limit = max(1, min(1000, int(request.args.get("limit") or "300")))
        sql = (
            "SELECT id, client_name, project_name, phone, email, date, expiry, subtotal, discount, tax, total, status, updated_date, created_date "
            "FROM quotations WHERE 1=1 "
        )
        params = []
        if status:
            sql += "AND status=%s "
            params.append(status)
        if q:
            like = f"%{q}%"
            sql += "AND (id LIKE %s OR client_name LIKE %s OR project_name LIKE %s OR phone LIKE %s OR email LIKE %s OR notes LIKE %s) "
            params.extend([like, like, like, like, like, like])
        sql += "ORDER BY updated_date DESC, created_date DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows})

    payload = request.get_json() or {}
    row_id = (payload.get("id") or "").strip() or f"q-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    client_name = (payload.get("client_name") or "").strip()
    project_name = (payload.get("project_name") or "").strip()
    if not client_name or not project_name:
        return jsonify({"ok": False, "error": "client_name/project_name required"}), 400
    def _fnum(x, d=0.0):
        try:
            return float(x if x is not None and str(x).strip() != "" else d)
        except Exception:
            return float(d)
    cols = [
        "id", "client_name", "project_name", "contact", "phone", "email", "address", "tax_id",
        "date", "expiry", "items", "subtotal", "discount", "tax", "total", "status", "notes", "extended_data"
    ]
    vals = [
        row_id,
        client_name,
        project_name,
        (payload.get("contact") or "").strip() or None,
        (payload.get("phone") or "").strip() or None,
        (payload.get("email") or "").strip() or None,
        (payload.get("address") or "").strip() or None,
        (payload.get("tax_id") or "").strip() or None,
        (payload.get("date") or "").strip() or None,
        (payload.get("expiry") or "").strip() or None,
        _osc_json_or_wrap(payload.get("items"), fallback_key="items"),
        _fnum(payload.get("subtotal"), 0),
        _fnum(payload.get("discount"), 0),
        _fnum(payload.get("tax"), 0),
        _fnum(payload.get("total"), 0),
        (payload.get("status") or "draft").strip() or "draft",
        (payload.get("notes") or "").strip() or None,
        _osc_json_or_wrap(payload.get("extended_data"), fallback_key="extended_data"),
    ]
    try:
        result, _ = _osc_exec(
            f"INSERT INTO quotations ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
            tuple(vals),
            fetch="none",
        )
        return jsonify({"ok": True, "mode": "insert", "id": row_id, "result": result})
    except Exception as e:
        msg = str(e)
        is_dup = ("1062" in msg) or ("Duplicate entry" in msg)
        if not is_dup:
            return jsonify({"ok": False, "error": msg}), 500
        sets = [
            "client_name=%s", "project_name=%s", "contact=%s", "phone=%s", "email=%s", "address=%s", "tax_id=%s",
            "date=%s", "expiry=%s", "items=%s", "subtotal=%s", "discount=%s", "tax=%s", "total=%s", "status=%s",
            "notes=%s", "extended_data=%s"
        ]
        vals2 = [
            client_name,
            project_name,
            (payload.get("contact") or "").strip() or None,
            (payload.get("phone") or "").strip() or None,
            (payload.get("email") or "").strip() or None,
            (payload.get("address") or "").strip() or None,
            (payload.get("tax_id") or "").strip() or None,
            (payload.get("date") or "").strip() or None,
            (payload.get("expiry") or "").strip() or None,
            _osc_json_or_wrap(payload.get("items"), fallback_key="items"),
            _fnum(payload.get("subtotal"), 0),
            _fnum(payload.get("discount"), 0),
            _fnum(payload.get("tax"), 0),
            _fnum(payload.get("total"), 0),
            (payload.get("status") or "draft").strip() or "draft",
            (payload.get("notes") or "").strip() or None,
            _osc_json_or_wrap(payload.get("extended_data"), fallback_key="extended_data"),
            row_id,
        ]
        result, _ = _osc_exec(f"UPDATE quotations SET {','.join(sets)} WHERE id=%s", tuple(vals2), fetch="none")
        return jsonify({"ok": True, "mode": "upsert", "id": row_id, "result": result})


@osc_bp.route("/api/osc/quotations/<row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_quotation_detail_api(row_id):
    row_id = (row_id or "").strip()
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM quotations WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM quotations WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    allowed = [
        "client_name", "project_name", "contact", "phone", "email", "address", "tax_id", "date", "expiry",
        "items", "subtotal", "discount", "tax", "total", "status", "notes", "extended_data"
    ]
    sets, vals = [], []
    for k in allowed:
        if k not in payload:
            continue
        sets.append(f"{k}=%s")
        if k in {"subtotal", "discount", "tax", "total"}:
            try:
                vals.append(float(payload.get(k) or 0))
            except Exception:
                return jsonify({"ok": False, "error": f"{k} invalid"}), 400
        elif k in {"items", "extended_data"}:
            vals.append(_osc_json_or_wrap(payload.get(k), fallback_key=k))
        else:
            vals.append((payload.get(k) or "").strip() or None)
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE quotations SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Quotation templates
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/quotation-templates", methods=["GET", "POST"])
@login_required
def osc_quotation_templates_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        limit = max(1, min(1000, int(request.args.get("limit") or "300")))
        sql = "SELECT id, name, description, items, notes, is_default, updated_date, created_date FROM quotation_templates WHERE 1=1 "
        params = []
        if q:
            like = f"%{q}%"
            sql += "AND (name LIKE %s OR description LIKE %s OR notes LIKE %s) "
            params.extend([like, like, like])
        sql += "ORDER BY is_default DESC, updated_date DESC, created_date DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows})
    payload = request.get_json() or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    is_default = 1 if str(payload.get("is_default") or "").strip().lower() in {"1", "true", "yes", "on"} else 0
    result, _ = _osc_exec(
        "INSERT INTO quotation_templates (name, description, items, notes, is_default) VALUES (%s,%s,%s,%s,%s)",
        (
            name,
            (payload.get("description") or "").strip() or None,
            _osc_json_or_wrap(payload.get("items"), fallback_key="items"),
            (payload.get("notes") or "").strip() or None,
            is_default,
        ),
        fetch="none",
    )
    return jsonify({"ok": True, "id": result.get("lastrowid"), "result": result})


@osc_bp.route("/api/osc/quotation-templates/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_quotation_template_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM quotation_templates WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM quotation_templates WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    allowed = ["name", "description", "items", "notes", "is_default"]
    sets, vals = [], []
    for k in allowed:
        if k not in payload:
            continue
        sets.append(f"{k}=%s")
        if k == "is_default":
            vals.append(1 if str(payload.get(k) or "").strip().lower() in {"1", "true", "yes", "on"} else 0)
        elif k == "items":
            vals.append(_osc_json_or_wrap(payload.get("items"), fallback_key="items"))
        else:
            vals.append((payload.get(k) or "").strip() or None)
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE quotation_templates SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Calendar events
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/calendar/events", methods=["GET", "POST"])
@login_required
def osc_calendar_events_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        case_number = (request.args.get("case_number") or "").strip()
        start_date = (request.args.get("start_date") or "").strip()
        end_date = (request.args.get("end_date") or "").strip()
        limit = max(1, min(1000, int(request.args.get("limit") or "300")))
        sql = (
            "SELECT id, event_id, title, summary, description, start_date, end_date, color, location, is_all_day, reminder_minutes, case_number, created_date, updated_date "
            "FROM calendar_events WHERE 1=1 "
        )
        params = []
        if case_number:
            sql += "AND case_number=%s "
            params.append(case_number)
        if start_date:
            sql += "AND start_date >= %s "
            params.append(start_date)
        if end_date:
            sql += "AND end_date <= %s "
            params.append(end_date)
        if q:
            like = f"%{q}%"
            sql += "AND (title LIKE %s OR summary LIKE %s OR description LIKE %s OR location LIKE %s OR case_number LIKE %s) "
            params.extend([like, like, like, like, like])
        sql += "ORDER BY start_date DESC, id DESC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows})

    payload = request.get_json() or {}
    title = (payload.get("title") or "").strip()
    start = (payload.get("start_date") or "").strip()
    end = (payload.get("end_date") or "").strip()
    if not title or not start or not end:
        return jsonify({"ok": False, "error": "title/start_date/end_date required"}), 400
    event_id = (payload.get("event_id") or "").strip() or f"osc-{uuid.uuid4().hex[:20]}"
    is_all_day = 1 if str(payload.get("is_all_day") or "").strip().lower() in {"1", "true", "yes", "on"} else 0
    reminder = _osc_safe_int(payload.get("reminder_minutes"), 0)
    result, _ = _osc_exec(
        "INSERT INTO calendar_events (event_id, title, summary, description, start_date, end_date, color, location, is_all_day, reminder_minutes, raw_data, case_number) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            event_id,
            title,
            (payload.get("summary") or "").strip() or None,
            (payload.get("description") or "").strip() or None,
            start,
            end,
            (payload.get("color") or "#3498db").strip() or "#3498db",
            (payload.get("location") or "").strip() or None,
            is_all_day,
            reminder,
            _osc_json_or_wrap(payload.get("raw_data"), fallback_key="raw_data"),
            (payload.get("case_number") or "").strip() or None,
        ),
        fetch="none",
    )
    return jsonify({"ok": True, "id": result.get("lastrowid"), "event_id": event_id, "result": result})


@osc_bp.route("/api/osc/calendar/events/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_calendar_event_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM calendar_events WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM calendar_events WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})

    payload = request.get_json() or {}
    allowed = [
        "event_id", "title", "summary", "description", "start_date", "end_date",
        "color", "location", "is_all_day", "reminder_minutes", "raw_data", "case_number"
    ]
    sets, vals = [], []
    for k in allowed:
        if k not in payload:
            continue
        sets.append(f"{k}=%s")
        if k in {"is_all_day", "reminder_minutes"}:
            vals.append(_osc_safe_int(payload.get(k), 0))
        elif k == "raw_data":
            vals.append(_osc_json_or_wrap(payload.get("raw_data"), fallback_key="raw_data"))
        else:
            vals.append((payload.get(k) or "").strip() or None)
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE calendar_events SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Clients
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/clients", methods=["GET", "POST"])
@login_required
def osc_clients_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        limit = max(1, min(500, int(request.args.get("limit") or "200")))
        if q:
            like = f"%{q}%"
            rows, _ = _osc_exec(
                """
                SELECT id, name, contact_person, phone, email, address, tax_id, notes, status, updated_date, created_date
                FROM clients
                WHERE name LIKE %s OR phone LIKE %s OR email LIKE %s
                ORDER BY updated_date DESC, created_date DESC
                LIMIT %s
                """,
                (like, like, like, limit),
                fetch="all",
            )
        else:
            rows, _ = _osc_exec(
                """
                SELECT id, name, contact_person, phone, email, address, tax_id, notes, status, updated_date, created_date
                FROM clients
                ORDER BY updated_date DESC, created_date DESC
                LIMIT %s
                """,
                (limit,),
                fetch="all",
            )
        return jsonify({"ok": True, "items": rows})
    payload = request.get_json() or {}
    row_id = (payload.get("id") or f"webc-{uuid.uuid4().hex[:12]}").strip()
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    cols = ["id", "name", "contact_person", "phone", "email", "address", "tax_id", "notes", "status"]
    vals = [
        row_id,
        name,
        (payload.get("contact_person") or "").strip() or None,
        (payload.get("phone") or "").strip() or None,
        (payload.get("email") or "").strip() or None,
        (payload.get("address") or "").strip() or None,
        (payload.get("tax_id") or "").strip() or None,
        (payload.get("notes") or "").strip() or None,
        (payload.get("status") or "進行中").strip() or "進行中",
    ]
    result, _ = _osc_exec(
        f"INSERT INTO clients ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
        tuple(vals),
        fetch="none",
    )
    return jsonify({"ok": True, "result": result, "id": row_id})


@osc_bp.route("/api/osc/clients/<row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_client_detail_api(row_id):
    row_id = (row_id or "").strip()
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM clients WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM clients WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    allowed = ["name", "contact_person", "phone", "email", "address", "tax_id", "notes", "status"]
    sets = []
    vals = []
    for k in allowed:
        if k in payload:
            sets.append(f"{k}=%s")
            vals.append((payload.get(k) or "").strip() or None)
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    sets.append("updated_date=NOW()")
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE clients SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Meetings
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/meetings", methods=["GET", "POST"])
@login_required
def osc_meetings_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        limit = max(1, min(500, int(request.args.get("limit") or "200")))
        if q:
            like = f"%{q}%"
            rows, _ = _osc_exec(
                """
                SELECT id, case_number, client_name, type, datetime, duration, location, notes, reminder, reminder_time, status, todo_id
                FROM meetings
                WHERE case_number LIKE %s OR client_name LIKE %s OR type LIKE %s OR notes LIKE %s
                ORDER BY datetime DESC, id DESC
                LIMIT %s
                """,
                (like, like, like, like, limit),
                fetch="all",
            )
        else:
            rows, _ = _osc_exec(
                """
                SELECT id, case_number, client_name, type, datetime, duration, location, notes, reminder, reminder_time, status, todo_id
                FROM meetings
                ORDER BY datetime DESC, id DESC
                LIMIT %s
                """,
                (limit,),
                fetch="all",
            )
        return jsonify({"ok": True, "items": rows})
    payload = request.get_json() or {}
    client_name = (payload.get("client_name") or "").strip()
    meeting_type = (payload.get("type") or "").strip()
    when = (payload.get("datetime") or "").strip()
    if not client_name or not meeting_type or not when:
        return jsonify({"ok": False, "error": "client_name/type/datetime required"}), 400
    when = when.replace("T", " ")
    cols = ["case_number", "client_name", "type", "datetime", "duration", "location", "notes", "reminder", "reminder_time", "status"]
    vals = [
        (payload.get("case_number") or "").strip() or None,
        client_name,
        meeting_type,
        when,
        int(payload.get("duration") or 60),
        (payload.get("location") or "").strip() or None,
        (payload.get("notes") or "").strip() or None,
        int(payload.get("reminder") if payload.get("reminder") is not None else 1),
        int(payload.get("reminder_time") or 30),
        (payload.get("status") or "scheduled").strip() or "scheduled",
    ]
    result, _ = _osc_exec(
        f"INSERT INTO meetings ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
        tuple(vals),
        fetch="none",
    )
    return jsonify({"ok": True, "result": result})


@osc_bp.route("/api/osc/meetings/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_meeting_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM meetings WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM meetings WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    allowed = ["case_number", "client_name", "type", "datetime", "duration", "location", "notes", "reminder", "reminder_time", "status", "todo_id"]
    sets = []
    vals = []
    for k in allowed:
        if k in payload:
            sets.append(f"{k}=%s")
            val = payload.get(k)
            if k == "datetime" and val:
                val = str(val).replace("T", " ")
            vals.append(val)
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE meetings SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Todos
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/todos", methods=["GET", "POST"])
@login_required
def osc_todos_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        limit = max(1, min(500, int(request.args.get("limit") or "200")))
        if q:
            like = f"%{q}%"
            rows, _ = _osc_exec(
                """
                SELECT id, case_number, client_name, todo_type, todo_date, todo_time, description, status, source_file, created_date, completed_date
                FROM case_todos
                WHERE case_number LIKE %s OR client_name LIKE %s OR todo_type LIKE %s OR description LIKE %s
                ORDER BY todo_date DESC, id DESC
                LIMIT %s
                """,
                (like, like, like, like, limit),
                fetch="all",
            )
        else:
            rows, _ = _osc_exec(
                """
                SELECT id, case_number, client_name, todo_type, todo_date, todo_time, description, status, source_file, created_date, completed_date
                FROM case_todos
                ORDER BY todo_date DESC, id DESC
                LIMIT %s
                """,
                (limit,),
                fetch="all",
            )
        return jsonify({"ok": True, "items": rows})
    payload = request.get_json() or {}
    case_number = (payload.get("case_number") or "").strip()
    todo_type = (payload.get("todo_type") or "").strip()
    if not case_number or not todo_type:
        return jsonify({"ok": False, "error": "case_number/todo_type required"}), 400
    cols = ["case_number", "client_name", "todo_type", "todo_date", "todo_time", "description", "status", "source_file"]
    vals = [
        case_number,
        (payload.get("client_name") or "").strip() or None,
        todo_type,
        (payload.get("todo_date") or "").strip() or None,
        (payload.get("todo_time") or "").strip() or None,
        (payload.get("description") or "").strip() or None,
        (payload.get("status") or "pending").strip() or "pending",
        (payload.get("source_file") or "").strip() or None,
    ]
    result, _ = _osc_exec(
        f"INSERT INTO case_todos ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
        tuple(vals),
        fetch="none",
    )
    return jsonify({"ok": True, "result": result})


@osc_bp.route("/api/osc/todos/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_todo_detail_api(row_id):
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM case_todos WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM case_todos WHERE id=%s", (row_id,), fetch="none")
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    allowed = ["case_number", "client_name", "todo_type", "todo_date", "todo_time", "description", "status", "source_file", "google_calendar_id", "google_calendar_event_id"]
    sets = []
    vals = []
    for k in allowed:
        if k in payload:
            sets.append(f"{k}=%s")
            vals.append((payload.get(k) or "").strip() or None)
    if "status" in payload:
        if _osc_is_todo_done_status(payload.get("status") or ""):
            sets.append("completed_date=COALESCE(completed_date, NOW())")
        else:
            sets.append("completed_date=NULL")
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE case_todos SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True, "result": result})


# Public release: legal-research/opinion-library routes are intentionally removed.


# ══════════════════════════════════════════════════════════════════════════════
# Forms (preview / export)
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/forms/preview", methods=["POST"])
@login_required
def osc_forms_preview_api():
    payload = request.get_json() or {}
    form_type = (payload.get("form_type") or "").strip()
    if not form_type:
        return jsonify({"ok": False, "error": "form_type required"}), 400
    case_row = _osc_get_case_identity_by_payload(payload)
    fields = payload.get("fields") or {}
    if form_type == "legal_attest":
        content = fields.get("notes") or "(內文空白)"
        doc = (
            f"存證信函預覽\n\n"
            f"寄件人：{fields.get('sender_name')}\n"
            f"寄件地址：{fields.get('sender_addr')}\n"
            f"收件人：{fields.get('receiver_name')}\n"
            f"收件地址：{fields.get('receiver_addr')}\n"
            f"內文預覽：\n{content}\n\n（按下「匯出 WORD + PDF」即會產生符合郵局格式之對齊版式 PDF 歸檔）"
        )
        return jsonify({
            "ok": True,
            "case": case_row,
            "form_type": "legal_attest",
            "title": "存證信函草稿",
            "preview_text": doc,
            "suggested_filename": "legal_attest"
        })

    try:
        out = _osc_build_form_preview(form_type, case_row, fields if isinstance(fields, dict) else {})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "case": case_row, **out})


@osc_bp.route("/api/osc/forms/export", methods=["POST"])
@login_required
def osc_forms_export_api():
    try:
        _record_last_public_base_url()
    except Exception:
        _log.debug("silent-catch _record_last_public_base_url", exc_info=True)
    payload = request.get_json() or {}
    form_type = (payload.get("form_type") or "").strip()
    if not form_type:
        return jsonify({"ok": False, "error": "form_type required"}), 400
    case_row = _osc_get_case_identity_by_payload(payload)
    fields = payload.get("fields") or {}
    if form_type == "legal_attest":
        from skills.legal_attest.generator import core
        export_dir = f"{_MAGI_ROOT}/exports"
        os.makedirs(export_dir, exist_ok=True)
        filename_base = f"legal_attest_{uuid.uuid4().hex[:8]}"
        pdf_path = os.path.join(export_dir, f"{filename_base}.pdf")

        sender_name_list = [[fields.get("sender_name") or ""]]
        sender_addr_list = [fields.get("sender_addr") or ""]
        receiver_name_list = [[fields.get("receiver_name") or ""]]
        receiver_addr_list = [fields.get("receiver_addr") or ""]
        content = fields.get("notes") or "(內文空白)"

        try:
            core.generate_text_and_letter(
                sender_name_list, sender_addr_list,
                receiver_name_list, receiver_addr_list,
                [], [],
                content
            )
            core.merge_text_and_letter(pdf_path)
            core.clean_temp_files()
        except Exception as e:
            return jsonify({"ok": False, "error": f"產生存證信函失敗: {e}"}), 500

        public_url = f"{_get_public_base_url()}/exports/{filename_base}.pdf"
        doc = (
            f"存證信函已產出！\n\n"
            f"寄件人：{fields.get('sender_name')}\n"
            f"收件人：{fields.get('receiver_name')}\n"
            f"內文預覽：\n{content}"
        )
        return jsonify(
            {
                "ok": True,
                "case": case_row,
                "form_type": "legal_attest",
                "title": "存證信函預覽",
                "preview_text": doc,
                "export": {"success": True},
                "export_pdf": {"success": True, "url": public_url},
                "export_docx": {"success": False},
                "export_errors": [],
            }
        )

    try:
        out = _osc_build_form_preview(form_type, case_row, fields if isinstance(fields, dict) else {})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    actual_form_type = out.get("form_type")
    if actual_form_type in ["power_of_attorney", "receipt", "contract"]:
        from api.osc_document_generator import generate_receipt, generate_poa, generate_engagement_agreement

        export_dir = f"{_MAGI_ROOT}/exports"
        os.makedirs(export_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:8]
        filename_base = f"{actual_form_type}_{stamp}_{token}"
        docx_path = os.path.join(export_dir, f"{filename_base}.docx")

        data = dict(case_row)
        for k, v in (fields if isinstance(fields, dict) else {}).items():
            if v: data[k] = v

        data['案號'] = data.get('court_case_no', '')
        data['股別'] = data.get('court_branch', '')
        data['委任人/當事人'] = data.get('client_name', '')
        data['案由/事件'] = data.get('case_reason', '')
        data['受任律師'] = data.get('lawyer_name', '')
        data['通訊地址'] = data.get('address', '')
        data['聯絡電話'] = data.get('phone', '')
        data['身分證字號'] = data.get('tax_id', '')
        data['委任範圍'] = data.get('item', '')
        data['金額'] = data.get('amount', '')
        data['委任費用(數字)'] = data.get('amount', '')
        data['法院/檢察署'] = data.get('court_name', '')
        data['取代日期'] = data.get('date', '')

        config = {}
        try:
            config['company_name'] = '偵理法律事務所'
            config['default_lawyer'] = '喬政翔'
        except Exception:
            _log.debug("silent-catch config defaults", exc_info=True)

        try:
            if actual_form_type == "receipt":
                doc = generate_receipt(data, data.get('item') or '法律服務費', config)
            elif actual_form_type == "power_of_attorney":
                case_type = '民事'
                role = '代理人'
                cat = str(data.get('case_category', ''))
                if '刑' in cat:
                    case_type = '刑事'
                    role = '辯護人' if '被告' in str(data.get('client_role', '')) else '告訴代理人'
                elif '行' in cat:
                    case_type = '行政'
                doc = generate_poa(data, case_type, role, config)
            elif actual_form_type == "contract":
                doc = generate_engagement_agreement(data, config)

            doc.save(docx_path)
            docx_meta = _export_file_meta(docx_path)
            exported = {
                "success": docx_meta.get("success"),
                "export": docx_meta,
                "export_docx": docx_meta,
                "export_pdf": {"success": False, "error": "pdf_conversion_skip"},
                "errors": [] if docx_meta.get("success") else [{"type": "docx", "error": docx_meta.get("error")}]
            }
        except Exception as e:
            exported = {"success": False, "errors": [{"type": "generator", "error": str(e)}], "export_docx": {}, "export_pdf": {}}
    else:
        exported = _export_osc_form_files(
            out.get("title") or out.get("form_type") or "OSC 文件",
            out.get("preview_text") or "",
            out.get("suggested_filename") or "osc_form",
        )
    return jsonify(
        {
            "ok": bool(exported.get("success")),
            "case": case_row,
            **out,
            "export": exported.get("export") or {"success": False},
            "export_docx": exported.get("export_docx") or {"success": False},
            "export_pdf": exported.get("export_pdf") or {"success": False},
            "export_errors": exported.get("errors") or [],
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# Document stamping (正本/副本/繕本 + 附委任狀 + 繕本已送對造)
# 對應原版 osc.py:25984 _add_overlays_and_stamp 桌面功能
# 後端委派給 skills/doc-producer/action.py（PyMuPDF 實作）
# ══════════════════════════════════════════════════════════════════════════════


def _osc_load_doc_producer_action():
    import importlib.util

    skill_script = os.path.join(_MAGI_ROOT, "skills", "doc-producer", "action.py")
    spec = importlib.util.spec_from_file_location("magi_doc_producer_action", skill_script)
    if not spec or not spec.loader:
        raise RuntimeError("doc-producer action.py cannot be loaded")
    doc_producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doc_producer)
    return doc_producer


def _osc_stamp_center_from_payload(payload: dict) -> dict[str, float] | None:
    raw = payload.get("stamp_center") or payload.get("manual_stamp_coords")
    if raw in (None, "", False):
        return None
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        raw = {"x": raw[0], "y": raw[1]}
    if not isinstance(raw, dict):
        raise ValueError("stamp_center must be {x, y}")
    try:
        x = float(raw.get("x"))
        y = float(raw.get("y"))
    except Exception as exc:
        raise ValueError("stamp_center.x/y must be numbers") from exc
    if x < 0 or y < 0:
        raise ValueError("stamp_center.x/y must be positive")
    return {"x": x, "y": y}


def _osc_prepare_stamp_preview_pdf(src: Path, *, normalize: bool, output_dir: Path) -> tuple[Path, list[Path]]:
    doc_producer = _osc_load_doc_producer_action()
    convert_docx_to_pdf = doc_producer.convert_docx_to_pdf

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    temp_files: list[Path] = []
    ext = src.suffix.lower()
    preview_pdf = output_dir / f"{src.stem}_{stamp}_stamp_preview_base.pdf"
    temp_files.append(preview_pdf)
    if ext == ".pdf":
        shutil.copy2(src, preview_pdf)
    elif ext in {".docx", ".doc"}:
        converted = convert_docx_to_pdf(str(src), str(preview_pdf))
        if not converted.get("success"):
            raise RuntimeError(f"DOCX 轉 PDF 失敗：{converted.get('error') or ''}")
    else:
        raise ValueError(f"unsupported file type: {ext} (only .pdf/.docx/.doc)")

    if normalize:
        rotated = output_dir / f"{src.stem}_{stamp}_stamp_preview_rotated.pdf"
        temp_files.append(rotated)
        _osc_rotate_pdf_to_portrait(preview_pdf, rotated)
        return rotated, temp_files
    return preview_pdf, temp_files


@osc_bp.route("/api/osc/documents/stamp-preview", methods=["POST"])
@login_required
def osc_documents_stamp_preview_api():
    """回傳書狀最後一頁預覽，供網頁版手動點選律師章位置。"""
    import fitz

    payload = request.get_json(silent=True) or {}
    file_path = (payload.get("file_path") or "").strip()
    normalize = bool(payload.get("normalize"))
    if not file_path:
        return jsonify({"ok": False, "error": "file_path required"}), 400

    candidates = _osc_local_path_candidates(file_path)
    abs_path = _osc_resolve_existing_local_path(candidates)
    if not abs_path:
        return jsonify({"ok": False, "error": f"file not found: {file_path}"}), 404
    if not _osc_is_safe_local_path(abs_path):
        return jsonify({"ok": False, "error": f"file not in allowed roots: {abs_path}"}), 403

    src = Path(abs_path)
    temp_files: list[Path] = []
    try:
        preview_pdf, temp_files = _osc_prepare_stamp_preview_pdf(src, normalize=normalize, output_dir=src.parent)
        with fitz.open(preview_pdf) as doc:
            if doc.page_count < 1:
                return jsonify({"ok": False, "error": "PDF has no pages"}), 400
            page_index = doc.page_count - 1
            page = doc[page_index]
            matrix = fitz.Matrix(1.45, 1.45)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image_bytes = pix.tobytes("png")
            image_data = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
            return jsonify(
                {
                    "ok": True,
                    "image_data": image_data,
                    "page_index": page_index,
                    "page_width": page.rect.width,
                    "page_height": page.rect.height,
                    "rendered_width": pix.width,
                    "rendered_height": pix.height,
                    "normalize": normalize,
                }
            )
    except Exception as e:
        _log.exception("stamp preview failed")
        return jsonify({"ok": False, "error": f"產生蓋章預覽失敗：{e}"}), 500
    finally:
        for temp_path in temp_files:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                _log.debug("silent-catch cleanup stamp preview temp: %s", temp_path, exc_info=True)


@osc_bp.route("/api/osc/documents/stamp", methods=["POST"])
@login_required
def osc_documents_stamp_api():
    """書狀蓋章：在 PDF 加 正本/副本/繕本 + 可選 附委任狀 / 繕本已送對造。

    Request:
        {
            "file_path": "/abs/path/to/file.pdf or .docx",
            "copy_type": "正本" | "副本" | "繕本",
            "add_poa": bool,
            "add_sent_to_opponent": bool
        }

    DOCX 會先轉 PDF 再蓋章；PDF 直接蓋章。
    """
    import subprocess as _sp

    payload = request.get_json(silent=True) or {}
    file_path = (payload.get("file_path") or "").strip()
    copy_type = (payload.get("copy_type") or "正本").strip()
    add_poa = bool(payload.get("add_poa"))
    add_sent_to_opponent = bool(payload.get("add_sent_to_opponent"))
    try:
        stamp_center = _osc_stamp_center_from_payload(payload)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if not file_path:
        return jsonify({"ok": False, "error": "file_path required"}), 400
    if copy_type not in ("正本", "副本", "繕本"):
        return jsonify({"ok": False, "error": "copy_type must be 正本/副本/繕本"}), 400

    # 路徑安全性：解析 + allowed-roots 檢查
    candidates = _osc_local_path_candidates(file_path)
    abs_path = _osc_resolve_existing_local_path(candidates)
    if not abs_path:
        return jsonify({"ok": False, "error": f"file not found: {file_path}"}), 404
    if not _osc_is_safe_local_path(abs_path):
        return jsonify({"ok": False, "error": f"file not in allowed roots: {abs_path}"}), 403

    abs_str = str(abs_path)
    ext = os.path.splitext(abs_str)[1].lower()

    if ext == ".pdf":
        task = "mark"
        skill_payload = {
            "input": abs_str,
            "copy_type": copy_type,
            "add_poa": add_poa,
            "add_sent_to_opponent": add_sent_to_opponent,
            "stamp_image": _osc_photo_path("lawyer_stamp.png"),
        }
        if stamp_center:
            skill_payload["stamp_center"] = stamp_center
    elif ext in (".docx", ".doc"):
        task = "produce"
        skill_payload = {
            "input": abs_str,
            "copy_type": copy_type,
            "add_poa": add_poa,
            "add_sent_to_opponent": add_sent_to_opponent,
            "stamp_image": _osc_photo_path("lawyer_stamp.png"),
        }
        if stamp_center:
            skill_payload["stamp_center"] = stamp_center
    else:
        return jsonify(
            {"ok": False, "error": f"unsupported file type: {ext} (only .pdf/.docx/.doc)"}
        ), 400

    # 呼叫 doc-producer skill
    try:
        from api.runtime_paths import get_skill_python
        skill_python = str(get_skill_python())
    except Exception as e:
        return jsonify({"ok": False, "error": f"runtime python not available: {e}"}), 500

    skill_script = os.path.join(_MAGI_ROOT, "skills", "doc-producer", "action.py")
    if not os.path.isfile(skill_script):
        return jsonify({"ok": False, "error": "doc-producer skill missing"}), 500

    task_arg = f"{task} {json.dumps(skill_payload, ensure_ascii=False)}"

    try:
        proc = _sp.run(
            [skill_python, skill_script, "--task", task_arg],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except _sp.TimeoutExpired:
        return jsonify({"ok": False, "error": "skill timeout (180s)"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": f"skill invocation failed: {e}"}), 500

    out = (proc.stdout or "").strip()
    if not out:
        return jsonify(
            {"ok": False, "error": f"skill no stdout. stderr: {(proc.stderr or '')[:300]}"}
        ), 500

    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": f"skill output parse failed: {out[:300]}"}), 500

    if not result.get("success"):
        return jsonify({"ok": False, "error": result.get("error") or "skill returned success=false"}), 500

    # 取結果路徑：mark → output；produce → outputs.marked / outputs.merged / outputs.pdf
    output_path = (
        result.get("output")
        or (result.get("outputs") or {}).get("merged")
        or (result.get("outputs") or {}).get("marked")
        or (result.get("outputs") or {}).get("pdf")
        or ""
    )

    # log activity（best-effort）
    try:
        _osc_log_activity(
            "stamp_document",
            "document",
            file_path,
            json.dumps(
                {
                    "copy_type": copy_type,
                    "add_poa": add_poa,
                    "add_sent_to_opponent": add_sent_to_opponent,
                    "output": output_path,
                    "task": task,
                    "stamp_center": stamp_center,
                },
                ensure_ascii=False,
            ),
        )
    except Exception:
        _log.debug("silent-catch _osc_log_activity stamp", exc_info=True)

    return jsonify(
        {
            "ok": True,
            "input_path": abs_str,
            "output_path": output_path,
            "copy_type": copy_type,
            "add_poa": add_poa,
            "add_sent_to_opponent": add_sent_to_opponent,
            "stamp_center": stamp_center,
            "task": task,
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# Document finalization (正本/繕本/留底 + 證據編號 + 合併)
# 對應原版 osc.py:26446 finalize_and_generate_pdf → generate_final_pdf_part*
# ══════════════════════════════════════════════════════════════════════════════

_OSC_EVIDENCE_RE = re.compile(
    r"^(?P<type>原證|被證|告證|甲證|乙證|上證|被上證|相證|抗證|聲證|附證|附件|陳件|證據|證物)\s*(?P<num_str>[\d一二三四五六七八九十]*)"
)


def _osc_parse_evidence_number(num_str: str) -> int:
    if not num_str:
        return 0
    try:
        return int(num_str)
    except ValueError:
        mapping = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        if num_str in mapping:
            return mapping[num_str]
        if num_str.endswith("十") and len(num_str) <= 2:
            if len(num_str) == 1:
                return 10
            return mapping.get(num_str[0], 99) * 10
    return 999


def _osc_collect_evidence_pdfs(folder: Path, *, source_path: Path, max_num: int = 50) -> list[tuple[int, str, Path, str]]:
    items: list[tuple[int, int, str, Path, str]] = []
    if not folder.is_dir():
        return []
    for pdf_path in folder.glob("*.pdf"):
        if pdf_path.resolve() == source_path.resolve():
            continue
        stem = pdf_path.stem
        if "_temp" in stem or stem.endswith("_含證據"):
            continue
        match = _OSC_EVIDENCE_RE.search(stem)
        if not match:
            continue
        evid_type = match.group("type")
        num_str = match.group("num_str") or ""
        number = _osc_parse_evidence_number(num_str)
        priority = 0 if number < max_num and evid_type in {"附件", "附證"} else (1 if number < max_num else 2)
        items.append((priority, number, evid_type, pdf_path, num_str))
    items.sort(key=lambda x: (x[0], x[1], x[2], x[3].name))
    return [(number, evid_type, pdf_path, num_str) for _, number, evid_type, pdf_path, num_str in items]


def _osc_rotate_pdf_to_portrait(input_path: Path, output_path: Path) -> None:
    import fitz

    with fitz.open(input_path) as src:
        out = fitz.open()
        try:
            for page in src:
                if page.rect.width > page.rect.height:
                    page.set_rotation((page.rotation + 90) % 360)
                out.insert_pdf(src, from_page=page.number, to_page=page.number)
            out.save(output_path, garbage=4, deflate=True, clean=True)
        finally:
            out.close()


def _osc_add_vertical_evidence_label(input_path: Path, output_path: Path, evid_type: str, num_str: str, font_size: float = 10.0) -> None:
    import fitz

    label = f"{evid_type}{num_str}"
    with fitz.open(input_path) as doc:
        if doc.page_count:
            page = doc[0]
            x_start = page.rect.width * 0.96
            y_cursor = page.rect.height * 0.04
            for char in label:
                page.insert_text((x_start, y_cursor), char, fontsize=font_size, fontname="china-ss", color=(0, 0, 0))
                y_cursor += font_size * 1.3
        doc.save(output_path, garbage=4, deflate=True, clean=True)


@osc_bp.route("/api/osc/documents/finalize", methods=["POST"])
@login_required
def osc_documents_finalize_api():
    """定稿書狀：產生正本、指定份數繕本、留底，並把同資料夾證據 PDF 編號後合併。"""
    import fitz

    payload = request.get_json(silent=True) or {}
    file_path = (payload.get("file_path") or "").strip()
    if not file_path:
        return jsonify({"ok": False, "error": "file_path required"}), 400

    candidates = _osc_local_path_candidates(file_path)
    abs_path = _osc_resolve_existing_local_path(candidates)
    if not abs_path:
        return jsonify({"ok": False, "error": f"file not found: {file_path}"}), 404
    if not _osc_is_safe_local_path(abs_path):
        return jsonify({"ok": False, "error": f"file not in allowed roots: {abs_path}"}), 403

    try:
        num_copies = max(0, min(20, int(payload.get("num_copies") if payload.get("num_copies") is not None else 1)))
    except Exception:
        return jsonify({"ok": False, "error": "num_copies must be integer"}), 400
    add_poa = bool(payload.get("add_poa"))
    add_sent_to_opponent = bool(payload.get("add_sent_to_opponent"))
    include_evidence = payload.get("include_evidence", True) is not False
    try:
        stamp_center = _osc_stamp_center_from_payload(payload)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    src = Path(abs_path)
    ext = src.suffix.lower()
    if ext not in {".pdf", ".docx", ".doc"}:
        return jsonify({"ok": False, "error": f"unsupported file type: {ext} (only .pdf/.docx/.doc)"}), 400

    try:
        doc_producer = _osc_load_doc_producer_action()
        convert_docx_to_pdf = doc_producer.convert_docx_to_pdf
        mark_copy_type = doc_producer.mark_copy_type
    except Exception as e:
        return jsonify({"ok": False, "error": f"doc-producer skill unavailable: {e}"}), 500

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = src.parent
    base_name = src.stem
    temp_files: list[Path] = []
    outputs: dict[str, object] = {}

    try:
        base_pdf = output_dir / f"{base_name}_{stamp}_base_temp.pdf"
        temp_files.append(base_pdf)
        if ext == ".pdf":
            shutil.copy2(src, base_pdf)
        else:
            converted = convert_docx_to_pdf(str(src), str(base_pdf))
            if not converted.get("success"):
                return jsonify({"ok": False, "error": f"DOCX 轉 PDF 失敗：{converted.get('error') or ''}"}), 500

        rotated_base = output_dir / f"{base_name}_{stamp}_rotated_base_temp.pdf"
        temp_files.append(rotated_base)
        _osc_rotate_pdf_to_portrait(base_pdf, rotated_base)

        components: list[Path] = []
        copy_specs: list[tuple[str, bool, bool]] = [("正本", add_poa, add_sent_to_opponent)]
        copy_specs.extend(("繕本", False, False) for _ in range(num_copies))
        copy_specs.append(("留底", False, False))

        copy_counts: dict[str, int] = {}
        for copy_type, poa, sent in copy_specs:
            copy_counts[copy_type] = copy_counts.get(copy_type, 0) + 1
            suffix = copy_type if copy_counts[copy_type] == 1 else f"{copy_type}{copy_counts[copy_type]}"
            out_path = output_dir / f"{base_name}_{stamp}_{suffix}.pdf"
            temp_files.append(out_path)
            marked = mark_copy_type(
                str(rotated_base),
                output_pdf=str(out_path),
                copy_type=copy_type,
                add_poa=poa,
                add_sent_to_opponent=sent,
                stamp_image=_osc_photo_path("lawyer_stamp.png"),
                stamp_center=stamp_center,
            )
            if not marked.get("success"):
                return jsonify({"ok": False, "error": f"{copy_type} 標記失敗：{marked.get('error') or ''}"}), 500
            components.append(out_path)

        evidence_outputs: list[Path] = []
        if include_evidence:
            for _, evid_type, evid_path, num_str in _osc_collect_evidence_pdfs(src.parent, source_path=src):
                rotated_evidence = output_dir / f"{base_name}_{stamp}_rotated_evid_{evid_type}{num_str}.pdf"
                labeled_evidence = output_dir / f"{base_name}_{stamp}_evid_labeled_{evid_type}{num_str}.pdf"
                temp_files.extend([rotated_evidence, labeled_evidence])
                _osc_rotate_pdf_to_portrait(evid_path, rotated_evidence)
                _osc_add_vertical_evidence_label(rotated_evidence, labeled_evidence, evid_type, num_str)
                evidence_outputs.append(labeled_evidence)

        merged = fitz.open()
        try:
            for component in components:
                with fitz.open(component) as comp_doc:
                    merged.insert_pdf(comp_doc)
                for evidence in evidence_outputs:
                    with fitz.open(evidence) as evid_doc:
                        merged.insert_pdf(evid_doc)
            copies_text = f"(正{num_copies}繕留存)" if num_copies > 0 else "(正留存)"
            final_path = output_dir / f"{base_name}_{stamp}{copies_text}_含證據.pdf"
            merged.save(final_path, garbage=4, deflate=True, clean=True)
        finally:
            merged.close()

        outputs = {
            "final": str(final_path),
            "copies": [str(p) for p in components],
            "evidence": [str(p) for p in evidence_outputs],
            "evidence_count": len(evidence_outputs),
            "num_copies": num_copies,
            "stamp_center": stamp_center,
            "final_meta": _export_file_meta(str(final_path)),
        }

        try:
            _osc_log_activity(
                "finalize_document",
                "document",
                str(src),
                json.dumps(outputs, ensure_ascii=False),
            )
        except Exception:
            _log.debug("silent-catch _osc_log_activity finalize", exc_info=True)

        return jsonify({"ok": True, "input_path": str(src), "output_path": str(final_path), "outputs": outputs, "message": "定稿 PDF 已產出並合併完成"})
    except Exception as e:
        _log.exception("document finalize failed")
        return jsonify({"ok": False, "error": f"定稿 PDF 產生失敗：{e}"}), 500
    finally:
        for temp_path in temp_files:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                _log.debug("silent-catch cleanup finalize temp: %s", temp_path, exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
# CSV Import / Export — Cases & Clients (P1)
# ══════════════════════════════════════════════════════════════════════════════

# Cases 欄位對映（CSV 中文標題 → DB column）
# 2026-04-29: 5 個原本 IGNORED 的欄位（案件標的/開始日期/開庭日期/承辦律師/股別）
# 已透過 ensure_cases_schema 加入 cases 表，CSV 匯入匯出完整 round-trip。
_CASES_CSV_MAP = {
    "案件編號": "case_number",
    "當事人": "client_name",       # 必填
    "呼號": "client_name_en",
    "案件分類": "case_type",
    "案件類型": "case_type",
    "案件種類": "case_category",
    "案件標的": "case_subject",
    "案由": "case_reason",
    "狀態": "status",
    "開始日期": "start_date",
    "開庭日期": "court_date",
    "承辦律師": "lawyer",
    "法院案號": "court_case_no",
    "股別": "court_division",
    "法院/地檢署名稱": "court_name",
}

_CASES_CSV_HEADERS = [
    "案件編號", "當事人", "呼號", "案件分類", "案件種類", "案件標的", "案由",
    "狀態", "開始日期", "開庭日期", "承辦律師", "法院案號", "股別", "法院/地檢署名稱",
]

# Clients 欄位對映
_CLIENTS_CSV_MAP = {
    "姓名": "name",
    "name": "name",
    "聯絡人": "contact_person",
    "contact_person": "contact_person",
    "電話": "phone",
    "phone": "phone",
    "email": "email",
    "地址": "address",
    "address": "address",
    "統編": "tax_id",
    "tax_id": "tax_id",
}

_CLIENTS_CSV_HEADERS_ZH = ["姓名", "聯絡人", "電話", "email", "地址", "統編"]


def _parse_csv_date(s):
    """YYYY-MM-DD or YYYY/MM/DD → YYYY-MM-DD, or None."""
    if not s:
        return None
    s = s.strip().replace("/", "-")
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


@osc_bp.route("/api/osc/cases/import-csv", methods=["POST"])
@login_required
def osc_cases_import_csv_api():
    """CSV 案件批次匯入。

    Multipart form: file=<CSV>
    必填欄位：當事人
    回傳 {ok, imported, skipped, errors:[{row, reason}]}
    """
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "file required"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "file required"}), 400

    try:
        content = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        content = f.read().decode("big5", errors="replace")

    # 防呆：若上游 export 寫了重複 BOM，第一個 fieldname 會帶 ﻿ 前綴
    if content and content[0] == "﻿":
        content = content[1:]
    reader = csv.DictReader(io.StringIO(content))
    fieldnames = reader.fieldnames or []
    if "當事人" not in fieldnames:
        return jsonify({"ok": False, "error": "CSV 缺少必填欄位「當事人」"}), 400

    imported = 0
    skipped = 0
    errors = []

    for idx, row in enumerate(reader, start=2):  # row 1 = header
        client_name = (row.get("當事人") or "").strip()
        if not client_name:
            errors.append({"row": idx, "reason": "當事人欄位為空"})
            skipped += 1
            continue

        case_number = (row.get("案件編號") or "").strip()
        if not case_number:
            case_number = f"web-csv-{uuid.uuid4().hex[:12]}"

        # Check duplicate case_number
        try:
            existing, _ = _osc_exec(
                "SELECT id FROM cases WHERE case_number=%s LIMIT 1",
                (case_number,),
                fetch="one",
            )
            if existing:
                errors.append({"row": idx, "reason": f"案件編號 {case_number} 已存在"})
                skipped += 1
                continue
        except Exception as e:
            errors.append({"row": idx, "reason": f"查重失敗: {e}"})
            skipped += 1
            continue

        row_id = f"web-{uuid.uuid4().hex[:12]}"
        case_type = (row.get("案件分類") or row.get("案件類型") or "").strip() or None
        case_category = _osc_norm_case_category((row.get("案件種類") or "").strip())
        case_subject = (row.get("案件標的") or "").strip() or None
        case_reason = (row.get("案由") or "").strip() or None
        status = (row.get("狀態") or "進行中").strip() or "進行中"
        court_case_no = (row.get("法院案號") or "").strip() or None
        court_name = (row.get("法院/地檢署名稱") or "").strip() or None
        client_name_en = (row.get("呼號") or "").strip() or None
        start_date = _parse_csv_date(row.get("開始日期", ""))
        court_date = _parse_csv_date(row.get("開庭日期", ""))
        lawyer = (row.get("承辦律師") or "").strip() or None
        court_division = (row.get("股別") or "").strip() or None

        cols = [
            "id", "case_number", "client_name", "client_name_en",
            "case_type", "case_category", "case_subject", "case_reason",
            "court_case_no", "court_name", "court_division",
            "start_date", "court_date", "lawyer", "status",
        ]
        vals = [
            row_id, case_number, client_name, client_name_en,
            case_type, case_category or None, case_subject, case_reason,
            court_case_no, court_name, court_division,
            start_date, court_date, lawyer, status,
        ]
        try:
            _osc_exec(
                f"INSERT INTO cases ({','.join(cols)}) VALUES ({','.join(['%s']*len(cols))})",
                tuple(vals),
                fetch="none",
            )
            imported += 1
        except Exception as e:
            errors.append({"row": idx, "reason": str(e)})
            skipped += 1

    return jsonify({"ok": True, "imported": imported, "skipped": skipped, "errors": errors})


@osc_bp.route("/api/osc/cases/export-csv", methods=["GET"])
@login_required
def osc_cases_export_csv_api():
    """匯出全部案件為 CSV（中文 header，utf-8-sig，與匯入相容）。"""
    rows, _ = _osc_exec(
        """
        SELECT case_number, client_name, client_name_en, case_type, case_category,
               case_subject, case_reason, status, start_date, court_date,
               lawyer, court_case_no, court_division, court_name
        FROM cases
        ORDER BY updated_at DESC, created_date DESC
        """,
        (),
        fetch="all",
    )

    def _date_str(v):
        if not v:
            return ""
        try:
            if hasattr(v, "isoformat"):
                return v.isoformat()
        except Exception:
            pass
        return str(v)

    buf = io.StringIO()
    # 不要在這裡寫 BOM，下方 encode("utf-8-sig") 會加；
    # 否則雙 BOM 會讓 import 的 DictReader 把 BOM 當欄位名一部分（fieldname 變 "﻿案件編號"）。
    writer = csv.writer(buf)
    writer.writerow(_CASES_CSV_HEADERS)
    for r in rows:
        writer.writerow([
            r.get("case_number") or "",
            r.get("client_name") or "",
            r.get("client_name_en") or "",
            r.get("case_type") or "",
            r.get("case_category") or "",
            r.get("case_subject") or "",
            r.get("case_reason") or "",
            r.get("status") or "",
            _date_str(r.get("start_date")),
            _date_str(r.get("court_date")),
            r.get("lawyer") or "",
            r.get("court_case_no") or "",
            r.get("court_division") or "",
            r.get("court_name") or "",
        ])

    filename = f"案件資料匯出_{time.strftime('%Y%m%d')}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@osc_bp.route("/api/osc/clients/import-csv", methods=["POST"])
@login_required
def osc_clients_import_csv_api():
    """CSV 當事人批次匯入。

    Multipart form: file=<CSV>
    支援中英欄位混用。重複 (name, phone) 跳過。
    回傳 {ok, imported, skipped, errors:[{row, reason}]}
    """
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "file required"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "file required"}), 400

    try:
        content = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        content = f.read().decode("big5", errors="replace")

    if content and content[0] == "﻿":
        content = content[1:]
    reader = csv.DictReader(io.StringIO(content))
    fieldnames = reader.fieldnames or []
    # Accept 姓名 or name as required
    has_name_col = "姓名" in fieldnames or "name" in fieldnames
    if not has_name_col:
        return jsonify({"ok": False, "error": "CSV 缺少必填欄位「姓名」或「name」"}), 400

    imported = 0
    skipped = 0
    errors = []

    for idx, row in enumerate(reader, start=2):
        name = (row.get("姓名") or row.get("name") or "").strip()
        if not name:
            errors.append({"row": idx, "reason": "姓名欄位為空"})
            skipped += 1
            continue

        phone = (row.get("電話") or row.get("phone") or "").strip() or None

        # Deduplicate by (name, phone)
        try:
            if phone:
                dup, _ = _osc_exec(
                    "SELECT id FROM clients WHERE name=%s AND phone=%s LIMIT 1",
                    (name, phone),
                    fetch="one",
                )
            else:
                dup, _ = _osc_exec(
                    "SELECT id FROM clients WHERE name=%s AND (phone IS NULL OR phone='') LIMIT 1",
                    (name,),
                    fetch="one",
                )
            if dup:
                errors.append({"row": idx, "reason": f"{name} / {phone or '無電話'} 已存在"})
                skipped += 1
                continue
        except Exception as e:
            errors.append({"row": idx, "reason": f"查重失敗: {e}"})
            skipped += 1
            continue

        row_id = f"webc-{uuid.uuid4().hex[:12]}"
        contact_person = (row.get("聯絡人") or row.get("contact_person") or "").strip() or None
        email = (row.get("email") or "").strip() or None
        address = (row.get("地址") or row.get("address") or "").strip() or None
        tax_id = (row.get("統編") or row.get("tax_id") or "").strip() or None

        cols = ["id", "name", "contact_person", "phone", "email", "address", "tax_id", "status"]
        vals = [row_id, name, contact_person, phone, email, address, tax_id, "進行中"]
        try:
            _osc_exec(
                f"INSERT INTO clients ({','.join(cols)}) VALUES ({','.join(['%s']*len(cols))})",
                tuple(vals),
                fetch="none",
            )
            imported += 1
        except Exception as e:
            errors.append({"row": idx, "reason": str(e)})
            skipped += 1

    return jsonify({"ok": True, "imported": imported, "skipped": skipped, "errors": errors})


@osc_bp.route("/api/osc/clients/export-csv", methods=["GET"])
@login_required
def osc_clients_export_csv_api():
    """匯出全部當事人為 CSV（中文 header，utf-8-sig，與匯入相容）。"""
    rows, _ = _osc_exec(
        """
        SELECT name, contact_person, phone, email, address, tax_id
        FROM clients
        ORDER BY updated_date DESC, created_date DESC
        """,
        (),
        fetch="all",
    )

    buf = io.StringIO()
    # encode("utf-8-sig") 已加 BOM，不要重複
    writer = csv.writer(buf)
    writer.writerow(_CLIENTS_CSV_HEADERS_ZH)
    for r in rows:
        writer.writerow([
            r.get("name") or "",
            r.get("contact_person") or "",
            r.get("phone") or "",
            r.get("email") or "",
            r.get("address") or "",
            r.get("tax_id") or "",
        ])

    filename = f"當事人資料匯出_{time.strftime('%Y%m%d')}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


# ══════════════════════════════════════════════════════════════════════════════
# LAF wizard
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/laf-wizard/run", methods=["POST"])
@login_required
def osc_laf_wizard_run_api():
    payload = request.get_json() or {}
    mode = (payload.get("mode") or "preview").strip().lower()
    if mode not in {"preview", "draft", "submit"}:
        return jsonify({"ok": False, "error": "mode must be preview|draft|submit"}), 400
    action = _osc_map_laf_action(payload.get("action") or "")
    if action not in {"go_live", "inquiry", "fee", "condition", "withdrawal", "closing"}:
        return jsonify({"ok": False, "error": "unsupported action"}), 400
    if mode == "submit" and (not getattr(current_user, "is_admin", lambda: False)()):
        return jsonify({"ok": False, "error": "admin_required_for_submit"}), 403

    ident = _osc_prepare_laf_identity(payload)
    fields = payload.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    reason = str(payload.get("reason") or "").strip()
    try:
        LAFOrchestrator = _osc_import_laf_orchestrator()
        orchestrator_inst = LAFOrchestrator(dry_run=(mode == "preview"))
        if mode == "submit":
            result = orchestrator_inst.execute_portal_action_submit(
                action=action,
                laf_case_number=ident["laf_case_number"],
                case_number=ident["case_number"],
                client_name=ident["client_name"],
                reason=reason,
                fields=fields,
            )
        else:
            result = orchestrator_inst.execute_portal_action_draft(
                action=action,
                laf_case_number=ident["laf_case_number"],
                case_number=ident["case_number"],
                client_name=ident["client_name"],
                reason=reason,
                fields=fields,
            )
        artifact = _osc_enrich_portal_preview(orchestrator_inst._last_portal_artifact if hasattr(orchestrator_inst, "_last_portal_artifact") else {})
        return jsonify(
            {
                "ok": bool(isinstance(result, dict) and result.get("ok")),
                "mode": mode,
                "action": action,
                "identity": ident,
                "result": result if isinstance(result, dict) else {"ok": bool(result)},
                "artifact": artifact,
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "mode": mode, "action": action, "identity": ident}), 500


@osc_bp.route("/api/osc/laf-backfill", methods=["POST"])
@login_required
def osc_laf_backfill_api():
    """手動觸發法扶案號補填（資料夾 + 接案清冊）。"""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        from laf_nightly_audit import run_backfill_only
        result = run_backfill_only(notify=False)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# Archive wizard
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/archive-wizard/preview", methods=["GET"])
@login_required
def osc_archive_wizard_preview_api():
    limit = max(1, min(1000, int(request.args.get("limit") or "300")))
    try:
        out = _osc_build_archive_preview(limit=limit)
        return jsonify(out)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@osc_bp.route("/api/osc/archive-wizard/execute", methods=["POST"])
@login_required
def osc_archive_wizard_execute_api():
    payload = request.get_json() or {}
    if not bool(payload.get("confirm")):
        return jsonify({"ok": False, "error": "confirm_required"}), 400
    force = bool(payload.get("force"))
    case_ids = payload.get("case_ids") or []
    if isinstance(case_ids, str):
        case_ids = [x.strip() for x in case_ids.split(",") if x.strip()]
    case_ids = [str(x).strip() for x in case_ids if str(x).strip()]

    preview = _osc_build_archive_preview(limit=1000)
    items = preview.get("items") or []
    pick = [it for it in items if (not case_ids) or (str(it.get("id")) in set(case_ids))]
    moved = []
    skipped = []
    errors = []

    for it in pick:
        try:
            result = _osc_move_archive_item(it, force=force)
            if result.get("ok"):
                moved.append(result)
            else:
                skipped.append(result)
        except Exception as e:
            errors.append({"id": it.get("id"), "case_number": it.get("case_number"), "error": str(e)})

    return jsonify(
        {
            "ok": not errors,
            "summary": {"selected": len(pick), "moved": len(moved), "skipped": len(skipped), "errors": len(errors)},
            "moved": moved,
            "skipped": skipped,
            "errors": errors,
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# Labor law calculator
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/labor-law/calc", methods=["POST"])
@login_required
def osc_labor_law_calc():
    """
    勞動基準法計算器 API。
    """
    skill_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "skills", "labor-law-calculator", "action.py"
    )
    skill_dir = os.path.dirname(skill_path)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("labor_law_action", os.path.abspath(skill_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return jsonify({"ok": False, "error": f"無法載入 skill：{e}"}), 500

    from werkzeug.utils import secure_filename as _secure_filename

    uploaded_paths: list = []
    temp_dir = None
    if request.content_type and "multipart" in request.content_type:
        task = request.form.get("task", "")
        try:
            monthly_wage = float(request.form.get("monthly_wage") or 0) or None
        except Exception:
            monthly_wage = None
        wage_by_year_raw = request.form.get("monthly_wage_by_year")
        temp_dir = tempfile.mkdtemp(prefix="labor_law_")
        for f in request.files.getlist("files[]") + request.files.getlist("file"):
            dest = os.path.join(temp_dir, _secure_filename(f.filename))
            f.save(dest)
            uploaded_paths.append(dest)
    else:
        data = request.get_json() or {}
        task = data.get("task", "")
        try:
            monthly_wage = float(data.get("monthly_wage") or 0) or None
        except Exception:
            monthly_wage = None
        wage_by_year_raw = data.get("monthly_wage_by_year")
        uploaded_paths = [str(p) for p in (data.get("file_paths") or [])]

    wage_by_year = None
    if wage_by_year_raw:
        try:
            raw = wage_by_year_raw if isinstance(wage_by_year_raw, dict) else json.loads(wage_by_year_raw)
            wage_by_year = {int(k): float(v) for k, v in raw.items()}
        except Exception:
            _log.debug("silent-catch wage_by_year parse", exc_info=True)

    kwargs = {}
    if monthly_wage:
        kwargs["monthly_wage"] = monthly_wage
    if wage_by_year:
        kwargs["monthly_wage_by_year"] = wage_by_year
    if uploaded_paths:
        kwargs["file_paths"] = uploaded_paths
        kwargs.setdefault("mode", "calc_file")

    from concurrent.futures import TimeoutError as FuturesTimeoutError
    from api.thread_pools import io_pool
    try:
        _future = io_pool.submit(mod.run, task, **kwargs)
        result_text = _future.result(timeout=120)  # 120s hard cap
    except FuturesTimeoutError:
        return jsonify({"ok": False, "error": "skill execution timed out (120s)"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

    return jsonify({"ok": True, "result": result_text})


@osc_bp.route("/api/osc/labor-law/parse-files", methods=["POST"])
@login_required
def osc_labor_law_parse_files():
    """
    解析指定路徑的出勤 Excel/PDF，回傳每日加班明細（不計算金額）。
    """
    skill_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "skills", "labor-law-calculator", "action.py"
    )
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("labor_law_action", os.path.abspath(skill_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return jsonify({"ok": False, "error": f"無法載入 skill：{e}"}), 500

    data = request.get_json() or {}
    file_paths = [str(p) for p in (data.get("file_paths") or [])]
    monthly_wage = float(data.get("monthly_wage") or 0) or None

    if not file_paths:
        return jsonify({"ok": False, "error": "請提供 file_paths"}), 400

    all_records = []
    errors = []
    for fp in file_paths:
        ext = fp.lower().split(".")[-1]
        try:
            if ext in ("xlsx", "xls"):
                recs = mod._parse_attendance_excel(fp)
            elif ext == "pdf":
                recs = mod._parse_holiday_pdf(fp)
            else:
                errors.append(f"不支援：{fp}")
                continue
            all_records.extend([{
                "date": r.date_str,
                "weekday": r.weekday,
                "day_type": r.day_type,
                "pre_ot_min": r.pre_ot_min,
                "post_ot_min": r.post_ot_min,
                "total_ot_min": r.total_ot_min,
                "source": r.source,
                "note": r.note,
                "ot_pay": mod._calc_ot_pay_for_record(r, monthly_wage) if monthly_wage else None,
            } for r in recs])
        except Exception as e:
            errors.append(f"{fp}: {e}")

    return jsonify({
        "ok": True,
        "total_records": len(all_records),
        "total_ot_hours": round(sum(r["total_ot_min"] for r in all_records) / 60, 2),
        "total_ot_pay": round(sum(r["ot_pay"] for r in all_records if r["ot_pay"]), 2) if monthly_wage else None,
        "records": all_records,
        "errors": errors,
    })


# ── Checklist defaults ────────────────────────────────────────────────────────

def _laf_default_checklist_items():
    """生成法扶預設清單，所得清單年度依當下時間動態。"""
    from datetime import datetime
    now = datetime.now()
    roc = now.year - 1911
    if now.month >= 7:
        latest, previous = roc - 1, roc - 2
    else:
        latest, previous = roc - 2, roc - 3

    return [
        ("household_reg_self", "最近一個月之全戶戶籍謄本(記事勿省略)"),
        ("jcic_credit_report", "最近一個月金融聯合徵信中心「個人中文債權人清冊」及「信用報告」"),
        ("tax_list_self", f"最近二年度綜合所得稅各類所得資料清單({previous}、{latest}年度)"),
        ("property_list_self", "最近一個月財產資料歸屬清單"),
        ("labor_insurance_self", "最近一個月勞工保險被保險人投保資料表及其明細"),
        ("income_proof_self", "最近三個月薪資證明文件"),
        ("income_affidavit", "收入切結書 (因無法提供薪資單)"),
        ("bank_book_self", "近二年所有銀行存摺封面及內頁影本(補摺至最新)"),
        ("bank_assoc_inquiry", "銀行公會存款紀錄查詢申請書"),
        ("insurance_list_self", "壽險公會投保紀錄(含要保人及被保險人)"),
        ("insurance_policy_self", "所有壽險保單解約金數額證明"),
        ("stock_investment_self", "證券集保庫存及歷史交易紀錄"),
        ("business_tax_return", "前五年內營利事業申報及核定書表(401報表)"),
        ("household_reg_parents", "父母之全戶戶籍謄本(記事勿省略)"),
        ("tax_list_parents", f"父母最近二年度所得稅清單({previous}、{latest}年度)"),
        ("property_list_parents", "父母最近一個月財產清單"),
        ("household_reg_children", "扶養子女之全戶戶籍謄本(記事勿省略)"),
        ("tax_list_children", f"子女最近二年度所得稅清單({previous}、{latest}年度)"),
        ("property_list_children", "子女最近一個月財產清單"),
        ("student_cert_children", "扶養子女在學證明(滿20歲)"),
        ("rental_contract", "租約影本及近三個月租金收據"),
        ("relative_building_transcript", "親屬之建物謄本或稅籍證明"),
        ("residence_consent_form", "居住親屬房屋同意書"),
        ("relative_land_transcript", "親屬之土地謄本"),
        ("court_documents", "現有強制執行或訴訟案件之命令或裁判影本"),
        ("negotiation_docs", "過往銀行協商或調解協議書/筆錄"),
        ("expense_receipt", "裁判費新臺幣 1,000 元 (備齊後支付)"),
        ("income_expense_table", "以月為單位之一年收支表"),
    ]


def _laf_debt_required_spec():
    """OSC 原消債應備事項表規格，保留條件式展開邏輯。"""
    items = dict(_laf_default_checklist_items())
    links = {
        "household_reg_self": "戶籍謄本申請教學:\nhttps://reurl.cc/LnKNl3\nhttps://reurl.cc/WOKDNy",
        "jcic_credit_report": "聯徵信用報告與債權人清冊申請教學:\nhttps://reurl.cc/nYG7vd\nhttps://reurl.cc/axK1d3\nhttps://reurl.cc/yA9kv2",
        "tax_list_self": "所得清單與財產清冊申請教學:\nhttps://reurl.cc/0WME4x",
        "property_list_self": "所得清單與財產清冊申請教學:\nhttps://reurl.cc/0WME4x",
        "labor_insurance_self": "勞保清冊申請教學:\nhttps://reurl.cc/MzKR6L",
        "insurance_list_self": "壽險公會投保紀錄申請教學:\nhttps://reurl.cc/mYKlR1\nhttps://reurl.cc/7VzR45\nhttps://reurl.cc/Y3K8Yl",
        "stock_investment_self": "證券集保紀錄申請教學:\n網路申請：\nhttps://investor.tdcc.com.tw/QDSIO/",
        "income_expense_table": "收支明細表範本:\nhttps://reurl.cc/K91M9q",
        "business_tax_return": "營利事業申報書(401報表)申請教學:\nhttps://reurl.cc/koKlpn",
        "bank_assoc_inquiry": "銀行公會存款查詢申請教學:\nhttps://www.ba.org.tw/PublicInformation/BusinessDetail/31",
        "income_affidavit": "收入切結書範本:\nhttps://reurl.cc/6qnNqb",
        "residence_consent_form": "居住親屬房屋同意書範本:\nhttps://reurl.cc/Y3K830",
        "rental_contract": "房租收據範本:\nhttps://reurl.cc/rYOLYr",
    }

    def it(key):
        return {"item_key": key, "item_label": items[key], "link": links.get(key, "")}

    return {
        "status_options": ["待補", "已繳", "免附"],
        "toggles": [
            {"key": "dependents_parents", "label": "有扶養父母"},
            {"key": "dependents_children", "label": "有扶養子女"},
            {"key": "rental", "label": "有租屋居住"},
            {"key": "resides_relative_property", "label": "居住親屬房產"},
            {"key": "litigation", "label": "有其他訴訟/強執"},
            {"key": "negotiation", "label": "曾與銀行協商/調解"},
            {"key": "has_business", "label": "五年內有營業"},
            {"key": "passbook_issue", "label": "存摺無法補登"},
            {"key": "no_payslip", "label": "無法提供薪資單"},
            {"key": "other_items", "label": "其他自訂項目"},
        ],
        "sections": [
            {"key": "basic", "title": "基本資料", "items": [it("household_reg_self"), it("jcic_credit_report")]},
            {
                "key": "self_assets",
                "title": "本人財產證明",
                "items": [
                    it("tax_list_self"), it("property_list_self"), it("labor_insurance_self"),
                    {**it("income_proof_self"), "hide_when": "no_payslip"},
                    {**it("income_affidavit"), "show_when": "no_payslip"},
                    {**it("bank_book_self"), "hide_when": "passbook_issue"},
                    {**it("bank_assoc_inquiry"), "show_when": "passbook_issue"},
                    it("insurance_list_self"), it("insurance_policy_self"),
                    it("stock_investment_self"), {**it("business_tax_return"), "show_when": "has_business"},
                ],
            },
            {
                "key": "parents",
                "title": "扶養父母資料",
                "show_when": "dependents_parents",
                "items": [it("household_reg_parents"), it("tax_list_parents"), it("property_list_parents")],
            },
            {
                "key": "children",
                "title": "扶養子女資料",
                "show_when": "dependents_children",
                "items": [
                    it("household_reg_children"), it("tax_list_children"),
                    it("property_list_children"), it("student_cert_children"),
                ],
            },
            {
                "key": "special",
                "title": "其他特殊狀況文件",
                "items": [
                    {**it("rental_contract"), "show_when": "rental"},
                    {**it("relative_building_transcript"), "show_when": "resides_relative_property"},
                    {**it("residence_consent_form"), "show_when": "resides_relative_property"},
                    {**it("relative_land_transcript"), "show_when": "resides_relative_property"},
                    {**it("court_documents"), "show_when": "litigation"},
                    {**it("negotiation_docs"), "show_when": "negotiation"},
                ],
            },
            {
                "key": "fees",
                "title": "費用與其他",
                "items": [it("expense_receipt"), it("income_expense_table")],
            },
            {"key": "custom", "title": "其他自訂項目", "show_when": "other_items", "items": []},
        ],
    }


def _laf_debt_known_item_keys() -> set[str]:
    keys = set()
    for section in _laf_debt_required_spec()["sections"]:
        keys.update(str(item.get("item_key") or "") for item in section.get("items") or [])
    return {k for k in keys if k}


def _laf_number_candidates_for_case(case: dict) -> dict:
    laf_no_re = re.compile(r"\d{6,8}-[A-Za-z]-\d{3}")
    priority_keywords = ("開辦通知書", "接案通知書", "准予扶助證明書", "委任狀")
    folder = str(case.get("folder_path") or "").strip()
    roots = [p for p in _osc_local_path_candidates(folder) if p and os.path.isdir(p)]
    out = {"candidates": [], "source": "", "scanned_roots": roots[:3]}
    priority = set()
    fallback = set()
    for root in roots[:3]:
        scan_dirs = [os.path.join(root, "01_法扶資料"), os.path.join(root, "02_開辦資料"), root]
        for scan_dir in [p for p in scan_dirs if os.path.isdir(p)]:
            try:
                for dirpath, _dirnames, filenames in os.walk(scan_dir):
                    for filename in filenames:
                        matches = laf_no_re.findall(filename)
                        if not matches:
                            continue
                        if any(keyword in filename for keyword in priority_keywords):
                            priority.update(matches)
                        else:
                            fallback.update(matches)
            except Exception:
                continue
            if priority:
                break
        if priority:
            break
    chosen = sorted(priority or fallback)
    out["candidates"] = chosen
    out["source"] = "開辦通知書/接案通知書" if priority else ("案件資料夾" if fallback else "")
    return out


@osc_bp.route("/api/osc/checklists/debt-required", methods=["GET"])
@login_required
def osc_laf_debt_required_get():
    case_number = request.args.get("case_number", "").strip()
    if not case_number:
        return jsonify({"ok": False, "error": "case_number 必填"}), 400
    case, _ = _osc_exec("SELECT * FROM cases WHERE case_number=%s LIMIT 1", (case_number,), fetch="one")
    rows, _ = _osc_exec(
        "SELECT id, case_number, item_key, item_label, status, notes, last_updated "
        "FROM legal_aid_checklists WHERE case_number=%s ORDER BY last_updated DESC, id DESC",
        (case_number,), fetch="all",
    )
    candidates = _laf_number_candidates_for_case(case or {}) if case else {"candidates": [], "source": "", "scanned_roots": []}
    return jsonify({
        "ok": True,
        "case": case or {"case_number": case_number},
        "spec": _laf_debt_required_spec(),
        "items": rows or [],
        "laf_number_candidates": candidates,
    })


@osc_bp.route("/api/osc/checklists/debt-required/save", methods=["POST"])
@login_required
def osc_laf_debt_required_save():
    data = request.get_json(silent=True) or {}
    case_number = (data.get("case_number") or "").strip()
    if not case_number:
        return jsonify({"ok": False, "error": "case_number 必填"}), 400
    raw_items = data.get("items") or []
    if not isinstance(raw_items, list):
        return jsonify({"ok": False, "error": "items must be list"}), 400
    active_keys: set[str] = set()
    saved = 0
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item_key = str(item.get("item_key") or "").strip()
        item_label = str(item.get("item_label") or "").strip()
        if not item_key or not item_label:
            continue
        status = str(item.get("status") or "待補").strip() or "待補"
        notes = str(item.get("notes") or "").strip()
        active_keys.add(item_key)
        _osc_exec(
            "INSERT INTO legal_aid_checklists (case_number, item_key, item_label, status, notes, last_updated) "
            "VALUES (%s, %s, %s, %s, %s, NOW()) "
            "ON DUPLICATE KEY UPDATE item_label=VALUES(item_label), status=VALUES(status), notes=VALUES(notes), last_updated=NOW()",
            (case_number, item_key, item_label, status, notes),
            fetch="none",
        )
        saved += 1
    known_keys = _laf_debt_known_item_keys()
    db_rows, _ = _osc_exec(
        "SELECT item_key FROM legal_aid_checklists WHERE case_number=%s",
        (case_number,),
        fetch="all",
    )
    deleted = 0
    for row in db_rows or []:
        key = str(row.get("item_key") if isinstance(row, dict) else row[0]).strip()
        if (key in known_keys or key.startswith("custom_item_")) and key not in active_keys:
            _osc_exec(
                "DELETE FROM legal_aid_checklists WHERE case_number=%s AND item_key=%s",
                (case_number, key),
                fetch="none",
            )
            deleted += 1
    return jsonify({"ok": True, "saved_count": saved, "deleted_count": deleted})


@osc_bp.route("/api/osc/cases/<row_id>/laf-number/sync", methods=["POST"])
@login_required
def osc_case_laf_number_sync(row_id):
    case, _ = _osc_exec("SELECT * FROM cases WHERE id=%s", ((row_id or "").strip(),), fetch="one")
    if not case:
        return jsonify({"ok": False, "error": "case_not_found"}), 404
    payload = request.get_json(silent=True) or {}
    manual = str(payload.get("laf_case_no") or "").strip()
    candidates = _laf_number_candidates_for_case(case)
    chosen = manual
    if not chosen:
        if len(candidates["candidates"]) == 1:
            chosen = candidates["candidates"][0]
        elif len(candidates["candidates"]) > 1:
            return jsonify({"ok": False, "error": "multiple_candidates", **candidates}), 409
        else:
            return jsonify({"ok": False, "error": "laf_number_not_found", **candidates}), 404
    _osc_exec(
        "UPDATE cases SET laf_case_no=%s, application_no=CASE WHEN application_no IS NULL OR application_no='' THEN %s ELSE application_no END, updated_at=NOW() WHERE id=%s",
        (chosen, chosen, case.get("id")),
        fetch="none",
    )
    return jsonify({"ok": True, "laf_case_no": chosen, "source": candidates.get("source") or ("手動輸入" if manual else "")})


# ── 1A. legal_aid_checklists endpoints (5) ───────────────────────────────────

@osc_bp.route("/api/osc/checklists/legal-aid", methods=["GET"])
@login_required
def osc_laf_checklist_get():
    case_number = request.args.get("case_number", "").strip()
    if not case_number:
        return jsonify({"ok": False, "error": "case_number 必填"}), 400
    rows, _ = _osc_exec(
        "SELECT id, case_number, item_key, item_label, status, notes, last_updated "
        "FROM legal_aid_checklists WHERE case_number=%s ORDER BY last_updated DESC, id DESC",
        (case_number,), fetch="all"
    )
    items = []
    for r in (rows or []):
        # _osc_exec 回 dict rows
        lu = r.get("last_updated") if isinstance(r, dict) else r[6]
        items.append({
            "id": r.get("id") if isinstance(r, dict) else r[0],
            "case_number": r.get("case_number") if isinstance(r, dict) else r[1],
            "item_key": r.get("item_key") if isinstance(r, dict) else r[2],
            "item_label": r.get("item_label") if isinstance(r, dict) else r[3],
            "status": r.get("status") if isinstance(r, dict) else r[4],
            "notes": r.get("notes") if isinstance(r, dict) else r[5],
            "last_updated": lu.isoformat() if hasattr(lu, "isoformat") else (lu if lu else None),
        })
    return jsonify({"ok": True, "items": items})


@osc_bp.route("/api/osc/checklists/legal-aid", methods=["POST"])
@login_required
def osc_laf_checklist_post():
    data = request.get_json(silent=True) or {}
    case_number = (data.get("case_number") or "").strip()
    if not case_number:
        return jsonify({"ok": False, "error": "case_number 必填"}), 400
    item_key = (data.get("item_key") or "").strip()
    if not item_key:
        item_key = f"custom_{uuid.uuid4().hex[:8]}"
    item_label = (data.get("item_label") or "").strip()
    status = (data.get("status") or "待補").strip()
    notes = (data.get("notes") or "").strip()
    result, _ = _osc_exec(
        "INSERT INTO legal_aid_checklists (case_number, item_key, item_label, status, notes, last_updated) "
        "VALUES (%s, %s, %s, %s, %s, NOW()) "
        "ON DUPLICATE KEY UPDATE item_label=VALUES(item_label), status=VALUES(status), notes=VALUES(notes), last_updated=NOW()",
        (case_number, item_key, item_label, status, notes), fetch="none"
    )
    row, _ = _osc_exec(
        "SELECT id FROM legal_aid_checklists WHERE case_number=%s AND item_key=%s",
        (case_number, item_key), fetch="one"
    )
    row_id = row[0] if row else None
    return jsonify({"ok": True, "id": row_id, "case_number": case_number, "item_key": item_key})


@osc_bp.route("/api/osc/checklists/legal-aid/<int:row_id>", methods=["PUT"])
@login_required
def osc_laf_checklist_put(row_id):
    data = request.get_json(silent=True) or {}
    sets = []
    vals = []
    if "status" in data:
        sets.append("status=%s"); vals.append(data["status"])
    if "notes" in data:
        sets.append("notes=%s"); vals.append(data["notes"])
    if "item_label" in data:
        sets.append("item_label=%s"); vals.append(data["item_label"])
    if not sets:
        return jsonify({"ok": False, "error": "無可更新欄位"}), 400
    sets.append("last_updated=NOW()")
    vals.append(row_id)
    _osc_exec(f"UPDATE legal_aid_checklists SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True})


@osc_bp.route("/api/osc/checklists/legal-aid/<int:row_id>", methods=["DELETE"])
@login_required
def osc_laf_checklist_delete(row_id):
    _osc_exec("DELETE FROM legal_aid_checklists WHERE id=%s", (row_id,), fetch="none")
    return jsonify({"ok": True})


@osc_bp.route("/api/osc/checklists/legal-aid/seed", methods=["POST"])
@login_required
def osc_laf_checklist_seed():
    data = request.get_json(silent=True) or {}
    case_number = (data.get("case_number") or "").strip()
    if not case_number:
        return jsonify({"ok": False, "error": "case_number 必填"}), 400
    items = _laf_default_checklist_items()
    inserted = 0
    skipped = 0
    for item_key, item_label in items:
        existing, _ = _osc_exec(
            "SELECT id FROM legal_aid_checklists WHERE case_number=%s AND item_key=%s",
            (case_number, item_key), fetch="one"
        )
        if existing:
            skipped += 1
        else:
            _osc_exec(
                "INSERT INTO legal_aid_checklists (case_number, item_key, item_label, status, last_updated) "
                "VALUES (%s, %s, %s, '待補', NOW())",
                (case_number, item_key, item_label), fetch="none"
            )
            inserted += 1
    return jsonify({"ok": True, "inserted_count": inserted, "skipped_count": skipped})


# ── 1B. case_checklists endpoints (4) ────────────────────────────────────────

@osc_bp.route("/api/osc/checklists/case", methods=["GET"])
@login_required
def osc_case_checklist_get():
    case_number = request.args.get("case_number", "").strip()
    if not case_number:
        return jsonify({"ok": False, "error": "case_number 必填"}), 400
    rows, _ = _osc_exec(
        "SELECT id, case_number, item_label, status, notes, is_active "
        "FROM case_checklists WHERE case_number=%s AND is_active=1 ORDER BY id DESC",
        (case_number,), fetch="all"
    )
    items = []
    for r in (rows or []):
        items.append({
            "id": r.get("id") if isinstance(r, dict) else r[0],
            "case_number": r.get("case_number") if isinstance(r, dict) else r[1],
            "item_label": r.get("item_label") if isinstance(r, dict) else r[2],
            "status": r.get("status") if isinstance(r, dict) else r[3],
            "notes": r.get("notes") if isinstance(r, dict) else r[4],
            "is_active": r.get("is_active") if isinstance(r, dict) else r[5],
        })
    return jsonify({"ok": True, "items": items})


@osc_bp.route("/api/osc/checklists/case", methods=["POST"])
@login_required
def osc_case_checklist_post():
    data = request.get_json(silent=True) or {}
    case_number = (data.get("case_number") or "").strip()
    item_label = (data.get("item_label") or "").strip()
    if not case_number or not item_label:
        return jsonify({"ok": False, "error": "case_number 與 item_label 必填"}), 400
    status = (data.get("status") or "待補").strip()
    notes = (data.get("notes") or "").strip()
    _osc_exec(
        "INSERT INTO case_checklists (case_number, item_label, status, notes, is_active) "
        "VALUES (%s, %s, %s, %s, 1) "
        "ON DUPLICATE KEY UPDATE status=VALUES(status), notes=VALUES(notes), is_active=1",
        (case_number, item_label, status, notes), fetch="none"
    )
    row, _ = _osc_exec(
        "SELECT id FROM case_checklists WHERE case_number=%s AND item_label=%s",
        (case_number, item_label), fetch="one"
    )
    row_id = row[0] if row else None
    return jsonify({"ok": True, "id": row_id, "case_number": case_number, "item_label": item_label})


@osc_bp.route("/api/osc/checklists/case/<int:row_id>", methods=["PUT"])
@login_required
def osc_case_checklist_put(row_id):
    data = request.get_json(silent=True) or {}
    sets = []
    vals = []
    if "status" in data:
        sets.append("status=%s"); vals.append(data["status"])
    if "notes" in data:
        sets.append("notes=%s"); vals.append(data["notes"])
    if "is_active" in data:
        sets.append("is_active=%s"); vals.append(int(data["is_active"]))
    if not sets:
        return jsonify({"ok": False, "error": "無可更新欄位"}), 400
    vals.append(row_id)
    _osc_exec(f"UPDATE case_checklists SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True})


@osc_bp.route("/api/osc/checklists/case/<int:row_id>", methods=["DELETE"])
@login_required
def osc_case_checklist_delete(row_id):
    _osc_exec("UPDATE case_checklists SET is_active=0 WHERE id=%s", (row_id,), fetch="none")
    return jsonify({"ok": True})
# ── P3: Auto Backup / Restore ──────────────────────────────────────────────────

_BACKUP_DIR = Path(os.path.expanduser("~/.magi/backups/osc"))

_BACKUP_TABLES = [
    "cases",
    "clients",
    "todos",
    "meetings",
    "calendar_events",
    "legal_aid_checklists",
    "case_checklists",
    "quotations",
    "transactions",
    "legal_aid_branches",
    "settings",
]


def _osc_backup_dir() -> Path:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return _BACKUP_DIR


def _osc_backup_table(table: str) -> list:
    """Return all rows for a table; returns [] if table doesn't exist."""
    try:
        rows, _ = _osc_exec(f"SELECT * FROM `{table}`", (), fetch="all")
        return rows if rows else []
    except Exception:
        return None  # None = table doesn't exist / error


def _osc_create_backup(label: str = "manual") -> dict:
    """Write backup JSON to disk; prune to 7 files; return metadata."""
    import zoneinfo

    tz = zoneinfo.ZoneInfo("Asia/Taipei")
    now = datetime.now(tz)
    ts = now.strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^\w\-]", "", label)[:20] or "manual"
    filename = f"backup_{ts}_{safe_label}.json"

    tables_data = {}
    table_counts = {}
    for table in _BACKUP_TABLES:
        rows = _osc_backup_table(table)
        if rows is not None:
            tables_data[table] = rows
            table_counts[table] = len(rows)

    payload = {
        "version": 1,
        "created_at": now.isoformat(),
        "label": safe_label,
        "tables": tables_data,
        "table_counts": table_counts,
    }

    backup_path = _osc_backup_dir() / filename
    backup_path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")

    # Prune: keep at most 7 files
    files = sorted(_osc_backup_dir().glob("backup_*.json"), key=lambda p: p.stat().st_mtime)
    while len(files) > 7:
        files[0].unlink(missing_ok=True)
        files.pop(0)

    return {
        "filename": filename,
        "size_bytes": backup_path.stat().st_size,
        "table_counts": table_counts,
    }


def _osc_parse_backup_meta(p: Path) -> dict:
    """Read minimal metadata from a backup file."""
    try:
        stat = p.stat()
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {
            "filename": p.name,
            "size_bytes": stat.st_size,
            "created_at": raw.get("created_at", ""),
            "table_counts": raw.get("table_counts", {}),
        }
    except Exception as e:
        return {
            "filename": p.name,
            "size_bytes": 0,
            "created_at": "",
            "table_counts": {},
            "error": str(e),
        }


@osc_bp.route("/api/osc/backups", methods=["GET"])
@login_required
def osc_backup_list():
    """List backup files, sorted by mtime DESC, max 50."""
    bd = _osc_backup_dir()
    files = sorted(bd.glob("backup_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]
    items = [_osc_parse_backup_meta(p) for p in files]
    return jsonify({"ok": True, "items": items})


@osc_bp.route("/api/osc/backups", methods=["POST"])
@login_required
def osc_backup_create():
    """Create a new backup snapshot."""
    data = request.get_json() or {}
    label = str(data.get("label") or "manual").strip() or "manual"
    try:
        meta = _osc_create_backup(label)
        return jsonify({"ok": True, **meta})
    except Exception as e:
        logger.error("[osc_backup_create] %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@osc_bp.route("/api/osc/backups/<filename>/restore", methods=["POST"])
@login_required
def osc_backup_restore(filename):
    """Restore from a backup file. dry_run=true for preview."""
    # Sanitize filename
    if not re.fullmatch(r"backup_[\w\-\.]+\.json", filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400

    backup_path = _osc_backup_dir() / filename
    if not backup_path.exists():
        return jsonify({"ok": False, "error": "Backup file not found"}), 404

    data = request.get_json() or {}
    dry_run = bool(data.get("dry_run"))
    confirm = bool(data.get("confirm"))

    if not dry_run and not confirm:
        return jsonify({"ok": False, "error": "Need dry_run=true or confirm=true"}), 400

    try:
        payload = json.loads(backup_path.read_text(encoding="utf-8"))
    except Exception as e:
        return jsonify({"ok": False, "error": f"Cannot parse backup: {e}"}), 500

    tables = payload.get("tables") or {}
    inserted_count = 0
    skipped_count = 0
    errors = []

    for table, rows in tables.items():
        if not rows:
            continue
        if not isinstance(rows, list):
            continue

        for row in rows:
            if not isinstance(row, dict):
                continue
            cols = list(row.keys())
            if not cols:
                continue

            if dry_run:
                # Count rows in DB that are already present by primary key 'id'
                pk_val = row.get("id")
                if pk_val is not None:
                    try:
                        existing, _ = _osc_exec(
                            f"SELECT COUNT(*) AS cnt FROM `{table}` WHERE id=%s",
                            (pk_val,),
                            fetch="one",
                        )
                        if existing and int(existing.get("cnt", 0)) > 0:
                            skipped_count += 1
                        else:
                            inserted_count += 1
                    except Exception as e:
                        errors.append(f"{table}[{pk_val}]: {e}")
                else:
                    inserted_count += 1
            else:
                # Real INSERT IGNORE
                try:
                    placeholders = ", ".join(["%s"] * len(cols))
                    col_names = ", ".join(f"`{c}`" for c in cols)
                    vals = tuple(row[c] for c in cols)
                    sql = f"INSERT IGNORE INTO `{table}` ({col_names}) VALUES ({placeholders})"
                    result, _ = _osc_exec(sql, vals, fetch="none")
                    # rowcount == 0 means duplicate was skipped
                    if result is not None and hasattr(result, "rowcount") and result.rowcount == 0:
                        skipped_count += 1
                    else:
                        inserted_count += 1
                except Exception as e:
                    errors.append(f"{table}: {e}")

    return jsonify({
        "ok": True,
        "mode": "dry_run" if dry_run else "restore",
        "inserted_count": inserted_count,
        "skipped_count": skipped_count,
        "errors": errors[:50],
    })


@osc_bp.route("/api/osc/backups/<filename>", methods=["DELETE"])
@login_required
def osc_backup_delete(filename):
    """Delete a backup file."""
    if not re.fullmatch(r"backup_[\w\-\.]+\.json", filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400

    backup_path = _osc_backup_dir() / filename
    if not backup_path.exists():
        return jsonify({"ok": False, "error": "File not found"}), 404

    try:
        backup_path.unlink()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# P2: 報價單 PDF 匯出
# ─────────────────────────────────────────────────────────────────────────────

def _osc_find_font() -> str:
    """Return a path to a CJK TrueType/TrueType Collection font available on macOS."""
    candidates = [
        os.path.join(_MAGI_ROOT, "font", "NotoSansTC-VariableFont_wght.ttf"),
        os.path.join(_MAGI_ROOT, "font", "static", "NotoSansTC-Regular.ttf"),
        "/Applications/Paperclip.app/Contents/Resources/font/NotoSansTC-VariableFont_wght.ttf",
        "/Applications/Paperclip.app/Contents/Resources/font/static/NotoSansTC-Regular.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode MS.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return ""


def _osc_build_quotation_pdf(row: dict) -> bytes:
    """Generate a PDF using the same layout as the standalone OSC quotation exporter."""
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    )
    from reportlab.platypus import Image as ReportlabImage
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_path = _osc_find_font()
    font_name = "NotoSansTC"
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont(font_name, font_path))
        except Exception:
            font_name = "Helvetica"
    else:
        font_name = "Helvetica"

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )

    styles = getSampleStyleSheet()
    company_style = ParagraphStyle("Company", parent=styles["Heading1"], fontName=font_name, fontSize=20, alignment=TA_LEFT, leading=24)
    title_style = ParagraphStyle("CustomTitle", parent=styles["Heading1"], fontName=font_name, fontSize=18, alignment=TA_CENTER, spaceAfter=15, leading=24)
    subtitle_style = ParagraphStyle("Subtitle", parent=styles["Normal"], fontName="Helvetica", fontSize=10, alignment=TA_LEFT, leading=12)
    normal_style = ParagraphStyle("CustomNormal", parent=styles["Normal"], fontName=font_name, fontSize=10, leading=14)
    table_text_style = ParagraphStyle("QuotationTableText", parent=styles["Normal"], fontName=font_name, fontSize=9, leading=12)
    total_style = ParagraphStyle("QuotationTotal", parent=normal_style, alignment=TA_RIGHT, fontSize=12, leading=16)

    story = []

    company_name = (
        _osc_get_setting_value("company_name", "")
        or _osc_get_setting_value("firm_name", "")
        or "偵理法律事務所"
    )
    company_name_en = _osc_get_setting_value("company_name_en", "") or "ZHENLI LAW FIRM"
    logo_path = _osc_existing_resource_path(
        "logo_path",
        "logo.png",
        "/Applications/Paperclip.app/Contents/Resources/photo/logo.png",
    )
    card_path = _osc_existing_resource_path(
        "business_card_path",
        "namecard.png",
        "/Applications/Paperclip.app/Contents/Resources/photo/namecard.png",
    )

    try:
        if logo_path and os.path.exists(logo_path):
            logo_img = ReportlabImage(str(logo_path), width=2 * cm, height=2 * cm)
            title_data = [[logo_img, [Paragraph(company_name, company_style), Paragraph(company_name_en, subtitle_style)]]]
        else:
            logo_text = Paragraph("⚖", ParagraphStyle("LogoText", parent=normal_style, fontSize=30, alignment=TA_CENTER))
            title_data = [[logo_text, [Paragraph(company_name, company_style), Paragraph(company_name_en, subtitle_style)]]]
        title_table = Table(title_data, colWidths=[2.5 * cm, 15.5 * cm])
        title_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(title_table)
    except Exception:
        story.append(Paragraph(company_name, company_style))

    story.append(HRFlowable(width="100%", thickness=1, color=colors.grey))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("法律服務報價單", title_style))

    extended_raw = row.get("extended_data") or {}
    if isinstance(extended_raw, str):
        try:
            extended = json.loads(extended_raw)
        except Exception:
            extended = {}
    elif isinstance(extended_raw, dict):
        extended = extended_raw
    else:
        extended = {}

    client_name = str(row.get("client_name") or "")
    project_name = str(row.get("project_name") or "")
    contact = str(row.get("contact") or extended.get("contact") or "")
    phone = str(row.get("phone") or "")
    email = str(row.get("email") or "")
    address = str(row.get("address") or "")
    lawyer = str(extended.get("lawyer") or _osc_get_setting_value("default_lawyer", "") or "喬政翔律師")
    specialist = str(extended.get("specialist") or _osc_get_setting_value("default_specialist", "") or "林稚芳法務專員")
    specialist_phone = str(extended.get("specialist_phone") or _osc_get_setting_value("specialist_phone", "") or "03-8357-186；0937-753-800")

    def _fmt_date(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = text.split(" ")[0].replace("-", "/")
        parts = text.split("/")
        if len(parts) == 3 and all(parts):
            return f"{parts[0]} 年 {parts[1]} 月 {parts[2]} 日"
        return str(value or "")

    raw_date = str(row.get("date") or "")
    raw_expiry = str(row.get("expiry") or "")
    date_for_calc = raw_date.split(" ")[0].replace("-", "/")
    expiry_for_calc = raw_expiry.split(" ")[0].replace("-", "/")
    formatted_date = _fmt_date(raw_date)

    info_data = [
        [Paragraph(f"當事人姓名：{client_name}", normal_style), Paragraph(f"當事人聯絡人：{contact}", normal_style)],
        [Paragraph(f"當事人電話：{phone}", normal_style), Paragraph(f"事務所聯絡人：{specialist}", normal_style)],
    ]
    if address:
        info_data.append([Paragraph(f"當事人地址：{address}", normal_style), ""])
    info_data.append([Paragraph(f"Email：{email}", normal_style), Paragraph(f"報價日期：{formatted_date}", normal_style)])
    info_table = Table(info_data, colWidths=[9 * cm, 9 * cm])
    info_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.3 * cm))

    try:
        days = (datetime.strptime(expiry_for_calc, "%Y/%m/%d") - datetime.strptime(date_for_calc, "%Y/%m/%d")).days
    except Exception:
        days = 30
    story.append(Paragraph(f"* 本報價單有效期限為 {days} 天", normal_style))
    story.append(Paragraph(f"● 本案承辦律師：{lawyer}", normal_style))
    story.append(Paragraph(f"● 本案承辦專員：{specialist}（聯絡：{specialist_phone}）", normal_style))
    story.append(Paragraph("感謝您的信任，希望有機會能提供您專業的法律服務", normal_style))
    story.append(Spacer(1, 0.5 * cm))

    items_raw = row.get("items") or "[]"
    if isinstance(items_raw, str):
        try:
            items = json.loads(items_raw)
        except Exception:
            items = []
    elif isinstance(items_raw, list):
        items = items_raw
    else:
        items = []

    table_data = [
        [
            Paragraph("<b>項目</b>", table_text_style),
            Paragraph("<b>服務內容</b>", table_text_style),
            Paragraph("<b>單位</b>", table_text_style),
            Paragraph("<b>單價</b>", table_text_style),
            Paragraph("<b>金額</b>", table_text_style),
        ]
    ]
    for idx, it in enumerate(items, 1):
        if not isinstance(it, dict):
            continue
        name = str(it.get("item") or it.get("name") or it.get("service") or "")
        desc = str(it.get("description") or it.get("desc") or "")
        service_content = name + (f" （{desc}）" if desc else "")
        unit = str(it.get("unit") or "式")
        qty = it.get("qty") or it.get("quantity") or 1
        unit_price = it.get("cost") if it.get("cost") is not None else (it.get("unit_price") or it.get("price") or 0)
        subtotal = it.get("subtotal") or it.get("amount") or ""
        try:
            if not subtotal:
                subtotal = float(qty or 0) * float(unit_price or 0)
        except Exception:
            pass
        try:
            qty_text = f"{int(float(qty))} {unit}"
        except Exception:
            qty_text = f"{qty} {unit}".strip()
        try:
            unit_price_text = f"{float(unit_price or 0):,.0f} 元"
        except Exception:
            unit_price_text = str(unit_price or "")
        try:
            subtotal_text = f"{float(subtotal or 0):,.0f} 元"
        except Exception:
            subtotal_text = str(subtotal or "")
        table_data.append([
            Paragraph(str(idx), table_text_style),
            Paragraph(service_content, table_text_style),
            Paragraph(qty_text, table_text_style),
            Paragraph(unit_price_text, table_text_style),
            Paragraph(subtotal_text, table_text_style),
        ])

    if len(table_data) > 1:
        tbl = Table(table_data, colWidths=[1.5 * cm, 9 * cm, 2 * cm, 3 * cm, 3 * cm])
        tbl.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#34495e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 0.5 * cm))

    try:
        total_text = f"NT$ {float(row.get('total') or 0):,.0f}"
    except Exception:
        total_text = str(row.get("total") or "")
    story.append(Paragraph(f"<b>總價：{total_text}</b>", total_style))
    story.append(Spacer(1, 0.5 * cm))

    bank_name = _osc_get_setting_value("bank_name", "") or "您的銀行"
    bank_account_name = _osc_get_setting_value("bank_account_name", "") or "您的戶名"
    bank_account_number = _osc_get_setting_value("bank_account_number", "") or "您的帳號"
    remittance_account_info = (
        f"• 銀行名稱：{bank_name}\n"
        f"• 戶名：{bank_account_name}\n"
        f"• 帳號：{bank_account_number}"
    )
    account_paragraph = Paragraph(
        "<b>付款方式與帳戶資訊</b><br/><br/>"
        "本報價於當事人確認及付款後生效<br/><br/>"
        + remittance_account_info.replace("\n", "<br/>"),
        normal_style,
    )
    if card_path and os.path.exists(card_path):
        card_image = ReportlabImage(str(card_path), width=6 * cm, height=9.6 * cm)
    else:
        card_image = Paragraph("找不到名片圖", normal_style)
    bottom_table = Table([[card_image, account_paragraph]], colWidths=[7 * cm, 11 * cm])
    bottom_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (1, 0), (1, 0), 10),
    ]))
    story.append(bottom_table)

    notes = str(row.get("notes") or "")
    if notes:
        story.append(Spacer(1, 0.5 * cm))
        story.append(Paragraph("<b>補充說明</b>", normal_style))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
        story.append(Paragraph(notes.replace("\n", "<br/>"), normal_style))

    doc.build(story)
    return buf.getvalue()


@osc_bp.route("/api/osc/quotations/<row_id>/export-pdf", methods=["GET"])
@login_required
def osc_quotation_export_pdf(row_id):
    """Export a quotation as PDF and return as attachment."""
    row, _ = _osc_exec("SELECT * FROM quotations WHERE id=%s", (row_id,), fetch="one")
    if not row:
        return _osc_download_error_response("找不到這份報價單。", 404)

    try:
        pdf_bytes = _osc_build_quotation_pdf(row)
    except Exception as e:
        logging.exception("PDF generation error for quotation %s", row_id)
        return _osc_download_error_response(f"報價單 PDF 產生失敗：{e}", 500)

    today = datetime.now().strftime("%Y%m%d")
    safe_name = (row.get("client_name") or row_id or "quotation").replace("/", "_")
    filename = f"報價單_{safe_name}_{today}.pdf"

    buf = io.BytesIO(pdf_bytes)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


# ─────────────────────────────────────────────────────────────────────────────
# P2: 地址標籤 PNG 預覽 + 下載
# ─────────────────────────────────────────────────────────────────────────────

def _osc_build_address_label(
    sender_name: str,
    sender_address: str,
    receiver_name: str,
    receiver_address: str,
) -> bytes:
    """Render address label PNG (8cm×4cm @300 DPI) and return raw PNG bytes."""
    import textwrap
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    W, H = 945, 472  # 8cm × 4cm @ 300 DPI

    font_path = _osc_find_font()

    def _load_font(size: int):
        if font_path:
            try:
                return ImageFont.truetype(font_path, size, index=0)
            except Exception:
                pass
        return ImageFont.load_default()

    font_large = _load_font(28)
    font_normal = _load_font(22)
    font_small = _load_font(18)

    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    # Sender block (top-left, small)
    draw.text((20, 18), sender_name, fill="black", font=font_small)
    if sender_address:
        addr_lines = textwrap.wrap(sender_address, width=28)
        for i, line in enumerate(addr_lines[:2]):
            draw.text((20, 42 + i * 22), line, fill="black", font=font_small)

    # Separator line
    draw.line([(20, 110), (W - 20, 110)], fill="#cccccc", width=1)

    # Receiver name (large, center-ish)
    draw.text((50, 130), receiver_name, fill="black", font=font_large)

    # Receiver address (wrapped)
    addr_lines = textwrap.wrap(receiver_address, width=18)
    y = 178
    for line in addr_lines[:4]:
        draw.text((50, y), line, fill="black", font=font_normal)
        y += 34

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@osc_bp.route("/api/osc/cases/<row_id>/address-label", methods=["GET"])
@login_required
def osc_case_address_label(row_id):
    """Generate address label PNG for a case.

    Query params:
      mode      preview|download  (default: preview)
      recipient court|defendant|laf
    """
    mode = (request.args.get("mode") or "preview").strip().lower()
    recipient = (request.args.get("recipient") or "").strip().lower()

    if recipient not in ("court", "defendant", "laf"):
        return _osc_download_error_response("請選擇法院、對造或法扶分會。", 400)

    # Load case
    case, _ = _osc_exec("SELECT * FROM cases WHERE id=%s", (row_id,), fetch="one")
    if not case:
        return _osc_download_error_response("找不到案件資料。", 404)

    case_number = str(case.get("case_number") or "")
    client_name = str(case.get("client_name") or "")

    # Sender
    sender_name = _osc_get_setting_value("firm_name", "")
    sender_address = _osc_get_setting_value("firm_address", "")

    # Receiver
    receiver_name = ""
    receiver_address = ""

    if recipient == "court":
        court_name = str(case.get("court_name") or "").strip()
        if not court_name:
            return _osc_download_error_response("案件未設定法院或地檢署名稱，請先編輯案件資料。", 400)
        receiver_name = court_name
        # Try to look up address from courts table
        court_row, _ = _osc_exec(
            "SELECT address FROM courts WHERE name=%s LIMIT 1",
            (court_name,),
            fetch="one",
        )
        receiver_address = str((court_row or {}).get("address") or "")

    elif recipient == "defendant":
        opp_rows, _ = _osc_exec(
            "SELECT name, address FROM opponents WHERE case_number=%s AND is_active=1 ORDER BY id LIMIT 1",
            (case_number,),
            fetch="all",
        )
        if not opp_rows:
            # Fallback: try notes
            notes = str(case.get("notes") or "")
            if not notes.strip():
                return _osc_download_error_response("案件沒有對造資料，請先補入對造姓名與地址。", 400)
            receiver_name = client_name or case_number
            receiver_address = notes[:80]
        else:
            opp = opp_rows[0]
            receiver_name = str(opp.get("name") or "")
            receiver_address = str(opp.get("address") or "")

    elif recipient == "laf":
        laf_branch = str(case.get("laf_branch") or "").strip()
        if not laf_branch:
            return _osc_download_error_response("案件未設定法扶分會，請先編輯案件資料。", 400)
        receiver_name = laf_branch
        branch_row, _ = _osc_exec(
            "SELECT address FROM legal_aid_branches WHERE name=%s LIMIT 1",
            (laf_branch,),
            fetch="one",
        )
        receiver_address = str((branch_row or {}).get("address") or "")

    try:
        png_bytes = _osc_build_address_label(
            sender_name, sender_address, receiver_name, receiver_address
        )
    except Exception as e:
        logging.exception("Address label generation error for case %s", row_id)
        return _osc_download_error_response(f"地址標籤產生失敗：{e}", 500)

    today = datetime.now().strftime("%Y%m%d")
    filename = f"地址標籤_{case_number or row_id}_{recipient}.png"

    as_attachment = (mode == "download")
    buf = io.BytesIO(png_bytes)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="image/png",
        as_attachment=as_attachment,
        download_name=filename,
    )
