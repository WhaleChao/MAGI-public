from __future__ import annotations

import json
import sys
import types
from io import BytesIO
from pathlib import Path

from flask import Flask
from flask_login import LoginManager, UserMixin


class _User(UserMixin):
    def __init__(self, user_id: str, role: str = "admin"):
        self.id = user_id
        self.role = role


class _Orchestrator:
    def __init__(self, reply: str = "ok"):
        self.reply = reply
        self.calls = []

    def process_message(self, **kwargs):
        self.calls.append(kwargs)
        return self.reply


def _make_app(tmp_path: Path, orchestrator=None, notifications=None, normalize=None):
    from api.blueprints.web_runtime import create_web_runtime_blueprint

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "process_monitor.html").write_text("monitor {{ user.id }}", encoding="utf-8")

    app = Flask(__name__, template_folder=str(template_dir))
    app.config.update(SECRET_KEY="test-secret", TESTING=True)
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "login"

    @login_manager.request_loader
    def _load_user(request):
        user_id = (request.headers.get("X-User-ID") or "").strip()
        role = (request.headers.get("X-User-Role") or "admin").strip()
        return _User(user_id, role=role) if user_id else None

    bp = create_web_runtime_blueprint(
        orchestrator=orchestrator or _Orchestrator(),
        logger=app.logger,
        web_notifications=notifications if notifications is not None else {},
        normalize_output_text=normalize,
        magi_root=tmp_path,
    )
    app.register_blueprint(bp)
    return app


