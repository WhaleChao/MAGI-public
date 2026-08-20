from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from scripts import v3_release_bundle as bundle


ROOT = Path(__file__).resolve().parents[2]

MUTABLE_ENV_KEYS = {
    "MAGI_RUNTIME_DIR",
    "MAGI_AGENT_DIR",
    "MAGI_DATA_DIR",
    "MAGI_EXPORTS_DIR",
    "MAGI_METRICS_DIR",
    "MAGI_AUTOPILOT_RUNS_DIR",
    "MAGI_MUTABLE_STATIC_DIR",
    "MAGI_LOG_DIR",
    "MAGI_BACKGROUND_LOCK_DIR",
    "MAGI_FILE_REVIEW_STATE_DIR",
    "MAGI_FILE_REVIEW_BG_JOB_DIR",
    "MAGI_EEFILE_DOWNLOAD_FOLDER",
    "MAGI_FILE_REVIEW_EMAIL_MONITOR_STATE",
    "MAGI_LAF_GMAIL_STATE_PATH",
    "MAGI_LAF_GMAIL_MONITOR_STATE",
    "MAGI_LAF_GMAIL_PENDING_PATH",
    "MAGI_BRAIN_SQLITE_PATH",
    "MAGI_CLOUDFLARED_LOG_PATH",
    "MAGI_PENDING_ENV_UPDATE_FILE",
    "MAGI_OSC_FILE_SHARE_STORE",
    "MAGI_OSC_FILE_SHARE_PUBLIC_BASE_FILE",
    "MAGI_OSC_PREVIEW_CACHE_DIR",
    "MAGI_OSC_PREVIEW_CACHE_MAX_BYTES",
    "MAGI_OSC_UPLOAD_CACHE_DIR",
    "MAGI_WORLDMONITOR_REPORT_DIR",
    "MAGI_AGENT_STATUS_PUBLIC_PATH",
    "MAGI_SAAS_AUDIT_PATH",
    "MAGI_RATE_LIMIT_DB_PATH",
    "MAGI_GIBBERISH_LOG",
    "MAGI_TAIWAN_LEGAL_MCP_ROOT",
    "MAGI_TAIWAN_LEGAL_MCP_CACHE",
}


@pytest.fixture(autouse=True)
def _unlock_read_only_candidate_directories_after_test(tmp_path: Path):
    yield
    for directory, _directory_names, _file_names in os.walk(
        tmp_path, topdown=False, followlinks=False
    ):
        path = Path(directory)
        if not path.is_symlink():
            path.chmod(0o755)


def _app_with_blueprint(blueprint) -> Flask:
    app = Flask(__name__)
    app.secret_key = "runtime-isolation-test"
    app.config.update(TESTING=True, LOGIN_DISABLED=True)
    app.register_blueprint(blueprint)
    return app


def test_launcher_deploy_mutable_env_matrix_and_release_test_contract_are_explicit() -> None:
    launcher = (ROOT / "bin" / "magi-v3-python").read_text(encoding="utf-8")
    deploy = (ROOT / "scripts" / "v3_deploy_prepare.py").read_text(encoding="utf-8")
    launcher_keys = set(re.findall(r"^export\s+([A-Z][A-Z0-9_]*)=", launcher, re.MULTILINE))
    plist_source = deploy.split("def _plist(", 1)[1].split("def _encoded_json", 1)[0]
    deploy_keys = set(re.findall(r'^\s*"([A-Z][A-Z0-9_]*)"\s*:', plist_source, re.MULTILINE))

    assert MUTABLE_ENV_KEYS <= launcher_keys
    assert MUTABLE_ENV_KEYS <= deploy_keys
    assert set(bundle.REQUIRED_PACKAGE_FILES).isdisjoint(bundle.REQUIRED_TEST_TARGETS)
    assert "tests/v3/test_runtime_isolation_regressions.py" in bundle.REQUIRED_TEST_TARGETS
    assert bundle.REQUIRED_FILES == bundle.REQUIRED_PACKAGE_FILES + bundle.REQUIRED_TEST_TARGETS
    assert '"test_execution_evidence": "not_evaluated_by_bundle_builder"' in (
        ROOT / "scripts" / "v3_release_bundle.py"
    ).read_text(encoding="utf-8")


