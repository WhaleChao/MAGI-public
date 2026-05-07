"""
Draft / form generation helpers extracted from api.server.
"""

from __future__ import annotations

import json
import html as ihtml
import logging
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

from api.model_config import TEXT_PRIMARY_MODEL
from api.runtime_paths import ensure_path_on_sys_path, get_orch_dir
from api.case_path_mapper import preferred_case_roots
from api.osc.insight_filters import displayable_insight_item, is_non_extractable_legal_insight

# ---------------------------------------------------------------------------
# Lazy back-references into server helpers.
# These are resolved at call time via the _srv() accessor so that
# circular-import problems are avoided.
# ---------------------------------------------------------------------------
_server_mod = None


def _srv():
    global _server_mod
    if _server_mod is None:
        import api.server as _s
        _server_mod = _s
    return _server_mod


# ── helpers forwarded from server ──────────────────────────────────────────

def _osc_exec(sql, params=(), fetch="none"):
    # 2026-04-30: server.py 重構後不再 export _osc_exec；改直接呼叫 osc.utils。
    # 保留 _srv() fallback 以相容未來可能的 monkeypatch。
    try:
        from api.osc.utils import _osc_exec as _utils_exec
        return _utils_exec(sql, params=params, fetch=fetch)
    except (ImportError, AttributeError):
        return _srv()._osc_exec(sql, params=params, fetch=fetch)


def _osc_truthy(v) -> bool:
    try:
        from api.osc.utils import _osc_truthy as _utils_truthy
        return _utils_truthy(v)
    except (ImportError, AttributeError):
        return _srv()._osc_truthy(v)


def _osc_get_setting_value(key: str, default: str = "") -> str:
    # bug fix 2026-05-02：server.py 重構後不再 export，try utils 再 fallback _srv
    try:
        from api.osc.utils import _osc_get_setting_value as _utils_fn
        return _utils_fn(key, default)
    except (ImportError, AttributeError):
        return _srv()._osc_get_setting_value(key, default)


def _osc_unique_strings(values) -> list[str]:
    try:
        from api.osc.utils import _osc_unique_strings as _utils_fn
        return _utils_fn(values)
    except (ImportError, AttributeError):
        return _srv()._osc_unique_strings(values)


def _osc_collect_insights():
    # _osc_collect_insights 在 api.osc.judicial（不在 utils）
    try:
        from api.osc.judicial import _osc_collect_insights as _judicial_fn
        return _judicial_fn()
    except (ImportError, AttributeError):
        return _srv()._osc_collect_insights()


def _osc_read_reference_document(raw_path: str, max_chars: int = 9000) -> dict:
    try:
        from api.osc.utils import _osc_read_reference_document as _utils_fn
        return _utils_fn(raw_path, max_chars=max_chars)
    except (ImportError, AttributeError):
        return _srv()._osc_read_reference_document(raw_path, max_chars=max_chars)


def _osc_norm_path(path_str: str) -> str:
    try:
        from api.osc.utils import _osc_norm_path as _utils_fn
        return _utils_fn(path_str)
    except (ImportError, AttributeError):
        return _srv()._osc_norm_path(path_str)


def _osc_local_path_candidates(path_str: str) -> list[str]:
    try:
        from api.osc.utils import _osc_local_path_candidates as _utils_fn
        return _utils_fn(path_str)
    except (ImportError, AttributeError):
        return _srv()._osc_local_path_candidates(path_str)


def _osc_guess_case_folder(case_number: str) -> str:
    try:
        from api.osc.utils import _osc_guess_case_folder as _utils_fn
        return _utils_fn(case_number)
    except (ImportError, AttributeError):
        return _srv()._osc_guess_case_folder(case_number)


def _public_url_for_local_file(local_path: str) -> str:
    return _srv()._public_url_for_local_file(local_path)


def _get_runtime_config() -> dict:
    return _srv().RUNTIME_CONFIG


def _get_draft_prompt_template() -> str:
    return _srv()._OSC_DRAFT_PROMPT_TEMPLATE


# ═══════════════════════════════════════════════════════════════════════════
# Draft generation
# ═══════════════════════════════════════════════════════════════════════════

