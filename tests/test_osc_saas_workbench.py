from pathlib import Path

import pytest


def test_quality_check_blocks_prompt_leak_and_internal_case_number():
    from api.osc.saas_workbench import quality_check

    result = quality_check(
        {
            "case_number": "114年度訴字第123號",
            "reason": "損害賠償",
            "text": "<|channel>thought\nOSC-2026-001 應准許。",
        }
    )

    assert result["pass"] is False
    codes = {x["code"] for x in result["issues"]}
    assert "prompt_or_reasoning_leak" in codes
    assert "internal_case_number" in codes


def test_client_packet_uses_debt_checklist():
    from api.osc.saas_workbench import build_client_packet

    def fake_exec(*args, **kwargs):
        return ({}, None)

    result = build_client_packet(
        fake_exec,
        {"client_name": "王小明", "reason": "消債更生", "case_number": "2026-0001"},
    )

    assert result["ok"] is True
    assert "債權人清冊" in result["copy_text"]
    assert result["portal_mode"] == "packet_only"


def test_conflict_check_flags_opponent_records():
    from api.osc.saas_workbench import conflict_check

    def fake_exec(sql, params=(), fetch="all"):
        if "FROM opponents" in sql:
            return ([{"id": 1, "case_number": "2025-0001", "opponent_name": "張三", "notes": ""}], None)
        return ([], None)

    result = conflict_check(fake_exec, {"opponent_name": "張三"})

    assert result["risk"] == "high"
    assert result["matches"][0]["side"] == "opponent"
    assert result["matches"][0]["actions"][0]["act"] == "saas-opponent-edit"


def test_intake_runtime_record_is_local_jsonl(tmp_path, monkeypatch):
    from api.osc import saas_workbench

    monkeypatch.setattr(saas_workbench, "INTAKE_PATH", tmp_path / "intake.jsonl")

    def fake_exec(*args, **kwargs):
        return ([], None)

    result = saas_workbench.record_intake(
        fake_exec,
        {"client_name": "李四", "case_reason": "損害賠償", "summary": "電話諮詢"},
        actor="tester",
    )

    assert result["ok"] is True
    assert Path(saas_workbench.INTAKE_PATH).exists()
    assert saas_workbench.recent_intakes(1)[0]["client_name"] == "李四"


def test_saas_overview_exposes_ten_capabilities(monkeypatch, tmp_path):
    from api.osc import draft_learning, saas_workbench

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "learning.jsonl")
    monkeypatch.setattr(saas_workbench, "INTAKE_PATH", tmp_path / "intake.jsonl")
    monkeypatch.setattr(saas_workbench, "ONBOARDING_PATH", tmp_path / "onboarding.json")
    monkeypatch.setattr(saas_workbench, "NOTIFICATION_PREFS_PATH", tmp_path / "notify.json")
    monkeypatch.setattr(saas_workbench, "WORKFLOW_TEMPLATES_PATH", tmp_path / "workflow.json")

    def fake_exec(*args, **kwargs):
        sql = args[0]
        if "COUNT(*) AS c" in sql:
            return ({"c": 0}, None)
        return ([], None)

    result = saas_workbench.build_saas_overview(fake_exec)

    assert result["ok"] is True
    assert len(result["capabilities"]) == 14
    assert {x["key"] for x in result["capabilities"]} >= {
        "learning_center",
        "quality_gate",
        "risk_dashboard",
        "conflict_check",
        "nerv_status_page",
        "operations_report",
        "onboarding_checklist",
        "notification_preferences",
        "workflow_templates",
        "diagnostics_export",
    }
    assert result["readiness"]["mode"] == "single_host"
    assert result["readiness"]["status_page"]["url"] == "/dashboard/nerv"
    assert "多租戶" in result["readiness"]["not_needed"]
    assert "公開上傳入口" in result["readiness"]["not_needed"]
    assert {x["key"] for x in result["readiness"]["checks"]} >= {"nerv_status", "not_needed_scope"}
    assert result["integration"]["principle"].startswith("這裡集中顯示常用資訊")
    assert all(x.get("owner") and x.get("source") and x.get("role") for x in result["capabilities"])
    target_tabs = {
        x["target_tab"]
        for x in result["integration"]["items"]
        if x.get("target_tab")
    }
    for item in result["integration"]["items"]:
        for target in item.get("target_tabs") or []:
            target_tabs.add(target["tab"])
    assert target_tabs >= {"todos", "clients", "documents", "drafts", "saasTimelineSection"}
    timeline = next(x for x in result["capabilities"] if x["key"] == "document_timeline")
    assert timeline["primary_action"]["act"] == "saas-section-jump"
    assert timeline["primary_action"]["section"] == "saasTimelineSection"
    assert timeline["secondary_actions"][0]["tab"] == "documents"
    assert timeline["title"] == "文件證據時間線"
    nerv = next(x for x in result["capabilities"] if x["key"] == "nerv_status_page")
    assert nerv["primary_action"]["act"] == "open-url"
    assert "對外資料" in {x["title"] for x in result["capabilities"]}
    assert result["onboarding"]["summary"]["required"] >= 1
    assert result["notification_preferences"]["prefs"]["system_health"] == "system_only"
    assert result["workflow_templates"]["count"] >= 4
    assert result["workflow_templates"]["legal_workflow_agents"]
    assert result["workflow_templates"]["practice_profiles"]
    assert result["workflow_templates"]["reference"]["import_mode"] == "conceptual_patterns_only"
    assert result["ai_governance"]["policies"]
    assert result["task_boards"]["refresh"]["interval_hours"] == 6
    assert "MAGI 事務統計" in result["operations_text"]


