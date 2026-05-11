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
        "client_portal",
        "operations_report",
    }
    assert result["integration"]["principle"].startswith("事務所營運工作台只做跨模組總控")
    assert all(x.get("owner") and x.get("source") and x.get("role") for x in result["capabilities"])
    assert {x["target_tab"] for x in result["integration"]["items"]} >= {"todos", "clients", "documents", "drafts"}


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

    resp = saas_client.post(
        "/api/osc/saas/quality-check",
        json={"text": "<|channel>thought\nOSC-2026-001", "case_number": "114年度訴字第1號"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["pass"] is False

    resp = saas_client.post("/api/osc/saas/client-packet", json={"client_name": "王小明", "reason": "消債更生"})
    assert resp.status_code == 200
    assert "債權人清冊" in resp.get_json()["copy_text"]
