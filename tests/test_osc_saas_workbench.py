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

    def fake_exec(*args, **kwargs):
        sql = args[0]
        if "COUNT(*) AS c" in sql:
            return ({"c": 0}, None)
        return ([], None)

    result = saas_workbench.build_saas_overview(fake_exec)

    assert result["ok"] is True
    assert len(result["capabilities"]) == 10
    assert {x["key"] for x in result["capabilities"]} >= {
        "learning_center",
        "quality_gate",
        "risk_dashboard",
        "conflict_check",
        "nerv_status_page",
        "operations_report",
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


def test_saas_workbench_template_has_actionable_entry_links():
    html = Path("templates/partials/osc/saasWorkbench.html").read_text(encoding="utf-8")

    assert "資料來源與處理入口" in html
    assert 'id="saasReadinessGrid"' in html
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

    assert result["items"][0]["owner"] == "待辦事項"
    assert result["items"][0]["target_tab"] == "todos"
    assert {x["act"] for x in result["items"][0]["actions"]} == {"saas-todo-edit", "saas-todo-complete"}


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


@pytest.fixture
def saas_client(monkeypatch, tmp_path):
    from flask import Flask
    from flask_login import LoginManager, UserMixin
    from api.blueprints import osc_cases
    from api.osc import draft_learning, saas_workbench

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "learning.jsonl")
    monkeypatch.setattr(saas_workbench, "INTAKE_PATH", tmp_path / "intake.jsonl")

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
    assert len(resp.get_json()["capabilities"]) == 10
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
