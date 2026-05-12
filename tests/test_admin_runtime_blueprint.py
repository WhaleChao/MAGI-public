from __future__ import annotations

import io
import json
import sys
import time
import types
from pathlib import Path

from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, current_user


class _User(UserMixin):
    def __init__(self, user_id: str, role: str = "admin"):
        self.id = user_id
        self.role = role

    def is_admin(self):
        return self.role == "admin"


class _Orchestrator:
    def __init__(self):
        self.started = []
        self.replied = []

    def get_skill_interview_state(self, user_id, channel):
        return {"active": False, "user_id": user_id, "channel": channel}

    def start_skill_interview(self, user_id, channel, role, initial_request, trigger_reason="manual"):
        self.started.append((user_id, channel, role, initial_request, trigger_reason))
        return "已建立。資料夾：`demo-skill`"

    def reply_skill_interview(self, user_id, channel, role, reply_text):
        self.replied.append((user_id, channel, role, reply_text))
        return True, "新 SKILL 已建立並啟用。資料夾：`demo-skill`"


def _make_app(tmp_path: Path, monkeypatch, *, attachment_queue=None):
    from api.blueprints.admin_runtime import create_admin_runtime_blueprint

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    skill_dir = tmp_path / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo Skill\n\nsummary", encoding="utf-8")
    (skill_dir / "action.py").write_text("def main():\n    return 0\n", encoding="utf-8")

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

    def _require_json_auth(admin: bool = False):
        if not getattr(current_user, "is_authenticated", False):
            return jsonify({"ok": False, "error": "auth_required"}), 401
        if admin and not current_user.is_admin():
            return jsonify({"ok": False, "error": "admin_required"}), 403
        return None

    def _skill_doc_path(skill_name: str) -> Path:
        return tmp_path / "skills" / skill_name / "SKILL.md"

    def _skill_action_path(skill_name: str) -> Path:
        return tmp_path / "skills" / skill_name / "action.py"

    def _skill_summary(content: str) -> str:
        return content.strip().splitlines()[0].lstrip("# ").strip() if content.strip() else ""

    product_updates = {}

    def _update_product_runtime(product: str, **updates):
        product_updates[product] = updates
        return updates

    def _nerv_payload():
        return {"ok": True, "products": {"laf": {"profile": {"portal_env": "prod"}}}}

    class _MysqlConnector:
        @staticmethod
        def connect(**kwargs):
            class _Conn:
                def is_connected(self):
                    return True

                def close(self):
                    return None

            return _Conn()

    orchestrator = _Orchestrator()
    bp = create_admin_runtime_blueprint(
        logger=app.logger,
        orchestrator=orchestrator,
        require_json_auth=_require_json_auth,
        list_skill_docs=lambda: [{"name": "demo-skill", "summary": "Demo", "has_skill_doc": True}],
        nerv_skill_interview_user_id=lambda: f"nerv:{getattr(current_user, 'id', '')}",
        extract_interview_skill_name=lambda message: "demo-skill" if "demo-skill" in str(message) else "",
        skill_doc_path=_skill_doc_path,
        skill_action_path=_skill_action_path,
        skill_summary=_skill_summary,
        nerv_product_runtime_payload=_nerv_payload,
        nerv_product_names=("file_review", "transcript", "laf"),
        update_product_runtime=_update_product_runtime,
        cloudflared_alive=lambda: True,
        server_start_time=time.time() - 120,
        attachment_job_queue=attachment_queue,
        list_attachment_job_ids=lambda: ["job-1"],
        read_attachment_job=lambda job_id: {"status": "queued"},
        expected_magi_api_key="test-api-key",
        db_config={"host": "127.0.0.1", "user": "u", "password": "p"},
        mysql_connector=_MysqlConnector,
        safe_remove_tmp=lambda path: Path(path).unlink(missing_ok=True),
        magi_root=tmp_path,
    )
    app.register_blueprint(bp)
    return app, orchestrator, product_updates


