from __future__ import annotations

import time
from pathlib import Path

from flask import Flask


class _NoConnectMysql:
    @staticmethod
    def connect(**_kwargs):
        raise AssertionError("readiness probes must not open database connections")


def _make_probe_app(tmp_path: Path, *, db_password: str = "p") -> Flask:
    from api.blueprints.admin_runtime import create_admin_runtime_blueprint

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret")
    app.register_blueprint(
        create_admin_runtime_blueprint(
            logger=app.logger,
            orchestrator=object(),
            require_json_auth=lambda admin=False: None,
            list_skill_docs=lambda: [],
            nerv_skill_interview_user_id=lambda: "nerv:test",
            extract_interview_skill_name=lambda _message: "",
            skill_doc_path=lambda name: tmp_path / "skills" / name / "SKILL.md",
            skill_action_path=lambda name: tmp_path / "skills" / name / "action.py",
            skill_summary=lambda content: str(content or "").strip(),
            nerv_product_runtime_payload=lambda: {"ok": True},
            nerv_product_names=(),
            update_product_runtime=lambda product, **updates: updates,
            cloudflared_alive=lambda: False,
            server_start_time=time.time() - 1,
            attachment_job_queue=None,
            list_attachment_job_ids=lambda: [],
            read_attachment_job=lambda _job_id: {},
            expected_magi_api_key="",
            db_config={"host": "127.0.0.1", "user": "u", "password": db_password},
            mysql_connector=_NoConnectMysql,
            safe_remove_tmp=lambda _path: None,
            magi_root=tmp_path,
        )
    )
    return app


def test_livez_is_process_only_and_machine_readable(tmp_path):
    client = _make_probe_app(tmp_path).test_client()

    response = client.get("/livez")

    assert response.status_code == 200
    assert response.content_type.startswith("application/json")
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"] == "live"
    assert payload["probe"] == "liveness"
    assert payload["readiness_checked"] is False
    assert isinstance(payload["timestamp"], (int, float))
    assert isinstance(payload["uptime_seconds"], (int, float))
    assert "checks" not in payload


def test_readyz_uses_lightweight_local_readiness_checks(tmp_path):
    client = _make_probe_app(tmp_path).test_client()

    response = client.get("/readyz")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"] == "ready"
    assert payload["probe"] == "readiness"
    assert payload["liveness"]["status"] == "live"
    assert set(payload["checks"]) == {"root", "runtime_dir", "db_config"}
    assert payload["checks"]["root"]["ok"] is True
    assert payload["checks"]["runtime_dir"]["ok"] is True
    assert payload["checks"]["db_config"]["password_configured"] is True


def test_readyz_reports_not_ready_without_required_db_config(tmp_path):
    client = _make_probe_app(tmp_path, db_password="").test_client()

    response = client.get("/readyz")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["status"] == "not_ready"
    assert payload["checks"]["db_config"]["ok"] is False
    assert payload["checks"]["db_config"]["password_configured"] is False


def test_readyz_scope_saas_uses_formal_readiness_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_SAAS_MODE", "1")
    monkeypatch.setenv("MAGI_PUBLIC_SOURCE_ROOT_DIR", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "short")
    monkeypatch.setenv("MAGI_API_KEY", "short")
    client = _make_probe_app(tmp_path).test_client()

    response = client.get("/readyz?scope=saas")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["status"] == "not_ready"
    assert "tenant_identity" in payload["failed_keys"]


def test_health_get_remount_is_disabled(tmp_path):
    client = _make_probe_app(tmp_path).test_client()

    response = client.get("/health?remount=1")

    assert response.status_code == 405
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"] == "remount_requires_admin_post"