def _osc_clean_draft_output(text: str) -> str:
    cleaned = ihtml.unescape(str(text or ""))
    cleaned = cleaned.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    cleaned = re.sub(r"^#+\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*(.+?)\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned)
    cleaned = re.sub(r"_(.+?)_", r"\1", cleaned)
    cleaned = re.sub(r"^[-*_]{3,}\s*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"```(?:[\s\S]*?)```", "", cleaned)
    cleaned = re.sub(r"`(.+?)`", r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _osc_render_draft_template(template: str, values: dict) -> str:
    rendered = str(template or "")
    for key, value in (values or {}).items():
        rendered = rendered.replace("{" + str(key) + "}", str(value or ""))
    return rendered


def _osc_draft_enabled_flag() -> bool:
    raw = _osc_get_setting_value("enable_draft_generation", "")
    if raw:
        return _osc_truthy(raw)
    cfg_val = _get_runtime_config().get("enable_draft_generation")
    if isinstance(cfg_val, bool):
        return cfg_val
    if cfg_val is None:
        return True
    return _osc_truthy(cfg_val)


def _osc_draft_defendant(case_row: dict, payload: dict) -> str:
    explicit = str(payload.get("defendant") or payload.get("opponent_name") or "").strip()
    if explicit:
        return explicit
    row_value = str((case_row or {}).get("opponent_name") or "").strip()
    if row_value:
        return row_value
    case_number = str((payload.get("case_number") or (case_row or {}).get("case_number") or "")).strip()
    if not case_number:
        return ""
    try:
        rows, _ = _osc_exec(
            "SELECT name FROM opponents WHERE case_number=%s AND (is_active=1 OR is_active IS NULL) ORDER BY updated_date DESC, id DESC LIMIT 5",
            (case_number,),
            fetch="all",
        )
        names = _osc_unique_strings((r.get("name") for r in (rows or [])))
        return "、".join(names)
    except Exception:
        return ""


def _osc_resolve_draft_insights(payload: dict) -> list[dict]:
    return []


def _osc_collect_draft_reference_style(payload: dict) -> tuple[str, list[dict], list[str]]:
    selected = payload.get("selected_documents") or payload.get("reference_documents") or []
    blocks = []
    refs = []
    warnings = []
    for raw in (selected or [])[:3]:
        if not isinstance(raw, dict):
            continue
        file_path = str(raw.get("file_path") or raw.get("path") or "").strip()
        file_name = str(raw.get("file_name") or os.path.basename(file_path or "") or "參考文件").strip()
        provided = str(raw.get("text") or raw.get("content") or "").strip()
        read_meta = None
        text = provided
        if not text and file_path:
            read_meta = _osc_read_reference_document(file_path, max_chars=9000)
            text = str(read_meta.get("text") or "").strip()
        if text:
            excerpt = text[:3000].strip()
            blocks.append(f"--- 參考範本：{file_name} ---\n{excerpt}")
        else:
            reason = str((read_meta or {}).get("error") or "no_content")
            warnings.append(f"{file_name}: {reason}")
        refs.append(
            {
                "id": str(raw.get("id") or "").strip(),
                "file_name": file_name,
                "file_path": file_path,
                "resolved_path": str((read_meta or {}).get("resolved_path") or "").strip(),
                "loaded": bool(text),
            }
        )
    return ("\n\n".join(blocks).strip() or "(無參考範本)"), refs, warnings


def _osc_build_draft_context(payload: dict) -> dict:
    body = payload or {}
    case_row = {}
    case_id = str(body.get("case_id") or body.get("selected_case_id") or "").strip()
    lookup_number = str(body.get("case_lookup_number") or body.get("selected_case_number") or body.get("case_number") or "").strip()

    if case_id:
        case_row, _ = _osc_exec("SELECT * FROM cases WHERE id=%s LIMIT 1", (case_id,), fetch="one")
    elif lookup_number:
        case_row, _ = _osc_exec(
            """
            SELECT * FROM cases
            WHERE case_number=%s OR court_case_no=%s OR court_case_number=%s
            ORDER BY updated_at DESC, created_date DESC
            LIMIT 1
            """,
            (lookup_number, lookup_number, lookup_number),
            fetch="one",
        )
    case_row = case_row or {}

    doc_type = str(body.get("doc_type") or body.get("document_type") or "").strip()
    case_number = str(
        body.get("case_number")
        or case_row.get("court_case_number")
        or case_row.get("court_case_no")
        or case_row.get("case_number")
        or ""
    ).strip()
    division = str(body.get("division") or case_row.get("court_division") or "").strip()
    court_name = str(body.get("court_name") or case_row.get("court_name") or "").strip()
    reason = str(body.get("reason") or case_row.get("case_reason") or "").strip()
    plaintiff = str(body.get("plaintiff") or case_row.get("client_name") or "").strip()
    defendant = _osc_draft_defendant(case_row, body)
    case_facts = str(body.get("case_facts") or body.get("facts") or case_row.get("description") or case_row.get("notes") or "").strip()

    reference_style, references, warnings = _osc_collect_draft_reference_style(body)
    custom_template = _osc_get_setting_value("draft_prompt_template", "").strip()
    template = custom_template if custom_template else _get_draft_prompt_template()
    values = {
        "doc_type": doc_type or "(未指定)",
        "case_number": case_number or "(待填)",
        "division": division or "(待填)",
        "court_name": court_name or "(待填)",
        "reason": reason or "(未指定)",
        "plaintiff": plaintiff or "(待填)",
        "defendant": defendant or "(待填)",
        "case_facts": case_facts or "(未提供)",
        "public_reference_note": "(public release: external legal-research sources are not included)",
        "reference_style": reference_style or "(無參考範本)",
    }
    prompt = _osc_render_draft_template(template, values)
    suggested_filename = str(body.get("suggested_filename") or "").strip()
    if not suggested_filename:
        parts = [doc_type or "書狀草稿", case_number or case_row.get("case_number") or "未命名"]
        suggested_filename = "_".join(str(p).strip() for p in parts if str(p).strip())
    return {
        "case": case_row,
        "doc_type": doc_type,
        "case_number": case_number,
        "division": division,
        "court_name": court_name,
        "reason": reason,
        "plaintiff": plaintiff,
        "defendant": defendant,
        "case_facts": case_facts,
        "selected_insights": selected_insights,
        "selected_documents": references,
        "warnings": warnings,
        "prompt": prompt,
        "template_source": "custom" if custom_template else "default",
        "suggested_filename": suggested_filename,
        "export_title": doc_type or "書狀草稿",
    }


def _osc_generate_draft_with_casper(prompt: str) -> str:
    skill_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "skills", "casper-client"))
    if skill_dir not in sys.path:
        sys.path.append(skill_dir)
    try:
        from casper_tools_client import casper_chat  # type: ignore
    except Exception as e:
        raise RuntimeError(f"CASPER 客戶端載入失敗: {e}")

    result = casper_chat(prompt, timeout_sec=90)
    if not isinstance(result, dict) or not result.get("success"):
        err = (result.get("error") if isinstance(result, dict) else "") or "unknown_error"
        raise RuntimeError(f"CASPER 生成失敗: {err}")
    text = str(result.get("response") or "").strip()
    if not text:
        raise RuntimeError("CASPER 生成失敗: empty response")
    return text


def _osc_generate_draft_with_ollama(prompt: str, model: str, ollama_url: str) -> str:
    """透過 oMLX (OpenAI-compatible API) 生成草稿。保留函式名以相容既有呼叫端。"""
    base = (ollama_url or os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:11434")).rstrip("/")
    url = base + "/v1/chat/completions"
    body = {
        "model": model or TEXT_PRIMARY_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2048,
        "stream": False,
    }
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.URLError as e:
        raise RuntimeError(f"oMLX 連線失敗: {e}")
    try:
        data = json.loads(raw or "{}")
    except Exception as e:
        raise RuntimeError(f"oMLX 回應解析失敗: {e}")
    choices = data.get("choices") or []
    text = (choices[0].get("message", {}).get("content", "") if choices else "").strip()
    if not text:
        raise RuntimeError("oMLX 未回傳內容")
    return text


def _osc_generate_draft_with_gemini(prompt: str) -> tuple[str, str]:
    allow_cloud = str(os.environ.get("MAGI_ALLOW_CLOUD_MODELS", "0") or "").strip().lower() in {"1", "true", "yes", "on"}
    if not allow_cloud:
        return _osc_generate_draft_with_casper(prompt), "casper"
    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or _osc_get_setting_value("gemini_api_key", "")
        or ""
    ).strip()
    model_name = (
        os.environ.get("GEMINI_MODEL")
        or _osc_get_setting_value("gemini_model", "")
        or str(_get_runtime_config().get("gemini_model") or "").strip()
        or "gemini-2.0-flash"
    ).strip()
    if not api_key:
        raise RuntimeError("未設定 Gemini API Key")
    try:
        import google.generativeai as genai  # type: ignore
    except Exception as e:
        raise RuntimeError(f"google-generativeai 套件不可用: {e}")
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(prompt)
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            raise RuntimeError("Gemini 未回傳內容")
        return text, model_name
    except Exception as e:
        raise RuntimeError(f"Gemini API 錯誤: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Case identity lookup
# ═══════════════════════════════════════════════════════════════════════════

def _osc_get_case_identity_by_payload(payload: dict) -> dict:
    p = payload or {}
    row = None
    row_id = str(p.get("case_id") or p.get("id") or "").strip()
    case_number = str(p.get("case_number") or "").strip()
    laf_case_no = str(p.get("laf_case_no") or p.get("laf_case_number") or "").strip()
    client_name = str(p.get("client_name") or "").strip()

    if row_id:
        row, _ = _osc_exec(
            """
            SELECT id, case_number, client_name, case_category, case_stage, case_reason, status, folder_path,
                   laf_case_no, application_no, court_case_no
            FROM cases
            WHERE id=%s
            LIMIT 1
            """,
            (row_id,),
            fetch="one",
        )
    if (not row) and case_number:
        row, _ = _osc_exec(
            """
            SELECT id, case_number, client_name, case_category, case_stage, case_reason, status, folder_path,
                   laf_case_no, application_no, court_case_no
            FROM cases
            WHERE case_number=%s
            LIMIT 1
            """,
            (case_number,),
            fetch="one",
        )
    if (not row) and laf_case_no:
        row, _ = _osc_exec(
            """
            SELECT id, case_number, client_name, case_category, case_stage, case_reason, status, folder_path,
                   laf_case_no, application_no, court_case_no
            FROM cases
            WHERE laf_case_no=%s
            LIMIT 1
            """,
            (laf_case_no,),
            fetch="one",
        )
    if (not row) and client_name:
        row, _ = _osc_exec(
            """
            SELECT id, case_number, client_name, case_category, case_stage, case_reason, status, folder_path,
                   laf_case_no, application_no, court_case_no
            FROM cases
            WHERE client_name=%s
            ORDER BY updated_at DESC, created_date DESC
            LIMIT 1
            """,
            (client_name,),
            fetch="one",
        )
    return row or {}


# ═══════════════════════════════════════════════════════════════════════════
# Form generation
# ═══════════════════════════════════════════════════════════════════════════

def _osc_build_form_preview(form_type: str, case_row: dict, fields: dict) -> dict:
    ftype = str(form_type or "").strip().lower()
    if ftype in {"poa", "power_of_attorney", "委任狀", "委任状"}:
        ftype = "power_of_attorney"
    elif ftype in {"receipt", "收據", "收据"}:
        ftype = "receipt"
    elif ftype in {"contract", "契約", "契約書", "契约", "契约书"}:
        ftype = "contract"
    else:
        raise ValueError("unsupported_form_type")

    c = case_row or {}
    f = fields or {}
    today = datetime.now().strftime("%Y-%m-%d")

    if ftype == "contract":
        title = "契約書草稿"
        doc = (
            f"{title}\n\n"
            f"日期：{f.get('date') or today}\n"
            f"當事人：{f.get('client_name') or c.get('client_name') or ''}\n"
            f"案件編號：{f.get('case_number') or c.get('case_number') or ''}\n"
            f"法院案號：{f.get('court_case_no') or c.get('court_case_no') or ''}\n"
            f"法扶案號：{f.get('laf_case_no') or c.get('laf_case_no') or ''}\n"
            f"受任律師：{f.get('lawyer_name') or '＿＿＿＿'}\n"
            f"費用項目：{f.get('item') or ''}\n"
            f"金額：{f.get('amount') or ''}\n"
            f"備註：{f.get('notes') or ''}\n"
            f"\n\n（以下為契約條文草稿，請自行替換正文）"
        )
        filename = f"契約書草稿_{c.get('case_number') or '未指定位案件'}"
        return {"form_type": ftype, "title": title, "preview_text": doc, "suggested_filename": filename}

    if ftype == "power_of_attorney":
        title = "委任狀草稿"
        doc = (
            f"{title}\n\n"
            f"日期：{f.get('date') or today}\n"
            f"當事人：{f.get('client_name') or c.get('client_name') or ''}\n"
            f"案件編號：{f.get('case_number') or c.get('case_number') or ''}\n"
            f"法院案號：{f.get('court_case_no') or c.get('court_case_no') or ''}\n"
            f"法扶案號：{f.get('laf_case_no') or c.get('laf_case_no') or ''}\n"
            f"案由：{f.get('case_reason') or c.get('case_reason') or ''}\n"
            f"受任律師：{f.get('lawyer_name') or '＿＿＿＿'}\n"
            f"備註：{f.get('notes') or ''}\n"
        )
        filename = f"委任狀草稿_{c.get('case_number') or '未指定位案件'}"
        return {"form_type": ftype, "title": title, "preview_text": doc, "suggested_filename": filename}

    amount = f.get("amount") or ""
    if amount:
        try:
            amount = f"{float(amount):,.0f}"
        except Exception:
            amount = str(amount)
    title = "收據草稿"
    doc = (
        f"{title}\n\n"
        f"日期：{f.get('date') or today}\n"
        f"收據編號：{f.get('receipt_no') or ''}\n"
        f"當事人：{f.get('client_name') or c.get('client_name') or ''}\n"
        f"案件編號：{f.get('case_number') or c.get('case_number') or ''}\n"
        f"法扶案號：{f.get('laf_case_no') or c.get('laf_case_no') or ''}\n"
        f"費用項目：{f.get('item') or '法律服務費'}\n"
        f"金額：{amount}\n"
        f"付款方式：{f.get('payment_method') or ''}\n"
        f"備註：{f.get('notes') or ''}\n"
    )
    filename = f"收據草稿_{c.get('case_number') or '未指定位案件'}"
    return {"form_type": ftype, "title": title, "preview_text": doc, "suggested_filename": filename}


# ═══════════════════════════════════════════════════════════════════════════
# LAF import helpers
# ═══════════════════════════════════════════════════════════════════════════

def _osc_import_laf_orchestrator():
    ensure_path_on_sys_path(get_orch_dir())
    from laf_orchestrator import LAFOrchestrator  # type: ignore
    return LAFOrchestrator


def _osc_map_laf_action(action: str) -> str:
    a = str(action or "").strip().lower()
    aliases = {
        "開辦": "go_live",
        "go_live": "go_live",
        "golive": "go_live",
        "疑義": "inquiry",
        "inquiry": "inquiry",
        "訴訟中費用支付": "fee",
        "fee": "fee",
        "二階段": "condition",
        "condition": "condition",
        "撤回": "withdrawal",
        "withdrawal": "withdrawal",
        "結案": "closing",
        "closing": "closing",
    }
    return aliases.get(a, a)


def _osc_prepare_laf_identity(payload: dict) -> dict:
    case_row = _osc_get_case_identity_by_payload(payload)
    return {
        "case_row": case_row,
        "laf_case_number": str(payload.get("laf_case_no") or payload.get("laf_case_number") or case_row.get("laf_case_no") or "").strip(),
        "case_number": str(payload.get("case_number") or case_row.get("case_number") or "").strip(),
        "client_name": str(payload.get("client_name") or case_row.get("client_name") or "").strip(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Portal / archive preview
# ═══════════════════════════════════════════════════════════════════════════

def _osc_enrich_portal_preview(artifact: dict) -> dict:
    art = dict(artifact or {})
    png = str(art.get("png") or "").strip()
    html = str(art.get("html") or "").strip()
    png_export = art.get("png_export") if isinstance(art.get("png_export"), dict) else {}
    html_export = art.get("html_export") if isinstance(art.get("html_export"), dict) else {}

    if (not png_export) and png:
        u = _public_url_for_local_file(png)
        if u:
            png_export = {"url": u, "path": png}
            art["png_export"] = png_export
    if (not html_export) and html:
        u = _public_url_for_local_file(html)
        if u:
            html_export = {"url": u, "path": html}
            art["html_export"] = html_export
    return art


def _osc_get_closed_archive_base() -> str:
    env_base = (os.environ.get("MAGI_CLOSED_CASE_ARCHIVE_PATH") or "").strip()
    if env_base:
        return env_base
    try:
        ensure_path_on_sys_path(get_orch_dir())
        from osc_core.paths import get_closed_case_archive_path  # type: ignore
        p = (get_closed_case_archive_path() or "").strip()
        if p:
            return p
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "get_closed_archive_base", exc_info=True)
    roots = preferred_case_roots(include_closed=True)
    if len(roots) > 1:
        return roots[1]
    if roots:
        return roots[0]
    return str(Path.home() / "Library" / "CloudStorage" / "SynologyDrive-homes" / "99_結案案件")


def _osc_build_archive_preview(limit: int = 300) -> dict:
    rows, _ = _osc_exec(
        """
        SELECT id, case_number, client_name, status, folder_path, updated_at
        FROM cases
        WHERE (status LIKE %s OR status LIKE %s OR LOWER(status)='closed')
        ORDER BY updated_at DESC, created_date DESC
        LIMIT %s
        """,
        ("%結案%", "%Closed%", int(limit)),
        fetch="all",
    )
    archive_base = _osc_get_closed_archive_base()
    archive_local_candidates = _osc_local_path_candidates(_osc_norm_path(archive_base))
    archive_local = ""
    for c in archive_local_candidates:
        if os.path.exists(c):
            archive_local = c
            break
    if not archive_local:
        try:
            from api.nas_mount_guard import ensure_nas_mounts
            ensure_nas_mounts()
            for c in archive_local_candidates:
                if os.path.exists(c):
                    archive_local = c
                    break
        except Exception:
            logging.getLogger(__name__).debug("silent-catch archive preview mount retry", exc_info=True)
    if (not archive_local) and archive_local_candidates:
        archive_local = archive_local_candidates[0]
    items = []
    for r in (rows or []):
        source_raw = (r.get("folder_path") or "").strip() or _osc_guess_case_folder(r.get("case_number") or "")
        source_norm = _osc_norm_path(source_raw)
        local_candidates = _osc_local_path_candidates(source_norm)
        source_local = ""
        for c in local_candidates:
            if c and os.path.exists(c):
                source_local = c
                break
        if not source_local:
            try:
                from api.nas_mount_guard import ensure_nas_mounts
                ensure_nas_mounts()
                for c in local_candidates:
                    if c and os.path.exists(c):
                        source_local = c
                        break
            except Exception:
                logging.getLogger(__name__).debug("silent-catch archive source mount retry", exc_info=True)
        folder_name = os.path.basename(source_local.rstrip("/")) if source_local else os.path.basename(source_norm.rstrip("/"))
        target_local = os.path.join(archive_local, folder_name) if archive_local and folder_name else ""
        target_exists = bool(target_local and os.path.exists(target_local))
        source_exists = bool(source_local and os.path.exists(source_local))
        item = {
            "id": r.get("id"),
            "case_number": r.get("case_number") or "",
            "client_name": r.get("client_name") or "",
            "status": r.get("status") or "",
            "source_path": source_norm,
            "source_local": source_local,
            "source_exists": source_exists,
            "target_local": target_local,
            "target_exists": target_exists,
            "ready": bool(source_exists and target_local and (not target_exists)),
            "updated_at": r.get("updated_at"),
        }
        if not source_exists:
            item["reason"] = "來源資料夾不存在或未同步到本機"
        elif target_exists:
            item["reason"] = "封存目標已存在"
        else:
            item["reason"] = "可搬移"
        items.append(item)
    return {
        "ok": True,
        "archive_base": archive_base,
        "archive_local": archive_local,
        "items": items,
        "summary": {
            "total": len(items),
            "ready": len([x for x in items if x.get("ready")]),
            "missing_source": len([x for x in items if (not x.get("source_exists"))]),
            "target_exists": len([x for x in items if x.get("target_exists")]),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# JSON helpers
# ═══════════════════════════════════════════════════════════════════════════

def _osc_template_data_json_or_wrap(v: Optional[str]) -> Optional[str]:
    """
    document_templates.template_data has CHECK(json_valid(...)).
    Accept plain text from UI and wrap into JSON to avoid 4025 failures.
    """
    s = (v or "").strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
        return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        return json.dumps({"content": s}, ensure_ascii=False)


def _osc_json_or_wrap(v, fallback_key: str = "content") -> Optional[str]:
    s = ("" if v is None else str(v)).strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
        return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        return json.dumps({fallback_key: s}, ensure_ascii=False)