def test_dashboard_nerv_health_status_and_logs(tmp_path, monkeypatch):
    import requests
    import subprocess as _subprocess

    app, _, _ = _make_app(tmp_path, monkeypatch, attachment_queue=types.SimpleNamespace(stats=lambda: {"total": 2, "active": 1}))
    (tmp_path / "static" / "magi_status.json").write_text(
        json.dumps({"timestamp": "2026-04-03T12:00:00", "nodes": {"casper": {"online": True, "model": "gemma-4-e4b"}}}),
        encoding="utf-8",
    )
    (tmp_path / ".agent" / "server.log").write_text("l1\nl2\n", encoding="utf-8")

    monkeypatch.setenv("MAGI_LINE_WEBHOOK_ENDPOINT", "https://example.test/line/webhook")

    def _fake_get(url, timeout=0):
        if url.endswith("/v1/models"):
            return types.SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "gemma-4-e4b"}]})
        if url.endswith("/health"):
            return types.SimpleNamespace(status_code=200, json=lambda: {})
        if url.endswith("/api/tags"):
            raise RuntimeError("offline")
        raise AssertionError(url)

    monkeypatch.setattr(requests, "get", _fake_get)

    http_pool = types.ModuleType("skills.bridge.http_pool")
    http_pool.get_session = lambda: types.SimpleNamespace(get=lambda url, timeout=0: types.SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "gemma-4-e4b"}]}))
    monkeypatch.setitem(sys.modules, "skills.bridge.http_pool", http_pool)

    faiss_mod = types.ModuleType("skills.memory.faiss_index")
    faiss_mod.FAISSMemoryIndex = types.SimpleNamespace(get_instance=lambda: types.SimpleNamespace(total=9))
    monkeypatch.setitem(sys.modules, "skills.memory.faiss_index", faiss_mod)

    nas_mod = types.ModuleType("api.nas_mount_guard")
    nas_mod._SHARES = [("homes", "/Volumes/homes")]
    nas_mod._is_mounted = lambda vol: True
    monkeypatch.setitem(sys.modules, "api.nas_mount_guard", nas_mod)

    psutil_mod = types.ModuleType("psutil")
    psutil_mod.virtual_memory = lambda: types.SimpleNamespace(percent=50, available=8 * 1024**3)
    psutil_mod.disk_usage = lambda path: types.SimpleNamespace(percent=20, free=100 * 1024**3)
    psutil_mod.cpu_percent = lambda interval=0.1: 12.5
    monkeypatch.setitem(sys.modules, "psutil", psutil_mod)
    monkeypatch.setattr(_subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))

    client = app.test_client()

    response = client.get("/dashboard/nerv/api/health", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["omlx"]["status"] == "online"
    assert data["line_webhook"]["status"] == "online"

    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.get_json()["nodes"]["casper"]["model"] == "gemma-4-e4b"

    response = client.get("/api/live-log?limit=1", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["lines"] == ["l2"]

    response = client.get("/health")
    assert response.status_code == 200
    health = response.get_json()
    assert health["status"] in {"operational", "degraded"}
    assert health["omlx"]["ok"] is True
    assert health["faiss"]["vectors"] == 9
    assert health["attachment_jobs"]["active"] == 1

    response = client.get("/health", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    assert "MAGI 系統健康狀態" in response.get_data(as_text=True)
    assert response.get_json(silent=True) is None


def test_health_reports_omlx_8083_unmanaged_as_degraded(tmp_path, monkeypatch):
    import subprocess as _subprocess

    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def _session_get(url, timeout=0):
        if "8083" in url:
            return _Resp(200, {"data": [{"id": "mlx-community/SmolLM3-3B-4bit"}]})
        if "8082" in url:
            return _Resp(200, {"data": [{"id": "phi-4-mini-instruct"}]})
        if "8080" in url:
            return _Resp(200, {"data": [{"id": "gemma-4-e4b-it-4bit"}]})
        raise AssertionError(url)

    http_pool = types.ModuleType("skills.bridge.http_pool")
    http_pool.get_session = lambda: types.SimpleNamespace(get=_session_get)
    monkeypatch.setitem(sys.modules, "skills.bridge.http_pool", http_pool)

    faiss_mod = types.ModuleType("skills.memory.faiss_index")
    faiss_mod.FAISSMemoryIndex = types.SimpleNamespace(get_instance=lambda: types.SimpleNamespace(total=3))
    monkeypatch.setitem(sys.modules, "skills.memory.faiss_index", faiss_mod)

    nas_mod = types.ModuleType("api.nas_mount_guard")
    nas_mod._SHARES = [("homes", "/Volumes/homes")]
    nas_mod._is_mounted = lambda vol: True
    nas_mod._USER_MOUNT_ROOT = "/tmp"
    monkeypatch.setitem(sys.modules, "api.nas_mount_guard", nas_mod)

    psutil_mod = types.ModuleType("psutil")
    psutil_mod.virtual_memory = lambda: types.SimpleNamespace(percent=50, available=8 * 1024**3)
    psutil_mod.disk_usage = lambda path: types.SimpleNamespace(percent=20, free=100 * 1024**3)
    psutil_mod.cpu_percent = lambda interval=0.1: 12.5
    monkeypatch.setitem(sys.modules, "psutil", psutil_mod)

    def _fake_run(argv, **kwargs):
        label = argv[-1] if argv else ""
        if label in {"com.magi.omlx", "com.magi.omlx-phi4"}:
            return types.SimpleNamespace(returncode=0)
        if label in {"com.magi.omlx-smol", "com.magi.omlx-smollm3"}:
            return types.SimpleNamespace(returncode=1)
        return types.SimpleNamespace(returncode=1)

    monkeypatch.setattr(_subprocess, "run", _fake_run)

    response = client.get("/health")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "degraded"
    assert "smol" in body["omlx"]["unmanaged_alive"]
    assert body["omlx"]["services"]["smol"]["port"] == 8083
    assert body["omlx"]["services"]["smol"]["management_state"] == "unmanaged"


def test_system_self_repair_and_transcribe_routes(tmp_path, monkeypatch):
    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    sys_test_mod = types.ModuleType("skills.ops.system_test")
    sys_test_mod.run_all_tests = lambda: {"ok": True, "passed": 12}
    monkeypatch.setitem(sys.modules, "skills.ops.system_test", sys_test_mod)

    repair_dir = tmp_path / "skills" / "magi-self-repair"
    repair_dir.mkdir(parents=True)
    (repair_dir / "action.py").write_text(
        "def repair_targets(targets):\n    return {'ok': True, 'targets': targets}\n",
        encoding="utf-8",
    )

    transcribe_mod = types.ModuleType("skills.bridge.balthasar_bridge")
    transcribe_mod.transcribe = lambda path, language=None, taigi_hint=False: {"text": "ok", "language": language, "taigi_hint": taigi_hint}
    monkeypatch.setitem(sys.modules, "skills.bridge.balthasar_bridge", transcribe_mod)

    response = client.post("/api/system-test", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["passed"] == 12

    response = client.post("/api/self-repair", headers={"X-User-ID": "u1"}, json={"targets": ["a"]})
    assert response.status_code == 200
    assert response.get_json()["targets"] == ["a"]

    response = client.post(
        "/api/transcribe",
        headers={"X-MAGI-API-KEY": "test-api-key"},
        data={"file": (io.BytesIO(b"audio"), "sample.wav"), "language": "zh-TW", "taigi_hint": "1"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["text"] == "ok"
    assert response.get_json()["language"] == "zh-TW"
    assert response.get_json()["taigi_hint"] is True


def test_nerv_skill_routes_and_heavy_runtime_controls(tmp_path, monkeypatch):
    monkeypatch.delenv("NVIDIA_NIM_ENABLE", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    app, orchestrator, product_updates = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    history_mod = types.ModuleType("skills.management.skill_interview")
    history_mod.list_interview_history = lambda limit=10: [{"skill": "demo-skill", "limit": limit}]
    monkeypatch.setitem(sys.modules, "skills.management.skill_interview", history_mod)

    genesis_mod = types.ModuleType("skills.evolution.skill_genesis")
    genesis_mod.list_skill_versions = lambda skill_name: {"success": True, "versions": [{"id": "v1"}]}
    genesis_mod.rollback_skill_version = lambda skill_name, version_id="": {"success": True, "version_id": version_id}
    monkeypatch.setitem(sys.modules, "skills.evolution.skill_genesis", genesis_mod)

    router_mod = types.ModuleType("skills.bridge.embedding_router")
    router_mod.get_router = lambda: types.SimpleNamespace(is_ready=True, rebuild_cache=lambda: None)
    bridge_mod = types.ModuleType("skills.bridge")
    bridge_mod.__path__ = []
    monkeypatch.setitem(sys.modules, "skills.bridge", bridge_mod)
    monkeypatch.setitem(sys.modules, "skills.bridge.embedding_router", router_mod)

    semantic_mod = types.ModuleType("skills.bridge.semantic_router")
    semantic_mod._SKILLS_CACHE = "x"
    semantic_mod._SKILLS_CACHE_TS = 1.0
    monkeypatch.setitem(sys.modules, "skills.bridge.semantic_router", semantic_mod)

    response = client.get("/api/nerv/skill-interview", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["interview"]["channel"] == "NERV"

    response = client.post("/api/nerv/skill-interview/start", headers={"X-User-ID": "u1"}, json={"request": "做一個 skill"})
    assert response.status_code == 200
    assert orchestrator.started

    response = client.post("/api/nerv/skill-interview/reply", headers={"X-User-ID": "u1"}, json={"message": "回答"})
    assert response.status_code == 200
    assert response.get_json()["finalized"] is True
    assert response.get_json()["skill_name"] == "demo-skill"

    response = client.get("/api/skills/interview-history?limit=5", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["history"][0]["limit"] == 5

    response = client.get("/api/skills/demo-skill/versions", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["versions"][0]["id"] == "v1"

    response = client.post("/api/skills/demo-skill/rollback", headers={"X-User-ID": "u1"}, json={"version_id": "v1"})
    assert response.status_code == 200
    assert response.get_json()["result"]["version_id"] == "v1"

    response = client.get("/api/nerv/skills", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["skills"][0]["name"] == "demo-skill"

    response = client.get("/api/nerv/product-runtime", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    response = client.post(
        "/api/nerv/product-runtime",
        headers={"X-User-ID": "u1"},
        json={"product": "laf", "portal_env": "prod"},
    )
    assert response.status_code == 200
    assert product_updates["laf"]["portal_env"] == "prod"

    response = client.get("/api/nerv/heavy-runtime", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    heavy = response.get_json()
    assert heavy["enabled"] is False
    assert heavy["configured"] is False
    assert heavy["command_prefixes"] == ["@heavy", "@重型"]

    response = client.post(
        "/api/nerv/heavy-runtime",
        headers={"X-User-ID": "u1"},
        json={"enabled": True, "api_key": "nvapi-testkey1234567890"},
    )
    assert response.status_code == 200
    heavy = response.get_json()
    assert heavy["enabled"] is True
    assert heavy["configured"] is True
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "NVIDIA_NIM_ENABLE=1" in env_text
    assert "NVIDIA_NIM_API_KEY=nvapi-testkey1234567890" in env_text

    response = client.get("/api/nerv/skills/demo-skill", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["skill"]["has_skill_doc"] is True

    response = client.post("/api/nerv/skills/demo-skill", headers={"X-User-ID": "u1"}, json={"content": "# Updated"})
    assert response.status_code == 200
    assert "Updated" in response.get_json()["skill"]["summary"]


def test_nerv_remote_access_status_and_actions(tmp_path, monkeypatch):
    from api.blueprints import admin_runtime as mod

    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    original_exists = mod.os.path.exists

    def _exists(path):
        if str(path) == "/opt/homebrew/bin/tailscale":
            return True
        return original_exists(path)

    def _run(args, capture_output=True, text=True, timeout=4):
        if args[:2] == ["launchctl", "list"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout="123\t0\torg.chromium.chromoting\n456\t0\thomebrew.mxcl.tailscale\n",
                stderr="",
            )
        if args and args[-1] == "--json":
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"Self": {"TailscaleIPs": ["198.51.100.2"], "DNSName": "magi.tailnet.test."}}),
                stderr="",
            )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    launched = []
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/opt/homebrew/bin/tailscale" if name == "tailscale" else None)
    monkeypatch.setattr(mod.os.path, "exists", _exists)
    monkeypatch.setattr(mod.subprocess, "run", _run)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda cmd, cwd=None: launched.append((cmd, cwd)) or types.SimpleNamespace(pid=1))

    response = client.get("/api/nerv/remote-access", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["tailscale"]["ip"] == "198.51.100.2"
    assert data["google_remote_desktop"]["access_url"].startswith("https://remotedesktop.google.com")
    assert data["policy"]["public_vnc_exposed"] is False

    response = client.post(
        "/api/nerv/remote-access/action",
        headers={"X-User-ID": "u1"},
        json={"action": "open_google_remote_desktop"},
    )
    assert response.status_code == 200
    assert launched[-1][0] == ["open", "https://remotedesktop.google.com/access"]


def test_operational_issue_health_reconciles_recovered_and_false_positive(tmp_path, monkeypatch):
    from api.blueprints.admin_runtime import _compute_operational_issue_health

    now = 2_000_000.0
    runtime_dir = tmp_path / ".runtime"
    runtime_dir.mkdir(parents=True)

    issue_rows = [
        {
            "ts": now - 1800,
            "severity": "High",
            "source": "discord_bot.cron_scheduler",
            "command": "cron:job_debug_cleanup",
            "error": "Traceback ...",
        },
        {
            "ts": now - 600,
            "severity": "High",
            "source": "discord_bot.cron_scheduler",
            "command": "cron:job_disk_low_water_alarm",
            "error": "exit=255 stderr= stdout_tail={\"success\": true}",
        },
        {
            "ts": now - 300,
            "severity": "High",
            "source": "discord_bot.cron_scheduler",
            "command": "cron:job_obsidian_ingest",
            "error": "exit=1 stderr=Syntax Warning: May not be a PDF file",
        },
        {
            "ts": now - 900,
            "severity": "High",
            "source": "discord_bot.cron_scheduler",
            "command": "cron:job_obsidian_ingest",
            "error": "exit=1 stderr=old failure for same job",
        },
        {
            "ts": now - 5000,
            "severity": "High",
            "source": "discord_bot.cron_scheduler",
            "command": "cron:job_old_failure",
            "error": "exit=1 stderr=old",
        },
        {
            "ts": now - 200,
            "severity": "High",
            "source": "disk_low_water_alarm",
            "command": "cron:job_disk_low_water_alarm",
            "error": "磁碟低水位告警",
        },
    ]
    issue_path = runtime_dir / "issue_agenda.jsonl"
    issue_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in issue_rows) + "\n",
        encoding="utf-8",
    )

    cron_state = {
        "job_debug_cleanup": {"last_run": str(now - 100)},
        "job_obsidian_ingest": {"last_run": str(now - 400)},
    }
    (runtime_dir / "cron_state.json").write_text(
        json.dumps(cron_state, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setenv("MAGI_OPERATIONAL_ACTIVE_ISSUE_WINDOW_SEC", "3600")
    summary = _compute_operational_issue_health(tmp_path, now)
    assert summary["raw_cron_failures_24h"] == 5
    assert summary["active_cron_failures_24h"] == 1
    assert summary["active_distinct_jobs_24h"] == 1
    assert summary["false_positive_cron_failures_24h"] == 1
    assert summary["active_high_severity_24h"] == 2
    assert summary["inactive_cron_failures_24h"] == 3
    assert summary["recovered_cron_failures_24h"] == 1
    assert summary["superseded_cron_failures_24h"] == 1
    assert summary["stale_cron_failures_24h"] == 1
    assert summary["inactive_or_noise_cron_failures_24h"] == 4


def test_operational_issue_health_treats_live_recovered_guards_as_inactive(tmp_path, monkeypatch):
    from api.blueprints.admin_runtime import _compute_operational_issue_health
    import api.blueprints.admin_runtime as mod

    runtime_dir = tmp_path / ".runtime"
    runtime_dir.mkdir()
    now = 2_000_000.0
    issue_rows = [
        {
            "ts": now - 120,
            "severity": "High",
            "source": "discord_bot.cron_scheduler",
            "command": "cron:job_omlx_switch_day",
            "error": "exit=4 stdout_tail=8080 model not ready",
        },
        {
            "ts": now - 60,
            "severity": "High",
            "source": "discord_bot.cron_scheduler",
            "command": "cron:job_resource_governor",
            "error": "exit=2 stdout_tail=critical resource governor",
        },
    ]
    (runtime_dir / "issue_agenda.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in issue_rows) + "\n",
        encoding="utf-8",
    )
    (runtime_dir / "cron_state.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("MAGI_OPERATIONAL_ACTIVE_ISSUE_WINDOW_SEC", "3600")
    monkeypatch.setattr(mod, "_is_omlx_switch_recovered", lambda: True)
    monkeypatch.setattr(mod, "_is_resource_governor_recovered", lambda: True)

    summary = _compute_operational_issue_health(tmp_path, now)

    assert summary["raw_cron_failures_24h"] == 2
    assert summary["active_cron_failures_24h"] == 0
    assert summary["recovered_cron_failures_24h"] == 2
    assert summary["inactive_or_noise_cron_failures_24h"] == 2
