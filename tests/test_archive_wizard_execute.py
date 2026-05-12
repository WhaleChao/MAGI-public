def test_archive_execute_uses_selected_case_lookup(monkeypatch):
    from flask import Flask
    from flask_login import LoginManager, UserMixin

    from api.blueprints.osc_cases import osc_bp

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.secret_key = "test"
    login = LoginManager(app)

    class User(UserMixin):
        id = "test"

    @login.user_loader
    def _load(_user_id):
        return User()

    app.register_blueprint(osc_bp)
    calls = {"preview": 0, "selected_sql": 0}

    def fake_exec(sql, params=(), fetch="none"):
        if "WHERE id IN" in sql:
            calls["selected_sql"] += 1
            assert params == ("101",)
            return [
                {
                    "id": 101,
                    "case_number": "2026-0101",
                    "client_name": "測試當事人",
                    "status": "已結案",
                    "legal_aid_status": "",
                    "folder_path": "/tmp/source",
                }
            ], {}
        return {}, {}

    def fake_preview(limit=300):
        calls["preview"] += 1
        return {"ok": True, "items": []}

    def fake_item(row):
        return {
            "id": row["id"],
            "case_number": row["case_number"],
            "source_local": "/tmp/source",
            "target_local": "/tmp/archive/source",
            "ready": True,
        }

    def fake_move(item, *, force=False):
        return {"ok": True, "id": item["id"], "case_number": item["case_number"], "reason": "moved"}

    monkeypatch.setattr("api.blueprints.osc_cases._osc_exec", fake_exec)
    monkeypatch.setattr("api.blueprints.osc_cases._osc_build_archive_preview", fake_preview)
    monkeypatch.setattr("api.blueprints.osc_cases._osc_archive_item_for_row", fake_item)
    monkeypatch.setattr("api.blueprints.osc_cases._osc_move_archive_item", fake_move)

    resp = app.test_client().post(
        "/api/osc/archive-wizard/execute",
        json={"confirm": True, "case_ids": ["101"], "max_items": 1},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["summary"]["moved"] == 1
    assert data["summary"]["selected"] == 1
    assert calls == {"preview": 0, "selected_sql": 1}
