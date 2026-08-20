from __future__ import annotations

import io
import json
import os
import stat
import sys
import threading
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


def _make_app(tmp_path: Path, monkeypatch, *, attachment_queue=None, mysql_connector_override=None):
    from api.blueprints.admin_runtime import create_admin_runtime_blueprint

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    # Bind every blueprint instance to this fixture's isolated state directory.
    # This must override any inherited runner/host MAGI_AGENT_DIR before the
    # production blueprint resolves its paths.
    monkeypatch.setenv("MAGI_AGENT_DIR", str(agent_dir))
    skill_dir = tmp_path / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    faiss_dir = tmp_path / "skills" / "memory" / "index_cache"
    faiss_dir.mkdir(parents=True)
    (faiss_dir / "meta.json").write_text(
        json.dumps({"total": 9, "index_type": "flat"}),
        encoding="utf-8",
    )
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
        mysql_connector=mysql_connector_override or _MysqlConnector,
        safe_remove_tmp=lambda path: Path(path).unlink(missing_ok=True),
        magi_root=tmp_path,
    )
    app.register_blueprint(bp)
    return app, orchestrator, product_updates


def test_safe_epoch_accepts_iso_strings_without_warning(caplog):
    from api.blueprints.admin_runtime import _safe_epoch

    caplog.clear()
    value = _safe_epoch("2026-05-26T06:30:45.824976")

    assert value > 0
    assert not caplog.records


def test_dashboard_nerv_health_status_and_logs(tmp_path, monkeypatch):
    import requests
    import subprocess as _subprocess

    hostile_agent_dir = tmp_path / "hostile-agent"
    hostile_agent_dir.mkdir()
    (hostile_agent_dir / "server.log").write_text("hostile\n", encoding="utf-8")
    monkeypatch.setenv("MAGI_AGENT_DIR", str(hostile_agent_dir))
    app, _, _ = _make_app(tmp_path, monkeypatch, attachment_queue=types.SimpleNamespace(stats=lambda: {"total": 2, "active": 1}))
    assert os.environ["MAGI_AGENT_DIR"] == str(tmp_path / ".agent")
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
    nas_mod.get_share_status = lambda share, vol: {
        "mounted": True,
        "mounted_path": vol,
        "fallback": False,
        "fallback_path": "",
        "available": True,
        "mode": "smb",
    }
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

    response = client.get("/api/status", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["nodes"]["casper"]["model"] == "gemma-4-e4b"

    response = client.get("/api/live-log?limit=1", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["lines"] == ["l2"]
    assert not (tmp_path / "casper.log").exists()

    response = client.get("/health", headers={"X-MAGI-API-KEY": "test-api-key"})
    health = response.get_json()
    assert response.status_code == (200 if health["status"] == "operational" else 503)
    assert health["status"] in {"operational", "degraded"}
    assert health["omlx"]["ok"] is True
    assert health["faiss"]["vectors"] == 9
    assert health["attachment_jobs"]["active"] == 1

    response = client.get("/health", headers={"Accept": "text/html", "X-MAGI-API-KEY": "test-api-key"})
    assert response.status_code == (200 if health["status"] == "operational" else 503)
    assert response.content_type.startswith("text/html")
    assert "MAGI 系統健康狀態" in response.get_data(as_text=True)
    assert response.get_json(silent=True) is None


def test_apple_vision_capability_uses_metadata_without_framework_import(monkeypatch):
    import api.blueprints.admin_runtime as admin_runtime

    monkeypatch.delitem(sys.modules, "skills.apple.apple_intelligence", raising=False)
    monkeypatch.delitem(sys.modules, "Vision", raising=False)
    monkeypatch.setattr(
        admin_runtime.importlib.util,
        "find_spec",
        lambda name: types.SimpleNamespace(name=name) if name == "Vision" else None,
    )

    capability = admin_runtime._apple_vision_capability_metadata()

    assert capability["available"] is True
    assert capability["probe"] == "module_metadata"
    assert capability["models"] == ["VNRecognizeTextRequest"]
    assert "Vision" not in sys.modules
    assert "skills.apple.apple_intelligence" not in sys.modules


def test_dashboard_nerv_health_cache_and_singleflight(tmp_path, monkeypatch):
    import requests
    import subprocess as _subprocess
    import api.blueprints.admin_runtime as admin_runtime

    monkeypatch.setenv("MAGI_NERV_HEALTH_CACHE_TTL_SEC", "5")
    monkeypatch.delenv("MAGI_LINE_WEBHOOK_ENDPOINT", raising=False)

    first_submit_started = threading.Event()
    release_first_submit = threading.Event()

    class _ImmediateFuture:
        def __init__(self, fn):
            try:
                self.value = fn()
                self.error = None
            except BaseException as exc:  # exercise Future.result semantics
                self.value = None
                self.error = exc

        def result(self, timeout=None):
            if self.error is not None:
                raise self.error
            return self.value

    class _CountingPool:
        def __init__(self):
            self.lock = threading.Lock()
            self.submits = 0

        def submit(self, fn):
            with self.lock:
                self.submits += 1
                submit_no = self.submits
            if submit_no == 1:
                first_submit_started.set()
                assert release_first_submit.wait(timeout=3)
            return _ImmediateFuture(fn)

    pool = _CountingPool()
    monkeypatch.setattr(admin_runtime, "io_pool", pool)

    def _fake_get(url, timeout=0):
        if url.endswith("/v1/models"):
            return types.SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "gemma-4-e4b"}]})
        if url.endswith("/api/tags"):
            return types.SimpleNamespace(status_code=200, json=lambda: {"models": []})
        return types.SimpleNamespace(status_code=200, json=lambda: {})

    monkeypatch.setattr(requests, "get", _fake_get)
    monkeypatch.setattr(
        _subprocess,
        "run",
        lambda *args, **kwargs: types.SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        admin_runtime,
        "_apple_vision_capability_metadata",
        lambda: {
            "available": True,
            "status": "online",
            "engine": "macOS Vision",
            "models": ["VNRecognizeTextRequest"],
            "count": 1,
            "probe": "module_metadata",
            "detail": "Vision module is installed",
        },
    )

    app, _, _ = _make_app(tmp_path, monkeypatch)
    responses = []

    def _request_health():
        with app.test_client() as client:
            response = client.get("/status/api/health", headers={"X-User-ID": "u1"})
            responses.append((response.status_code, response.get_json(), dict(response.headers)))

    first = threading.Thread(target=_request_health)
    second = threading.Thread(target=_request_health)
    first.start()
    assert first_submit_started.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    assert pool.submits == 1
    release_first_submit.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert pool.submits == 14
    assert sorted(status for status, _, _ in responses) == [200, 200]
    assert sorted(payload["cached"] for _, payload, _ in responses) == [False, True]
    assert all(payload["cache_ttl_seconds"] == 5.0 for _, payload, _ in responses)
    assert all(headers["Cache-Control"] == "no-store" for _, _, headers in responses)
    assert all(headers["Pragma"] == "no-cache" for _, _, headers in responses)

    with app.test_client() as client:
        cached_response = client.get("/status/api/health", headers={"X-User-ID": "u1"})
        cached = cached_response.get_json()
    assert cached["cached"] is True
    assert cached_response.headers["Cache-Control"] == "no-store"
    assert cached_response.headers["Pragma"] == "no-cache"
    assert pool.submits == 14


