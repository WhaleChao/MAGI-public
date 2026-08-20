from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from dotenv import load_dotenv

from api.runtime_paths import dotenv_override_allowed
from scripts import v3_release_bundle as release_bundle


ROOT = Path(__file__).resolve().parents[2]


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_v3_env_file_cannot_replace_hash_bound_launch_environment(tmp_path, monkeypatch):
    env_file = tmp_path / "magi.env"
    env_file.write_text(
        "MAGI_ROOT_DIR=/Users/ai/Desktop/MAGI_v2\n"
        "MAGI_ORCH_DIR=/Users/ai/Desktop/MAGI_v2/casper_ecosystem/law_firm_orchestrators\n",
        encoding="utf-8",
    )
    release = str(tmp_path / "candidate")
    release_orch = str(Path(release) / "casper_ecosystem" / "law_firm_orchestrators")
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-test")
    monkeypatch.setenv("MAGI_ROOT_DIR", release)
    monkeypatch.setenv("MAGI_ORCH_DIR", release_orch)

    assert dotenv_override_allowed() is False
    load_dotenv(env_file, override=dotenv_override_allowed())

    assert os.environ["MAGI_ROOT_DIR"] == release
    assert os.environ["MAGI_ORCH_DIR"] == release_orch

    monkeypatch.delenv("MAGI_V3_RELEASE_ID")
    assert dotenv_override_allowed() is True
    load_dotenv(env_file, override=dotenv_override_allowed())
    assert os.environ["MAGI_ROOT_DIR"] == "/Users/ai/Desktop/MAGI_v2"


