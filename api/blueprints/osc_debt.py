# -*- coding: utf-8 -*-
"""
OSC 消費者債務清理 API Blueprint

路由：
  GET  /api/osc/debt/forms                → 列出所有消債表單類型
  GET  /api/osc/debt/schema/<form_type>   → 取得指定表單的欄位定義
  POST /api/osc/debt/generate             → 產生文件（回傳 docx 下載連結）
  GET  /api/osc/debt/address-data         → 取得債權人地址自動完成資料
  POST /api/osc/debt/address-data         → 新增/更新地址資料
  GET  /api/osc/debt/courts               → 取得法院清單
  POST /api/osc/debt/merge-pdf            → 合併 PDF 檔案
  POST /api/osc/debt/batch-generate       → 批次產生所有文件
  POST /api/osc/debt/auto-import          → 從已有文件自動帶入資料
  POST /api/osc/debt/validate             → 驗證表單資料
  GET  /api/osc/debt/expense-reference    → 取得法定費用參考金額
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import uuid

from flask import Blueprint, jsonify, request, send_file

logger = logging.getLogger("OSC_Debt")

osc_debt_bp = Blueprint("osc_debt", __name__)

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _export_dir():
    d = os.path.join(_MAGI_ROOT, "exports")
    os.makedirs(d, exist_ok=True)
    return d


def _save_doc(doc, form_type: str, data: dict) -> dict:
    """共用的文件儲存邏輯"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    token = uuid.uuid4().hex[:8]
    filename_map = {
        "application": "01_消費者債務清理聲請狀",
        "asset_statement": "02_財產及收入狀況說明書",
        "creditor_list": "03_債權人清冊",
        "report": "陳報狀",
    }
    base_name = filename_map.get(form_type, "消債文件")
    name = data.get("name") or data.get("A4") or ""
    if name:
        filename = f"{base_name}（{name}）_{stamp}_{token}.docx"
    else:
        filename = f"{base_name}_{stamp}_{token}.docx"

    docx_path = os.path.join(_export_dir(), filename)
    doc.save(docx_path)

    return {
        "ok": True,
        "form_type": form_type,
        "filename": os.path.basename(docx_path),
        "path": docx_path,
        "url": f"/exports/{os.path.basename(docx_path)}",
        "message": f"已產生 {base_name}",
    }


# ═══════════════════════════════════════════════════════════════
# 基本查詢 API
# ═══════════════════════════════════════════════════════════════

@osc_debt_bp.route("/api/osc/debt/forms", methods=["GET"])
def debt_forms_list():
    from api.debt_document_generator import get_all_form_types
    return jsonify({"ok": True, "forms": get_all_form_types()})


@osc_debt_bp.route("/api/osc/debt/schema/<form_type>", methods=["GET"])
def debt_form_schema(form_type):
    from api.debt_document_generator import get_form_schema
    schema = get_form_schema(form_type)
    if not schema:
        return jsonify({"ok": False, "error": f"未知的表單類型: {form_type}"}), 404
    return jsonify({"ok": True, "schema": schema})


@osc_debt_bp.route("/api/osc/debt/courts", methods=["GET"])
def debt_courts_list():
    from api.debt_document_generator import COURT_OPTIONS
    return jsonify({"ok": True, "courts": COURT_OPTIONS})


@osc_debt_bp.route("/api/osc/debt/expense-reference", methods=["GET"])
def debt_expense_reference():
    """取得法定費用參考金額（勞保費、健保費等）"""
    try:
        from api.debt_document_generator import STATUTORY_EXPENSE_REFERENCES
        return jsonify({"ok": True, "reference": STATUTORY_EXPENSE_REFERENCES})
    except Exception:
        return jsonify({"ok": True, "reference": {
            "勞保費": 1042,
            "健保費": 826,
        }})


# ═══════════════════════════════════════════════════════════════
# 證據資料掃描 API
# ═══════════════════════════════════════════════════════════════

