from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _release_metadata() -> dict[str, tuple[int, int]]:
    roots = (
        ROOT / "api",
        ROOT / "daemon.py",
        ROOT / "scripts",
        ROOT / "skills" / "crawler-targets",
        ROOT / "skills" / "judgment-collector",
        ROOT / "skills" / "magi-autopilot",
        ROOT / "skills" / "market-briefing",
        ROOT / "skills" / "memory",
        ROOT / "static",
        ROOT / ".agent",
        ROOT / ".runtime",
    )
    rows: dict[str, tuple[int, int]] = {}
    for base in roots:
        if base.is_file():
            stat = base.stat()
            rows[str(base.relative_to(ROOT))] = (stat.st_size, stat.st_mtime_ns)
            continue
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                stat = path.stat()
                rows[str(path.relative_to(ROOT))] = (stat.st_size, stat.st_mtime_ns)
    return rows


def test_scheduled_modules_keep_v3_release_immutable(tmp_path: Path) -> None:
    agent = tmp_path / "shared" / "agent"
    runtime = tmp_path / "shared" / "runtime"
    mutable_static = tmp_path / "shared" / "static"
    exports = tmp_path / "shared" / "exports"
    distill = tmp_path / "shared" / "gemma-distill"
    training_lock = tmp_path / "shared" / "host" / "training.lock"
    (agent / "judgment-collector").mkdir(parents=True)
    (agent / "judgment-collector" / "judgments.json").write_text("[]\n", encoding="utf-8")

    before = _release_metadata()
    environment = os.environ.copy()
    environment.update(
        {
            "GEMMA_DISTILL_DIR": str(distill),
            "MAGI_AGENT_DIR": str(agent),
            "MAGI_CORTEX_SYNC_STATE_PATH": str(runtime / "cortex_sync_state.json"),
            "MAGI_AUTOPILOT_NO_VENV": "1",
            "MAGI_AUTOPILOT_RUNS_DIR": str(runtime / "autopilot-runs"),
            "MAGI_CRAWL_TARGETS_NO_VENV": "1",
            "MAGI_EXPORTS_DIR": str(exports),
            "MAGI_MUTABLE_STATIC_DIR": str(mutable_static),
            "MAGI_JUDGMENTS_JSON_PATH": str(agent / "judgment-collector" / "judgments.json"),
            "MAGI_ROOT": str(ROOT),
            "MAGI_ROOT_DIR": str(ROOT),
            "MAGI_RUNTIME_DIR": str(runtime),
            "MAGI_TRAINING_LOCK_PATH": str(training_lock),
            "MAGI_V3_SHARED_STATE_DIR": str(tmp_path / "shared"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    code = r'''
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

root = Path(os.environ["MAGI_ROOT_DIR"]).resolve()
agent = Path(os.environ["MAGI_AGENT_DIR"]).resolve()
runtime = Path(os.environ["MAGI_RUNTIME_DIR"]).resolve()
mutable_static = Path(os.environ["MAGI_MUTABLE_STATIC_DIR"]).resolve()
exports = Path(os.environ["MAGI_EXPORTS_DIR"]).resolve()
training_lock = Path(os.environ["MAGI_TRAINING_LOCK_PATH"]).resolve()

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, root / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

fake_failover = types.ModuleType("api.db_failover")
fake_failover.probe_remote = lambda: False
fake_failover.get_osc_host = lambda: "127.0.0.1"
fake_failover._switch_to_local = lambda: None
sys.modules["api.db_failover"] = fake_failover

from skills.memory import cortex_sync
assert Path(cortex_sync.STATE_FILE) == runtime / "cortex_sync_state.json"
syncer = object.__new__(cortex_sync.CortexSync)
syncer.state = {"legal_news_last_id": 7}
syncer._save_state()
legacy_seed = runtime / "legacy-cortex-seed.json"
legacy_seed.write_text('{"judgments_last_id": 9}', encoding="utf-8")
mutable_seed_target = runtime / "seeded" / "cortex_sync_state.json"
original_state_path = cortex_sync.STATE_FILE
original_legacy_path = cortex_sync._LEGACY_STATE_FILE
cortex_sync._LEGACY_STATE_FILE = str(legacy_seed)
sealed_release = (root / "release-manifest.json").is_file()
if not sealed_release:
    cortex_sync.STATE_FILE = str(mutable_seed_target)
seeded = cortex_sync.CortexSync()
if sealed_release:
    # A sealed V3 release keeps its canonical shared-state file and may not
    # fall back to a legacy state file.
    assert seeded.state == {"legal_news_last_id": 7}
    assert not mutable_seed_target.exists()
else:
    # Source/V2 compatibility still supports a one-time legacy seed.
    assert seeded.state == {"judgments_last_id": 9}
    assert not mutable_seed_target.exists()
    seeded._save_state()
    assert mutable_seed_target.is_file()
assert json.loads(legacy_seed.read_text(encoding="utf-8")) == {"judgments_last_id": 9}
cortex_sync.STATE_FILE = original_state_path
cortex_sync._LEGACY_STATE_FILE = original_legacy_path

autopilot = load("scheduled_autopilot_state_test", "skills/magi-autopilot/action.py")
assert Path(autopilot.STATE_PATH) == runtime / "_autopilot_state.json"
assert Path(autopilot.AUTOPILOT_LOCK_PATH) == runtime / "_autopilot.lock"
assert Path(autopilot.MAGI_RUNTIME_OVERRIDES_PATH) == agent / "runtime_overrides.json"
autopilot._ensure_dirs()
autopilot._save_json(autopilot.STATE_PATH, {"nightly": {"ok": True}})
Path(autopilot.AUTOPILOT_LOCK_PATH).write_text("test", encoding="utf-8")

crawler = load("scheduled_crawler_state_test", "skills/crawler-targets/action.py")
assert Path(crawler.STATE_PATH) == runtime / "_crawl_targets.json"
crawler._save_state({"targets": []})

from scripts import weekend_bookmark_batch as bookmark
assert bookmark.STATE_FILE == agent / "bookmark_batch_state.json"
assert bookmark.BACKFILL_PLAN_PATH == runtime / "bookmark_backfill_plan_latest.json"
assert bookmark.FOLLOWUP_PLAN_PATH == runtime / "bookmark_followup_plan_latest.json"
bookmark._save_state({"completed": {}, "vision_done": {}})
bookmark.BACKFILL_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
bookmark.BACKFILL_PLAN_PATH.write_text("{}", encoding="utf-8")
bookmark.FOLLOWUP_PLAN_PATH.write_text("{}", encoding="utf-8")

market = load("scheduled_market_briefing_state_test", "skills/market-briefing/action.py")
assert market.AGENT_DIR == agent
assert market.NOTIFY_LOG_PATH == mutable_static / "market_briefing_notify.log"
market._save_state({"watchlist": []})
market._notify_log("state-routing-test")
from data import fetcher, perf_tracker, watchlist
fetcher._save_cache_fetcher({"twse_lookup": {}})
perf_tracker._save_perf({"records": [], "metrics": {}, "tuning_log": []})
assert watchlist.STATE_PATH == agent / "market_watchlist.json"
news = load("scheduled_market_news_state_test", "skills/market-briefing/market_news.py")
news._save_cache({"test": {"items": []}})
from skills.ops import export_text
assert Path(export_text.AGENT_DIR) == agent
assert Path(export_text.EXPORTS_DIR) == exports
assert export_text.export_txt("market report", prefix="route-test")["success"] is True

judgments = load("scheduled_judgment_state_test", "skills/judgment-collector/action.py")
assert Path(judgments._JUDGMENTS_JSON_PATH) == agent / "judgment-collector" / "judgments.json"
assert judgments._upsert_judgments_json(
    "測試裁判 001",
    "## 法律爭點\n- 測試案由\n\n"
    "## 實務見解\n- 按測試法第1條規定，測試案由之狀態資料應寫入共享狀態路徑，不得寫入封存版本。\n\n"
    "## 摘要方式\n- 測試固定文字。",
    "測試案由",
)
cleanup = load("scheduled_judgment_cleanup_state_test", "scripts/ops/cleanup_judgments_leaks.py")
assert cleanup.JSON_PATH == agent / "judgment-collector" / "judgments.json"
assert cleanup.cleanup_json(True, None).get("final_count") == 1

# Enabled cron entrypoints without explicit output flags must never fall back to
# mutable paths inside the immutable V3 release.
pdf_bookmarker = load("scheduled_pdf_bookmarker_benchmark_test", "scripts/ops/benchmark_pdf_bookmarker.py")
pdf_namer = load("scheduled_pdf_namer_benchmark_test", "scripts/ops/benchmark_pdf_namer.py")
osc_todos = load("scheduled_osc_todos_benchmark_test", "scripts/ops/benchmark_osc_todos.py")
for output in (pdf_bookmarker.OUTPUT_PATH, pdf_namer.OUTPUT_PATH, osc_todos.OUTPUT_PATH):
    output_path = Path(output)
    assert output_path.parent == runtime
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("{}\n", encoding="utf-8")

disk_alarm = load("scheduled_disk_low_water_alarm_test", "scripts/ops/disk_low_water_alarm.py")
assert disk_alarm.ALERT_STATE_PATH == runtime / "disk_low_water_alarm_state.json"
disk_alarm._write_alert_state("OK", 100.0, 50.0, emitted=False)

watchlist_backup = load("scheduled_watchlist_backup_test", "scripts/ops/backup_market_watchlist.py")
assert watchlist_backup.WATCHLIST_PATH == agent / "market_watchlist.json"
assert watchlist_backup.BACKUP_DIR == runtime / "backups" / "market_watchlist"
assert watchlist_backup.main() == 0
assert list(watchlist_backup.BACKUP_DIR.glob("*.json"))

nightly_health = load("scheduled_nightly_health_report_test", "scripts/nightly_health_report.py")
assert Path(nightly_health.AGENT_DIR) == agent
assert Path(nightly_health.RUNTIME_DIR) == runtime
assert Path(nightly_health.DELIVERY_LOG).parent == agent
assert Path(nightly_health.RESOURCE_GUARD_LOG).parent == runtime

transcript_index = load("scheduled_transcript_index_test", "skills/transcript-indexer/action.py")
assert transcript_index.INDEX_DB_PATH == agent / "transcript_index.json"
transcript_download = load("scheduled_transcript_download_test", "skills/transcript-downloader/action.py")
assert transcript_download.MANUAL_QUEUE_PATH == mutable_static / "transcript_manual_queue.jsonl"
assert transcript_download.TRANSCRIPT_SYNC_STATE_PATH == agent / "transcript_sync_state.json"
assert transcript_download.TRANSCRIPT_SYNC_RUNTIME_DIR == runtime / "transcript_sync"
assert transcript_download.TRANSCRIPT_SYNC_LOCK_PATH == agent / "transcript_sync.lock"
assert Path(transcript_download.DEFAULT_DOWNLOAD_FOLDER) == exports / "transcript-downloads"

translator_ape = load("scheduled_translator_ape_test", "scripts/ops/benchmark_translator_ape.py")
assert translator_ape.MUTABLE_STATIC_DIR == mutable_static
translator_ape._write_static_result({"ok": True, "rows": []})

research_brief = load("scheduled_research_brief_test", "skills/research-brief/action.py")
assert research_brief._RUNTIME_DIR == runtime / "research_brief"

weekly_cleanup = load("scheduled_weekly_cache_cleanup_test", "scripts/ops/weekly_cache_cleanup.py")
weekly_paths = {Path(item["path"]) for item in weekly_cleanup._TARGETS}
assert runtime / "cache" in weekly_paths
assert runtime / "graphify-cache" in weekly_paths
assert runtime / "osc_draft_ocr_cache" in weekly_paths

disk_cleanup = load("scheduled_disk_cleanup_test", "scripts/ops/disk_cleanup_healthcheck.py")
assert disk_cleanup._AGENT_DIR == agent
assert disk_cleanup._RUNTIME_DIR == runtime
assert set(disk_cleanup._runtime_compress_roots()) == {
    runtime / "logs",
    runtime / "metrics",
    runtime / "autopilot-runs",
    runtime / "reports",
}

wiki = load("scheduled_wiki_synthesizer_test", "scripts/wiki_synthesizer.py")
assert wiki.AGENT_DIR == agent
knowledge_lint = load("scheduled_knowledge_lint_test", "scripts/knowledge_lint.py")
assert knowledge_lint.AGENT_DIR == agent
assert knowledge_lint.REPORT_DIR == mutable_static
assert knowledge_lint.DEFAULT_DUPLICATE_BACKUP_DIR == runtime / "backups" / "knowledge_duplicate_cleanup"
osc_refresh = load("scheduled_osc_events_refresh_test", "scripts/ops/osc_events_refresh.py")
assert osc_refresh.LATEST_PATH == runtime / "osc_events_refresh_latest.json"
assert osc_refresh.PDF_SCAN_CACHE_PATH.parent == runtime
assert osc_refresh.PDF_SCAN_CURSOR_PATH.parent == runtime
tailscale_health = load("scheduled_tailscale_health_test", "scripts/ops/tailscale_funnel_healthcheck.py")
assert tailscale_health.STATE_PATH.parent == runtime
heavy_translation = load("scheduled_heavy_translation_test", "scripts/ops/heavy_translation_quality_live.py")
assert heavy_translation.GENERATED_FIXTURE == runtime / "fixtures" / "heavy_translation_quality_fixture.pdf"
business_readiness = load("scheduled_business_readiness_test", "scripts/ops/business_readiness_snapshot.py")
assert business_readiness._mutable_static_dir(root) == mutable_static
hardening = load("scheduled_hardening_audit_test", "scripts/ops/audit_operational_hardening.py")
assert hardening._runtime_dir() == runtime
assert hardening._legacy_pid_file_paths()
assert all(not path.resolve().is_relative_to(root) for path, _domain in hardening._legacy_pid_file_paths())
slow_archive = load("scheduled_slow_archive_test", "scripts/ops/start_slow_archive_closed_cases.py")
assert slow_archive.RUNTIME_DIR == runtime
resummary = load("scheduled_resummary_test", "scripts/ops/resummary_legacy_judgments_quality.py")
assert resummary.REPORT_PATH.parent == runtime
laf_portal = load("scheduled_laf_portal_test", "scripts/ops/laf_portal_new_files_scan.py")
assert laf_portal.MUTABLE_STATIC_DIR == mutable_static
reconcile_todos = load("scheduled_reconcile_todos_test", "scripts/ops/reconcile_overdue_todos.py")
assert reconcile_todos.RUNTIME_DIR == runtime
worldmonitor = load("scheduled_worldmonitor_test", "skills/worldmonitor-intel/action.py")
assert worldmonitor.MUTABLE_STATIC_DIR == mutable_static
insight_sync = load("scheduled_insight_sync_test", "scripts/sync_insights_to_vectors.py")
assert insight_sync._AGENT_DIR == agent

# Cron snapshot rebases explicit --json-out arguments, while these module-level
# defaults protect direct/manual invocations and internal sub-check writes.
business_live = load("scheduled_business_live_test", "scripts/ops/business_module_live_check.py")
assert business_live.DEFAULT_LIVE_REPORT.parent == runtime
assert business_live._runtime_status_file("probe.json") == runtime / "probe.json"
model_gate = load("scheduled_model_gate_test", "scripts/ops/model_live_gate.py")
assert model_gate.RUNTIME_DIR == runtime
bookmark_repair = load("scheduled_bookmark_repair_test", "scripts/ops/repair_pdf_bookmark_labels.py")
assert bookmark_repair.DEFAULT_REPORT.parent == runtime
assert bookmark_repair.HISTORY_PATH.parent == runtime
token_health = load("scheduled_token_health_test", "scripts/ops/token_health_check.py")
assert token_health.DEFAULT_REPORT_PATH == runtime / "token_health" / "token_health_latest.json"
obsidian_gate = load("scheduled_obsidian_gate_test", "scripts/ops/obsidian_acceptance_gate.py")
assert obsidian_gate.DEFAULT_JSON_OUT.parent == runtime
function_health = load("scheduled_function_health_test", "scripts/ops/function_health_index.py")
assert function_health.default_runtime_dir(root) == runtime
guardian = load("scheduled_guardian_test", "scripts/ops/magi_self_repair_guardian.py")
assert guardian.DEFAULT_RUNTIME_DIR == runtime

from scripts import nightly_distill_gemma as distill_job
assert distill_job.TRAINING_LOCK_PATH == training_lock
assert distill_job._LOG_DIR == Path(os.environ["GEMMA_DISTILL_DIR"])
distill_job._write_training_lock()
assert distill_job.TRAINING_LOCK_PATH.is_file()
distill_job._clear_training_lock()

print(json.dumps({"agent": str(agent), "runtime": str(runtime)}))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert json.loads(result.stdout.splitlines()[-1]) == {
        "agent": str(agent),
        "runtime": str(runtime),
    }
    assert (runtime / "cortex_sync_state.json").is_file()
    assert (runtime / "_autopilot_state.json").is_file()
    assert (runtime / "_autopilot.lock").is_file()
    assert (runtime / "_crawl_targets.json").is_file()
    assert (agent / "bookmark_batch_state.json").is_file()
    assert (agent / "market_watchlist.json").is_file()
    assert (agent / "market_data_cache.json").is_file()
    assert (agent / "market_perf_history.json").is_file()
    assert (agent / "market_news_cache.json").is_file()
    assert (agent / "judgment-collector" / "judgments.json").is_file()
    assert (mutable_static / "market_briefing_notify.log").is_file()
    assert (mutable_static / "translator_ape_latest.json").is_file()
    assert (runtime / "benchmark_pdf_bookmarker_latest.json").is_file()
    assert (runtime / "benchmark_pdf_namer_latest.json").is_file()
    assert (runtime / "benchmark_osc_todos_latest.json").is_file()
    assert (runtime / "disk_low_water_alarm_state.json").is_file()
    assert list((runtime / "backups" / "market_watchlist").glob("*.json"))
    assert list(exports.glob("route-test_*.txt"))
    assert not training_lock.exists()
    assert _release_metadata() == before


def test_scheduled_module_defaults_preserve_v2_paths() -> None:
    environment = os.environ.copy()
    for key in (
        "MAGI_AGENT_DIR",
        "MAGI_AUTOPILOT_RUNS_DIR",
        "MAGI_METRICS_DIR",
        "MAGI_MUTABLE_STATIC_DIR",
        "MAGI_ROOT",
        "MAGI_ROOT_DIR",
        "MAGI_RUNTIME_DIR",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    code = r'''
import importlib.util
import json
import sys
from pathlib import Path

root = Path.cwd().resolve()

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, root / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

bookmarker = load("v2_pdf_bookmarker", "scripts/ops/benchmark_pdf_bookmarker.py")
namer = load("v2_pdf_namer", "scripts/ops/benchmark_pdf_namer.py")
todos = load("v2_osc_todos", "scripts/ops/benchmark_osc_todos.py")
alarm = load("v2_disk_alarm", "scripts/ops/disk_low_water_alarm.py")
backup = load("v2_watchlist_backup", "scripts/ops/backup_market_watchlist.py")
nightly = load("v2_nightly_health", "scripts/nightly_health_report.py")
transcript = load("v2_transcript_index", "skills/transcript-indexer/action.py")
transcript_download = load("v2_transcript_download", "skills/transcript-downloader/action.py")
translator = load("v2_translator_ape", "scripts/ops/benchmark_translator_ape.py")
research = load("v2_research_brief", "skills/research-brief/action.py")
weekly = load("v2_weekly_cleanup", "scripts/ops/weekly_cache_cleanup.py")
disk_cleanup = load("v2_disk_cleanup", "scripts/ops/disk_cleanup_healthcheck.py")

assert Path(bookmarker.OUTPUT_PATH) == root / ".runtime" / "benchmark_pdf_bookmarker_latest.json"
assert Path(namer.OUTPUT_PATH) == root / ".runtime" / "benchmark_pdf_namer_latest.json"
assert Path(todos.OUTPUT_PATH) == root / ".runtime" / "benchmark_osc_todos_latest.json"
assert alarm.ALERT_STATE_PATH == root / ".runtime" / "disk_low_water_alarm_state.json"
assert backup.WATCHLIST_PATH == root / ".agent" / "market_watchlist.json"
assert backup.BACKUP_DIR == root / ".runtime" / "backups" / "market_watchlist"
assert Path(nightly.AGENT_DIR) == root / ".agent"
assert transcript.INDEX_DB_PATH == root / ".agent" / "transcript_index.json"
assert transcript_download.MANUAL_QUEUE_PATH == root / "static" / "transcript_manual_queue.jsonl"
assert transcript_download.TRANSCRIPT_SYNC_STATE_PATH == root / ".agent" / "transcript_sync_state.json"
assert transcript_download.TRANSCRIPT_SYNC_RUNTIME_DIR == root / ".runtime" / "transcript_sync"
assert transcript_download.TRANSCRIPT_SYNC_LOCK_PATH == root / ".agent" / "transcript_sync.lock"
assert translator.MUTABLE_STATIC_DIR == root / "static"
assert research._RUNTIME_DIR == root / ".runtime" / "research_brief"
weekly_paths = {Path(item["path"]) for item in weekly._TARGETS}
assert root / ".cache" in weekly_paths
assert root / "graphify-out" / "cache" in weekly_paths
assert root / ".runtime" / "osc_draft_ocr_cache" in weekly_paths
assert disk_cleanup._AGENT_DIR == root / ".agent"
assert disk_cleanup._RUNTIME_DIR == root / ".runtime"
print(json.dumps({"ok": True}))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == {"ok": True}