def test_core_imports_and_first_writes_leave_candidate_tree_immutable(tmp_path):
    candidate = tmp_path / "candidate"
    exercised_files = {
        Path("api/orchestrator.py"),
        Path("api/answer_provenance.py"),
        Path("api/debug_capture.py"),
        Path("api/discord_bot.py"),
        Path("api/tools_api.py"),
        Path("api/channel_context.py"),
        Path("api/product_runtime.py"),
        Path("api/saas_audit.py"),
        Path("api/agentic/telemetry.py"),
        Path("api/discord_channel_router.py"),
        Path("api/laf_closing_transfer.py"),
        Path("api/debt_document_generator.py"),
        Path("api/blueprints/osc_debt.py"),
        Path("api/blueprints/osc_pdf.py"),
        Path("api/blueprints/web_runtime.py"),
        Path("api/osc/draft_learning.py"),
        Path("api/osc/saas_workbench.py"),
        Path("api/pipelines/message_pipeline.py"),
        Path("api/poa_chat_handler.py"),
        Path("api/session/conversation_history.py"),
        Path("api/session/verified_fact_gate.py"),
        Path("skills/memory/job_queue.py"),
        Path("skills/legal/laf.py"),
        Path("scripts/ops/background_task_locks.py"),
        Path("scripts/ops/laf_gmail_dispatch_scan.py"),
        Path("scripts/ops/osc_shell_nas_helper.py"),
        Path("skills/file-review-orchestrator/action.py"),
    }
    snapshot = release_bundle.snapshot_sources(ROOT)
    assert exercised_files <= {Path(entry.path) for entry in snapshot}
    for entry in snapshot:
        relative = Path(entry.path)
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)

    shared = tmp_path / "shared"
    agent = shared / "agent"
    runtime = shared / "runtime"
    mutable_static = shared / "static"
    state_path = mutable_static / "laf_gmail_monitor_state.json"
    pending_path = runtime / "laf_gmail_dispatch_pending.json"
    env_file = tmp_path / "candidate.env"
    env_file.write_text("DUMMY_SECRET=value\n", encoding="utf-8")
    before = _snapshot(candidate)

    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    environment.update(
        {
            "CANDIDATE_ROOT": str(candidate),
            "MAGI_V3_RELEASE_ID": "v3-test",
            "MAGI_V3_SHARED_STATE_DIR": str(shared),
            "MAGI_ROOT": str(candidate),
            "MAGI_ROOT_DIR": str(candidate),
            "MAGI_ORCH_DIR": str(
                candidate / "casper_ecosystem" / "law_firm_orchestrators"
            ),
            "MAGI_CODE_DIR": str(
                candidate / "casper_ecosystem" / "law_firm_orchestrators"
            ),
            "MAGI_AGENT_DIR": str(agent),
            "MAGI_DATA_DIR": str(agent),
            "MAGI_EXPORTS_DIR": str(shared / "exports"),
            "MAGI_RUNTIME_DIR": str(runtime),
            "MAGI_MUTABLE_STATIC_DIR": str(mutable_static),
            "MAGI_METRICS_DIR": str(shared / "metrics"),
            "MAGI_BACKGROUND_LOCK_DIR": str(runtime / "locks"),
            "MAGI_LAF_GMAIL_STATE_PATH": str(state_path),
            "MAGI_LAF_GMAIL_MONITOR_STATE": str(state_path),
            "MAGI_LAF_GMAIL_PENDING_PATH": str(pending_path),
            "MAGI_FILE_REVIEW_EMAIL_MONITOR_STATE": str(
                mutable_static / "file_review_email_monitor_state.json"
            ),
            "MAGI_BRAIN_SQLITE_PATH": str(agent / "magi_brain.db"),
            "MAGI_FILE_REVIEW_STATE_DIR": str(shared / "file-review"),
            "MAGI_PAYMENT_REGISTRY_PATH": str(shared / "file-review" / "downloads" / "payment_registry.json"),
            "MAGI_PAYMENT_PROOF_REGISTRY_PATH": str(shared / "file-review" / "downloads" / "payment_proof_registry.json"),
            "MAGI_FILE_REVIEW_BG_JOB_DIR": str(shared / "file-review" / "bg-jobs"),
            "MAGI_EEFILE_DOWNLOAD_FOLDER": str(shared / "file-review" / "downloads"),
            "MAGI_LAF_PROCESSED_EMAILS_PATH": str(agent / "laf-orchestrator" / "processed_laf_emails.json"),
            "MAGI_JUDGMENTS_JSON_PATH": str(agent / "judgment-collector" / "judgments.json"),
            "MAGI_PDF_NAMER_CASE_INDEX": str(shared / "pdf-namer" / "_case_index.json"),
            "MAGI_CORTEX_SYNC_STATE_PATH": str(runtime / "cortex_sync_state.json"),
            "MAGI_DEBT_ADDRESS_BOOK_DIR": str(shared / "debt" / "address-book"),
            "MAGI_CLOUDFLARED_LOG_PATH": str(shared / "logs" / "cloudflared.log"),
            "MAGI_ENV_FILE": str(env_file),
            "DISCORD_BOT_TOKEN": "candidate-isolation-test-token",
            "MAGI_DISABLE_SERVER_STARTUP_HOOKS": "1",
            "MAGI_SKIP_IMPORT_PROBES": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(candidate),
            "DEPENDENCY_SITE_PACKAGES": sysconfig.get_paths()["purelib"],
        }
    )
    code = r'''
import json
import os
import runpy
import sys
import types
from pathlib import Path

candidate = Path(os.environ['CANDIDATE_ROOT'])
assert os.environ['PYTHONPATH'] == str(candidate)
sys.path.append(os.environ['DEPENDENCY_SITE_PACKAGES'])
agent = Path(os.environ['MAGI_AGENT_DIR'])
runtime = Path(os.environ['MAGI_RUNTIME_DIR'])
static = Path(os.environ['MAGI_MUTABLE_STATIC_DIR'])
shared = agent.parent

locks = runpy.run_path(str(candidate / 'scripts/ops/background_task_locks.py'))
assert locks['lock_dir']().resolve() == (runtime / 'locks').resolve()

jobs = runpy.run_path(str(candidate / 'skills/memory/job_queue.py'))
assert Path(jobs['_DB_PATH']).resolve() == (agent / 'jobs/job_queue.db').resolve()
with jobs['_get_conn']() as conn:
    assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0

orchestrator = runpy.run_path(str(candidate / 'api/orchestrator.py'))
assert Path(orchestrator['_ORCH_AGENT_DIR']).resolve() == agent.resolve()
assert Path(orchestrator['_magi_status_path']()).resolve() == (static / 'magi_status.json').resolve()
assert Path(orchestrator['_brain_sqlite_path']()).resolve() == (agent / 'magi_brain.db').resolve()
handlers = orchestrator['logger'].handlers
assert any(Path(handler.baseFilename).resolve().is_relative_to(agent.resolve()) for handler in handlers if hasattr(handler, 'baseFilename'))

history = runpy.run_path(str(candidate / 'api/session/conversation_history.py'))
assert Path(history['_DB_PATH']).resolve() == (runtime / 'conversation_history.sqlite3').resolve()
history['ConversationHistoryStore']()

verified = runpy.run_path(str(candidate / 'api/session/verified_fact_gate.py'))
assert Path(verified['_AUDIT_PATH']).resolve() == (runtime / 'verified_fact_audit.jsonl').resolve()

telemetry = runpy.run_path(str(candidate / 'api/agentic/telemetry.py'))
assert telemetry['public_agent_status_path']().resolve() == (static / 'agent_status_public_latest.json').resolve()
telemetry['write_public_agent_status']({'status': 'ready'})

product = runpy.run_path(str(candidate / 'api/product_runtime.py'))
assert Path(product['PRODUCT_RUNTIME_PATH']).resolve() == (agent / 'product_runtime.json').resolve()
product['update_product_runtime']('file_review', codex_mode='local')

provenance = runpy.run_path(str(candidate / 'api/answer_provenance.py'))
assert Path(provenance['_RUNTIME_DIR']).resolve() == runtime.resolve()
provenance['store_provenance']('test', [], '', 'test')

debug = runpy.run_path(str(candidate / 'api/debug_capture.py'))
assert debug['DEBUG_DIR'].resolve() == (runtime / 'debug_screenshots').resolve()
assert debug['DEBUG_MD'].resolve() == (runtime / 'debug_archive/debug_log.md').resolve()

router = runpy.run_path(str(candidate / 'api/discord_channel_router.py'))
assert Path(router['_CHANNEL_MAP_FILE']).resolve() == (agent / 'discord_channel_map.json').resolve()
assert Path(router['_NOTIFICATION_PREFS_FILE']).resolve() == (runtime / 'osc_saas_notification_prefs.json').resolve()
router['save_channel_map']({'general': '123'})

pipeline = runpy.run_path(str(candidate / 'api/pipelines/message_pipeline.py'))
assert Path(pipeline['_AGENT_DIR']).resolve() == agent.resolve()
review_pending = agent / 'file-review/review_submit_pending.json'
assert pipeline['get_file_review_pending_path'](candidate).resolve() == review_pending.resolve()

poa = runpy.run_path(str(candidate / 'api/poa_chat_handler.py'))
assert poa['AGENT_DIR'].resolve() == agent.resolve()

closing = runpy.run_path(str(candidate / 'api/laf_closing_transfer.py'))
assert closing['_DEFAULT_ARCHIVE_PENDING_PATH'].resolve() == (runtime / 'laf_closing_archive_pending.json').resolve()

saas_audit = runpy.run_path(str(candidate / 'api/saas_audit.py'))
assert saas_audit['AUDIT_PATH'].resolve() == (runtime / 'saas_audit_events.jsonl').resolve()
saas_audit['append_audit_event']('immutable-probe')

draft_learning = runpy.run_path(str(candidate / 'api/osc/draft_learning.py'))
assert draft_learning['EVENTS_PATH'].resolve() == (runtime / 'osc_draft_learning_events.jsonl').resolve()
assert draft_learning['record_draft_feedback']({'original': 'old', 'corrected': 'new'})['ok'] is True

workbench = runpy.run_path(str(candidate / 'api/osc/saas_workbench.py'))
for key in ('INTAKE_PATH', 'ONBOARDING_PATH', 'NOTIFICATION_PREFS_PATH', 'WORKFLOW_TEMPLATES_PATH'):
    assert workbench[key].resolve().is_relative_to(runtime.resolve())

debt = runpy.run_path(str(candidate / 'api/debt_document_generator.py'))
assert Path(debt['_EXPORTS_DIR']).resolve() == Path(os.environ['MAGI_EXPORTS_DIR']).resolve()
assert debt['get_debt_address_book_dir']().resolve() == Path(os.environ['MAGI_DEBT_ADDRESS_BOOK_DIR']).resolve()
debt_blueprint = runpy.run_path(str(candidate / 'api/blueprints/osc_debt.py'))
assert Path(debt_blueprint['_export_dir']()).resolve() == Path(os.environ['MAGI_EXPORTS_DIR']).resolve()

shell_helper = runpy.run_path(str(candidate / 'scripts/ops/osc_shell_nas_helper.py'))
assert Path(os.environ['MAGI_EXPORTS_DIR']).resolve() in shell_helper['_allowed_roots']()

tools_api = runpy.run_path(str(candidate / 'api/tools_api.py'))
assert Path(tools_api['EXTERNAL_CHAT_METRICS_PATH']).resolve() == (shared / 'metrics/external_chat_metrics.jsonl').resolve()

web_runtime = runpy.run_path(str(candidate / 'api/blueprints/web_runtime.py'))
assert web_runtime['_agent_state_dir'](candidate).resolve() == agent.resolve()
assert web_runtime['_mutable_static_dir'](candidate).resolve() == static.resolve()
assert web_runtime['_chat_upload_dir'](candidate).resolve() == (agent / 'chat_uploads').resolve()
assert web_runtime['_magi_web_outputs_dir'](candidate).resolve() == (shared / 'exports/magi_outputs').resolve()

osc_pdf = runpy.run_path(str(candidate / 'api/blueprints/osc_pdf.py'))
assert osc_pdf['_upload_dir']().resolve() == (agent / 'pdf_uploads').resolve()

(agent / 'telegram_channel_state.json').write_text(json.dumps({'topicMap': {'laf': 9}}), encoding='utf-8')
(agent / 'discord_channel_map.json').write_text(json.dumps({'filereview_dispatch': '456'}), encoding='utf-8')
channel_context = runpy.run_path(str(candidate / 'api/channel_context.py'))
assert channel_context['reverse_lookup_telegram_topic'](9, str(candidate)) == 'laf'
assert channel_context['reverse_lookup_discord_channel']('456', str(candidate)) == 'filereview'

file_review = runpy.run_path(str(candidate / 'skills/file-review-orchestrator/action.py'))
assert Path(file_review['DEFAULT_DOWNLOAD_FOLDER']).resolve() == (Path(os.environ['MAGI_FILE_REVIEW_STATE_DIR']) / 'downloads').resolve()
assert Path(file_review['BG_JOB_DIR']).resolve() == Path(os.environ['MAGI_FILE_REVIEW_BG_JOB_DIR']).resolve()
assert Path(file_review['_REVIEW_PENDING_FILE']).resolve() == review_pending.resolve()
file_review['_save_review_pending']({
    'A1B2C3': {'status': 'pending', 'expires_at': __import__('time').time() + 600},
})
token, _entry, state = pipeline['_find_file_review_confirm_record']('確認碼 A1B2C3')
assert (token, state) == ('A1B2C3', 'pending')
file_review['_write_download_job']('immutable-probe', {'status': 'ok'})

discord = runpy.run_path(str(candidate / 'api/discord_bot.py'))
assert Path(discord['_CLOUDFLARED_LOG_PATH']).resolve() == Path(os.environ['MAGI_CLOUDFLARED_LOG_PATH']).resolve()

import importlib.util
laf_spec = importlib.util.spec_from_file_location('candidate_real_laf', candidate / 'skills/legal/laf.py')
assert laf_spec and laf_spec.loader
candidate_laf = importlib.util.module_from_spec(laf_spec)
sys.modules[laf_spec.name] = candidate_laf
laf_spec.loader.exec_module(candidate_laf)
real_monitor = candidate_laf.LAFGmailMonitor(
    str(shared / 'credentials.json'),
    str(shared / 'token.pickle'),
)
assert Path(real_monitor._processed_ids_file).resolve() == (agent / 'laf-orchestrator/processed_laf_emails.json').resolve()
assert real_monitor._state_path.resolve() == Path(os.environ['MAGI_LAF_GMAIL_MONITOR_STATE']).resolve()
assert real_monitor._file_review_state_path.resolve() == Path(os.environ['MAGI_FILE_REVIEW_EMAIL_MONITOR_STATE']).resolve()
real_monitor._write_monitor_state('immutable-probe')
real_monitor._write_file_review_monitor_state('immutable-probe')

laf_orch = types.ModuleType('casper_ecosystem.law_firm_orchestrators.laf_orchestrator')
laf_orch.LAFOrchestrator = object
laf = types.ModuleType('laf')
laf.LAFGmailMonitor = object
sys.modules['casper_ecosystem'] = types.ModuleType('casper_ecosystem')
sys.modules['casper_ecosystem.law_firm_orchestrators'] = types.ModuleType('law_firm_orchestrators')
sys.modules['casper_ecosystem.law_firm_orchestrators.laf_orchestrator'] = laf_orch
sys.modules['laf'] = laf
scan = runpy.run_path(str(candidate / 'scripts/ops/laf_gmail_dispatch_scan.py'))
assert scan['_output_path']('MAGI_LAF_GMAIL_STATE_PATH', candidate / 'static/legacy.json', scan['DEFAULT_STATE_PATH']).resolve() == (static / 'laf_gmail_monitor_state.json').resolve()
assert scan['_output_path']('MAGI_LAF_GMAIL_PENDING_PATH', candidate / '.runtime/legacy.json', scan['DEFAULT_PENDING_PATH']).resolve() == (runtime / 'laf_gmail_dispatch_pending.json').resolve()

candidate_prefixes = ('api', 'scripts', 'skills', 'casper_ecosystem', 'magi_v3')
origins = {}
for name, module in sys.modules.items():
    origin = getattr(module, '__file__', None)
    if origin and (name in candidate_prefixes or name.startswith(tuple(prefix + '.' for prefix in candidate_prefixes))):
        resolved = Path(origin).resolve()
        assert resolved.is_relative_to(candidate.resolve()), (name, resolved, candidate)
        origins[name] = str(resolved)
assert 'api.runtime_paths' in origins
assert origins

print(json.dumps({'agent': str(agent), 'runtime': str(runtime), 'static': str(static), 'origins': origins}))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-S", "-c", code],
        cwd=candidate,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        # This probe imports and exercises 28 production modules.  On the
        # supported 16 GB Mac, APFS metadata checks can legitimately take a
        # little over 90 seconds while the live scheduler is active.  Keep a
        # hard bound, but give the complete first-write isolation proof enough
        # room to finish instead of creating a false deployment red light.
        timeout=180,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert observed["origins"]
    assert all(Path(path).resolve().is_relative_to(candidate.resolve()) for path in observed["origins"].values())
    assert _snapshot(candidate) == before
    assert (agent / "jobs" / "job_queue.db").is_file()
    assert (agent / "file-review" / "review_submit_pending.json").is_file()
    assert (runtime / "locks").is_dir()
    assert (agent / "casper.log").is_file()


def test_status_consumers_follow_mutable_static_and_agent_dirs(tmp_path, monkeypatch):
    mutable_static = tmp_path / "shared" / "static"
    agent = tmp_path / "shared" / "agent"
    runtime = tmp_path / "shared" / "runtime"
    monkeypatch.setenv("MAGI_MUTABLE_STATIC_DIR", str(mutable_static))
    monkeypatch.setenv("MAGI_AGENT_DIR", str(agent))
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(runtime))

    from api.blueprints import admin_runtime, dashboard_pages
    from scripts.ops import business_readiness_snapshot

    assert admin_runtime._mutable_static_dir(tmp_path) == mutable_static
    assert admin_runtime._agent_state_dir(tmp_path) == agent
    assert admin_runtime._runtime_state_dir(tmp_path) == tmp_path / "shared" / "runtime"
    assert dashboard_pages._mutable_static_dir() == mutable_static
    assert dashboard_pages._runtime_dir() == tmp_path / "shared" / "runtime"
    assert business_readiness_snapshot._mutable_static_dir(tmp_path) == mutable_static
    assert business_readiness_snapshot._runtime_dir(tmp_path) == tmp_path / "shared" / "runtime"
    assert business_readiness_snapshot._agent_dir(tmp_path) == agent


def test_http_mutable_paths_preserve_v2_defaults_without_launch_bindings(tmp_path, monkeypatch):
    for name in (
        "MAGI_AGENT_DIR",
        "MAGI_DATA_DIR",
        "MAGI_EXPORTS_DIR",
        "MAGI_MUTABLE_STATIC_DIR",
        "MAGI_METRICS_DIR",
        "MAGI_EXTERNAL_CHAT_METRICS_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    from api import channel_context
    from api.blueprints import osc_pdf, web_runtime

    assert web_runtime._agent_state_dir(tmp_path) == tmp_path / ".agent"
    assert web_runtime._mutable_static_dir(tmp_path) == tmp_path / "static"
    assert web_runtime._chat_upload_dir(tmp_path) == tmp_path / ".agent" / "chat_uploads"
    assert web_runtime._magi_web_outputs_dir(tmp_path) == tmp_path / "static" / "exports" / "magi_outputs"

    fake_module_path = tmp_path / "api" / "blueprints" / "osc_pdf.py"
    monkeypatch.setattr(osc_pdf, "__file__", str(fake_module_path))
    assert osc_pdf._upload_dir() == tmp_path / ".agent" / "pdf_uploads"

    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "telegram_channel_state.json").write_text(
        json.dumps({"topicMap": {"laf": 9}}), encoding="utf-8"
    )
    (agent_dir / "discord_channel_map.json").write_text(
        json.dumps({"filereview_dispatch": "456"}), encoding="utf-8"
    )
    assert channel_context.reverse_lookup_telegram_topic(9, str(tmp_path)) == "laf"
    assert channel_context.reverse_lookup_discord_channel("456", str(tmp_path)) == "filereview"
