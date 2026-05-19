from flask import Flask
from flask_login import LoginManager, UserMixin


def _app():
    from api.blueprints.osc_cases import osc_bp

    app = Flask(__name__)
    app.config.update(TESTING=True, LOGIN_DISABLED=True)
    app.secret_key = "test"
    login = LoginManager(app)

    class User(UserMixin):
        id = "test"

    @login.user_loader
    def _load(_user_id):
        return User()

    app.register_blueprint(osc_bp)
    return app


def test_bulk_complete_before_updates_stale_open_todos(monkeypatch):
    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        if fetch == "one":
            assert params[0] == "2026-02-01"
            return {"c": 2408}, None
        assert fetch == "none"
        assert params[0] == "已完成"
        assert params[1] == "2026-02-01"
        return {"rowcount": 2408}, None

    monkeypatch.setattr("api.blueprints.osc_cases._osc_exec", fake_exec)
    monkeypatch.setattr("api.blueprints.osc_cases._osc_log_activity", lambda *args, **kwargs: None)

    resp = _app().test_client().post(
        "/api/osc/todos/bulk-complete-before",
        json={"cutoff_date": "2026-02-01"},
    )

    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["matched"] == 2408
    assert data["updated"] == 2408
    assert len(calls) == 2


def test_bulk_complete_before_dry_run_does_not_update(monkeypatch):
    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        return {"c": 7}, None

    monkeypatch.setattr("api.blueprints.osc_cases._osc_exec", fake_exec)

    resp = _app().test_client().post(
        "/api/osc/todos/bulk-complete-before",
        json={"cutoff_date": "2026-02-01", "dry_run": True},
    )

    data = resp.get_json()
    assert resp.status_code == 200
    assert data["dry_run"] is True
    assert data["matched"] == 7
    assert len(calls) == 1
