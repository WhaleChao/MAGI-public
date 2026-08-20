from __future__ import annotations

from contextlib import contextmanager

from flask import Flask, g


def test_line_root_and_alias_callbacks_dispatch_to_offline_handler(monkeypatch):
    import api.server as server
    import api.startup as startup
    import api.webhooks.line as line_webhook

    calls: list[tuple[str, object]] = []

    class OfflineLineHandler:
        @staticmethod
        def handle(body, signature):
            calls.append(("line_handler", (body, signature)))

    monkeypatch.setitem(server.app.config, "MAGI_CSRF_TEST_MODE", True)
    monkeypatch.setattr(server, "LINE_BOT_ENABLED", True)
    monkeypatch.setattr(server, "_check_rate_limit", lambda _bucket: False)
    monkeypatch.setattr(server, "handler", OfflineLineHandler())
    monkeypatch.setattr(server.logger, "info", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.logger, "warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.logger, "error", lambda *args, **kwargs: None)
    monkeypatch.setattr(startup, "_record_last_public_base_url", lambda: calls.append(("public_url", None)))
    monkeypatch.setattr(
        line_webhook,
        "_record_last_line_callback",
        lambda path: calls.append(("line_callback", path)),
    )

    client = server.app.test_client()
    responses = [
        client.post(path, data=b'{"events": []}', headers={"X-Line-Signature": "offline-signature"})
        for path in ("/", "/callback", "/line/webhook")
    ]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert [response.get_data(as_text=True) for response in responses] == ["OK", "OK", "OK"]
    assert [name for name, _payload in calls].count("line_handler") == 3
    assert [payload for name, payload in calls if name == "line_callback"] == [
        "/",
        "/callback",
        "/line/webhook",
    ]


def test_line_rate_limit_returns_retry_after(monkeypatch):
    import api.server as server

    def reject(_bucket):
        g.magi_rate_limit_retry_after = 37
        return True

    monkeypatch.setitem(server.app.config, "MAGI_CSRF_TEST_MODE", True)
    monkeypatch.setattr(server, "_check_rate_limit", reject)
    response = server.app.test_client().post("/line/webhook", data=b"{}")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "37"


def test_login_and_register_commit_only_to_in_memory_database(monkeypatch):
    import api.db_helper as db_helper
    import api.server as server

    statements: list[tuple[str, object]] = []
    commits: list[str] = []

    class Cursor:
        def __init__(self):
            self.last_sql = ""

        def execute(self, sql, params=None):
            self.last_sql = " ".join(str(sql).split())
            statements.append((self.last_sql, params))

        def fetchone(self):
            if "COUNT(*)" in self.last_sql.upper():
                return (1,)
            return {
                "id": 7,
                "username": "fixture-user",
                "password_hash": "fixture-hash",
                "role": "admin",
                "tenant_id": "fixture-tenant",
                "tenant_role": "admin",
            }

    class Connection:
        @staticmethod
        def commit():
            commits.append("commit")

    @contextmanager
    def get_cursor(*args, **kwargs):
        yield Connection(), Cursor()

    monkeypatch.setattr(db_helper, "get_cursor", get_cursor)
    monkeypatch.setattr(server, "check_password_hash", lambda candidate, supplied: True)
    monkeypatch.setattr(server, "generate_password_hash", lambda supplied: f"fixture:{supplied}")
    monkeypatch.setenv("MAGI_ALLOW_PUBLIC_REGISTRATION", "1")

    client = server.app.test_client()
    login_response = client.post(
        "/login",
        data={"username": "fixture-user", "password": "secret", "next": "/dashboard"},
        follow_redirects=False,
    )
    register_response = client.post(
        "/register",
        data={"username": "new-fixture", "password": "secret", "next": "/dashboard"},
        follow_redirects=True,
    )

    assert login_response.status_code == 302
    assert login_response.headers["Location"].endswith("/dashboard")
    assert register_response.status_code == 200
    assert b"login" in register_response.data.lower()
    assert any(sql.startswith("SELECT * FROM users") for sql, _params in statements)
    assert any(sql.startswith("INSERT INTO users") for sql, _params in statements)
    assert commits == ["commit"]


def test_telegram_webhook_dispatches_to_offline_update_handler(monkeypatch):
    import api.server as server
    import api.startup as startup
    import api.webhooks.telegram as telegram

    calls: list[object] = []
    monkeypatch.setattr(server, "_check_rate_limit", lambda _bucket: False)
    monkeypatch.setattr(telegram, "_telegram_verify_webhook_secret", lambda: True)
    monkeypatch.setattr(
        telegram,
        "_telegram_handle_update",
        lambda update, from_poll=False: calls.append((update, from_poll)) or {"ok": True, "fixture": True},
    )
    monkeypatch.setattr(startup, "_record_last_public_base_url", lambda: calls.append("public-url"))

    app = Flask("offline-telegram-certification")
    app.config.update(TESTING=True, SECRET_KEY="offline")
    app.register_blueprint(telegram.telegram_bp)
    response = app.test_client().post(
        "/telegram/webhook",
        json={"update_id": 7, "message": {"text": "offline"}},
    )

    assert response.status_code == 200
    assert response.get_json() == {"fixture": True, "ok": True}
    assert ({"update_id": 7, "message": {"text": "offline"}}, False) in calls


def test_telegram_rate_limit_returns_retry_after(monkeypatch):
    import api.server as server
    import api.webhooks.telegram as telegram

    def reject(_bucket):
        g.magi_rate_limit_retry_after = 29
        return True

    monkeypatch.setattr(server, "_check_rate_limit", reject)
    app = Flask("offline-telegram-rate-limit")
    app.config.update(TESTING=True, SECRET_KEY="offline")
    app.register_blueprint(telegram.telegram_bp)
    response = app.test_client().post("/telegram/webhook", json={"update_id": 9})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "29"


def test_iron_dome_mutations_dispatch_to_offline_sync_adapters(monkeypatch):
    import skills.ops.iron_dome_sync as iron_dome

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        iron_dome,
        "receive_update_notification",
        lambda source, source_hash: calls.append(("notify", (source, source_hash)))
        or {"success": True, "updated": True},
    )
    monkeypatch.setattr(
        iron_dome,
        "broadcast_update",
        lambda: calls.append(("broadcast", None)) or {"offline-node": {"success": True}},
    )

    app = Flask("offline-iron-dome-certification")
    app.config.update(TESTING=True, SECRET_KEY="offline")
    iron_dome.register_iron_dome_routes(app)
    client = app.test_client()
    notify_response = client.post(
        "/api/iron_dome/notify",
        json={"source": "offline-node", "hash": "fixture-hash"},
    )
    broadcast_response = client.post("/api/iron_dome/broadcast", json={})

    assert notify_response.status_code == 200
    assert notify_response.get_json() == {"success": True, "updated": True}
    assert broadcast_response.status_code == 200
    assert broadcast_response.get_json() == {
        "broadcast_results": {"offline-node": {"success": True}}
    }
    assert calls == [
        ("notify", ("offline-node", "fixture-hash")),
        ("broadcast", None),
    ]