def test_saas_workbench_template_has_actionable_entry_links():
    html = Path("templates/partials/osc/saasWorkbench.html").read_text(encoding="utf-8")

    assert "資料來源與處理入口" in html
    assert 'id="saasReadinessGrid"' in html
    assert 'id="saasOnboardingSection"' in html
    assert 'id="saasNotificationSection"' in html
    assert 'id="saasWorkflowSection"' in html
    assert 'id="saasGovernanceSection"' in html
    assert 'id="saasTaskBoardSection"' in html
    assert 'id="saasOscTodoBody"' in html
    assert 'id="saasCalendarEventBody"' in html
    assert "事件待辦" in html
    assert "OSC 建立待辦" in html
    assert "行事曆事件" in html
    assert "功能整合關係" not in html
    assert "管理工具" in html
    assert "事務總覽" not in html
    assert "諮詢／接案追蹤" in html
    assert "所務" not in html
    assert "事務所營運" not in html
    assert "工作台" not in html
    assert "面板" not in html
    assert "漏斗" not in html
    assert "重命名" not in html
    assert "對外文件產生包" not in html
    assert "當事人入口" not in html
    assert "客戶入口" not in html
    assert "資料包" not in html
    assert 'id="saasTimelineSection"' in html
    for tab in ["cases", "clients", "todos", "calendar", "laf", "documents", "drafts"]:
        assert f'data-tab="{tab}"' in html


def test_saas_tools_are_embedded_in_dashboard_not_separate_nav():
    osc = Path("templates/osc.html").read_text(encoding="utf-8")
    dashboard = Path("templates/partials/osc/dashboard.html").read_text(encoding="utf-8")

    assert 'data-tab="saasWorkbench"' not in osc
    assert 'include "partials/osc/saasWorkbench.html"' in dashboard


def test_dashboard_laf_case_labels_are_consistent():
    html = Path("templates/partials/osc/dashboard.html").read_text(encoding="utf-8")

    assert "未結法扶案件" in html
    assert "已結法扶案件" in html
    assert "未結法扶</div>" not in html


def test_operations_report_separates_total_active_and_closing_pending(monkeypatch, tmp_path):
    from api.osc import draft_learning, saas_workbench

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "learning.jsonl")
    monkeypatch.setattr(saas_workbench, "INTAKE_PATH", tmp_path / "intake.jsonl")

    def fake_exec(sql, params=(), fetch="one"):
        if "COUNT(*) AS c FROM cases" in sql and "WHERE" not in sql:
            return ({"c": 182}, None)
        if "COUNT(*) AS c FROM cases" in sql and "NOT IN" in sql:
            return ({"c": 143}, None)
        if "COUNT(*) AS c FROM cases" in sql and "已結案，待送出" in sql:
            return ({"c": 1}, None)
        if "COUNT(*) AS c FROM cases" in sql and "status='已結案'" in sql:
            return ({"c": 38}, None)
        if "COUNT(*) AS c" in sql:
            return ({"c": 0}, None)
        return ([], None)

    result = saas_workbench.build_operations_report(fake_exec)

    assert result["total_cases"] == 182
    assert result["active_cases"] == 143
    assert result["closed_cases"] == 38
    assert result["closing_pending_cases"] == 1