def test_golem_v3_stages_pending_secret_without_mutating_active_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from api.blueprints import golem_console

    active_env = tmp_path / "active-hash-bound.env"
    active_env.write_text("NVIDIA_NIM_ENABLE=0\n", encoding="utf-8")
    active_before = active_env.read_bytes()
    pending = tmp_path / "shared" / "runtime" / "pending-config" / "env_updates.json"
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-runtime-test")
    monkeypatch.setenv("MAGI_PENDING_ENV_UPDATE_FILE", str(pending))
    monkeypatch.setattr(golem_console, "_ENV_PATH", active_env)
    monkeypatch.setattr(golem_console, "_is_admin_user", lambda: True)

    synthetic_secret = "nv" + "api-runtime-isolation-secret"
    response = _app_with_blueprint(golem_console.golem_console_bp).test_client().post(
        "/api/golem/api-keys",
        json={"id": "nvidia_nim", "api_key": synthetic_secret, "enable": True},
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["status"] == "pending_controlled_rebind"
    assert payload["saved"] is False
    assert payload["requires_controlled_redeploy_or_rebind"] is True
    assert active_env.read_bytes() == active_before
    staged = json.loads(pending.read_text(encoding="utf-8"))
    assert staged["updates"]["NVIDIA_NIM_API_KEY"] == synthetic_secret
    assert staged["contract"] == {
        "active_env_mutation_allowed": False,
        "requires_controlled_redeploy_or_rebind": True,
        "apply_in_current_process": False,
    }
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert os.environ.get("NVIDIA_NIM_API_KEY") != synthetic_secret


def test_golem_v2_keeps_existing_direct_env_edit_contract(tmp_path: Path, monkeypatch) -> None:
    from api.blueprints import golem_console

    v2_env = tmp_path / ".env"
    v2_env.write_text("NVIDIA_NIM_ENABLE=0\n", encoding="utf-8")
    monkeypatch.delenv("MAGI_V3_RELEASE_ID", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    monkeypatch.setattr(golem_console, "_ENV_PATH", v2_env)
    monkeypatch.setattr(golem_console, "_is_admin_user", lambda: True)

    response = _app_with_blueprint(golem_console.golem_console_bp).test_client().post(
        "/api/golem/api-keys",
        json={"id": "nvidia_nim", "api_key": "nvapi-v2-compatible-secret", "enable": True},
    )

    assert response.status_code == 200
    assert response.get_json()["saved"] is True
    content = v2_env.read_text(encoding="utf-8")
    assert "NVIDIA_NIM_API_KEY=nvapi-v2-compatible-secret" in content
    assert "NVIDIA_NIM_ENABLE=1" in content
    assert list(tmp_path.glob(".env.bak-*"))


def test_osc_share_and_forms_routes_first_write_only_external_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "immutable-release"
    candidate.mkdir()
    candidate.chmod(0o555)
    shared = tmp_path / "shared"
    runtime = shared / "runtime"
    exports = shared / "exports"
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-route-test")
    monkeypatch.setenv("MAGI_ROOT_DIR", str(candidate))
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("MAGI_EXPORTS_DIR", str(exports))
    monkeypatch.setenv("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL", "https://share.invalid")
    monkeypatch.delenv("MAGI_OSC_FILE_SHARE_STORE", raising=False)
    monkeypatch.delenv("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_FILE", raising=False)

    from api.blueprints import osc_files as osc_files_module

    osc_files = importlib.reload(osc_files_module)
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.4\n% runtime isolation\n")
    monkeypatch.setattr(osc_files, "_require_file_operator", lambda: None)
    monkeypatch.setattr(osc_files, "_resolve_safe_file", lambda _path: str(sample))
    monkeypatch.setattr(osc_files, "_audit_file_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(osc_files, "current_user", SimpleNamespace(id="runtime-test"))

    app = _app_with_blueprint(osc_files.osc_files_bp)
    share_response = app.test_client().post(
        "/api/osc/files/share",
        json={"path": str(sample), "ttl_sec": 300},
    )
    assert share_response.status_code == 200, share_response.get_json()
    assert osc_files._SHARE_STORE_PATH == runtime / "osc_file_shares.json"
    assert osc_files._SHARE_PUBLIC_BASE_FILE == runtime / "osc_share_public_base_url.txt"
    assert osc_files._CHUNK_TMP_DIR == runtime / "cache" / "paperclip-uploads"
    assert osc_files._SHARE_STORE_PATH.is_file()
    assert any((runtime / "osc_file_share_cache").iterdir())

    from api.blueprints import osc_cases
    from api import osc_document_generator, startup

    class _Document:
        def save(self, path: str) -> None:
            Path(path).write_bytes(b"synthetic-docx")

    monkeypatch.setattr(osc_cases, "_record_last_public_base_url", lambda: None)
    monkeypatch.setattr(osc_cases, "_osc_get_case_identity_by_payload", lambda _payload: {"client_name": "測試"})
    monkeypatch.setattr(
        osc_cases,
        "_osc_build_form_preview",
        lambda *_args, **_kwargs: {"form_type": "receipt", "title": "收據", "preview_text": "測試"},
    )
    monkeypatch.setattr(osc_cases, "_osc_document_generator_config", lambda: {})
    monkeypatch.setattr(osc_document_generator, "generate_receipt", lambda *_args, **_kwargs: _Document())
    monkeypatch.setattr(startup, "_export_docx_pdf", lambda *_args, **_kwargs: {"success": False})
    form_app = _app_with_blueprint(osc_cases.osc_bp)
    form_response = form_app.test_client().post(
        "/api/osc/forms/export",
        json={"form_type": "receipt", "fields": {}},
    )
    assert form_response.status_code == 200, form_response.get_json()
    exported = Path(form_response.get_json()["export_docx"]["path"])
    assert exported.is_file()
    assert exported.is_relative_to(exports)
    assert not any(candidate.iterdir())


def test_mutable_static_producers_are_visible_to_dashboard_and_public_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    mutable_static = tmp_path / "shared" / "static"
    runtime = tmp_path / "shared" / "runtime"
    monkeypatch.setenv("MAGI_MUTABLE_STATIC_DIR", str(mutable_static))
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv(
        "MAGI_AGENT_STATUS_PUBLIC_PATH",
        str(mutable_static / "agent_status_public_latest.json"),
    )
    monkeypatch.setenv("FLASK_SECRET_KEY", "runtime-isolation-test")

    from api.agentic.telemetry import public_agent_status_path, write_public_agent_status
    from api.app_factory import create_base_app
    from api.blueprints import admin_runtime, dashboard_pages
    from api.saas_readiness import _audit_event_path

    write_public_agent_status({"status": "ready", "private": "removed"})
    published = json.loads(public_agent_status_path().read_text(encoding="utf-8"))
    response = create_base_app().test_client().get("/static/agent_status_public_latest.json")
    assert response.status_code == 200
    assert response.get_json() == published

    reports = mutable_static / "worldmonitor_reports"
    reports.mkdir(parents=True)
    (reports / "20260716.md").write_text("# Runtime isolation report\nexternal state", encoding="utf-8")
    rows = dashboard_pages._iter_worldmonitor_reports()
    assert dashboard_pages._worldmonitor_report_dir() == reports
    assert rows and rows[0]["name"] == "20260716.md"
    assert _audit_event_path(ROOT) == runtime / "saas_audit_events.jsonl"
    assert admin_runtime._token_health_report_candidates(ROOT)[0] == (
        runtime / "token_health" / "token_health_latest.json"
    )


def test_worker_and_watchdog_subprocess_first_writes_leave_release_unchanged(tmp_path: Path) -> None:
    candidate = tmp_path / "immutable-release"
    for relative in (
        Path("scripts/ops/laf_report_worker.py"),
        Path("scripts/ops/slow_archive_closed_cases.py"),
        Path("scripts/share_tunnel_supervisor.py"),
    ):
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for path in sorted(candidate.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    candidate.chmod(0o555)
    shared = tmp_path / "shared"
    runtime = shared / "runtime"
    agent = shared / "agent"
    log_dir = shared / "logs"
    environment = os.environ.copy()
    environment.update(
        {
            "MAGI_V3_RELEASE_ID": "v3-subprocess-test",
            "MAGI_ROOT": str(candidate),
            "MAGI_ROOT_DIR": str(candidate),
            "MAGI_RUNTIME_DIR": str(runtime),
            "MAGI_AGENT_DIR": str(agent),
            "MAGI_LOG_DIR": str(log_dir),
            "MAGI_OSC_FILE_SHARE_PUBLIC_BASE_FILE": str(runtime / "osc_share_public_base_url.txt"),
            "MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL": "https://share.invalid",
            "MAGI_OSC_PREVIEW_CACHE_DIR": str(runtime / "cache" / "paperclip-preview"),
            "MAGI_OSC_PREVIEW_CACHE_MAX_BYTES": "67108864",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    code = r'''
import os
import sys
from pathlib import Path

from api.osc import preview
assert preview.CACHE_DIR == Path(os.environ["MAGI_OSC_PREVIEW_CACHE_DIR"])
assert preview.CACHE_MAX_BYTES == 67108864

from api.osc import taiwan_legal_mcp
runtime = Path(os.environ["MAGI_RUNTIME_DIR"])
assert taiwan_legal_mcp._DEFAULT_ROOT == runtime / "mcp-taiwan-legal-db"
assert taiwan_legal_mcp._DEFAULT_CACHE == runtime / "taiwan_legal_mcp/cache.sqlite3"

from scripts.ops import laf_report_worker
laf_report_worker._append_job_event("runtime-test", "queued", {"ok": True})

import types
blueprints = types.ModuleType("api.blueprints")
blueprints.__path__ = []
osc_cases = types.ModuleType("api.blueprints.osc_cases")
osc_cases._osc_archive_relative_parent = lambda *args, **kwargs: ""
osc_cases._osc_find_active_case_folder = lambda *args, **kwargs: ""
sys.modules["api.blueprints"] = blueprints
sys.modules["api.blueprints.osc_cases"] = osc_cases
case_path_mapper = types.ModuleType("api.case_path_mapper")
case_path_mapper.local_case_path_candidates = lambda *args, **kwargs: []
case_path_mapper.preferred_case_roots = lambda *args, **kwargs: []
case_path_mapper.translate_local_path_to_canonical = lambda value, *args, **kwargs: value
sys.modules["api.case_path_mapper"] = case_path_mapper
osc_utils = types.ModuleType("api.osc.utils")
osc_utils._osc_exec = lambda *args, **kwargs: []
osc_utils._osc_norm_case_category = lambda value: value
osc_utils._osc_norm_path = lambda value: value
osc_utils._osc_replace_path_prefix_references = lambda *args, **kwargs: None
sys.modules["api.osc.utils"] = osc_utils
domains = types.ModuleType("api.domains")
domains.__path__ = []
case_lock = types.ModuleType("api.domains.case_file_operation_lock")
case_lock.acquire_case_file_operation_lock = lambda **kwargs: {"acquired": True}
case_lock.release_case_file_operation_lock = lambda: None
sys.modules["api.domains"] = domains
sys.modules["api.domains.case_file_operation_lock"] = case_lock

from scripts.ops import slow_archive_closed_cases
slow_archive_closed_cases.DEFAULT_ARCHIVE_ROOTS = (runtime / "missing-archive",)
sys.argv = ["slow_archive_closed_cases.py", "--print-json"]
assert slow_archive_closed_cases.main() == 0

from scripts import share_tunnel_supervisor
share_tunnel_supervisor._stable_share_base_url = lambda: "https://share.invalid"
def stop(_seconds):
    raise SystemExit(0)
share_tunnel_supervisor.time.sleep = stop
try:
    share_tunnel_supervisor.main()
except SystemExit as exc:
    assert exc.code == 0
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=candidate,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert (runtime / "laf_report_jobs.jsonl").is_file()
    assert (runtime / "slow_archive_closed_cases_latest.json").is_file()
    assert (runtime / "slow_archive_closed_cases.jsonl").is_file()
    assert (runtime / "osc_share_public_base_url.txt").read_text(encoding="utf-8").strip() == "https://share.invalid"
    assert (agent / "paperclip_share_tunnel_url.txt").is_file()
    assert (log_dir / "paperclip_share_cloudflared.log").is_file()
    assert not (candidate / ".runtime").exists()
    assert not (candidate / ".agent").exists()
    assert not (candidate / "logs").exists()


def test_v2_resolver_fallbacks_remain_under_v2_root(tmp_path: Path, monkeypatch) -> None:
    from api import runtime_paths

    root = tmp_path / "v2-root"
    monkeypatch.setenv("MAGI_ROOT_DIR", str(root))
    for name in (
        "MAGI_V3_RELEASE_ID",
        "MAGI_SHARED_STATE_DIR",
        "MAGI_V3_SHARED_STATE_DIR",
        "MAGI_RUNTIME_DIR",
        "MAGI_AGENT_DIR",
        "MAGI_DATA_DIR",
        "MAGI_DB_BACKUP_DIR",
        "MAGI_EXPORTS_DIR",
        "MAGI_MUTABLE_STATIC_DIR",
    ):
        monkeypatch.delenv(name, raising=False)

    assert runtime_paths.dotenv_override_allowed() is True
    assert runtime_paths.get_runtime_dir() == root / ".runtime"
    assert runtime_paths.get_agent_dir() == root / ".agent"
    assert runtime_paths.get_exports_dir() == root / "exports"
    assert runtime_paths.get_mutable_static_dir() == root / "static"
    assert runtime_paths.get_judicial_archive_dir() == root / "archive" / "judicial_search"
    assert runtime_paths.get_database_backup_dir() == root / "_db_backups" / "law_firm_data"


def test_v3_shared_root_is_default_for_core_mutable_resolvers(
    tmp_path: Path, monkeypatch
) -> None:
    from api import runtime_paths

    release = tmp_path / "sealed-release"
    shared = tmp_path / "shared"
    monkeypatch.setenv("MAGI_ROOT_DIR", str(release))
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-test")
    monkeypatch.setenv("MAGI_V3_SHARED_STATE_DIR", str(shared))
    for name in (
        "MAGI_SHARED_STATE_DIR",
        "MAGI_RUNTIME_DIR",
        "MAGI_AGENT_DIR",
        "MAGI_DATA_DIR",
        "MAGI_DB_BACKUP_DIR",
    ):
        monkeypatch.delenv(name, raising=False)

    assert runtime_paths.get_runtime_dir() == shared / "runtime"
    assert runtime_paths.get_agent_dir() == shared / "agent"
    assert runtime_paths.get_judicial_archive_dir() == shared / "archive" / "judicial_search"
    assert runtime_paths.get_database_backup_dir() == shared / "db-backups" / "law_firm_data"


def test_database_backup_binary_resolver_accepts_explicit_bound_binary(
    tmp_path: Path, monkeypatch
) -> None:
    from skills.ops.database import backup_restore

    binary = tmp_path / "mysqldump"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setenv("MAGI_MYSQLDUMP_BIN", str(binary))
    monkeypatch.setattr(backup_restore.shutil, "which", lambda _name: None)

    assert backup_restore._find_bin("mysqldump") == str(binary.resolve())


def test_release_quality_collection_imports_use_v3_shared_state_without_leaf_bindings(
    tmp_path: Path,
) -> None:
    release = tmp_path / "sealed-release"
    shared = tmp_path / "shared"
    release.mkdir()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
        "MAGI_ROOT": str(release),
        "MAGI_ROOT_DIR": str(release),
        "MAGI_V3_RELEASE_ID": "v3-test",
        "MAGI_V3_SHARED_STATE_DIR": str(shared),
    }
    code = r'''
from pathlib import Path
from api import startup
from skills.research import web_research

shared = Path(__import__("os").environ["MAGI_V3_SHARED_STATE_DIR"])
assert Path(startup.AGENT_DIR).resolve() == (shared / "agent").resolve()
assert Path(web_research.SEARCH_CACHE_DIR).resolve() == (
    shared / "runtime" / "cache" / "web_search"
).resolve()
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=release,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert not (release / ".agent").exists()
    assert not (release / "cache").exists()