@osc_debt_bp.route("/api/osc/debt/scan-evidence/<case_id>", methods=["GET"])
def debt_scan_evidence(case_id):
    """
    掃描指定案件的證據資料夾，回傳各欄位匹配結果。

    流程：
    1. 從 DB 取得案件的 folder_path（Windows canonical 路徑）
    2. 透過 case_path_mapper 轉換為本地路徑
    3. 找到「XX_證據資料」子資料夾
    4. 掃描檔名，與 EVIDENCE_SCAN_MAP 比對
    5. 回傳匹配結果供前端自動帶入
    """
    from api.debt_document_generator import scan_evidence_folder, REPORT_OPTIONS, EVIDENCE_SCAN_MAP

    case_id = (case_id or "").strip()
    if not case_id:
        return jsonify({"ok": False, "error": "缺少案件ID"}), 400

    # 從 DB 查案件資料
    try:
        from api.server import _osc_exec
        row, _ = _osc_exec(
            "SELECT id, case_number, client_name, case_category, case_type, folder_path FROM cases WHERE id=%s",
            (case_id,), fetch="one"
        )
    except Exception as e:
        logger.exception("查詢案件失敗")
        return jsonify({"ok": False, "error": f"DB 查詢失敗: {e}"}), 500

    if not row:
        return jsonify({"ok": False, "error": f"找不到案件 ID: {case_id}"}), 404

    folder_path = (row.get("folder_path") or "").strip()
    if not folder_path:
        return jsonify({
            "ok": False,
            "error": "此案件尚未設定資料夾路徑",
            "case": {"id": row.get("id"), "client_name": row.get("client_name")},
        }), 400

    # 轉換路徑到本地
    try:
        from api.case_path_mapper import translate_case_path_to_local
        local_path = translate_case_path_to_local(folder_path, require_existing=False)
    except Exception:
        local_path = folder_path.replace("\\", "/")

    if not os.path.isdir(local_path):
        return jsonify({
            "ok": False,
            "error": f"案件資料夾不存在或未同步（路徑: {local_path}）",
            "folder_path": folder_path,
            "local_path": local_path,
            "case": {"id": row.get("id"), "client_name": row.get("client_name")},
        }), 400

    # 執行證據掃描
    scan_result = scan_evidence_folder(local_path)

    # 附加 REPORT_OPTIONS 供前端使用
    scan_result["case"] = {
        "id": row.get("id"),
        "case_number": row.get("case_number"),
        "client_name": row.get("client_name"),
        "case_category": row.get("case_category"),
        "case_type": row.get("case_type"),
    }
    scan_result["report_options"] = REPORT_OPTIONS
    scan_result["folder_path"] = folder_path
    scan_result["local_path"] = local_path

    return jsonify(scan_result)


@osc_debt_bp.route("/api/osc/debt/cases", methods=["GET"])
def debt_cases_list():
    """
    取得消費者債務清理類型的案件列表（供陳報狀選擇案件用）。
    """
    try:
        from api.server import _osc_exec
        rows, _ = _osc_exec(
            """
            SELECT id, case_number, client_name, case_category, case_type, court_case_no, folder_path
            FROM cases
            WHERE case_type = '消費者債務清理'
               OR case_category = '消費者債務清理'
               OR case_reason LIKE '%%消債%%'
               OR case_reason LIKE '%%更生%%'
               OR case_reason LIKE '%%清算%%'
            ORDER BY created_date DESC
            LIMIT 200
            """,
            fetch="all"
        )
        return jsonify({"ok": True, "cases": rows or []})
    except Exception as e:
        logger.exception("查詢消債案件列表失敗")
        return jsonify({"ok": False, "error": str(e), "cases": []}), 500


# ═══════════════════════════════════════════════════════════════
# 地址資料 API
# ═══════════════════════════════════════════════════════════════