def test_risk_dashboard_marks_source_module():
    from api.osc import saas_workbench

    def fake_exec(sql, params=(), fetch="all"):
        if "FROM case_todos" in sql:
            return (
                [
                    {
                        "id": 1,
                        "case_number": "2026-0001",
                        "client_name": "王小明",
                        "todo_type": "開庭",
                        "todo_date": "2026-05-01",
                        "description": "準備資料",
                        "status": "",
                    }
                ],
                None,
            )
        return ([], None)

    result = saas_workbench.build_risk_dashboard(fake_exec, limit=5)

    assert result["items"][0]["owner"] == "OSC 建立待辦"
    assert result["items"][0]["target_tab"] == "todos"
    assert {x["act"] for x in result["items"][0]["actions"]} == {"saas-todo-edit", "saas-todo-complete"}


def test_task_boards_split_calendar_imports_from_osc_todos():
    from api.osc import saas_workbench

    def fake_exec(sql, params=(), fetch="all"):
        if "FROM case_todos" in sql and "NOT LIKE 'gcal_import" in sql:
            return (
                [
                    {
                        "id": 1,
                        "case_number": "2026-0001",
                        "client_name": "王小明",
                        "todo_type": "補正",
                        "todo_date": "2026-05-20",
                        "todo_time": "",
                        "description": "OSC 從法院通知建立",
                        "status": "pending",
                        "source_file": "法院通知.pdf",
                    }
                ],
                None,
            )
        if "FROM case_todos" in sql and "source_file LIKE 'gcal_import" in sql:
            return (
                [
                    {
                        "id": 2,
                        "case_number": "2026-0002",
                        "client_name": "",
                        "todo_type": "開會",
                        "todo_date": "2026-05-21",
                        "todo_time": "10:00",
                        "description": "同事手動日曆事件",
                        "status": "pending",
                        "source_file": "gcal_import",
                    }
                ],
                None,
            )
        if "FROM calendar_events" in sql:
            return (
                [
                    {
                        "id": 3,
                        "case_number": "2026-0003",
                        "title": "開庭",
                        "start_date": "2026-05-22 09:30:00",
                        "description": "第一法庭",
                        "location": "花蓮地院",
                    }
                ],
                None,
            )
        return ([], None)

    result = saas_workbench.build_task_boards(fake_exec)

    assert result["refresh"]["interval_hours"] == 6
    assert result["osc_todos"]["count"] == 1
    assert result["osc_todos"]["items"][0]["source"] == "case_todos"
    assert result["calendar_events"]["source_counts"] == {"calendar_events": 1, "gcal_import": 1}
    assert {x["source"] for x in result["calendar_events"]["items"]} == {"calendar_events", "gcal_import"}


def test_document_timeline_reuses_document_actions():
    from api.osc import saas_workbench

    def fake_exec(sql, params=(), fetch="all"):
        if "FROM document_index" in sql:
            return (
                [
                    {
                        "id": 3,
                        "case_number": "2026-0001",
                        "file_name": "準備書狀.pdf",
                        "file_path": "/tmp/準備書狀.pdf",
                        "subfolder_name": "我方歷次書狀",
                        "reason": "",
                        "party": "",
                        "modified_date": "2026-05-11 10:00:00",
                    }
                ],
                None,
            )
        return ([], None)

    result = saas_workbench.build_document_timeline(fake_exec)

    assert result["items"][0]["actions"][0]["act"] == "doc-open"
    assert result["items"][0]["actions"][1]["act"] == "doc-copy"


def test_saas_generated_edit_actions_have_dispatch_handlers():
    root = Path(__file__).resolve().parents[1]
    events_js = (root / "static/osc/osc-events.js").read_text(encoding="utf-8")
    for act in [
        "saas-todo-edit",
        "saas-todo-complete",
        "saas-cal-edit",
        "saas-laf-detail",
        "saas-laf-status",
        "saas-case-edit",
        "saas-client-edit",
        "saas-opponent-edit",
    ]:
        assert f'if (act === "{act}")' in events_js
    for fn in [
        "reloadSaasOnboarding",
        "saveSaasNotificationPrefs",
        "downloadSaasDiagnosticPack",
        "copySaasOpsReport",
    ]:
        assert fn in events_js or fn in (root / "static/osc/tabs/saas.js").read_text(encoding="utf-8")


def test_six_hour_event_refresh_is_seeded():
    import json

    jobs = json.loads(Path("cron_jobs.json").read_text(encoding="utf-8"))
    job = next(x for x in jobs if x.get("id") == "job_osc_events_refresh")

    assert job["cron"] == "5 */6 * * *"
    assert job["enabled"] is True
    assert "osc_events_refresh.py" in job["command"]
    assert job["no_catchup"] is True


