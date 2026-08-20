from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager

from flask import Flask
from flask_login import LoginManager


def _app():
    from api.blueprints.osc_cases import osc_bp

    app = Flask(__name__)
    app.config.update(TESTING=True, LOGIN_DISABLED=True, SECRET_KEY="offline-route-cert")
    login = LoginManager(app)
    login.user_loader(lambda _user_id: None)
    app.register_blueprint(osc_bp)
    return app


def test_osc_crud_success_paths_use_transaction_journal(monkeypatch, tmp_path):
    from api.blueprints import osc_cases

    statements = []
    case_row = {
        "id": 7,
        "case_number": "OFFLINE-7",
        "client_name": "離線當事人",
        "status": "進行中",
        "legal_aid_status": "",
        "folder_path": str(tmp_path / "case"),
    }

    def execute(sql, params=(), fetch="none"):
        normalized = " ".join(sql.split())
        statements.append((normalized, params, fetch))
        if "SELECT id FROM case_checklists" in normalized:
            return (7,), {"fixture": True}
        if "FROM cases WHERE id=%s" in normalized:
            return dict(case_row), {"fixture": True}
        return {"lastrowid": 7, "rowcount": 1}, {"fixture": True}

    monkeypatch.setattr(osc_cases, "_osc_exec", execute)
    monkeypatch.setattr(osc_cases, "_osc_ensure_case_manual_status_columns", lambda: None)
    monkeypatch.setattr(osc_cases, "_osc_is_template_case", lambda row: False)
    monkeypatch.setattr(
        osc_cases,
        "_osc_set_case_status_manual",
        lambda *args, **kwargs: {"ok": True, "status": "已結案"},
    )
    monkeypatch.setattr(
        osc_cases,
        "_osc_start_archive_job",
        lambda *args, **kwargs: {"ok": True, "job_id": "offline"},
    )
    monkeypatch.setattr(osc_cases, "_osc_log_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        osc_cases,
        "record_draft_feedback",
        lambda payload, actor="": {"ok": True, "event": {"id": "offline"}},
    )
    monkeypatch.setattr(osc_cases, "is_non_extractable_legal_insight", lambda *args: False)
    monkeypatch.setattr(osc_cases, "_osc_backup_dir", lambda: tmp_path / "backups")
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "backup_offline.json").write_text("{}\n", encoding="utf-8")

    client = _app().test_client()
    requests = [
        ("DELETE", "/api/osc/backups/backup_offline.json", None),
        ("DELETE", "/api/osc/cases/7", None),
        ("POST", "/api/osc/cases/7/close", {"background": True}),
        (
            "POST",
            "/api/osc/checklists/case",
            {"case_number": "OFFLINE-7", "item_label": "離線項目"},
        ),
        ("PUT", "/api/osc/checklists/case/7", {"status": "完成"}),
        (
            "POST",
            "/api/osc/document-replacements",
            {"template_file": "offline.docx", "new_case_number": "OFFLINE-7"},
        ),
        ("DELETE", "/api/osc/document-replacements/7", None),
        (
            "POST",
            "/api/osc/document-templates",
            {"doc_type": "離線", "template_data": "離線範本"},
        ),
        ("PUT", "/api/osc/document-templates/7", {"doc_type": "離線更新"}),
        ("DELETE", "/api/osc/document-templates/7", None),
        (
            "POST",
            "/api/osc/drafts/feedback",
            {"case_number": "OFFLINE-7", "rating": "accepted"},
        ),
        ("DELETE", "/api/osc/pdf-generation-log/7", None),
        (
            "POST",
            "/api/osc/quotations",
            {"client_name": "離線當事人", "project_name": "離線專案"},
        ),
        ("PUT", "/api/osc/quotations/offline-q", {"status": "sent"}),
        ("DELETE", "/api/osc/quotations/offline-q", None),
        ("POST", "/api/osc/insights", {"insight_text": "離線法律見解"}),
    ]
    for method, path, body in requests:
        response = client.open(path, method=method, json=body)
        assert response.status_code == 200, (method, path, response.get_json())
        assert response.get_json()["ok"] is True

    assert any(statement.startswith("INSERT INTO document_templates") for statement, _, _ in statements)
    assert any(statement.startswith("UPDATE document_templates") for statement, _, _ in statements)
    assert any(statement.startswith("DELETE FROM document_templates") for statement, _, _ in statements)
    assert any(statement.startswith("INSERT INTO legal_insights") for statement, _, _ in statements)