def test_dashboard_nerv_health_telemetry_payload(tmp_path, monkeypatch):
    import requests
    import urllib.request as _urllib_request

    app, _, _ = _make_app(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".runtime").mkdir(exist_ok=True)
    (tmp_path / ".runtime" / "cron_state.json").write_text(
        json.dumps({
            "job_tesla": {"last_run": "2026-06-18T10:00:00", "status": "ok", "detail": "手動切換"},
        }),
        encoding="utf-8",
    )
    (tmp_path / "casper.log").write_text("cron: job_tesla switch model to gemma-4-12B\n", encoding="utf-8")
    monkeypatch.setenv("MAGI_LINE_WEBHOOK_ENDPOINT", "https://example.test/line/webhook")

    def _fake_get(url, timeout=0):
        if url.endswith("/v1/models"):
            return types.SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "gemma-4-e4b"}]})
        if url.endswith("/health"):
            return types.SimpleNamespace(status_code=200, json=lambda: {})
        if url.endswith("/api/tags"):
            return types.SimpleNamespace(status_code=200, json=lambda: {"models": [{"name": "phi-4-mini-instruct-4bit"}]})
        raise AssertionError(url)

    monkeypatch.setattr(requests, "get", _fake_get)

    def _fake_urlopen(url, timeout=0):
        payload = json.dumps({"data": [{"id": "gemma-4-12b-it-4bit"}, {"id": "phi4-mini"}, {"id": "smolllm"}]})

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return payload.encode("utf-8")

        return _Resp()

    monkeypatch.setattr(_urllib_request, "urlopen", _fake_urlopen)

    import scripts.ops.omlx_profile_policy as policy
    monkeypatch.setattr(policy, "expected_profile_now", lambda: ("day", "e4b"))

    psutil_mod = types.ModuleType("psutil")
    psutil_mod.virtual_memory = lambda: types.SimpleNamespace(
        total=16 * 1024**3,
        available=8 * 1024**3,
        percent=50,
    )
    psutil_mod.swap_memory = lambda: types.SimpleNamespace(percent=10)
    psutil_mod.disk_usage = lambda path: types.SimpleNamespace(percent=18.0, free=220 * 1024**3)
    psutil_mod.cpu_percent = lambda interval=0.1: 15.0
    monkeypatch.setitem(sys.modules, "psutil", psutil_mod)

    import subprocess as _subprocess
    monkeypatch.setattr(_subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1))

    client = app.test_client()
    response = client.get("/dashboard/nerv/api/health", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    data = response.get_json()
    assert "telemetry" in data

    telemetry = data["telemetry"]
    assert "system" in telemetry and "inference" in telemetry and "activity" in telemetry and "pressure" in telemetry
    assert telemetry["system"]["loadavg"]
    assert telemetry["system"]["uptime_seconds"] > 0
    assert telemetry["system"]["memory"]["pressure"] in {"ok", "warn", "critical"}
    assert telemetry["system"]["swap"]["percent"] == 10

    inference = telemetry["inference"]
    assert inference["active_profile"] in {"day", "day-e4b-degraded"}
    assert inference["active_profile_expected"] == "e4b"
    assert isinstance(inference["available_models"], list)
    assert inference["sidecars"]["phi4"]["status"] in {"online", "offline"}
    assert inference["sidecars"]["smol"]["status"] in {"online", "offline"}
    assert inference["summary"]["status"] in {"ok", "warn", "critical", "unknown"}

    activity = telemetry["activity"]
    assert isinstance(activity["events"], list)
    assert activity["count"] == len(activity["events"])
    assert any(event.get("source") == "cron_state" for event in activity["events"])

    pressure = telemetry["pressure"]
    assert pressure["level"] in {"ok", "warn", "critical"}
    assert isinstance(pressure["reasons"], list)


def test_dashboard_nerv_ignores_historical_swap_when_macos_memory_pressure_is_ok(tmp_path, monkeypatch):
    import urllib.request as _urllib_request
    import subprocess as _subprocess

    app, _, _ = _make_app(tmp_path, monkeypatch)

    def _fake_urlopen(url, timeout=0):
        payload = json.dumps({"data": [{"id": "gemma-4-12b-it-4bit"}]})

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return payload.encode("utf-8")

        return _Resp()

    monkeypatch.setattr(_urllib_request, "urlopen", _fake_urlopen)

    import scripts.ops.omlx_profile_policy as policy
    monkeypatch.setattr(policy, "expected_profile_now", lambda: ("night", "12b"))

    psutil_mod = types.ModuleType("psutil")
    psutil_mod.virtual_memory = lambda: types.SimpleNamespace(
        total=24 * 1024**3,
        available=18 * 1024**3,
        percent=25,
    )
    psutil_mod.swap_memory = lambda: types.SimpleNamespace(percent=93.6)
    psutil_mod.disk_usage = lambda path: types.SimpleNamespace(percent=18.0, free=220 * 1024**3)
    psutil_mod.cpu_percent = lambda interval=0.1: 12.0
    monkeypatch.setitem(sys.modules, "psutil", psutil_mod)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 1.0, 1.0), raising=False)
    monkeypatch.setattr(
        _subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=0,
            stdout="System-wide memory free percentage: 81%\n",
            stderr="",
        ),
    )

    response = app.test_client().get("/dashboard/nerv/api/health", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    telemetry = response.get_json()["telemetry"]
    assert telemetry["system"]["macos_memory_pressure"]["status"] == "ok"
    assert telemetry["system"]["swap"]["status"] == "historical"
    assert telemetry["pressure"]["level"] == "ok"
    assert any("macOS memory pressure is healthy" in item for item in telemetry["pressure"]["reasons"])


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
    nas_mod.get_share_status = lambda share, vol: {
        "mounted": True,
        "mounted_path": vol,
        "fallback": False,
        "fallback_path": "",
        "available": True,
        "mode": "smb",
    }
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

    response = client.get("/health", headers={"X-MAGI-API-KEY": "test-api-key"})
    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "degraded"
    assert "smol" in body["omlx"]["unmanaged_alive"]
    assert body["omlx"]["services"]["smol"]["port"] == 8083
    assert body["omlx"]["services"]["smol"]["management_state"] == "unmanaged"


def test_health_route_caches_heavy_probes(tmp_path, monkeypatch):
    import subprocess as _subprocess

    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    calls: list[str] = []

    def _fake_session_get(url, timeout=0):
        calls.append(url)
        return types.SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "gemma-4-e4b"}]})

    http_pool = types.ModuleType("skills.bridge.http_pool")
    http_pool.get_session = lambda: types.SimpleNamespace(get=_fake_session_get)
    monkeypatch.setitem(sys.modules, "skills.bridge.http_pool", http_pool)

    apple_mod = types.ModuleType("skills.apple.apple_intelligence")
    apple_mod.VISION_AVAILABLE = True
    monkeypatch.setitem(sys.modules, "skills.apple.apple_intelligence", apple_mod)

    browser_mod = types.ModuleType("skills.engine.playwright_wrapper")
    browser_mod.playwright_chromium_health = lambda **kwargs: {"ok": True, "engine": "playwright-chromium"}
    monkeypatch.setitem(sys.modules, "skills.engine.playwright_wrapper", browser_mod)

    faiss_mod = types.ModuleType("skills.memory.faiss_index")
    faiss_mod.FAISSMemoryIndex = types.SimpleNamespace(get_instance=lambda: types.SimpleNamespace(total=9))
    monkeypatch.setitem(sys.modules, "skills.memory.faiss_index", faiss_mod)

    nas_mod = types.ModuleType("api.nas_mount_guard")
    nas_mod.get_configured_shares = lambda refresh=False: [("homes", "/Volumes/homes")]
    nas_mod.get_share_status = lambda share, vol: {"mounted": True}
    nas_mod.ensure_nas_mounts = lambda: {"homes": True}
    monkeypatch.setitem(sys.modules, "api.nas_mount_guard", nas_mod)

    psutil_mod = types.ModuleType("psutil")
    psutil_mod.virtual_memory = lambda: types.SimpleNamespace(percent=50, available=8 * 1024**3)
    psutil_mod.disk_usage = lambda path: types.SimpleNamespace(percent=20, free=100 * 1024**3)
    psutil_mod.cpu_percent = lambda interval=0.1: 12.5
    monkeypatch.setitem(sys.modules, "psutil", psutil_mod)

    monkeypatch.setattr(_subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(returncode=0))

    first = client.get("/health", headers={"X-MAGI-API-KEY": "test-api-key"})
    first_calls = len(calls)
    first_json = first.get_json()
    assert first.status_code == (200 if first_json["status"] == "operational" else 503)
    assert first_json["cached"] is False
    assert first_json["probe"] == "aggregate_health"
    assert first_json["health_contract"]["readiness"] == "/readyz"
    assert first_json["readiness"]["status"] == "not_checked"
    assert first_calls >= 1

    second = client.get("/health", headers={"X-MAGI-API-KEY": "test-api-key"})
    second_json = second.get_json()
    assert second.status_code == (200 if second_json["status"] == "operational" else 503)
    assert second_json["cached"] is True
    assert len(calls) == first_calls

    fresh = client.get("/health?fresh=1", headers={"X-MAGI-API-KEY": "test-api-key"})
    fresh_json = fresh.get_json()
    assert fresh.status_code == (200 if fresh_json["status"] == "operational" else 503)
    assert fresh_json["cached"] is False
    assert len(calls) > first_calls