def test_onboarding_and_notification_preferences_persist(tmp_path, monkeypatch):
    from api.osc import saas_workbench

    monkeypatch.setattr(saas_workbench, "ONBOARDING_PATH", tmp_path / "onboarding.json")
    monkeypatch.setattr(saas_workbench, "NOTIFICATION_PREFS_PATH", tmp_path / "notify.json")

    result = saas_workbench.update_onboarding_status({"key": "public_audit", "done": True}, actor="tester")
    assert result["ok"] is True
    assert any(x["key"] == "public_audit" and x["done"] for x in result["items"])

    prefs = saas_workbench.save_notification_preferences({"system_health": "silent", "laf_general": "system_only"})
    assert prefs["prefs"]["system_health"] == "silent"
    assert prefs["prefs"]["laf_general"] == "system_only"


def test_diagnostic_pack_is_redacted_and_complete(tmp_path, monkeypatch):
    from api.osc import draft_learning, saas_workbench

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "learning.jsonl")
    monkeypatch.setattr(saas_workbench, "INTAKE_PATH", tmp_path / "intake.jsonl")
    monkeypatch.setattr(saas_workbench, "ONBOARDING_PATH", tmp_path / "onboarding.json")
    monkeypatch.setattr(saas_workbench, "NOTIFICATION_PREFS_PATH", tmp_path / "notify.json")

    def fake_exec(sql, params=(), fetch="one"):
        if "COUNT(*) AS c" in sql:
            return ({"c": 0}, None)
        return ([], None)

    pack = saas_workbench.build_diagnostic_pack(fake_exec)
    assert pack["ok"] is True
    assert pack["scope"] == "single_host_magi"
    assert pack["redaction"].startswith("No secrets")
    assert "readiness" in pack and "notification_preferences" in pack and "ai_governance" in pack


@pytest.fixture
def saas_client(monkeypatch, tmp_path):
    from flask import Flask
    from flask_login import LoginManager, UserMixin
    from api.blueprints import osc_cases
    from api.osc import draft_learning, saas_workbench

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "learning.jsonl")
    monkeypatch.setattr(saas_workbench, "INTAKE_PATH", tmp_path / "intake.jsonl")
    monkeypatch.setattr(saas_workbench, "ONBOARDING_PATH", tmp_path / "onboarding.json")
    monkeypatch.setattr(saas_workbench, "NOTIFICATION_PREFS_PATH", tmp_path / "notify.json")
    monkeypatch.setattr(saas_workbench, "WORKFLOW_TEMPLATES_PATH", tmp_path / "workflow.json")

    def fake_exec(sql, params=(), fetch="all"):
        if "COUNT(*) AS c" in sql:
            return ({"c": 0}, None)
        return ([], None)

    monkeypatch.setattr(osc_cases, "_osc_exec", fake_exec)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.secret_key = "test-saas"
    lm = LoginManager()
    lm.init_app(app)

    class _User(UserMixin):
        id = "tester"

    @lm.user_loader
    def _load_user(_user_id):
        return _User()

    app.register_blueprint(osc_cases.osc_bp)
    return app.test_client()


def test_saas_routes_smoke(saas_client):
    resp = saas_client.get("/api/osc/saas/overview")
    assert resp.status_code == 200
    assert len(resp.get_json()["capabilities"]) == 14
    assert resp.get_json()["readiness"]["mode"] == "single_host"

    resp = saas_client.post(
        "/api/osc/saas/quality-check",
        json={"text": "<|channel>thought\nOSC-2026-001", "case_number": "114年度訴字第1號"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["pass"] is False

    resp = saas_client.post("/api/osc/saas/client-packet", json={"client_name": "王小明", "reason": "消債更生"})
    assert resp.status_code == 200
    assert "債權人清冊" in resp.get_json()["copy_text"]

    resp = saas_client.get("/api/osc/saas/task-boards")
    assert resp.status_code == 200
    assert resp.get_json()["refresh"]["interval_hours"] == 6

    resp = saas_client.get("/api/osc/saas/onboarding")
    assert resp.status_code == 200
    assert resp.get_json()["summary"]["required"] >= 1

    resp = saas_client.post("/api/osc/saas/notification-prefs", json={"system_health": "silent"})
    assert resp.status_code == 200
    assert resp.get_json()["prefs"]["system_health"] == "silent"

    resp = saas_client.get("/api/osc/saas/workflow-templates")
    assert resp.status_code == 200
    assert resp.get_json()["count"] >= 4

    resp = saas_client.get("/api/osc/saas/ai-governance")
    assert resp.status_code == 200
    assert resp.get_json()["policies"]

    resp = saas_client.get("/api/osc/saas/operations-report")
    assert resp.status_code == 200
    assert "MAGI 事務統計" in resp.get_json()["text"]

    resp = saas_client.get("/api/osc/saas/diagnostic-pack")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    assert resp.get_json()["redaction"].startswith("No secrets")