def test_process_monitor_routes_render_and_toggle(tmp_path, monkeypatch):
    from api.blueprints import web_runtime as mod

    class _Done:
        stdout = "123 1 00:03:10 python api/server.py\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: _Done())

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "guardian_control.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
    (static_dir / "process_guardian_state.json").write_text(json.dumps({"mode": "watch"}), encoding="utf-8")

    app = _make_app(tmp_path)
    client = app.test_client()

    response = client.get("/ops/process-monitor", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"monitor u1" in response.data

    response = client.get("/api/ops/process-monitor", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["guardian_control_enabled"] is True
    assert data["guardian_state"]["mode"] == "watch"
    assert data["summary"]["core_count"] == 1

    response = client.post("/api/ops/process-guardian/toggle", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["enabled"] is False


def test_memory_stats_uses_runtime_files(tmp_path):
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    (agent_dir / "doc_vector_index.json").write_text(
        json.dumps([{"updated_at": "2026-04-03T10:00:00"}]),
        encoding="utf-8",
    )
    (agent_dir / "obsidian_vault_config.json").write_text(
        json.dumps({"vault_path": "/vault", "vault_name": "Vault"}),
        encoding="utf-8",
    )
    (agent_dir / "obsidian_index.json").write_text(
        json.dumps({"notes": {"n1": {}}, "updated_at": "2026-04-03T09:00:00"}),
        encoding="utf-8",
    )
    faiss_dir = tmp_path / "skills" / "memory" / "index_cache"
    faiss_dir.mkdir(parents=True)
    (faiss_dir / "mem_index.faiss").write_bytes(b"abc")

    app = _make_app(tmp_path)
    client = app.test_client()
    response = client.get("/api/memory/stats", headers={"X-User-ID": "u1"})

    assert response.status_code == 200
    data = response.get_json()
    assert data["doc_count"] == 1
    assert data["last_ingest"] == "2026-04-03T10:00:00"
    assert data["obsidian"]["vault_name"] == "Vault"
    assert data["obsidian"]["notes_indexed"] == 1
    assert data["faiss_size"] == 3


def test_memory_recall_and_remember_routes(tmp_path, monkeypatch):
    module = types.ModuleType("skills.memory.mem_bridge")
    remembered = {}

    def _recall(query, top_k=5, source_contains=None):
        return [{"content": query, "top_k": top_k, "source": source_contains}]

    def _remember(content, source):
        remembered["content"] = content
        remembered["source"] = source

    module.recall = _recall
    module.remember = _remember
    monkeypatch.setitem(sys.modules, "skills.memory.mem_bridge", module)

    app = _make_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/api/memory/recall",
        headers={"X-User-ID": "u1"},
        json={"query": "測試", "top_k": 7, "source": "manual"},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["memories"][0]["content"] == "測試"
    assert data["memories"][0]["top_k"] == 7
    assert data["memories"][0]["source"] == "manual"

    response = client.post(
        "/api/memory/remember",
        headers={"X-User-ID": "u1"},
        json={"content": "記住這段", "source": "dashboard"},
    )
    assert response.status_code == 200
    assert remembered == {"content": "記住這段", "source": "dashboard"}


def test_obsidian_sync_route_starts_ingest(tmp_path, monkeypatch):
    module = types.ModuleType("skills.obsidian.action")
    calls = []

    def _task_ingest(payload):
        calls.append(payload)

    module.task_ingest = _task_ingest
    monkeypatch.setitem(sys.modules, "skills.obsidian.action", module)

    from api.blueprints import web_runtime as mod

    class _ImmediateThread:
        def __init__(self, target, daemon):
            self._target = target
            self.daemon = daemon

        def start(self):
            self._target()

    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)

    app = _make_app(tmp_path)
    client = app.test_client()
    response = client.post("/api/memory/obsidian-sync", headers={"X-User-ID": "u1"})

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert calls == [{}]


def test_osc_chat_poll_and_judgments_routes(tmp_path):
    notifications = {"u1": [{"type": "info", "message": "hi"}]}
    orchestrator = _Orchestrator(reply="reply text")
    judgments_dir = tmp_path / "skills" / "judgment-collector"
    judgments_dir.mkdir(parents=True)
    (judgments_dir / "judgments.json").write_text(json.dumps([{"id": 1}]), encoding="utf-8")

    app = _make_app(
        tmp_path,
        orchestrator=orchestrator,
        notifications=notifications,
        normalize=lambda text, platform=None: f"{platform}:{text}",
    )
    client = app.test_client()

    response = client.post("/api/osc/chat", headers={"X-User-ID": "u1"}, json={"message": "你好"})
    assert response.status_code == 200
    assert response.get_json()["reply"] == "WEB:reply text"
    assert response.get_json()["reply_html"].startswith('<div class="web-reply">')
    assert orchestrator.calls[0]["platform"] == "WEB"
    assert orchestrator.calls[0]["role"] == "admin"

    response = client.get("/api/osc/poll", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["messages"] == [{"type": "info", "message": "hi"}]
    assert notifications["u1"] == []

    response = client.get("/api/osc/judgments_legacy", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json() == [{"id": 1}]


def test_osc_chat_upload_extracts_file_and_routes_to_orchestrator(tmp_path, monkeypatch):
    from api.blueprints import web_runtime as mod

    orchestrator = _Orchestrator(reply="upload reply")
    monkeypatch.setattr(
        mod,
        "_extract_chat_upload_text",
        lambda path, filename: {
            "success": True,
            "text": "這是檔案內容",
            "kind": "txt",
            "title": filename,
            "error": "",
        },
    )

    app = _make_app(tmp_path, orchestrator=orchestrator, normalize=lambda text, platform=None: text)
    client = app.test_client()
    response = client.post(
        "/api/osc/chat/upload",
        headers={"X-User-ID": "u1"},
        data={"message": "請摘要", "file": (BytesIO("測試".encode("utf-8")), "note.txt")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["reply"] == "upload reply"
    assert data["filename"] == "note.txt"
    assert data["kind"] == "txt"
    assert "note.txt" in orchestrator.calls[0]["message"]
    assert "這是檔案內容" in orchestrator.calls[0]["message"]
    assert (tmp_path / ".agent" / "chat_uploads").is_dir()


def test_web_reply_html_renders_discord_markdown_safely():
    from api.blueprints.web_runtime import format_web_reply_html

    html = format_web_reply_html(
        "🤖 **MAGI 功能總覽**\n"
        "━━━━━━━━━━━━\n"
        "- `查判決`：搜尋判決\n"
        "- [原文](https://example.test/a)\n"
        "- [危險](javascript:alert(1))\n"
        "<script>alert(1)</script>"
    )

    assert '<div class="web-reply">' in html
    assert "<h4>" in html
    assert "<ul>" in html
    assert "<code>查判決</code>" in html
    assert 'href="https://example.test/a"' in html
    assert "javascript:alert" not in html
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_web_reply_html_cleans_practice_insight_hash_headings():
    from api.blueprints.web_runtime import format_web_reply_html

    html = format_web_reply_html(
        "###實務見解\n"
        "法院見解摘要。\n"
        "###\n"
        "**## 引用裁判**\n"
        "- 最高法院 112 年度台上字第 1 號\n"
        "## 實務見解**"
    )

    assert "<h4>實務見解</h4>" in html
    assert "<h4>引用裁判</h4>" in html
    assert "法院見解摘要。" in html
    assert "最高法院 112 年度台上字第 1 號" in html
    assert "###" not in html
    assert "##" not in html