def test_remaining_osc_projection_and_sandbox_text_success(monkeypatch, tmp_path):
    from api.blueprints import osc_cases

    calls = []
    text_file = tmp_path / "case-note.txt"
    text_file.write_text("before", encoding="utf-8")
    case = {
        "id": 7,
        "case_number": "OFFLINE-7",
        "client_name": "離線當事人",
        "case_category": "法律扶助案件",
        "case_type": "民事",
        "case_stage": "一審",
        "case_reason": "離線測試",
        "status": "進行中",
        "legal_aid_status": "進行中",
        "folder_path": str(tmp_path),
        "manual_status_lock": 0,
        "manual_laf_status_lock": 0,
    }

    def execute(sql, params=(), fetch="none"):
        normalized = " ".join(sql.split())
        calls.append((normalized, params, fetch))
        if "FROM cases" in normalized and fetch == "one":
            return dict(case), {"fixture": True}
        if "SELECT id FROM clients" in normalized:
            return None, {"fixture": True}
        return {"rowcount": 1, "lastrowid": 9}, {"fixture": True}

    monkeypatch.setattr(osc_cases, "_osc_exec", execute)
    monkeypatch.setattr(
        osc_cases,
        "_osc_get_setting_value",
        lambda _key, default="": default,
    )
    monkeypatch.setattr(osc_cases, "generate_next_client_id", lambda: "offline-client")
    monkeypatch.setattr(osc_cases, "_osc_log_activity", lambda *args, **kwargs: calls.append(("activity", args, kwargs)))
    monkeypatch.setattr(osc_cases, "_osc_effective_case_folder_for_row", lambda *args, **kwargs: {"folder_path": str(tmp_path), "local_folder": str(tmp_path), "source": "sandbox"})
    monkeypatch.setattr(osc_cases, "_osc_folder_entries", lambda *args, **kwargs: {"ok": True, "entries": [{"name": text_file.name}], "current_relative_path": "", "parent_relative_path": ""})
    monkeypatch.setattr(osc_cases, "_osc_local_path_candidates", lambda *_args: [str(tmp_path)])
    monkeypatch.setattr(osc_cases, "_osc_smb_candidates", lambda *_args: [])
    monkeypatch.setattr(osc_cases, "_osc_resolve_existing_local_path", lambda raw, **_kwargs: str(text_file) if str(raw).endswith(".txt") else str(tmp_path))
    monkeypatch.setattr(osc_cases, "_osc_is_safe_local_path", lambda path: str(path).startswith(str(tmp_path)))
    monkeypatch.setattr(osc_cases, "conflict_check", lambda _exec, payload: {"ok": True, "risk": "none", "payload": payload})
    monkeypatch.setattr(osc_cases, "record_intake", lambda _exec, payload, actor="": {"ok": True, "event": {"id": "offline-intake"}, "payload": payload, "actor": actor})
    monkeypatch.setattr(osc_cases, "quality_check", lambda payload: {"ok": True, "pass": True, "payload": payload})
    monkeypatch.setattr(osc_cases, "build_client_packet", lambda _exec, payload: {"ok": True, "copy_text": "offline packet", "payload": payload})
    monkeypatch.setattr(osc_cases, "update_onboarding_status", lambda payload, actor="": {"ok": True, "saved": payload, "actor": actor})
    monkeypatch.setattr(osc_cases, "save_notification_preferences", lambda payload: {"ok": True, "saved": payload})
    monkeypatch.setattr(osc_cases, "_osc_build_draft_context", lambda payload: {"doc_type": "聲請狀", "case_facts": "離線事實", "prompt": "offline prompt", "case": case, "case_number": "OFFLINE-7"})
    monkeypatch.setattr(osc_cases, "_export_osc_form_files", lambda *args, **kwargs: {"success": True, "export": {"filename": "offline.docx"}, "errors": []})
    monkeypatch.setattr(osc_cases, "_osc_get_case_identity_by_payload", lambda payload: dict(case))
    monkeypatch.setattr(osc_cases, "_record_last_public_base_url", lambda: None)
    monkeypatch.setattr(osc_cases, "_osc_build_form_preview", lambda *args, **kwargs: {"form_type": "offline", "title": "離線表單", "preview_text": "offline"})
    monkeypatch.setattr(osc_cases, "_osc_fetch_url_text", lambda *args, **kwargs: {"ok": True, "text": "法院判決具體法律見解全文"})
    monkeypatch.setattr(osc_cases, "_osc_summarize_legal_insight", lambda text: "可引用法律見解")
    monkeypatch.setattr(osc_cases, "is_non_extractable_legal_insight", lambda *args: False)

    client = _app().test_client()
    requests = [
        ("GET", "/api/osc/cases/7/folder-browser", None, None),
        ("POST", "/api/osc/drafts/generate", {"doc_type": "聲請狀", "case_facts": "離線", "dry_run": True}, None),
        ("POST", "/api/osc/drafts/export", {"draft_text": "離線草稿", "case_number": "OFFLINE-7"}, None),
        ("GET", f"/api/osc/files/text?path={text_file}", None, None),
        ("PUT", "/api/osc/files/text", {"path": str(text_file), "content": "after"}, None),
        ("POST", "/api/osc/forms/preview", {"form_type": "offline", "case_id": 7}, None),
        ("POST", "/api/osc/insights/fetch-full", {"url": "https://offline.invalid/judgment", "title": "離線判決"}, None),
        ("POST", "/api/osc/saas/conflict-check", {"opponent_name": "離線相對人"}, None),
        ("POST", "/api/osc/saas/intake", {"client_name": "離線當事人"}, None),
        ("POST", "/api/osc/saas/quality-check", {"text": "離線品質"}, None),
        ("POST", "/api/osc/saas/client-packet", {"case_number": "OFFLINE-7"}, None),
        ("POST", "/api/osc/saas/onboarding", {"key": "offline", "done": True}, None),
        ("POST", "/api/osc/saas/notification-prefs", {"system_health": "system_only"}, None),
    ]
    for method, path, body, data in requests:
        response = client.open(path, method=method, json=body, data=data)
        assert response.status_code == 200, (method, path, response.get_json())
        assert response.get_json()["ok"] is True

    response = client.post(
        "/api/osc/clients/import-csv",
        data={"file": (io.BytesIO(("姓名,電話\n離線當事人," + "09" + "00000000\n").encode()), "clients.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["imported"] == 1
    assert text_file.read_text(encoding="utf-8") == "after"
    assert any(statement.startswith("INSERT INTO legal_insights") for statement, _, _ in calls if isinstance(statement, str))


def test_remaining_osc_files_documents_and_laf_success(monkeypatch, tmp_path):
    from api.blueprints import osc_cases
    from api.runtime_paths import get_exports_dir
    from casper_ecosystem.law_firm_orchestrators.osc import folder_utils

    calls = []
    case = {
        "id": 7,
        "case_number": "OFFLINE-7",
        "client_name": "離線當事人",
        "case_category": "法律扶助案件",
        "case_type": "民事",
        "case_stage": "一審",
        "case_reason": "離線測試",
        "status": "進行中",
        "legal_aid_status": "未開辦",
        "folder_path": str(tmp_path / "case"),
        "manual_status_lock": 0,
        "manual_laf_status_lock": 0,
    }
    (tmp_path / "case").mkdir()
    source_doc = tmp_path / "source.docx"
    source_doc.write_bytes(b"offline-docx")
    source_pdf = tmp_path / "source.pdf"
    source_pdf.write_bytes(b"%PDF-offline")

    def execute(sql, params=(), fetch="none"):
        normalized = " ".join(sql.split())
        calls.append((normalized, params, fetch))
        if "FROM cases" in normalized and fetch == "one":
            return dict(case), {"fixture": True}
        return {"rowcount": 1, "lastrowid": 9}, {"fixture": True}

    monkeypatch.setattr(osc_cases, "_osc_exec", execute)
    monkeypatch.setattr(osc_cases, "_osc_ensure_case_manual_status_columns", lambda: None)
    monkeypatch.setattr(osc_cases, "_osc_log_activity", lambda *args, **kwargs: calls.append(("activity", args, kwargs)))
    monkeypatch.setattr(osc_cases, "_osc_effective_case_folder_for_row", lambda *args, **kwargs: {"folder_path": str(tmp_path / "case"), "local_folder": str(tmp_path / "case")})
    monkeypatch.setattr(osc_cases, "_osc_select_case_creation_root", lambda: {"ok": True, "root": str(tmp_path), "temporary_synology_drive": False})
    monkeypatch.setattr(osc_cases, "_osc_case_folder_creation_guard", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(osc_cases, "_get_translate_local_path_to_canonical", lambda: (lambda path: path))
    monkeypatch.setattr(osc_cases, "_osc_try_create_drive_case_folder", lambda **kwargs: {"ok": True, "provider": "offline"})
    monkeypatch.setattr(folder_utils, "build_full_case_path", lambda *args, **kwargs: str(tmp_path / "created-case"))

    def create_structure(path, category):
        calls.append(("create_structure", path, category))
        return {"ok": True, "subfolders": ["01_離線"]}

    monkeypatch.setattr(folder_utils, "create_folder_structure", create_structure)
    monkeypatch.setattr(osc_cases, "_osc_local_path_candidates", lambda raw: [str(source_pdf)])
    monkeypatch.setattr(osc_cases, "_osc_smb_candidates", lambda raw: [])
    monkeypatch.setattr(osc_cases, "_osc_try_open_path", lambda path: calls.append(("open", path, None)) or {"ok": True})
    monkeypatch.setattr(osc_cases, "_osc_resolve_existing_local_path", lambda raw, **kwargs: str(source_doc) if str(raw).endswith(".docx") else str(source_pdf))
    monkeypatch.setattr(osc_cases, "_osc_is_safe_local_path", lambda path: str(path).startswith(str(tmp_path)))
    monkeypatch.setattr(osc_cases, "_osc_photo_path", lambda name: str(tmp_path / name))
    monkeypatch.setattr(osc_cases, "_MAGI_ROOT", str(tmp_path))
    skill = tmp_path / "skills" / "doc-producer" / "action.py"
    skill.parent.mkdir(parents=True)
    skill.write_text("# offline\n", encoding="utf-8")
    monkeypatch.setattr("api.runtime_paths.get_skill_python", lambda: Path(sys.executable))
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps({"success": True, "output": str(tmp_path / "stamped.pdf")}), stderr="", returncode=0))
    monkeypatch.setattr(osc_cases, "_osc_is_own_pleading_word", lambda *args: True)
    monkeypatch.setattr(osc_cases, "_osc_lookup_case_for_reuse", lambda **kwargs: dict(case))
    monkeypatch.setattr(osc_cases, "_osc_enrich_case_for_document_reuse", lambda row, payload: dict(row))
    monkeypatch.setattr(osc_cases, "_osc_reuse_output_dir", lambda *args: (str(tmp_path), []))
    monkeypatch.setattr(osc_cases, "_osc_reuse_document", lambda *args, **kwargs: {"output_path": str(tmp_path / "reused.docx"), "replacement_count": 2, "replacements": ["name"]})
    monkeypatch.setattr(osc_cases, "_osc_register_reused_document", lambda *args, **kwargs: [])
    monkeypatch.setattr(osc_cases, "_osc_get_case_identity_by_payload", lambda payload: dict(case))
    monkeypatch.setattr(osc_cases, "_record_last_public_base_url", lambda: None)
    monkeypatch.setattr(osc_cases, "_osc_build_form_preview", lambda *args, **kwargs: {"form_type": "offline", "title": "離線表單", "preview_text": "offline", "suggested_filename": "offline"})
    monkeypatch.setattr(osc_cases, "_export_osc_form_files", lambda *args, **kwargs: {"success": True, "export": {"success": True, "path": str(get_exports_dir() / "offline.docx")}, "errors": []})

    class Orchestrator:
        _last_portal_artifact = {"offline": True}

        def __init__(self, dry_run=False):
            calls.append(("laf_init", dry_run, None))

        def execute_portal_action_draft(self, **kwargs):
            calls.append(("laf_draft", kwargs, None))
            return {"ok": True, "mode": "offline"}

    monkeypatch.setattr(osc_cases, "_osc_prepare_laf_identity", lambda payload: {"laf_case_number": "LAF-7", "case_number": "OFFLINE-7", "client_name": "離線當事人"})
    monkeypatch.setattr(osc_cases, "_osc_import_laf_orchestrator", lambda: Orchestrator)
    monkeypatch.setattr(osc_cases, "_osc_enrich_portal_preview", lambda artifact: artifact)
    monkeypatch.setitem(sys.modules, "laf_nightly_audit", SimpleNamespace(run_backfill_only=lambda notify=False: {"ok": True, "notify": notify}))
    monkeypatch.setattr(osc_cases, "_load_labor_law_action_module", lambda: SimpleNamespace(run=lambda task, **kwargs: {"task": task, "kwargs": kwargs}))
    monkeypatch.setattr("api.thread_pools.io_pool.submit", lambda function, *args, **kwargs: SimpleNamespace(result=lambda timeout: function(*args, **kwargs)))

    client = _app().test_client()
    requests = [
        ("POST", "/api/osc/cases/7/create-folder", {}),
        ("POST", "/api/osc/documents/open", {"path": str(source_pdf)}),
        ("POST", "/api/osc/documents/stamp", {"file_path": str(source_pdf), "copy_type": "正本"}),
        ("POST", "/api/osc/drafts/reuse-document", {"source_path": str(source_doc), "case_id": "7", "case_number": "OFFLINE-7"}),
        ("POST", "/api/osc/forms/export", {"form_type": "offline", "case_id": 7}),
        ("POST", "/api/osc/laf/batch-status", {"legal_aid_status": "進行中"}),
        ("POST", "/api/osc/cases/7/laf-status", {"legal_aid_status": "進行中"}),
        ("POST", "/api/osc/laf-wizard/run", {"mode": "preview", "action": "inquiry"}),
        ("POST", "/api/osc/laf-backfill", {}),
        ("POST", "/api/osc/labor-law/calc", {"task": "overtime", "monthly_wage": 50000}),
    ]
    for method, path, body in requests:
        response = client.open(path, method=method, json=body)
        assert response.status_code == 200, (method, path, response.get_json())
        assert response.get_json()["ok"] is True

    assert any(row[0] == "create_structure" for row in calls)
    assert any(isinstance(row[0], str) and row[0].startswith("UPDATE cases") for row in calls)
    assert any(row[0] == "laf_draft" for row in calls)


def test_debt_folder_gcal_and_raziel_success_use_sandbox_providers(monkeypatch, tmp_path):
    from api.blueprints import osc_debt, osc_files, osc_gcal, raziel
    from api.osc import utils as osc_utils
    from api import case_path_mapper

    calls = []
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    monkeypatch.setattr(osc_utils, "_osc_exec", lambda *args, **kwargs: ({"id": "7", "case_number": "OFFLINE-7", "client_name": "離線當事人", "case_category": "消費者債務清理", "case_type": "消費者債務清理", "folder_path": str(case_dir)}, None))
    monkeypatch.setattr(case_path_mapper, "translate_case_path_to_local", lambda *args, **kwargs: str(case_dir))
    monkeypatch.setattr("api.debt_document_generator.scan_evidence_folder", lambda path: calls.append(("scan", path)) or {"ok": True, "matches": {}})
    monkeypatch.setattr(osc_files, "_require_file_operator", lambda: None)
    monkeypatch.setattr(osc_files, "_resolve_target_dir", lambda base: str(case_dir))
    monkeypatch.setattr(osc_files, "_osc_is_safe_local_path", lambda path: str(path).startswith(str(case_dir)))

    monkeypatch.setattr(raziel, "_apply_payload_to_config", lambda payload: {"max_api": 0, **payload})
    monkeypatch.setattr(raziel, "_run_raziel", lambda mode, config, max_api=None: calls.append(("raziel_run", mode)) or {"ok": True, "mode": mode})
    monkeypatch.setattr(raziel, "_tlr_preview_for_config", lambda config, limit=3: {"ok": True, "items": [], "limit": limit})
    monkeypatch.setattr(raziel, "_write_tlr_preview_file", lambda preview: str(tmp_path / "tlr.json"))
    monkeypatch.setattr(raziel, "_write_delivery_zip", lambda config, split: calls.append(("delivery", split)) or {"ok": True, "files": ["offline.zip"]})
    monkeypatch.setattr(raziel, "_delivery_split_bytes", lambda value: 1024)

    monkeypatch.setattr(osc_gcal, "_require_gcal_operator", lambda: None)
    monkeypatch.setattr(osc_gcal, "_get_setting", lambda key: f"offline-{key}")
    monkeypatch.setattr(osc_gcal, "_build_redirect_uri", lambda: "https://offline.invalid/callback")
    monkeypatch.setattr(osc_gcal, "TOKEN_PATH", tmp_path / "gcal-token.json")

    class Flow:
        credentials = SimpleNamespace(to_json=lambda: '{"offline":true}')

        @classmethod
        def from_client_config(cls, *args, **kwargs):
            return cls()

        def fetch_token(self, code):
            calls.append(("gcal_fetch", code))

    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", Flow)

    @contextmanager
    def token_lock(path):
        yield

    monkeypatch.setattr(osc_gcal, "google_token_file_lock", token_lock)
    monkeypatch.setattr(osc_gcal, "_write_token_atomic", lambda path, content: calls.append(("token_write", str(path))))

    app = Flask(__name__)
    app.config.update(TESTING=True, LOGIN_DISABLED=True, SECRET_KEY="offline-route-cert")
    login = LoginManager(app)
    login.user_loader(lambda _user_id: None)
    app.register_blueprint(osc_debt.osc_debt_bp)
    app.register_blueprint(osc_files.osc_files_bp)
    app.register_blueprint(osc_gcal.osc_gcal_bp)
    app.register_blueprint(raziel.raziel_bp)
    client = app.test_client()

    response = client.get("/api/osc/debt/scan-evidence/7")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    response = client.post("/api/osc/folders/mkdir", json={"base_path": str(case_dir), "name": "離線子目錄"})
    assert response.status_code == 200
    assert (case_dir / "離線子目錄").is_dir()
    for path, body in (
        ("/api/osc/raziel/run", {"mode": "preview"}),
        ("/api/osc/raziel/tlr-preview", {"limit": 1}),
        ("/api/osc/raziel/delivery", {"split_mb": 1}),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
    with client.session_transaction() as session:
        session["gcal_oauth_state"] = "offline-state"
    response = client.get("/api/osc/gcal/auth/callback?code=offline-code&state=offline-state")
    assert response.status_code == 200
    assert "授權成功" in response.get_data(as_text=True)
    assert ("gcal_fetch", "offline-code") in calls
    assert any(row[0] == "delivery" for row in calls)


def test_web_module_and_upload_success_use_sandbox_orchestrator(monkeypatch, tmp_path):
    from api.blueprints import web_runtime
    from flask_login import AnonymousUserMixin

    calls = []

    class Orchestrator:
        def process_message(self, **kwargs):
            calls.append(kwargs)
            return "offline reply"

    monkeypatch.setattr(web_runtime, "_chat_upload_dir", lambda root: tmp_path)
    monkeypatch.setattr(web_runtime, "_extract_chat_upload_text_for_task", lambda *args, **kwargs: {"success": True, "text": "offline file text", "kind": "text"})
    monkeypatch.setattr(web_runtime, "_run_direct_web_upload_text_task", lambda *args, **kwargs: {"reply": "offline upload reply", "artifacts": [], "task": "summary"})
    monkeypatch.setattr(web_runtime, "_create_web_delivery_artifacts", lambda *args, **kwargs: [])

    app = Flask(__name__)
    app.config.update(TESTING=True, LOGIN_DISABLED=True, SECRET_KEY="offline-route-cert")
    login = LoginManager(app)
    login.user_loader(lambda _user_id: None)

    class OfflineUser(AnonymousUserMixin):
        id = "offline-user"
        role = "admin"

    login.anonymous_user = OfflineUser
    app.register_blueprint(
        web_runtime.create_web_runtime_blueprint(
            orchestrator=Orchestrator(),
            logger=SimpleNamespace(
                debug=lambda *args, **kwargs: None,
                error=lambda *args, **kwargs: None,
                exception=lambda *args, **kwargs: None,
            ),
            web_notifications={},
            normalize_output_text=lambda text, **kwargs: text,
            magi_root=tmp_path,
        )
    )
    client = app.test_client()
    response = client.post(
        "/api/osc/magi-modules/run",
        json={"module": "laf", "command": "法扶指令"},
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    response = client.post(
        "/api/osc/chat/upload",
        data={"message": "請摘要", "file": (io.BytesIO(b"offline"), "offline.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["reply"] == "offline upload reply"
    assert calls and calls[0]["message"] == "法扶指令"
    assert any(path.name.endswith("offline.txt") for path in tmp_path.iterdir())