def test_health_route_marks_db_failure_degraded(tmp_path, monkeypatch):
    import subprocess as _subprocess

    class _FailingMysql:
        @staticmethod
        def connect(**_kwargs):
            raise RuntimeError("db offline")

    app, _, _ = _make_app(tmp_path, monkeypatch, mysql_connector_override=_FailingMysql)
    client = app.test_client()
    runtime = tmp_path / ".runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "operational_hardening_audit_latest.json").write_text(
        json.dumps({"cron": {"parse_failure_count": 0, "collision_count": 0}, "git": {}}),
        encoding="utf-8",
    )
    http_pool = types.ModuleType("skills.bridge.http_pool")
    http_pool.get_session = lambda: types.SimpleNamespace(get=lambda url, timeout=0: types.SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "gemma-4-e4b"}]}))
    monkeypatch.setitem(sys.modules, "skills.bridge.http_pool", http_pool)
    apple_mod = types.ModuleType("skills.apple.apple_intelligence")
    apple_mod.VISION_AVAILABLE = True
    monkeypatch.setitem(sys.modules, "skills.apple.apple_intelligence", apple_mod)
    browser_mod = types.ModuleType("skills.engine.playwright_wrapper")
    browser_mod.playwright_chromium_health = lambda **kwargs: {"ok": True}
    monkeypatch.setitem(sys.modules, "skills.engine.playwright_wrapper", browser_mod)
    nas_mod = types.ModuleType("api.nas_mount_guard")
    nas_mod.get_configured_shares = lambda refresh=False: []
    nas_mod.get_share_status = lambda share, vol: {"mounted": True}
    monkeypatch.setitem(sys.modules, "api.nas_mount_guard", nas_mod)
    monkeypatch.setattr(_subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(returncode=0))

    response = client.get("/health?fresh=1", headers={"X-MAGI-API-KEY": "test-api-key"})
    body = response.get_json()

    assert response.status_code == 503
    assert body["db"]["ok"] is False
    assert body["status"] == "degraded"