@osc_debt_bp.route("/api/osc/debt/address-data", methods=["GET", "POST"])
def debt_address_data():
    from api.debt_document_generator import get_address_options

    if request.method == "GET":
        return jsonify({"ok": True, **get_address_options()})

    # POST: 新增/更新地址
    payload = request.get_json(force=True, silent=True) or {}
    name = str(payload.get("name", "")).strip()
    address = str(payload.get("address", "")).strip()
    if not name or not address:
        return jsonify({"ok": False, "error": "名稱和地址為必填"}), 400

    try:
        from api.debt_document_generator import save_address_to_csv
        save_address_to_csv(name, address)
        return jsonify({"ok": True, "message": f"已儲存 {name} 的地址"})
    except Exception as e:
        logger.exception("地址儲存失敗")
        return jsonify({"ok": False, "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 文件產生 API
# ═══════════════════════════════════════════════════════════════

@osc_debt_bp.route("/api/osc/debt/generate", methods=["POST"])
def debt_generate_document():
    from api.debt_document_generator import (
        generate_application,
        generate_asset_statement,
        generate_creditor_list,
        generate_report,
    )

    payload = request.get_json(force=True, silent=True) or {}
    form_type = str(payload.get("form_type", "")).strip()
    data = payload.get("data") or {}

    generators = {
        "application": generate_application,
        "asset_statement": generate_asset_statement,
        "creditor_list": generate_creditor_list,
        "report": generate_report,
    }

    if form_type not in generators:
        return jsonify({"ok": False, "error": f"不支援的表單類型: {form_type}"}), 400

    try:
        doc = generators[form_type](data)
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        logger.exception("文件產生失敗: form_type=%s", form_type)
        return jsonify({"ok": False, "error": f"文件產生失敗: {e}"}), 500

    try:
        result = _save_doc(doc, form_type, data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": f"儲存失敗: {e}"}), 500


# ═══════════════════════════════════════════════════════════════
# 批次產生 API
# ═══════════════════════════════════════════════════════════════

@osc_debt_bp.route("/api/osc/debt/batch-generate", methods=["POST"])
def debt_batch_generate():
    """
    批次產生多份文件。
    body: {
        "data": { ... shared data ... },
        "types": ["application", "asset_statement", "creditor_list", "report"]
    }
    """
    from api.debt_document_generator import (
        generate_application,
        generate_asset_statement,
        generate_creditor_list,
        generate_report,
    )

    payload = request.get_json(force=True, silent=True) or {}
    data = payload.get("data") or {}
    types = payload.get("types") or ["application", "asset_statement", "creditor_list", "report"]

    generators = {
        "application": generate_application,
        "asset_statement": generate_asset_statement,
        "creditor_list": generate_creditor_list,
        "report": generate_report,
    }

    results = []
    errors = []

    for form_type in types:
        if form_type not in generators:
            errors.append({"form_type": form_type, "error": "不支援的類型"})
            continue
        try:
            doc = generators[form_type](data)
            result = _save_doc(doc, form_type, data)
            results.append(result)
        except Exception as e:
            logger.exception("批次產生失敗: %s", form_type)
            errors.append({"form_type": form_type, "error": str(e)})

    return jsonify({
        "ok": len(results) > 0,
        "results": results,
        "errors": errors,
        "message": f"已產生 {len(results)} 份文件" + (f"，{len(errors)} 份失敗" if errors else ""),
    })


# ═══════════════════════════════════════════════════════════════
# 自動帶入 API
# ═══════════════════════════════════════════════════════════════

@osc_debt_bp.route("/api/osc/debt/auto-import", methods=["POST"])
def debt_auto_import():
    """
    自動從 exports/ 目錄找到最近產生的財產說明書和債權人清冊，帶入聲請狀。
    支援兩種模式：
    1. 無上傳檔案 → 掃描 exports/ 目錄找最新的已產生文件
    2. multipart/form-data 上傳 asset_doc / creditor_doc → 直接讀取上傳檔案
    """
    from api.debt_document_generator import auto_import_from_docs
    import glob as _glob

    # 模式一：檢查是否有上傳檔案
    paths = {}
    temp_dir = None

    if request.files:
        temp_dir = tempfile.mkdtemp(prefix="magi_debt_import_")
        for key in ["asset_doc", "creditor_doc"]:
            f = request.files.get(key)
            if f and f.filename:
                save_path = os.path.join(temp_dir, f.filename)
                f.save(save_path)
                paths[key] = save_path

    # 模式二：無上傳檔案時，掃描 exports/ 目錄找最新文件
    if not paths:
        export_path = _export_dir()
        # 找最新的財產說明書
        asset_files = sorted(
            _glob.glob(os.path.join(export_path, "02_財產*說明書*.docx")),
            key=os.path.getmtime, reverse=True
        )
        if asset_files:
            paths["asset_doc"] = asset_files[0]

        # 找最新的債權人清冊
        creditor_files = sorted(
            _glob.glob(os.path.join(export_path, "03_債權人清冊*.docx")),
            key=os.path.getmtime, reverse=True
        )
        if creditor_files:
            paths["creditor_doc"] = creditor_files[0]

    if not paths:
        return jsonify({
            "ok": False,
            "error": "找不到已產生的財產說明書或債權人清冊。請先產生這些文件，或手動上傳。"
        }), 400

    try:
        result = auto_import_from_docs(
            asset_statement_path=paths.get("asset_doc", ""),
            creditor_list_path=paths.get("creditor_doc", ""),
        )
        found_files = []
        if paths.get("asset_doc"):
            found_files.append("財產說明書")
        if paths.get("creditor_doc"):
            found_files.append("債權人清冊")
        result["imported_from"] = found_files
        return jsonify({"ok": True, **result})
    except Exception as e:
        logger.exception("自動帶入失敗")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# 驗證 API
# ═══════════════════════════════════════════════════════════════

@osc_debt_bp.route("/api/osc/debt/validate", methods=["POST"])
def debt_validate():
    """
    驗證表單資料，回傳驗證結果。
    body: { "form_type": "...", "data": { ... } }
    """
    payload = request.get_json(force=True, silent=True) or {}
    form_type = str(payload.get("form_type", "")).strip()
    data = payload.get("data") or {}

    validators = {}
    try:
        from api.debt_document_generator import (
            validate_application_data,
            validate_asset_statement_data,
            validate_creditor_list_data,
            validate_report_data,
        )
        validators = {
            "application": validate_application_data,
            "asset_statement": validate_asset_statement_data,
            "creditor_list": validate_creditor_list_data,
            "report": validate_report_data,
        }
    except ImportError:
        return jsonify({"ok": True, "valid": True, "errors": {}})

    if form_type not in validators:
        return jsonify({"ok": False, "error": f"不支援的類型: {form_type}"}), 400

    try:
        valid, errors = validators[form_type](data)
        return jsonify({"ok": True, "valid": valid, "errors": errors})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# PDF 合併 API
# ═══════════════════════════════════════════════════════════════

@osc_debt_bp.route("/api/osc/debt/merge-pdf", methods=["POST"])
def debt_merge_pdf():
    from api.debt_document_generator import merge_debt_pdfs

    uploaded_files = request.files.getlist("files[]") or request.files.getlist("files")
    if not uploaded_files:
        return jsonify({"ok": False, "error": "未上傳任何檔案"}), 400

    temp_dir = tempfile.mkdtemp(prefix="magi_debt_merge_")
    file_paths = []
    for f in uploaded_files:
        if f.filename:
            save_path = os.path.join(temp_dir, f.filename)
            f.save(save_path)
            file_paths.append(save_path)

    if not file_paths:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": "沒有有效的檔案"}), 400

    try:
        add_bookmarks = request.form.get("add_bookmarks", "true").lower() == "true"
        output_path = merge_debt_pdfs(file_paths, add_bookmarks=add_bookmarks)
    except Exception as e:
        logger.exception("PDF 合併失敗")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": f"合併失敗: {e}"}), 500

    shutil.rmtree(temp_dir, ignore_errors=True)

    return jsonify({
        "ok": True,
        "filename": os.path.basename(output_path),
        "url": f"/exports/{os.path.basename(output_path)}",
        "message": f"已合併 {len(file_paths)} 個檔案",
    })
