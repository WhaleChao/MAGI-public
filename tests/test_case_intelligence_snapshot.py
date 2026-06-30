from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path


def _build_osc_app():
    from flask import Flask
    from flask_login import LoginManager, UserMixin

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.secret_key = "test"
    login = LoginManager()
    login.init_app(app)

    class _User(UserMixin):
        id = "test-user"

    @login.user_loader
    def _load_user(_user_id):
        return _User()

    from api.blueprints.osc_cases import osc_bp

    app.register_blueprint(osc_bp)
    return app


def test_case_intelligence_snapshot_aggregates_case_folder_docs_and_calendar(tmp_path):
    from api.osc.case_intelligence import build_case_intelligence_snapshot

    case_dir = tmp_path / "2026-0007-王小明-一審-詐欺"
    (case_dir / "04_我方歷次書狀").mkdir(parents=True)
    (case_dir / "09_法院通知或程序裁定").mkdir()
    (case_dir / "99_自訂資料").mkdir()

    calls: list[tuple[str, tuple, str]] = []

    def fake_exec(sql, params=(), fetch="all"):
        calls.append((sql, params, fetch))
        normalized = " ".join(sql.lower().split())
        if "from cases" in normalized and "left join" not in normalized:
            return (
                [
                    {
                        "id": "case-1",
                        "case_number": "2026-0007",
                        "client_name": "王小明",
                        "case_category": "法律扶助案件",
                        "case_type": "刑事",
                        "case_stage": "一審",
                        "case_reason": "涉詐欺",
                        "court_name": "臺灣臺北地方法院",
                        "court_case_no": "115年度訴字第7號",
                        "court_division": "義股",
                        "laf_case_no": "1150007-A-001",
                        "status": "進行中",
                        "legal_aid_status": "未結案",
                        "folder_path": str(case_dir),
                        "updated_at": datetime(2026, 6, 29, 12, 30, tzinfo=timezone.utc),
                        "created_date": date(2026, 6, 1),
                    }
                ],
                None,
            )
        if "from document_index" in normalized:
            return (
                [
                    {
                        "id": 11,
                        "case_number": "2026-0007",
                        "file_name": "刑事準備狀.docx",
                        "file_path": str(case_dir / "04_我方歷次書狀" / "刑事準備狀.docx"),
                        "subfolder_name": "04_我方歷次書狀",
                        "reason": "詐欺",
                        "party": "我方",
                        "modified_date": "2026-06-28 09:00:00",
                    }
                ],
                None,
            )
        if "from case_documents" in normalized:
            return (
                [
                    {
                        "id": 21,
                        "case_id": "case-1",
                        "case_number_ref": "2026-0007",
                        "document_type": "09_法院通知或程序裁定",
                        "file_name": "開庭通知.pdf",
                        "file_path": str(case_dir / "09_法院通知或程序裁定" / "開庭通知.pdf"),
                        "description": "第一次開庭通知",
                        "upload_date": "2026-06-27 10:00:00",
                    }
                ],
                None,
            )
        if "from case_todos" in normalized:
            return (
                [
                    {
                        "id": 31,
                        "case_number": "2026-0007",
                        "client_name": "王小明",
                        "todo_type": "開庭",
                        "todo_date": date(2026, 7, 8),
                        "todo_time": "10:00:00",
                        "description": "臺北地院開庭",
                        "status": "pending",
                        "source_file": "gcal_import:abc",
                        "created_date": "2026-06-25 08:00:00",
                    }
                ],
                None,
            )
        if "from calendar_events" in normalized:
            return (
                [
                    {
                        "id": 41,
                        "event_id": "evt-1",
                        "title": "2026-0007 王小明 開庭",
                        "summary": "",
                        "description": "臺北地院",
                        "start_date": "2026-07-08 10:00:00",
                        "end_date": "2026-07-08 11:00:00",
                        "location": "臺灣臺北地方法院",
                        "is_all_day": 0,
                        "case_number": "2026-0007",
                    }
                ],
                None,
            )
        return ([], None)

    snapshot = build_case_intelligence_snapshot(
        fake_exec,
        case_number="2026-0007",
        folder_resolver=lambda path: path if Path(path).exists() else "",
        now=datetime(2026, 6, 30, tzinfo=timezone.utc),
    )

    assert snapshot["ok"] is True
    assert snapshot["generated_at"] == "2026-06-30T00:00:00+00:00"
    case = snapshot["cases"][0]
    assert case["key"] == "case-1"
    assert case["name"] == "王小明"
    assert case["case_number"] == "2026-0007"
    assert case["case_reason"] == "詐欺"
    assert case["court"] == {
        "name": "臺灣臺北地方法院",
        "case_number": "115年度訴字第7號",
        "division": "義股",
    }
    assert {item["name"] for item in case["known_subfolders"] if item["exists"]} >= {
        "04_我方歷次書狀",
        "09_法院通知或程序裁定",
        "99_自訂資料",
    }
    assert {doc["source"] for doc in case["recent_docs"]} == {"document_index", "case_documents"}
    assert {ref["source"] for ref in case["calendar_refs"]} == {"case_todos", "calendar_events"}
    assert any(node["type"] == "case" for node in snapshot["graph"]["nodes"])
    assert any(edge["type"] == "has_recent_document" for edge in snapshot["graph"]["edges"])
    assert calls
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _params, _fetch in calls)


def test_case_intelligence_snapshot_keeps_optional_source_failures_as_warnings(tmp_path):
    from api.osc.case_intelligence import build_case_intelligence_snapshot

    case_dir = tmp_path / "2026-0008-測試"
    case_dir.mkdir()

    def fake_exec(sql, params=(), fetch="all"):
        normalized = " ".join(sql.lower().split())
        if "from cases" in normalized and "left join" not in normalized:
            return (
                [
                    {
                        "id": "case-2",
                        "case_number": "2026-0008",
                        "client_name": "測試",
                        "case_category": "一般案件",
                        "folder_path": str(case_dir),
                    }
                ],
                None,
            )
        if "from document_index" in normalized:
            raise RuntimeError("document_index unavailable")
        return ([], None)

    snapshot = build_case_intelligence_snapshot(
        fake_exec,
        case_number="2026-0008",
        folder_resolver=lambda path: path,
    )

    assert snapshot["ok"] is True
    assert snapshot["cases"][0]["recent_docs"] == []
    assert "document_index: RuntimeError: document_index unavailable" in snapshot["warnings"]


def test_case_intelligence_endpoint_returns_read_only_snapshot(monkeypatch, tmp_path):
    from api.blueprints import osc_cases

    case_dir = tmp_path / "2026-0009-端點測試"
    case_dir.mkdir()
    calls: list[str] = []

    def fake_exec(sql, params=(), fetch="all"):
        calls.append(sql)
        normalized = " ".join(sql.lower().split())
        if "from cases" in normalized and "left join" not in normalized:
            return (
                [
                    {
                        "id": "case-3",
                        "case_number": "2026-0009",
                        "client_name": "端點測試",
                        "case_category": "一般案件",
                        "folder_path": str(case_dir),
                    }
                ],
                None,
            )
        return ([], None)

    monkeypatch.setattr(osc_cases, "_osc_exec", fake_exec)
    monkeypatch.setattr(osc_cases, "_osc_case_intelligence_folder_resolver", lambda path: path)

    response = _build_osc_app().test_client().get("/api/osc/cases/case-3/intelligence-snapshot")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["cases"][0]["key"] == "case-3"
    assert payload["cases"][0]["case_number"] == "2026-0009"
    assert calls
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in calls)