def test_health_api_token_health_exposes_google_calendar_service(tmp_path, monkeypatch):
    import subprocess as _subprocess

    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()
    token_dir = tmp_path / ".runtime" / "token_health"
    token_dir.mkdir(parents=True)
    report_path = token_dir / "token_health_latest.json"
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "generated_at": "2026-07-09T01:00:00+00:00",
                "summary": {"total": 1, "failures": 0, "refreshed": 0, "skipped": 0},
                "checks": [
                    {
                        "kind": "google_oauth",
                        "name": "google_calendar",
                        "ok": True,
                        "status": "ok",
                        "message": "token valid",
                        "path": "/tmp/calendar-token.json",
                        "credentials_path": "/tmp/credentials.json",
                        "expires_at": "2099-01-01T00:00:00+00:00",
                        "expires_in_hours": 640000,
                        "refresh_token_present": True,
                        "scopes_ok": True,
                        "account_check_status": "not_verifiable_primary_hint",
                        "required": True,
                    }
                ],
                "failures": [],
            }
        ),
        encoding="utf-8",
    )
    http_pool = types.ModuleType("skills.bridge.http_pool")
    http_pool.get_session = lambda: types.SimpleNamespace(get=lambda url, timeout=0: types.SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "gemma-4-e4b"}]}))
    monkeypatch.setitem(sys.modules, "skills.bridge.http_pool", http_pool)
    apple_mod = types.ModuleType("skills.apple.apple_intelligence")
    apple_mod.VISION_AVAILABLE = True
    monkeypatch.setitem(sys.modules, "skills.apple.apple_intelligence", apple_mod)
    browser_mod = types.ModuleType("skills.engine.playwright_wrapper")
    browser_mod.playwright_chromium_health = lambda **kwargs: {"ok": True}
    monkeypatch.setitem(sys.modules, "skills.engine.playwright_wrapper", browser_mod)
    nas_mod = types.ModuleType("api.nas_mount_guard")
    nas_mod.get_configured_shares = lambda refresh=False: []
    nas_mod.get_share_status = lambda share, vol: {"mounted": True}
    monkeypatch.setitem(sys.modules, "api.nas_mount_guard", nas_mod)
    monkeypatch.setattr(_subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(returncode=0))

    body = client.get("/health?fresh=1", headers={"X-MAGI-API-KEY": "test-api-key"}).get_json()

    token_health = body["api_token_health"]
    assert token_health["report_path"] == str(report_path)
    assert token_health["generated_at"] == "2026-07-09T01:00:00+00:00"
    assert token_health["services"]["google_calendar"]["token_path"] == "/tmp/calendar-token.json"
    assert token_health["services"]["google_calendar"]["scopes_ok"] is True


