"""OSC (案件管理) API routes blueprint.

Extracted from server.py to reduce its size.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, request, jsonify, send_file
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


def _get_orchestrator():
    from api.server import orchestrator
    return orchestrator


def _get_normalize_output_text():
    try:
        from api.tw_output_guard import normalize_output_text
        return normalize_output_text
    except Exception:
        return None


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
    from api.server import _osc_fetch_url_text as _impl
    return _impl(url, timeout=timeout)


def _osc_lookup_fulltext_fallback(title: str = "", case_number: str = "", url: str = "") -> dict:
    from api.server import _osc_lookup_fulltext_fallback as _impl
    return _impl(title=title, case_number=case_number, url=url)


def _export_osc_form_files(title: str, preview_text: str, suggested_filename: str = "") -> dict:
    from api.server import _export_osc_form_files as _impl
    return _impl(title, preview_text, suggested_filename)


def _export_file_meta(path: str) -> dict:
    from api.server import _export_file_meta as _impl
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

## 參考實務見解
{legal_insights}

## 書寫風格參考（以下為過往類似書狀的格式範例）
{reference_style}

## 要求
1. 請按照上述參考風格撰寫完整的{doc_type}
2. 格式需符合台灣法院規範
3. 適當引用提供的實務見解（如有提供）
4. 確保案號、股別、法院名稱正確填入狀頭
5. 論述需有邏輯、條理分明
6. 請加入常見的法律用語和格式

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


# ══════════════════════════════════════════════════════════════════════════════
# OSC Meta
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/meta", methods=["GET"])
@login_required
def osc_meta_api():
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
                "legal_insights",
                "court_judgments",
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
        return jsonify({"ok": False, "error": str(e)}), 500


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
        limit = max(1, min(500, int(request.args.get("limit") or "200")))
        where = []
        params = []
        if q:
            like = f"%{q}%"
            where.append(
                """
                (
                    case_number LIKE %s
                    OR client_name LIKE %s
                    OR court_case_no LIKE %s
                    OR laf_case_no LIKE %s
                    OR application_no LIKE %s
                )
                """
            )
            params.extend([like, like, like, like, like])
        if category and category not in {"全部", "all", "ALL"}:
            if category == "消費者債務清理":
                where.append("(case_category = %s OR case_type = %s)")
                params.extend([category, category])
            else:
                where.append("case_category = %s")
                params.append(category)
        sql = """
            SELECT id, case_number, client_name, case_category, case_type, case_stage, case_reason,
                   laf_case_no, application_no, court_case_no, status, notes, updated_at, created_date
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
        "laf_case_no", "application_no", "court_case_no", "status", "notes", "folder_path"
    ]
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
        (payload.get("court_case_no") or payload.get("court_case_number") or "").strip() or None,
        (payload.get("status") or "Active").strip() or "Active",
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
            "court_case_no": (payload.get("court_case_no") or payload.get("court_case_number") or "").strip() or None,
            "status": (payload.get("status") or "Active").strip() or "Active",
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
        return jsonify({"ok": True, "result": result, "id": target.get("id"), "mode": "upsert"})


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
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Case open-folder / create-folder / folder-browser
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/cases/<row_id>/open-folder", methods=["POST"])
@login_required
def osc_case_open_folder_api(row_id):
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
    smb = smb_candidates[0] if smb_candidates else ""
    local_candidates = _osc_local_path_candidates(norm)

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
            "case": {"id": row.get("id"), "case_number": row.get("case_number"), "client_name": row.get("client_name")},
            "folder_path": norm,
            "smb_url": smb,
            "smb_candidates": smb_candidates,
            "local_candidates": local_candidates,
            "chosen_open_path": chosen_open_path,
            "open_result": open_result,
            "browser_supported": True,
            "browser_url": f"/api/osc/cases/{row_id}/folder-browser",
        }
    )


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
        reply = orchestrator.process_message(
            user_id=str(current_user.id),
            message=prompt,
            platform="WEB",
            role=current_user.role,
        )
        if _normalize_output_text:
            reply = _normalize_output_text(str(reply or ""), platform="WEB")
        return jsonify({"ok": True, "action": action, "case": case, "reply": str(reply or "")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
        "todo_pending": len([t for t in todos if str(t.get("status") or "").lower() not in {"completed", "done", "已完成"}]),
        "todo_completed": len([t for t in todos if str(t.get("status") or "").lower() in {"completed", "done", "已完成"}]),
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
        WHERE status IS NULL OR status='' OR LOWER(status) NOT IN ('completed', 'done')
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


@osc_bp.route("/api/osc/files/content", methods=["GET"])
@login_required
def osc_file_content_api():
    raw = str(request.args.get("path") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "path required"}), 400
    local_file = _osc_resolve_existing_local_path(raw, prefer_dir=False)
    if not local_file:
        return jsonify({"ok": False, "error": "file_not_found"}), 404
    if not _osc_is_safe_local_path(local_file):
        return jsonify({"ok": False, "error": "path_not_allowed"}), 403
    inline = str(request.args.get("inline") or "").strip() in {"1", "true", "yes"}
    mime, _ = mimetypes.guess_type(local_file)
    import io
    # --- size guard: reject files > 50 MB to avoid unbounded memory usage ---
    try:
        file_size = os.path.getsize(local_file)
    except OSError:
        file_size = 0
    if file_size > 50 * 1024 * 1024:  # 50 MB limit
        return jsonify({"ok": False, "error": "File too large", "size_mb": round(file_size / 1024 / 1024, 1)}), 413
    try:
        with open(local_file, "rb") as f:
            buf = io.BytesIO(f.read())
    except OSError as e:
        _log.error("osc_file_content_api read error (errno=%s): %s - file=%s", e.errno, e, local_file)
        return jsonify({"ok": False, "error": f"send_file_error: {e}"}), 500
    try:
        resp = send_file(
            buf,
            mimetype=mime or "application/octet-stream",
            as_attachment=not inline,
            download_name=os.path.basename(local_file),
        )
        try:
            st = os.stat(local_file)
            resp.headers["ETag"] = f'"{int(st.st_mtime)}-{st.st_size}"'
            resp.headers["Cache-Control"] = "private, max-age=300"
        except OSError:
            pass
        return resp
    except Exception as e:
        _log.error("osc_file_content_api send_file error: %s - file=%s", e, local_file)
        return jsonify({"ok": False, "error": f"send_file_error: {e}"}), 500


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
    _MAX_PER_FILE = 50 * 1024 * 1024   # 50 MB per file
    _MAX_TOTAL    = 200 * 1024 * 1024   # 200 MB total

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
            return jsonify({"ok": False, "error": "file_too_large", "file_name": name,
                            "size_mb": round(fsize / 1024 / 1024, 1), "limit_mb": 50}), 413
        total_saved += fsize
        if total_saved > _MAX_TOTAL:
            os.remove(dest)
            return jsonify({"ok": False, "error": "total_upload_too_large",
                            "total_mb": round(total_saved / 1024 / 1024, 1), "limit_mb": 200}), 413
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
        (payload.get("status") or "Active").strip() or "Active",
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
    if "status" in payload and str(payload.get("status")).strip().lower() == "completed":
        sets.append("completed_date=NOW()")
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE case_todos SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    return jsonify({"ok": True, "result": result})


# ══════════════════════════════════════════════════════════════════════════════
# Insights
# ══════════════════════════════════════════════════════════════════════════════


@osc_bp.route("/api/osc/insights", methods=["GET", "POST"])
@login_required
def osc_insights_api():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip().lower()
        case_number = (request.args.get("case_number") or "").strip().lower()
        case_reason = (request.args.get("case_reason") or "").strip().lower()
        limit = max(1, min(500, int(request.args.get("limit") or "300")))
        items = _osc_collect_insights()
        if q:
            def _hit(it):
                blob = " ".join(
                    [
                        str(it.get("title") or ""),
                        str(it.get("summary") or ""),
                        str(it.get("full_text") or ""),
                        str(it.get("case_number") or ""),
                        str(it.get("case_reason") or ""),
                        str(it.get("court") or ""),
                    ]
                ).lower()
                return q in blob
            items = [it for it in items if _hit(it)]
        if case_number:
            items = [it for it in items if case_number in str(it.get("case_number") or "").lower()]
        if case_reason:
            items = [
                it
                for it in items
                if case_reason in " ".join(
                    [
                        str(it.get("case_reason") or ""),
                        str(it.get("title") or ""),
                        str(it.get("summary") or ""),
                    ]
                ).lower()
            ]
        items = items[:limit]
        return jsonify({"ok": True, "items": items})
    payload = request.get_json() or {}
    insight_text = (payload.get("insight_text") or payload.get("full_text") or "").strip()
    if not insight_text:
        return jsonify({"ok": False, "error": "insight_text required"}), 400
    cols = ["case_number", "document_name", "court_reference", "court_type", "insight_type", "insight_text", "case_reason", "source_file", "raw_text"]
    vals = [
        (payload.get("case_number") or "").strip() or None,
        (payload.get("document_name") or payload.get("title") or "手動新增見解").strip(),
        (payload.get("court_reference") or payload.get("court") or "").strip() or None,
        (payload.get("court_type") or "").strip() or None,
        (payload.get("insight_type") or payload.get("source_type") or "manual").strip(),
        insight_text,
        (payload.get("case_reason") or "").strip() or None,
        (payload.get("source_file") or "").strip() or None,
        (payload.get("raw_text") or "").strip() or None,
    ]
    result, _ = _osc_exec(
        f"INSERT INTO legal_insights ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
        tuple(vals),
        fetch="none",
    )
    return jsonify({"ok": True, "result": result})


@osc_bp.route("/api/osc/insights/<insight_id>", methods=["GET"])
@login_required
def osc_insight_detail_api(insight_id):
    sid = (insight_id or "").strip()
    if sid.startswith("li-"):
        row_id = sid.split("-", 1)[1]
        row, _ = _osc_exec("SELECT * FROM legal_insights WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if sid.startswith("cj-"):
        row_id = sid.split("-", 1)[1]
        row, _ = _osc_exec("SELECT * FROM court_judgments WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    for it in _osc_collect_insights():
        if str(it.get("id")) == sid:
            return jsonify({"ok": True, "item": it})
    return jsonify({"ok": False, "error": "not found"}), 404


@osc_bp.route("/api/osc/insights/fetch-full", methods=["POST"])
@login_required
def osc_insights_fetch_full_api():
    payload = request.get_json() or {}
    url = (payload.get("url") or "").strip()
    raw_title = (payload.get("title") or "").strip()
    title = raw_title or "裁判見解全文"
    case_number = (payload.get("case_number") or "").strip() or None
    case_reason = (payload.get("case_reason") or "").strip() or None
    if not url and not case_number and not raw_title:
        return jsonify({"ok": False, "error": "url, title or case_number required"}), 400
    full_text = ""
    fallback_source = ""
    fetch_error = ""
    if url:
        fetched = _osc_fetch_url_text(url, timeout=15)
        if fetched.get("ok"):
            full_text = (fetched.get("text") or "").strip()
        else:
            fetch_error = fetched.get("error") or "fetch_failed"
    if not full_text:
        fallback = _osc_lookup_fulltext_fallback(title=title, case_number=case_number or "", url=url or "")
        if fallback.get("ok"):
            full_text = (fallback.get("text") or "").strip()
            fallback_source = str(fallback.get("source") or "")
    if not full_text:
        jy = _osc_fetch_fulltext_from_judicial(
            title=title,
            case_number=case_number or "",
            case_reason=case_reason or "",
            timeout_sec=45,
        )
        if jy.get("ok"):
            full_text = (jy.get("text") or "").strip()
            fallback_source = str(jy.get("source") or "")
    if not full_text:
        error_detail = fetch_error or "all_sources_exhausted"
        return jsonify({"ok": False, "error": error_detail, "detail": "URL 抓取失敗、本地 DB 無紀錄、判決收集器也未找到結果。請確認 URL 正確或直接貼上全文。"}), 400
    actor_id = str(getattr(current_user, "id", "") or "osc_web")
    _ = actor_id
    try:
        summary = _osc_summarize_legal_insight(full_text)
    except Exception as e:
        summary = f"摘要失敗：{e}"
    cols = ["case_number", "document_name", "court_reference", "insight_type", "insight_text", "case_reason", "source_file", "raw_text"]
    vals = [case_number, title, None, "web_fetch_fulltext", str(summary or "").strip(), case_reason, url, full_text]
    r, _ = _osc_exec(
        f"INSERT INTO legal_insights ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
        tuple(vals),
        fetch="none",
    )
    return jsonify(
        {
            "ok": True,
            "inserted": r,
            "item": {
                "source": "網頁全文擷取" if not fallback_source else f"網頁全文擷取（{fallback_source}）",
                "title": title,
                "case_number": case_number or "",
                "case_reason": case_reason or "",
                "url": url,
                "summary": str(summary or ""),
                "full_text": full_text,
            },
        }
    )


@osc_bp.route("/api/osc/judgments", methods=["GET"])
@login_required
def osc_judgments_compat_api():
    """
    Canonical judgments endpoint: returns merged insights from DB + judgments.json.
    """
    try:
        return jsonify(_osc_collect_insights())
    except Exception as e:
        logger.error(f"Error serving merged judgments: {e}")
        return jsonify([])


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
            config['default_lawyer'] = '喬政翔律師'
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
        cid = str(it.get("id") or "").strip()
        src = str(it.get("source_local") or "").strip()
        dst = str(it.get("target_local") or "").strip()
        if not src or not os.path.exists(src):
            skipped.append({"id": cid, "case_number": it.get("case_number"), "reason": "source_missing"})
            continue
        if not dst:
            skipped.append({"id": cid, "case_number": it.get("case_number"), "reason": "target_missing"})
            continue
        if os.path.exists(dst) and not force:
            skipped.append({"id": cid, "case_number": it.get("case_number"), "reason": "target_exists"})
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.move(src, dst)
            _osc_exec("UPDATE cases SET folder_path=%s, updated_at=NOW() WHERE id=%s", (dst, cid), fetch="none")
            moved.append({"id": cid, "case_number": it.get("case_number"), "from": src, "to": dst})
        except Exception as e:
            errors.append({"id": cid, "case_number": it.get("case_number"), "error": str(e)})

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
