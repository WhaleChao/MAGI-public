from __future__ import annotations

import json
from pathlib import Path

from flask import Flask
from flask_login import LoginManager, UserMixin


class _User(UserMixin):
    id = "u1"
    role = "admin"


def _make_app():
    from api.blueprints.golem_console import golem_console_bp

    app = Flask(__name__)
    app.config.update(SECRET_KEY="test-secret", TESTING=True)
    login = LoginManager()
    login.init_app(app)

    @login.request_loader
    def _load_user(request):
        return _User() if request.headers.get("X-User-ID") else None

    app.register_blueprint(golem_console_bp)
    return app


def test_golem_status_api_reports_process_skills_exports_and_memory(tmp_path, monkeypatch):
    from api.blueprints import golem_console as mod

    static_dir = tmp_path / "static"
    exports_dir = static_dir / "exports"
    agent_dir = tmp_path / ".agent"
    skills_dir = tmp_path / "skills"
    exports_dir.mkdir(parents=True)
    agent_dir.mkdir()
    skills_dir.mkdir()
    (exports_dir / "market_briefing_20260503.txt").write_text("full report", encoding="utf-8")
    (exports_dir / "translate_20260503.docx").write_bytes(b"doc")
    (agent_dir / "doc_vector_index.json").write_text(
        json.dumps({"documents": [{"source": "a"}, {"source": "b"}]}),
        encoding="utf-8",
    )
    (agent_dir / "server.log").write_text("server ok\n", encoding="utf-8")
    definitions = {
        "_meta": {"runtime_filter": {"tools_exposed": 2}},
        "tools": [
            {"name": "web_search", "sage": "casper", "method": "POST", "endpoint": "/search"},
            {"name": "summarize", "sage": "balthasar", "method": "POST", "endpoint": "/summarize"},
        ],
    }
    (skills_dir / "definitions.json").write_text(json.dumps(definitions), encoding="utf-8")

    monkeypatch.setattr(mod, "_MAGI_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_STATIC_DIR", static_dir)
    monkeypatch.setattr(mod, "_EXPORTS_DIR", exports_dir)
    monkeypatch.setattr(mod, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(mod, "_SKILLS_DEFINITIONS", skills_dir / "definitions.json")
    monkeypatch.setattr(mod, "_GUARDIAN_STATE", static_dir / "process_guardian_state.json")
    monkeypatch.setattr(
        mod,
        "_collect_process_monitor",
        lambda **kwargs: {
            "ok": True,
            "summary": {"core_count": 1, "worker_count": 2, "orphan_count": 0, "duplicate_groups": 0},
            "core": [{"label": "API", "pid": 10, "age": "00:01:00"}],
        },
    )

    client = _make_app().test_client()
    response = client.get("/api/golem/status", headers={"X-User-ID": "u1"})

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["skills"]["count"] == 2
    assert data["memory"]["doc_count"] == 2
    assert data["market_reports"][0]["name"] == "market_briefing_20260503.txt"
    assert data["exports"]


def test_golem_command_api_supports_safe_commands(monkeypatch):
    from api.blueprints import golem_console as mod

    monkeypatch.setattr(
        mod,
        "_collect_process_monitor",
        lambda **kwargs: {"ok": True, "summary": {"core_count": 0}, "core": []},
    )

    client = _make_app().test_client()
    response = client.post("/api/golem/command", headers={"X-User-ID": "u1"}, json={"command": "memory"})

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_golem_api_key_status_masks_secret(tmp_path, monkeypatch):
    from api.blueprints import golem_console as mod

    env_path = tmp_path / ".env"
    env_path.write_text("NVIDIA_NIM_ENABLE=1\nNVIDIA_NIM_API_KEY=nvapi-abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_ENV_PATH", env_path)
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_ENABLE", raising=False)

    client = _make_app().test_client()
    response = client.get("/api/golem/api-keys", headers={"X-User-ID": "u1"})

    assert response.status_code == 200
    item = response.get_json()["items"][0]
    assert item["configured"] is True
    assert item["enabled"] is True
    assert item["masked"].startswith("nvapi-")
    assert "abcdefghijklmnopqrstuvwxyz" not in item["masked"]


def test_golem_api_key_update_writes_env_and_runtime(tmp_path, monkeypatch):
    from api.blueprints import golem_console as mod

    env_path = tmp_path / ".env"
    env_path.write_text("NVIDIA_NIM_ENABLE=0\nNVIDIA_NIM_API_KEY=nvapi-oldkey000000000000\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_ENV_PATH", env_path)
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_ENABLE", raising=False)

    new_key = "nvapi-newkey1234567890abcdef"
    client = _make_app().test_client()
    response = client.post(
        "/api/golem/api-keys",
        headers={"X-User-ID": "u1"},
        json={"id": "nvidia_nim", "api_key": new_key, "enable": True},
    )

    assert response.status_code == 200
    text = env_path.read_text(encoding="utf-8")
    assert f"NVIDIA_NIM_API_KEY={new_key}" in text
    assert "NVIDIA_NIM_ENABLE=1" in text
    assert response.get_json()["item"]["masked"].endswith("cdef")