def test_health_drive_sync_auth_marker_self_heal(tmp_path, monkeypatch):
    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    class _AuthReq(RuntimeError):
        pass

    class _DriveService:
        def __init__(self, *, succeed: bool):
            self.succeed = succeed

        def files(self):
            succeed = self.succeed

            class _Files:
                def get(self, **_kwargs):
                    if not succeed:
                        raise _AuthReq("token-expired")

                    class _Request:
                        def execute(self):
                            return {"id": "root"}

                    return _Request()

            return _Files()

    def _probe_service_factory(*, write: bool):
        return _DriveService(succeed=True)

    drive_mod = types.ModuleType("api.osc.drive_case_sync")
    drive_mod.DriveCaseSyncAuthRequired = _AuthReq
    drive_mod.build_drive_service = lambda interactive=False, force_auth=False, write=False: _probe_service_factory(write=write)
    monkeypatch.setitem(sys.modules, "api.osc.drive_case_sync", drive_mod)

    marker_path = tmp_path / ".runtime" / "drive_sync" / "drive_case_sync_auth_required_latest.json"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps({"message": "old marker", "token_path": "/tmp/token", "write_scope": False}), encoding="utf-8")
    worker_state_path = marker_path.with_name("worker_state.json")
    worker_state_path.write_text(
        json.dumps(
            {
                "last_status": {
                    "ok": False,
                    "status": "auth_required",
                    "action_required": True,
                    "message": "old marker",
                    "token_path": "/tmp/token",
                    "write_scope": False,
                },
                "last_summary": {"auth_required": True},
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/health?fresh=1", headers={"X-MAGI-API-KEY": "test-api-key"})
    health = response.get_json()
    assert response.status_code == (200 if health["status"] == "operational" else 503)
    assert health["drive_sync"]["ok"] is True
    assert health["drive_sync"]["status"] == "ok"
    assert not marker_path.exists()
    worker_state = json.loads(worker_state_path.read_text(encoding="utf-8"))
    assert worker_state["last_status"]["ok"] is True
    assert worker_state["last_status"]["action_required"] is False
    assert worker_state["last_summary"]["auth_required"] is False
    assert worker_state["last_summary"]["auth_recovered"] is True


def test_health_drive_sync_auth_marker_stays_when_still_required(tmp_path, monkeypatch):
    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    class _AuthReq(RuntimeError):
        pass

    drive_mod = types.ModuleType("api.osc.drive_case_sync")
    drive_mod.DriveCaseSyncAuthRequired = _AuthReq

    def _raise_auth(interactive=False, force_auth=False, write=False):
        raise _AuthReq("still required")

    drive_mod.build_drive_service = _raise_auth
    monkeypatch.setitem(sys.modules, "api.osc.drive_case_sync", drive_mod)

    marker_path = tmp_path / ".runtime" / "drive_sync" / "drive_case_sync_auth_required_latest.json"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps({"message": "old marker", "token_path": "/tmp/token", "write_scope": True}),
        encoding="utf-8",
    )

    response = client.get("/health?fresh=1", headers={"X-MAGI-API-KEY": "test-api-key"})
    health = response.get_json()
    assert response.status_code == (200 if health["status"] == "operational" else 503)
    assert health["drive_sync"]["ok"] is False
    assert health["drive_sync"]["status"] == "auth_required"
    assert marker_path.exists()


def test_health_drive_sync_prefers_newer_kind_status_over_old_partial(tmp_path, monkeypatch):
    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    drive_mod = types.ModuleType("api.osc.drive_case_sync")
    drive_mod.DriveCaseSyncAuthRequired = RuntimeError
    drive_mod.build_drive_service = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "api.osc.drive_case_sync", drive_mod)

    drive_dir = tmp_path / ".runtime" / "drive_sync"
    drive_dir.mkdir(parents=True)
    (drive_dir / "worker_state.json").write_text(
        json.dumps(
            {
                "last_status": {
                    "ok": False,
                    "status": "partial_failure",
                    "action_required": True,
                    "finished_at": "2026-07-01T08:12:48+08:00",
                    "message": "old partial",
                },
                "last_summary": {"matched_case_folders": 23},
            }
        ),
        encoding="utf-8",
    )
    (drive_dir / "drive_case_sync_worker_status_all_files_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "status": "already_running",
                "action_required": False,
                "pid": os.getpid(),
                "worker_kind": "all_files",
                "started_at": "2026-07-01T12:22:21+08:00",
                "finished_at": "2026-07-01T12:22:21+08:00",
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/health?fresh=1", headers={"X-MAGI-API-KEY": "test-api-key"})
    health = response.get_json()

    assert response.status_code == (200 if health["status"] == "operational" else 503)
    assert health["drive_sync"]["ok"] is True
    assert health["drive_sync"]["status"] == "already_running"
    assert health["drive_sync"]["worker_kind"] == "all_files"
    assert health["drive_sync"]["status_by_kind"]["all_files"]["status"] == "already_running"


def test_health_drive_sync_accepts_sla_healthy_priority_when_latest_all_files_partial(tmp_path, monkeypatch):
    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    drive_mod = types.ModuleType("api.osc.drive_case_sync")
    drive_mod.DriveCaseSyncAuthRequired = RuntimeError
    drive_mod.build_drive_service = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "api.osc.drive_case_sync", drive_mod)
    monkeypatch.setenv("MAGI_DRIVE_SYNC_HEALTH_SLA_HOURS", "24")

    drive_dir = tmp_path / ".runtime" / "drive_sync"
    drive_dir.mkdir(parents=True)
    fresh_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    (drive_dir / "worker_state.json").write_text(
        json.dumps(
            {
                "last_status": {
                    "ok": False,
                    "status": "partial_failure",
                    "action_required": True,
                    "worker_kind": "all_files",
                    "started_at": fresh_iso,
                    "message": "latest all-files partial",
                },
                "last_summary": {"matched_case_folders": 95},
            }
        ),
        encoding="utf-8",
    )
    (drive_dir / "drive_case_sync_worker_status_priority_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "status": "ok",
                "action_required": False,
                "pid": 73028,
                "worker_kind": "priority",
                "started_at": fresh_iso,
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/health?fresh=1", headers={"X-MAGI-API-KEY": "test-api-key"})
    health = response.get_json()

    assert response.status_code == (200 if health["status"] == "operational" else 503)
    assert health["drive_sync"]["ok"] is False
    assert health["drive_sync"]["status"] == "kind_action_required"
    assert health["drive_sync"]["worker_kind"] == "priority"
    assert health["drive_sync"]["latest_status"] == "partial_failure"
    assert health["drive_sync"]["status_by_kind"]["priority"]["status"] == "ok"
    assert "all_files" in health["drive_sync"]["blocking_kinds"]


def test_health_drive_sync_semantic_collisions_are_safe_waiting_not_outage(tmp_path, monkeypatch):
    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    drive_mod = types.ModuleType("api.osc.drive_case_sync")
    drive_mod.DriveCaseSyncAuthRequired = RuntimeError
    drive_mod.build_drive_service = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "api.osc.drive_case_sync", drive_mod)
    monkeypatch.setenv("MAGI_DRIVE_SYNC_HEALTH_SLA_HOURS", "24")

    drive_dir = tmp_path / ".runtime" / "drive_sync"
    drive_dir.mkdir(parents=True)
    fresh_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    status = {
        "ok": False,
        "status": "partial_failure",
        "action_required": True,
        "pid": 73029,
        "worker_kind": "all_files",
        "started_at": fresh_iso,
        "finished_at": fresh_iso,
        "file_sync_summary": {
            "semantic_collision_files": 125,
            "case_errors": 1,
            "incomplete_case_scans": 0,
            "conflict_files": 0,
            "content_mismatch_files": 0,
        },
        "execution_summary": {
            "download_attempted": 0,
            "download_failed": 0,
            "upload_attempted": 0,
            "upload_failed": 0,
        },
        "drive_folder_summary": {"failed": 0},
        "drive_imported_folder_repair": {"errors": 0},
    }
    (drive_dir / "worker_state.json").write_text(
        json.dumps(
            {
                "last_status": {
                    key: value
                    for key, value in status.items()
                    if key
                    not in {
                        "file_sync_summary",
                        "execution_summary",
                        "drive_folder_summary",
                        "drive_imported_folder_repair",
                    }
                },
                "last_summary": {"matched_case_folders": 1},
            }
        ),
        encoding="utf-8",
    )
    (drive_dir / "drive_case_sync_worker_status_all_files_latest.json").write_text(
        json.dumps(status),
        encoding="utf-8",
    )

    response = client.get("/health?fresh=1", headers={"X-MAGI-API-KEY": "test-api-key"})
    health = response.get_json()

    assert health["drive_sync"]["ok"] is True
    assert health["drive_sync"]["status"] == "ok_with_waiting"
    assert health["drive_sync"]["waiting"] is True
    assert health["drive_sync"]["semantic_collision_files"] == 125
    assert health["drive_sync"]["blocking_kinds"] == []
    assert health["drive_sync"]["status_by_kind"]["all_files"]["waiting"] is True
    assert health["drive_sync"]["status_by_kind"]["all_files"]["write_failures"] == 0
    assert "0 上傳失敗" in health["drive_sync"]["detail"]

    html_response = client.get(
        "/health",
        headers={
            "Accept": "text/html",
            "X-MAGI-API-KEY": "test-api-key",
        },
    )
    html = html_response.get_data(as_text=True)
    assert html_response.content_type.startswith("text/html")
    assert "安全等待" in html
    assert "magi-theme.css" in html
    assert "data-magi-theme-toggle" in html


def test_health_drive_sync_ignores_stale_inactive_inventory_kind(tmp_path, monkeypatch):
    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    drive_mod = types.ModuleType("api.osc.drive_case_sync")
    drive_mod.DriveCaseSyncAuthRequired = RuntimeError
    drive_mod.build_drive_service = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "api.osc.drive_case_sync", drive_mod)
    monkeypatch.setenv("MAGI_DRIVE_SYNC_HEALTH_SLA_HOURS", "24")

    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_drive_case_sync_bidirectional",
                    "enabled": True,
                    "command": "python scripts/drive_case_sync_worker.py --matched-case-limit 8",
                },
                {
                    "id": "job_drive_case_sync_all_files",
                    "enabled": True,
                    "command": "python scripts/drive_case_sync_worker.py --direct-all-cases",
                },
                {
                    "id": "job_drive_case_sync_nightly",
                    "enabled": False,
                    "command": "python scripts/drive_case_sync_inventory.py --file-diff",
                },
            ]
        ),
        encoding="utf-8",
    )
    drive_dir = tmp_path / ".runtime" / "drive_sync"
    drive_dir.mkdir(parents=True)
    fresh_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    stale_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 48 * 3600))
    (drive_dir / "worker_state.json").write_text(
        json.dumps(
            {
                "last_status": {
                    "ok": True,
                    "status": "ok",
                    "action_required": False,
                    "worker_kind": "all_files",
                    "finished_at": fresh_iso,
                },
                "last_summary": {"matched_case_folders": 1},
                "status_by_kind": {
                    "all_files": {
                        "ok": True,
                        "status": "ok",
                        "action_required": False,
                        "worker_kind": "all_files",
                        "finished_at": fresh_iso,
                    },
                    "priority": {
                        "ok": True,
                        "status": "ok",
                        "action_required": False,
                        "worker_kind": "priority",
                        "finished_at": fresh_iso,
                    },
                    "inventory": {
                        "ok": True,
                        "status": "ok",
                        "action_required": False,
                        "worker_kind": "inventory",
                        "finished_at": stale_iso,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/health?fresh=1", headers={"X-MAGI-API-KEY": "test-api-key"})
    health = response.get_json()

    assert response.status_code == (200 if health["status"] == "operational" else 503)
    assert health["drive_sync"]["ok"] is True
    assert health["drive_sync"]["blocking_kinds"] == []
    assert health["drive_sync"]["active_kinds"] == ["all_files", "priority"]
    assert health["drive_sync"]["inactive_kinds"] == ["inventory"]
    assert health["drive_sync"]["status_by_kind"]["inventory"]["stale"] is True


def test_system_self_repair_and_transcribe_routes(tmp_path, monkeypatch):
    request_tmp = tmp_path / "request-tmp"
    request_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(request_tmp))
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
    transcribe_call = {}

    def _transcribe(path, language=None, taigi_hint=False):
        transcribe_call["path"] = path
        return {"text": "ok", "language": language, "taigi_hint": taigi_hint}

    transcribe_mod.transcribe = _transcribe
    monkeypatch.setitem(sys.modules, "skills.bridge.balthasar_bridge", transcribe_mod)

    response = client.post("/api/system-test", headers={"X-User-ID": "u1"}, json={"confirm": "system-test"})
    assert response.status_code == 200
    assert response.get_json()["passed"] == 12

    response = client.post(
        "/api/self-repair",
        headers={"X-User-ID": "u1"},
        json={"targets": ["a"], "confirm": "self-repair"},
    )
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
    uploaded = Path(transcribe_call["path"])
    assert uploaded.parent == request_tmp
    assert not uploaded.exists()


def test_admin_mutation_routes_require_admin_and_confirmation(tmp_path, monkeypatch):
    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    response = client.post(
        "/api/system-test",
        headers={"X-User-ID": "u1", "X-User-Role": "viewer"},
        json={"confirm": "system-test"},
    )
    assert response.status_code == 403

    response = client.post("/api/system-test", headers={"X-User-ID": "u1"}, json={})
    assert response.status_code == 400
    assert response.get_json()["error"] == "confirmation_required"

    response = client.post("/api/self-repair", headers={"X-User-ID": "u1"}, json={"targets": ["a"]})
    assert response.status_code == 400
    assert response.get_json()["error"] == "confirmation_required"


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
    def _update_skill_document(skill_name, content, reason="nerv_edit"):
        path = tmp_path / "skills" / skill_name / "SKILL.md"
        normalized = str(content).rstrip() + "\n"
        path.write_text(normalized, encoding="utf-8")
        return {"success": True, "path": str(path), "content": normalized, "snapshot": {"version_id": "pre-edit"}}
    genesis_mod.update_skill_document = _update_skill_document
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

    registry_calls = []
    plugin_mod = types.ModuleType("skills.plugin")
    plugin_mod.skill_registry = types.SimpleNamespace(discover=lambda force=False: registry_calls.append(force))
    monkeypatch.setitem(sys.modules, "skills.plugin", plugin_mod)

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
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    env_backups = list(tmp_path.glob(".env.bak-*"))
    assert env_backups
    assert stat.S_IMODE(env_backups[0].stat().st_mode) == 0o600

    response = client.get("/api/nerv/skills/demo-skill", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["skill"]["has_skill_doc"] is True

    response = client.post("/api/nerv/skills/demo-skill", headers={"X-User-ID": "u1"}, json={"content": "# Updated"})
    assert response.status_code == 200
    assert "Updated" in response.get_json()["skill"]["summary"]
    assert response.get_json()["skill"]["snapshot"]["version_id"] == "pre-edit"
    assert registry_calls == [True, True]


def test_nerv_heavy_runtime_v3_stages_secret_for_controlled_rebind(tmp_path, monkeypatch):
    active_env = tmp_path / "active-hash-bound.env"
    active_env.write_text("NVIDIA_NIM_ENABLE=0\n", encoding="utf-8")
    active_before = active_env.read_bytes()
    pending = tmp_path / "shared" / "runtime" / "pending-config" / "env_updates.json"
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-nerv-test")
    monkeypatch.setenv("MAGI_ENV_FILE", str(active_env))
    monkeypatch.setenv("MAGI_PENDING_ENV_UPDATE_FILE", str(pending))
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_ENABLE", raising=False)
    app, _, _ = _make_app(tmp_path, monkeypatch)

    secret = "nv" + "api-v3-pending-secret-123456"
    response = app.test_client().post(
        "/api/nerv/heavy-runtime",
        headers={"X-User-ID": "u1"},
        json={"enabled": True, "api_key": secret},
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["saved"] is False
    assert payload["status"] == "pending_controlled_rebind"
    assert payload["requires_controlled_redeploy_or_rebind"] is True
    assert secret not in response.get_data(as_text=True)
    assert active_env.read_bytes() == active_before
    staged = json.loads(pending.read_text(encoding="utf-8"))
    assert staged["requested_by"] == "nerv_heavy_runtime"
    assert staged["updates"]["NVIDIA_NIM_API_KEY"] == secret
    assert os.environ.get("NVIDIA_NIM_API_KEY") != secret


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
                stdout=json.dumps({"Self": {"TailscaleIPs": ["100.64.1.2"], "DNSName": "magi.tailnet.test."}}),
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
    assert data["tailscale"]["ip"] == "100.64.1.2"
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
    import api.blueprints.admin_runtime as mod

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
            "error": "磁碟低水位告警：可用空間 12.0 GB（閾值 30.0 GB）。",
            "context": {"free_gb": 12.0, "threshold_gb": 30.0, "severity": "High"},
        },
    ]
    issue_path = runtime_dir / "issue_agenda.jsonl"
    issue_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in issue_rows) + "\n",
        encoding="utf-8",
    )

    cron_state = {
        "job_debug_cleanup": {"last_success_at": str(now - 100)},
        "job_obsidian_ingest": {"last_run": str(now - 400)},
    }
    (runtime_dir / "cron_state.json").write_text(
        json.dumps(cron_state, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setenv("MAGI_OPERATIONAL_ACTIVE_ISSUE_WINDOW_SEC", "3600")
    monkeypatch.setattr(
        mod.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 50 * 1024 * 1024 * 1024})(),
    )
    summary = _compute_operational_issue_health(tmp_path, now)
    assert summary["raw_cron_failures_24h"] == 5
    assert summary["active_cron_failures_24h"] == 1
    assert summary["active_distinct_jobs_24h"] == 1
    assert summary["false_positive_cron_failures_24h"] == 1
    assert summary["active_high_severity_24h"] == 1
    assert summary["inactive_cron_failures_24h"] == 3
    assert summary["recovered_cron_failures_24h"] == 1
    assert summary["superseded_cron_failures_24h"] == 1
    assert summary["stale_cron_failures_24h"] == 1
    assert summary["recovered_non_cron_high_severity_24h"] == 1
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
        {
            "ts": now - 30,
            "severity": "High",
            "source": "discord_bot.cron_scheduler",
            "command": "cron:job_operational_hardening_audit",
            "error": "exit=1 stdout_tail={\"stale_runtime_lock_count\":1}",
        },
    ]
    (runtime_dir / "issue_agenda.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in issue_rows) + "\n",
        encoding="utf-8",
    )
    (runtime_dir / "cron_state.json").write_text("{}", encoding="utf-8")
    (runtime_dir / "operational_hardening_audit_latest.json").write_text(
        json.dumps(
            {
                "cron": {"parse_failure_count": 0, "collision_count": 0},
                "stale_runtime_locks": {"stale_count": 0},
                "silent_exception_handlers": {"critical_count": 0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("MAGI_OPERATIONAL_ACTIVE_ISSUE_WINDOW_SEC", "3600")
    monkeypatch.setattr(mod, "_is_omlx_switch_recovered", lambda **_kwargs: True)
    monkeypatch.setattr(mod, "_is_resource_governor_recovered", lambda: True)

    summary = _compute_operational_issue_health(tmp_path, now)

    assert summary["raw_cron_failures_24h"] == 3
    assert summary["active_cron_failures_24h"] == 0
    assert summary["recovered_cron_failures_24h"] == 3
    assert summary["inactive_or_noise_cron_failures_24h"] == 3


def test_live_validation_endpoint_payload_and_fast_checks(tmp_path, monkeypatch):
    import subprocess as _subprocess

    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    class _AuthReq(RuntimeError):
        pass

    class _DriveService:
        def files(self):
            class _Files:
                def get(self, **_kwargs):
                    class _Request:
                        def execute(self):
                            return {"id": "root"}

                    return _Request()

            return _Files()

    drive_mod = types.ModuleType("api.osc.drive_case_sync")
    drive_mod.DriveCaseSyncAuthRequired = _AuthReq
    drive_mod.build_drive_service = lambda interactive=False, force_auth=False, write=False: _DriveService()
    drive_mod.load_case_exclusion_payload = lambda include_env=False: {"updated_at": "2026-06-16T00:00:00", "relative_paths": [], "reason": ""}
    drive_mod.sync_case_exclusions = lambda relative_paths, reason="": {"updated_at": "", "relative_paths": [], "reason": reason}
    drive_mod.unsync_case_exclusions = lambda paths: ({}, 0)
    monkeypatch.setitem(sys.modules, "api.osc.drive_case_sync", drive_mod)

    status_file = tmp_path / ".runtime" / "drive_sync" / "worker_state.json"
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(
        json.dumps(
            {
                "last_status": {"ok": True, "status": "ok", "action_required": False},
                "last_summary": {"auth_required": False},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        _subprocess,
        "run",
        lambda args, **kwargs: types.SimpleNamespace(
            returncode=0,
            stdout="\n".join(
                [
                    "/usr/bin/python daemon.py",
                    "/usr/bin/python api/server.py",
                ]
            ),
            stderr="",
        ),
    )

    nas_mod = types.ModuleType("api.nas_mount_guard")
    nas_mod.get_configured_shares = lambda refresh=False: [("homes", "/Volumes/homes")]
    nas_mod.get_share_status = lambda share, vol: {"mounted": share == "homes"}
    monkeypatch.setitem(sys.modules, "api.nas_mount_guard", nas_mod)

    http_pool = types.ModuleType("skills.bridge.http_pool")
    http_pool.get_session = lambda: types.SimpleNamespace(get=lambda url, timeout=0: types.SimpleNamespace(status_code=200))
    monkeypatch.setitem(sys.modules, "skills.bridge.http_pool", http_pool)

    response = client.get("/api/live-validation", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] in {"operational", "degraded"}
    assert set(payload.keys()) >= {"daemon", "server", "tools_api", "nas", "drive", "db", "model", "summary", "timestamp"}
    assert payload["daemon"]["ok"] is True
    assert payload["server"]["ok"] is True
    assert payload["summary"]["ok"] is True
    assert payload["summary"]["status"] == payload["status"]


def test_drive_case_exclusions_endpoints_add_list_remove(tmp_path, monkeypatch):
    app, _, _ = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    store_path = tmp_path / ".runtime" / "drive_sync" / "case_exclusions.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "updated_at": "2026-06-01T00:00:00",
                "reason": "initial",
                "relative_paths": ["一般案件/Lumi/測試保留", "一般案件/Lumi/測試移除"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    list_resp = client.get("/api/drive-case-exclusions", headers={"X-User-ID": "u1"})
    assert list_resp.status_code == 200
    list_data = list_resp.get_json()
    assert list_data["ok"] is True
    assert list_data["count"] == 2
    assert "一般案件/Lumi/測試保留" in list_data["relative_paths"]

    add_resp = client.post(
        "/api/drive-case-exclusions",
        headers={"X-User-ID": "u1"},
        json={
            "relative_paths": ["一般案件/Lumi/測試新增", " 一般案件/Lumi/測試新增  ", "一般案件/Lumi/測試保留"],
            "reason": "ui-add",
        },
    )
    assert add_resp.status_code == 200
    add_data = add_resp.get_json()
    assert add_data["ok"] is True
    assert add_data["changed"] is True
    assert len(add_data["relative_paths"]) == 3
    assert "一般案件/Lumi/測試新增" in add_data["relative_paths"]

    remove_resp = client.delete(
        "/api/drive-case-exclusions",
        headers={"X-User-ID": "u1"},
        json={"relative_path": " 一般案件/Lumi/測試保留 "},
    )
    assert remove_resp.status_code == 200
    remove_data = remove_resp.get_json()
    assert remove_data["ok"] is True
    assert remove_data["changed"] is True
    assert remove_data["removed"] == 1
    assert "一般案件/Lumi/測試保留" not in remove_data["relative_paths"]
    assert "一般案件/Lumi/測試移除" in remove_data["relative_paths"]

    noop_remove = client.delete(
        "/api/drive-case-exclusions",
        headers={"X-User-ID": "u1"},
        json={"relative_path": "一般案件/Lumi/沒有這筆"},
    )
    assert noop_remove.status_code == 200
    noop_data = noop_remove.get_json()
    assert noop_data["ok"] is True
    assert noop_data["changed"] is False
    assert noop_data["removed"] == 0
    assert len(noop_data["relative_paths"]) == 2


def test_remaining_admin_mutations_succeed_with_disposable_adapters(tmp_path, monkeypatch):
    """Certify admin mutations without production DB, network, or /tmp writes."""
    import werkzeug.datastructures

    calls = []
    app, _, _ = _make_app(tmp_path, monkeypatch)

    system_test_mod = types.ModuleType("skills.ops.system_test")
    system_test_mod.run_all_tests = lambda: calls.append(("system_test", None)) or {"ok": True, "passed": 3}
    monkeypatch.setitem(sys.modules, "skills.ops.system_test", system_test_mod)

    repair_dir = tmp_path / "skills" / "magi-self-repair"
    repair_dir.mkdir(parents=True)
    (repair_dir / "action.py").write_text(
        "def repair_targets(targets):\n    return {'ok': True, 'targets': targets, 'fixture': True}\n",
        encoding="utf-8",
    )

    llm_direct_mod = types.ModuleType("skills.bridge.llm_direct")
    llm_direct_mod.apply_manual_command = lambda command, features=None: calls.append(
        ("codex_toggle", (command, features))
    )
    llm_direct_mod.public_status_report = lambda: {"enabled": True, "fixture": True}
    monkeypatch.setitem(sys.modules, "skills.bridge.llm_direct", llm_direct_mod)

    transcribe_mod = types.ModuleType("skills.bridge.balthasar_bridge")
    transcribe_mod.transcribe = lambda path, language=None, taigi_hint=False: calls.append(
        ("transcribe", (path, language, taigi_hint))
    ) or {"success": True, "text": "offline transcript"}
    monkeypatch.setitem(sys.modules, "skills.bridge.balthasar_bridge", transcribe_mod)

    quality_mod = types.ModuleType("api.handlers.output_quality_handler")
    quality_mod.estimate_transcript_source_chars_from_audio = lambda path: 10
    quality_mod.run_output_quality_gate = lambda *args, **kwargs: {"ok": True}
    monkeypatch.setitem(sys.modules, "api.handlers.output_quality_handler", quality_mod)

    def fake_save(_file, destination, *args, **kwargs):
        calls.append(("file_save", destination))

    monkeypatch.setattr(werkzeug.datastructures.FileStorage, "save", fake_save)

    client = app.test_client()
    admin_headers = {"X-User-ID": "fixture-admin"}
    responses = [
        client.post(
            "/api/codex-distributed/toggle",
            headers=admin_headers,
            json={"command": "enable", "features": ["fixture"]},
        ),
        client.post(
            "/api/drive-case-exclusions",
            headers=admin_headers,
            json={"relative_path": "一般案件/fixture", "reason": "certification"},
        ),
        client.delete(
            "/api/drive-case-exclusions",
            headers=admin_headers,
            json={"relative_path": "一般案件/fixture"},
        ),
        client.post(
            "/api/system-test",
            headers=admin_headers,
            json={"confirm": "system-test"},
        ),
        client.post(
            "/api/self-repair",
            headers=admin_headers,
            json={"targets": ["fixture"], "confirm": "self-repair"},
        ),
        client.post(
            "/api/transcribe",
            headers={"X-MAGI-API-KEY": "test-api-key"},
            data={"file": (io.BytesIO(b"offline-audio"), "fixture.wav")},
            content_type="multipart/form-data",
        ),
    ]

    assert [response.status_code for response in responses] == [200] * 6
    assert responses[0].get_json()["status"]["fixture"] is True
    assert responses[1].get_json()["changed"] is True
    assert responses[2].get_json()["removed"] == 1
    assert responses[3].get_json()["passed"] == 3
    assert responses[4].get_json()["targets"] == ["fixture"]
    assert responses[5].get_json()["text"] == "offline transcript"
    assert ("codex_toggle", ("enable", ["fixture"])) in calls
    assert any(name == "system_test" for name, _payload in calls)
    assert any(name == "transcribe" for name, _payload in calls)
    assert any(name == "file_save" for name, _payload in calls)


def test_admin_support_bundle_is_admin_only_no_store_and_honest_without_ledger(tmp_path, monkeypatch):
    app, _orchestrator, _updates = _make_app(tmp_path, monkeypatch)
    client = app.test_client()

    forbidden = client.get(
        "/api/admin/observability/support-bundle",
        headers={"X-User-ID": "member", "X-User-Role": "member"},
    )
    assert forbidden.status_code == 403

    response = client.get(
        "/api/admin/observability/support-bundle",
        headers={"X-User-ID": "admin", "X-User-Role": "admin"},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json() == {
        "ok": True,
        "status": "not_attested",
        "detail": "尚無可驗證 task ledger；尚未產生支援包。",
        "support_bundle": None,
    }


def test_admin_support_bundle_fails_closed_when_production_ownership_is_unverifiable(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_V3_DEPLOYMENT_MODE", "production")
    app, _orchestrator, _updates = _make_app(tmp_path, monkeypatch)
    response = app.test_client().get(
        "/api/admin/observability/support-bundle",
        headers={"X-User-ID": "admin", "X-User-Role": "admin"},
    )
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["status"] == "unavailable"


def test_admin_support_bundle_does_not_call_empty_ledger_verified(tmp_path, monkeypatch):
    from magi_v3.ledger import JobLedger

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    JobLedger(runtime / "ledger.sqlite3").initialize()
    app, _orchestrator, _updates = _make_app(tmp_path, monkeypatch)
    response = app.test_client().get(
        "/api/admin/observability/support-bundle",
        headers={"X-User-ID": "admin", "X-User-Role": "admin"},
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "not_attested"
    assert response.get_json()["support_bundle"] is None
