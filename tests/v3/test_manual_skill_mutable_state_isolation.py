from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _copy_candidate(candidate: Path, files: tuple[str, ...]) -> None:
    for relative_text in files:
        relative = Path(relative_text)
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)


def _v3_environment(candidate: Path, shared: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "FAISS_INDEX_DIR",
        "JUDICIAL_CACHE_DIR",
        "MAGI_AUTORESEARCH_RUNS_DIR",
        "MAGI_LAW_CACHE_DIR",
        "MAGI_LAW_VDB_STATE_PATH",
        "MAGI_MEMORY_BACKUP_DB_PATH",
        "MAGI_DAEMON_LOG_PATH",
        "MAGI_FILE_REVIEW_PENDING_PATH",
        "MAGI_WEB_RESEARCH_CACHE_DIR",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "MAGI_V3_RELEASE_ID": "v3-manual-state-test",
            "MAGI_ROOT": str(candidate),
            "MAGI_ROOT_DIR": str(candidate),
            "MAGI_AGENT_DIR": str(shared / "agent"),
            "MAGI_RUNTIME_DIR": str(shared / "runtime"),
            "MAGI_SHARED_STATE_DIR": str(shared),
            "MAGI_EXPORTS_DIR": str(shared / "exports"),
            "MAGI_MUTABLE_STATIC_DIR": str(shared / "static"),
            "MAGI_DAEMON_LOG_PATH": str(shared / "agent" / "daemon.log"),
            "MAGI_FILE_REVIEW_PENDING_PATH": str(
                shared / "agent" / "file-review" / "review_submit_pending.json"
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    return environment


def test_release_launcher_exports_manual_skill_state_bindings() -> None:
    launcher = (ROOT / "bin" / "magi-v3-python").read_text(encoding="utf-8")
    for binding in (
        'export MAGI_SHARED_STATE_DIR="$shared_state"',
        'export MAGI_AUTORESEARCH_RUNS_DIR="$shared_state/autoresearch-runs"',
        'export JUDICIAL_CACHE_DIR="$shared_state/runtime/cache/judicial_web_search"',
        'export MAGI_LAW_CACHE_DIR="$shared_state/runtime/cache/laws"',
        'export MAGI_LAW_VDB_STATE_PATH="$shared_state/agent/_statutes_vdb_state.json"',
        'export FAISS_INDEX_DIR="$shared_state/memory/index_cache"',
        'export MAGI_FILE_REVIEW_PENDING_PATH="$shared_state/agent/file-review/review_submit_pending.json"',
        'export MAGI_DAEMON_LOG_PATH="$shared_state/agent/daemon.log"',
        'export MAGI_GCAL_DUP_AUDIT_OUTPUT_DIR="$shared_state/exports/gcal_dedup"',
    ):
        assert binding in launcher

    cli = (ROOT / "scripts" / "magi_cli.sh").read_text(encoding="utf-8")
    assert 'faiss_dir="$FAISS_INDEX_DIR"' in cli
    assert 'faiss_dir="$MAGI_SHARED_STATE_DIR/memory/index_cache"' in cli
    assert 'faiss_dir="$magi_root/skills/memory/index_cache"' in cli


def test_night_talk_and_council_approval_share_external_state(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    _copy_candidate(
        candidate,
        (
            "skills/magi/council_approval.py",
            "skills/magi/night_talk.py",
            "skills/magi/skill_learner.py",
        ),
    )
    shared = tmp_path / "shared"
    before = _snapshot(candidate)
    code = r'''
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

candidate = Path(os.environ["MAGI_ROOT_DIR"])
agent = Path(os.environ["MAGI_AGENT_DIR"])

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, candidate / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

requests = types.ModuleType("requests")
requests.get = lambda *args, **kwargs: None
sys.modules["requests"] = requests

brain = types.ModuleType("skills.brain_manager.action")
brain.switch_brain_mode = lambda *args, **kwargs: {}
sys.modules["skills.brain_manager.action"] = brain

bridge = types.ModuleType("skills.bridge")
bridge.melchior_client = None
bridge.melchior_bridge = None
bridge.watcher_bridge = None
bridge.balthasar_bridge = None
sys.modules["skills.bridge"] = bridge
gateway = types.ModuleType("skills.bridge.inference_gateway")
gateway.InferenceGateway = object
sys.modules["skills.bridge.inference_gateway"] = gateway

council = load("skills.magi.council_approval", "skills/magi/council_approval.py")
assert Path(council.PENDING_FILE) == agent / "nightly_core_change_pending.json"
queued = council.queue_core_change_for_approval(
    "database migration", "review schema migration", {}, "full_quorum"
)
assert queued["success"] is True

night_talk = load("v3_manual_night_talk", "skills/magi/night_talk.py")
assert Path(night_talk.AGENDA_FILE) == agent / "nightly_council_agenda.md"
assert Path(night_talk.MINUTES_FILE) == agent / "nightly_council_minutes.md"
assert night_talk.queue_core_change_for_approval is council.queue_core_change_for_approval
again = night_talk.queue_core_change_for_approval(
    "database migration count 42", "review schema migration", {}, "full_quorum"
)
assert again["success"] is True
assert Path(again["path"]) == Path(council.PENDING_FILE)

learner = load("v3_manual_skill_learner", "skills/magi/skill_learner.py")
assert learner.COUNCIL_MINUTES_PATH == Path(night_talk.MINUTES_FILE)
learner.COUNCIL_MINUTES_PATH.parent.mkdir(parents=True, exist_ok=True)
learner.COUNCIL_MINUTES_PATH.write_text("nightly learning " * 20, encoding="utf-8")
captured = {}
learner.review_and_learn = lambda task, quiet=False: captured.update(task=task) or {"learned": False}
assert learner.night_review_skills()["reviewed"] is True
assert "nightly learning" in captured["task"]
print(json.dumps({"pending": council.PENDING_FILE}))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=_v3_environment(candidate, shared),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "pending": str(shared / "agent" / "nightly_core_change_pending.json")
    }
    assert (shared / "agent" / "nightly_core_change_pending.json").is_file()
    assert _snapshot(candidate) == before


def test_named_manual_skills_import_and_first_write_leave_candidate_immutable(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    files = (
        "skills/judicial-web-search/action.py",
        "skills/statutes-vdb/action.py",
        "skills/autoresearch/action.py",
        "scripts/obsidian_bulk_ingest.py",
        "skills/memory/faiss_index.py",
    )
    _copy_candidate(candidate, files)
    shared = tmp_path / "shared"
    before = _snapshot(candidate)

    code = r'''
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

candidate = Path(os.environ["MAGI_ROOT_DIR"])
agent = Path(os.environ["MAGI_AGENT_DIR"])
runtime = Path(os.environ["MAGI_RUNTIME_DIR"])
shared = Path(os.environ["MAGI_SHARED_STATE_DIR"])

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, candidate / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

fake_bs4 = types.ModuleType("bs4")
fake_bs4.BeautifulSoup = object
sys.modules["bs4"] = fake_bs4

fake_requests = types.ModuleType("requests")
fake_requests.Session = type("Session", (), {})
fake_requests.get = lambda *args, **kwargs: None
sys.modules["requests"] = fake_requests
fake_urllib3 = types.ModuleType("urllib3")
fake_urllib3.disable_warnings = lambda: None
sys.modules["urllib3"] = fake_urllib3

fake_numpy = types.ModuleType("numpy")
fake_numpy.ndarray = object
fake_numpy.float32 = float
fake_numpy.int64 = int
sys.modules["numpy"] = fake_numpy

fake_faiss = types.ModuleType("faiss")
class FakeIndexFlatIP:
    def __init__(self, dim):
        self.dim = dim
        self.ntotal = 0
    def add(self, values):
        self.ntotal += len(values)
fake_faiss.IndexFlatIP = FakeIndexFlatIP
fake_faiss.Index = object
fake_faiss.write_index = lambda index, path: Path(path).write_bytes(b"index")
sys.modules["faiss"] = fake_faiss

judicial = load("v3_manual_judicial", "skills/judicial-web-search/action.py")
assert Path(judicial.CACHE_DIR) == runtime / "cache" / "judicial_web_search"
judicial_result = judicial._write_fetch_cache(
    "https://example.invalid/judgment", "immutable probe", 1000, "test"
)
assert Path(judicial_result["text_path"]).is_file()

statutes = load("v3_manual_statutes", "skills/statutes-vdb/action.py")
assert Path(statutes.CACHE_DIR) == runtime / "cache" / "laws"
assert Path(statutes.STATE_PATH) == agent / "_statutes_vdb_state.json"
statutes._save_json(statutes.STATE_PATH, {"probe": True})
statutes._save_json(str(Path(statutes.CACHE_DIR) / "meta.json"), {"probe": True})

autoresearch = load("v3_manual_autoresearch", "skills/autoresearch/action.py")
assert autoresearch._RESULTS_DIR == shared / "autoresearch-runs"
autoresearch._ssh = lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", "")
run_result = autoresearch.cmd_run("test@example", "immutable")
assert run_result["success"] is True

fake_obsidian = types.ModuleType("skills.obsidian.action")
fake_obsidian.task_ingest_source = lambda **kwargs: {}
fake_obsidian.task_status = lambda: {}
fake_obsidian._get_vault_path = lambda: None
fake_obsidian.SOURCE_ROOTS = {"案件": None}
sys.modules["skills.obsidian.action"] = fake_obsidian
bulk = load("v3_manual_obsidian_bulk", "scripts/obsidian_bulk_ingest.py")
assert bulk.AGENT_DIR == agent
assert bulk.PROGRESS_PATH == agent / "obsidian_bulk_progress.json"
assert bulk.LOG_PATH == agent / "obsidian_bulk_ingest.log"
bulk.save_progress({"completed": {}, "failed": {}})

faiss_index = load("v3_manual_faiss", "skills/memory/faiss_index.py")
assert Path(faiss_index.INDEX_DIR) == shared / "memory" / "index_cache"
index = faiss_index.FAISSMemoryIndex(dim=2)
assert Path(faiss_index.INDEX_DIR).is_dir()

print(json.dumps({"ok": True}))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=_v3_environment(candidate, shared),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert json.loads(result.stdout.splitlines()[-1]) == {"ok": True}
    assert (shared / "agent" / "_statutes_vdb_state.json").is_file()
    assert (shared / "agent" / "obsidian_bulk_progress.json").is_file()
    assert (shared / "runtime" / "cache" / "judicial_web_search").is_dir()
    assert (shared / "runtime" / "cache" / "laws" / "meta.json").is_file()
    assert list((shared / "autoresearch-runs").glob("*.json"))
    assert (shared / "memory" / "index_cache").is_dir()
    assert _snapshot(candidate) == before


def test_expanded_manual_state_writers_leave_candidate_immutable(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    files = (
        "api/autopilot_artifacts.py",
        "skills/engine/feedback_loop.py",
        "skills/engine/knowledge_extractor.py",
        "skills/legal_attest/action.py",
        "skills/memory/magi_brain_setup.py",
        "skills/memory/sqlite_backup.py",
        "skills/ops/circuit_breaker.py",
        "skills/ops/user_activity_beacon.py",
        "skills/osc-orchestrator/action.py",
        "skills/pdf-annotator/action.py",
        "skills/screenshot-sorter-tw/action.py",
        "skills/transcript-todo-extractor/action.py",
        "skills/translator/legal_termbase.py",
    )
    _copy_candidate(candidate, files)
    shared = tmp_path / "shared"
    before = _snapshot(candidate)

    code = r'''
import importlib.util
import json
import os
import sys
from pathlib import Path

candidate = Path(os.environ["MAGI_ROOT_DIR"])
agent = Path(os.environ["MAGI_AGENT_DIR"])
runtime = Path(os.environ["MAGI_RUNTIME_DIR"])
shared = Path(os.environ["MAGI_SHARED_STATE_DIR"])
exports = Path(os.environ["MAGI_EXPORTS_DIR"])

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, candidate / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

feedback = load("v3_manual_feedback", "skills/engine/feedback_loop.py")
assert feedback.FEEDBACK_PATH == agent / "routing_feedback.json"
feedback.record_feedback("query", "chat", "correct")

knowledge = load("v3_manual_knowledge", "skills/engine/knowledge_extractor.py")
assert knowledge.EXTRACT_STATS_PATH == agent / "knowledge_extract_stats.json"
knowledge._update_stats("extracted")

attest = load("v3_manual_attest", "skills/legal_attest/action.py")
assert attest.STATE_PATH == agent / "legal_attest_state.json"
attest._save_state({"test": {"step": "init"}})

brain_setup = load("v3_manual_brain_setup", "skills/memory/magi_brain_setup.py")
assert Path(brain_setup.DB_PATH) == agent / "magi_brain.db"
brain_setup.init_db()

backup = load("v3_manual_sqlite_backup", "skills/memory/sqlite_backup.py")
assert Path(backup.BACKUP_DB_PATH) == agent / "memory" / "memory_backup.db"
assert backup.save_to_backup("immutable probe") > 0

circuit = load("v3_manual_circuit", "skills/ops/circuit_breaker.py")
assert Path(circuit.STATE_FILE) == agent / "circuit_breaker_state.json"
circuit.record_failure("immutable probe")

beacon = load("v3_manual_beacon", "skills/ops/user_activity_beacon.py")
assert Path(beacon._BEACON_PATH) == agent / "user_activity_beacon.json"
beacon.touch("test", "test")

osc = load("v3_manual_osc", "skills/osc-orchestrator/action.py")
assert Path(osc.PENDING_QUEUE_PATH) == agent / "osc-orchestrator" / "_pending_todos.jsonl"
queued = osc._enqueue_pending({"path": "/safe/case/file.pdf"}, "immutable_probe")
assert queued["queued"] is True

annotator = load("v3_manual_annotator", "skills/pdf-annotator/action.py")
assert annotator.STATE_PATH == agent / "annotation_state.json"
annotator._save_json(annotator.STATE_PATH, {"probe": True})

sorter = load("v3_manual_screenshot", "skills/screenshot-sorter-tw/action.py")
assert sorter.OUTPUT_BASE == exports / "screenshot-sorted"

todo = load("v3_manual_transcript_todo", "skills/transcript-todo-extractor/action.py")
assert todo.INDEX_DB_PATH == agent / "transcript_index.json"

termbase = load("v3_manual_termbase", "skills/translator/legal_termbase.py")
assert termbase._DATA_DIR == shared / "translator" / "moj_bilingual"
empty_laws = runtime / "empty-laws"
empty_laws.mkdir(parents=True)
assert termbase.build_tier1_from_moj(empty_laws) == shared / "translator" / "moj_bilingual" / "termbase.sqlite"

artifacts = load("v3_manual_autopilot_artifacts", "api/autopilot_artifacts.py")
assert artifacts.get_autopilot_runtime_dir(root=str(candidate)) == runtime / "autopilot"
artifacts.write_kill_reason(4242, "immutable probe", root=str(candidate))
assert artifacts.read_kill_reason(4242, root=str(candidate), delete=False) == "immutable probe"

print(json.dumps({"ok": True}))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=_v3_environment(candidate, shared),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert json.loads(result.stdout.splitlines()[-1]) == {"ok": True}
    assert (shared / "agent" / "routing_feedback.json").is_file()
    assert (shared / "agent" / "magi_brain.db").is_file()
    assert (shared / "agent" / "osc-orchestrator" / "_pending_todos.jsonl").is_file()
    assert (shared / "runtime" / "autopilot" / "kill_log.jsonl").is_file()
    assert (shared / "translator" / "moj_bilingual" / "termbase.sqlite").is_file()
    assert _snapshot(candidate) == before


def test_manual_state_defaults_preserve_v2_paths(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    files = (
        "skills/judicial-web-search/action.py",
        "skills/statutes-vdb/action.py",
        "skills/autoresearch/action.py",
        "scripts/obsidian_bulk_ingest.py",
        "skills/memory/faiss_index.py",
        "skills/magi/council_approval.py",
        "skills/magi/night_talk.py",
        "skills/magi/skill_learner.py",
    )
    _copy_candidate(candidate, files)
    environment = os.environ.copy()
    for name in (
        "FAISS_INDEX_DIR",
        "JUDICIAL_CACHE_DIR",
        "MAGI_AGENT_DIR",
        "MAGI_DATA_DIR",
        "MAGI_AUTORESEARCH_RUNS_DIR",
        "MAGI_FILE_REVIEW_PENDING_PATH",
        "MAGI_LAW_CACHE_DIR",
        "MAGI_LAW_VDB_STATE_PATH",
        "MAGI_ROOT",
        "MAGI_ROOT_DIR",
        "MAGI_RUNTIME_DIR",
        "MAGI_SHARED_STATE_DIR",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "CANDIDATE_ROOT": str(candidate),
            "MAGI_SKILL_PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    code = r'''
import importlib.util
import os
import sys
import types
from pathlib import Path

candidate = Path(os.environ["CANDIDATE_ROOT"])
repo_root = Path.cwd()

from api.runtime_paths import get_faiss_index_dir, get_file_review_pending_path
assert get_file_review_pending_path(candidate) == candidate / "skills" / "file-review-orchestrator" / ".review_submit_pending.json"
assert get_faiss_index_dir(candidate) == candidate / "skills" / "memory" / "index_cache"

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, candidate / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

fake_bs4 = types.ModuleType("bs4")
fake_bs4.BeautifulSoup = object
sys.modules["bs4"] = fake_bs4

fake_requests = types.ModuleType("requests")
fake_requests.Session = type("Session", (), {})
sys.modules["requests"] = fake_requests
fake_urllib3 = types.ModuleType("urllib3")
fake_urllib3.disable_warnings = lambda: None
sys.modules["urllib3"] = fake_urllib3

fake_numpy = types.ModuleType("numpy")
fake_numpy.ndarray = object
fake_numpy.float32 = float
fake_numpy.int64 = int
sys.modules["numpy"] = fake_numpy

fake_faiss = types.ModuleType("faiss")
fake_faiss.IndexFlatIP = object
fake_faiss.Index = object
sys.modules["faiss"] = fake_faiss

judicial = load("v2_manual_judicial", "skills/judicial-web-search/action.py")
assert Path(judicial.CACHE_DIR) == candidate / ".cache" / "judicial_web_search"

statutes = load("v2_manual_statutes", "skills/statutes-vdb/action.py")
assert Path(statutes.CACHE_DIR) == repo_root / "cache" / "laws"
assert Path(statutes.STATE_PATH) == repo_root / "_statutes_vdb_state.json"

autoresearch = load("v2_manual_autoresearch", "skills/autoresearch/action.py")
assert autoresearch._RESULTS_DIR == candidate / "skills" / "autoresearch" / "runs"

fake_obsidian = types.ModuleType("skills.obsidian.action")
fake_obsidian.task_ingest_source = lambda **kwargs: {}
fake_obsidian.task_status = lambda: {}
fake_obsidian._get_vault_path = lambda: None
fake_obsidian.SOURCE_ROOTS = {"案件": None}
sys.modules["skills.obsidian.action"] = fake_obsidian
bulk = load("v2_manual_obsidian_bulk", "scripts/obsidian_bulk_ingest.py")
assert bulk.AGENT_DIR == candidate / ".agent"
assert bulk.PROGRESS_PATH == candidate / ".agent" / "obsidian_bulk_progress.json"

faiss_index = load("v2_manual_faiss", "skills/memory/faiss_index.py")
assert Path(faiss_index.INDEX_DIR) == candidate / "skills" / "memory" / "index_cache"

council = load("skills.magi.council_approval", "skills/magi/council_approval.py")
assert Path(council.PENDING_FILE) == candidate / "nightly_core_change_pending.json"

brain = types.ModuleType("skills.brain_manager.action")
brain.switch_brain_mode = lambda *args, **kwargs: {}
sys.modules["skills.brain_manager.action"] = brain
bridge = types.ModuleType("skills.bridge")
bridge.melchior_client = None
bridge.melchior_bridge = None
bridge.watcher_bridge = None
bridge.balthasar_bridge = None
sys.modules["skills.bridge"] = bridge
gateway = types.ModuleType("skills.bridge.inference_gateway")
gateway.InferenceGateway = object
sys.modules["skills.bridge.inference_gateway"] = gateway
night_talk = load("v2_manual_night_talk", "skills/magi/night_talk.py")
assert Path(night_talk.AGENDA_FILE) == candidate / "nightly_council_agenda.md"
assert Path(night_talk.MINUTES_FILE) == candidate / "nightly_council_minutes.md"

learner = load("v2_manual_skill_learner", "skills/magi/skill_learner.py")
assert learner.COUNCIL_MINUTES_PATH == candidate / "nightly_council_minutes.md"
'''
    subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_faiss_producer_and_backup_consumer_share_one_external_index(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    environment = os.environ.copy()
    environment.pop("FAISS_INDEX_DIR", None)
    environment.update(
        {
            "MAGI_SHARED_STATE_DIR": str(shared),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    code = r'''
import json
import os
import sys
import types
from pathlib import Path

fake_faiss = types.ModuleType("faiss")
fake_faiss.Index = object
sys.modules["faiss"] = fake_faiss
fake_numpy = types.ModuleType("numpy")
fake_numpy.ndarray = object
fake_numpy.float32 = float
fake_numpy.int64 = int
sys.modules["numpy"] = fake_numpy
from skills.memory import faiss_index
from scripts import knowledge_lint

index_dir = Path(faiss_index.INDEX_DIR)
assert index_dir == Path(os.environ["MAGI_SHARED_STATE_DIR"]) / "memory" / "index_cache"
index_dir.mkdir(parents=True)
(index_dir / faiss_index.INDEX_FILE).write_bytes(b"index")
(index_dir / faiss_index.IDMAP_FILE).write_bytes(b"idmap")
(index_dir / "meta.json").write_text("{}", encoding="utf-8")
result = knowledge_lint._backup_faiss_files(Path(os.environ["MAGI_SHARED_STATE_DIR"]) / "backup")
assert result["ok"] is True
assert Path(result["source_dir"]) == index_dir
print(json.dumps(result, ensure_ascii=False))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload["files"] == ["mem_index.faiss", "mem_idmap.npy", "meta.json"]


def test_faiss_api_consumers_use_the_producer_index(tmp_path: Path, monkeypatch) -> None:
    shared = tmp_path / "shared"
    index_dir = shared / "memory" / "index_cache"
    index_dir.mkdir(parents=True)
    (index_dir / "meta.json").write_text(
        json.dumps({"total": 17, "index_type": "flat"}), encoding="utf-8"
    )
    (index_dir / "mem_index.faiss").write_bytes(b"external-index")
    monkeypatch.delenv("FAISS_INDEX_DIR", raising=False)
    monkeypatch.setenv("MAGI_SHARED_STATE_DIR", str(shared))

    from api.blueprints import admin_runtime, web_runtime
    from api.runtime_paths import get_faiss_index_dir

    assert get_faiss_index_dir(tmp_path) == index_dir
    assert admin_runtime.get_faiss_index_dir(tmp_path) == index_dir
    assert web_runtime.get_faiss_index_dir(tmp_path) == index_dir
    assert admin_runtime._read_faiss_metadata(tmp_path) == {
        "ok": True,
        "vectors": 17,
        "index_type": "flat",
        "metadata_only": True,
    }


def test_supported_manual_diagnostics_first_write_leave_candidate_immutable(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    files = (
        "scripts/ops/run_test_suite.py",
        "scripts/ops/smoke_test_full.py",
        "scripts/ops/skill_realworld_smoke.py",
        "scripts/ops/integration_smoke.py",
        "scripts/ops/magi_acceptance_gate.py",
        "scripts/ops/commercial_readiness_live.py",
        "scripts/ops/smart_model_router_live.py",
        "scripts/live_test_tw_legal_rag.py",
        "scripts/ops/test_summarize_judgments.py",
        "scripts/tests/apple_intelligence_smoke_test.py",
    )
    _copy_candidate(candidate, files)
    shared = tmp_path / "shared"
    before = _snapshot(candidate)
    code = r'''
import json
import os
import runpy
import sys
import types
from pathlib import Path

candidate = Path(os.environ["MAGI_ROOT_DIR"])
runtime = Path(os.environ["MAGI_RUNTIME_DIR"])
mutable_static = Path(os.environ["MAGI_MUTABLE_STATIC_DIR"])
exports = Path(os.environ["MAGI_EXPORTS_DIR"])

fitz = types.ModuleType("fitz")
fitz.open = lambda *args, **kwargs: None
sys.modules.setdefault("fitz", fitz)

runner = runpy.run_path(str(candidate / "scripts/ops/run_test_suite.py"))
runner_out = runner["resolve_runtime_output"](".runtime/runner.json")
assert runner_out == runtime / "runner.json"
runner_out.parent.mkdir(parents=True, exist_ok=True)
runner_out.write_text("{}", encoding="utf-8")

smoke = runpy.run_path(str(candidate / "scripts/ops/smoke_test_full.py"))
smoke_out = smoke["_output_path"](".runtime/smoke.json")
assert smoke_out == runtime / "smoke.json"
smoke_out.write_text("{}", encoding="utf-8")
assert smoke["MUTABLE_STATIC_DIR"] == mutable_static

acceptance = runpy.run_path(str(candidate / "scripts/ops/magi_acceptance_gate.py"))
assert acceptance["DEFAULT_JSON_OUT"] == runtime / "magi_acceptance_latest.json"
acceptance["_write_json"](Path(".runtime/acceptance.json"), {"ok": True})

commercial = runpy.run_path(str(candidate / "scripts/ops/commercial_readiness_live.py"))
assert commercial["RUNTIME_DIR"] == runtime
commands = commercial["live_validation_commands"]("python3")
for command in commands.values():
    if "--json-out" in command:
        assert Path(command[command.index("--json-out") + 1]).is_relative_to(runtime)

skill_smoke = runpy.run_path(str(candidate / "scripts/ops/skill_realworld_smoke.py"))
assert skill_smoke["REPORT_DIR"] == mutable_static / "reports"
json_report, md_report = skill_smoke["write_reports"]({
    "generated_at": "test",
    "runnable_skill_count": 0,
    "non_runnable_skill_count": 0,
    "pass_count": 0,
    "warn_count": 0,
    "fail_count": 0,
    "results": [],
    "non_runnable_skills": [],
})
assert json_report.is_relative_to(mutable_static)
assert md_report.is_relative_to(mutable_static)

integration = runpy.run_path(str(candidate / "scripts/ops/integration_smoke.py"))
assert integration["REPORT_DIR"] == mutable_static
integration["_write_reports"]({"generated_at": "test", "overall_ok": True, "checks": []})

smart_source = (candidate / "scripts/ops/smart_model_router_live.py").read_text(encoding="utf-8")
assert 'os.environ.get("MAGI_RUNTIME_DIR", "").strip()' in smart_source
live_rag_source = (candidate / "scripts/live_test_tw_legal_rag.py").read_text(encoding="utf-8")
assert 'os.environ.get("MAGI_RUNTIME_DIR", "").strip()' in live_rag_source
summarize_source = (candidate / "scripts/ops/test_summarize_judgments.py").read_text(encoding="utf-8")
assert 'os.environ.get("MAGI_MUTABLE_STATIC_DIR", "").strip()' in summarize_source

apple = runpy.run_path(str(candidate / "scripts/tests/apple_intelligence_smoke_test.py"))
assert apple["REPORT_DIR"] == exports / "reports"
apple["REPORT_DIR"].mkdir(parents=True, exist_ok=True)
(apple["REPORT_DIR"] / "probe.txt").write_text("ok", encoding="utf-8")

print(json.dumps({"ok": True}))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=_v3_environment(candidate, shared),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == {"ok": True}
    assert (shared / "runtime" / "runner.json").is_file()
    assert (shared / "runtime" / "smoke.json").is_file()
    assert (shared / "runtime" / "acceptance.json").is_file()
    assert list((shared / "static" / "reports").glob("skill_realworld_smoke_*"))
    assert (shared / "static" / "integration_smoke_latest.json").is_file()
    assert (shared / "exports" / "reports" / "probe.txt").is_file()
    assert _snapshot(candidate) == before


def test_nightly_and_tunnel_paths_are_external_and_legacy_sync_fails_closed(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    files = ("scripts/nightly_council.py", "scripts/cloudflare_tunnel.sh", "daemon.py")
    _copy_candidate(candidate, files)
    shared = tmp_path / "shared"
    environment = _v3_environment(candidate, shared)
    environment.update(
        {
            "MAGI_CLOUDFLARE_TUNNEL_DRY_RUN": "1",
            "MAGI_CLOUDFLARED_LOG_PATH": str(shared / "logs" / "cloudflared.log"),
            "MAGI_DAEMON_LOG_PATH": str(shared / "agent" / "daemon.log"),
        }
    )
    before = _snapshot(candidate)
    code = r'''
import json
import os
import runpy
import sys
import types
from pathlib import Path

candidate = Path(os.environ["MAGI_ROOT_DIR"])
sys.modules.setdefault("requests", types.ModuleType("requests"))
brain = types.ModuleType("skills.brain_manager.action")
brain.switch_brain_mode = lambda *args, **kwargs: {}
sys.modules["skills.brain_manager.action"] = brain
nightly = runpy.run_path(str(candidate / "scripts/nightly_council.py"))
assert Path(nightly["LOG_FILE"]) == Path(os.environ["MAGI_DAEMON_LOG_PATH"])
assert Path(nightly["STATUS_FILE"]) == Path(os.environ["MAGI_MUTABLE_STATIC_DIR"]) / "magi_status.json"
assert "V3 immutable release" in nightly["sync_from_synology"]()
print(json.dumps({"ok": True}))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == {"ok": True}
    tunnel = subprocess.run(
        ["bash", str(candidate / "scripts/cloudflare_tunnel.sh")],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert f"LOG={shared / 'logs' / 'cloudflared.log'}" in tunnel.stdout
    assert f"AGENT_DIR={shared / 'agent'}" in tunnel.stdout
    daemon_source = (candidate / "daemon.py").read_text(encoding="utf-8")
    assert 'os.environ.get("MAGI_DAEMON_LOG_PATH", "").strip()' in daemon_source
    assert _snapshot(candidate) == before


def test_residual_manual_paths_preserve_v2_defaults(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    files = (
        "scripts/nightly_council.py",
        "scripts/cloudflare_tunnel.sh",
        "scripts/ops/run_test_suite.py",
        "scripts/ops/smoke_test_full.py",
        "scripts/ops/skill_realworld_smoke.py",
        "scripts/ops/integration_smoke.py",
        "scripts/ops/magi_acceptance_gate.py",
        "scripts/ops/commercial_readiness_live.py",
        "scripts/ops/smart_model_router_live.py",
        "scripts/tests/apple_intelligence_smoke_test.py",
    )
    _copy_candidate(candidate, files)
    environment = os.environ.copy()
    for name in (
        "MAGI_AGENT_DIR",
        "MAGI_APPLE_SMOKE_REPORT_DIR",
        "MAGI_CLOUDFLARED_LOG_PATH",
        "MAGI_DAEMON_LOG_PATH",
        "MAGI_DATA_DIR",
        "MAGI_EXPORTS_DIR",
        "MAGI_MUTABLE_STATIC_DIR",
        "MAGI_ROOT",
        "MAGI_ROOT_DIR",
        "MAGI_RUNTIME_DIR",
        "MAGI_SHARED_STATE_DIR",
        "MAGI_SKILL_SMOKE_REPORT_DIR",
        "MAGI_V3_RELEASE_ID",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "CANDIDATE_ROOT": str(candidate),
            "MAGI_CLOUDFLARE_TUNNEL_DRY_RUN": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    code = r'''
import json
import os
import runpy
import sys
import types
from pathlib import Path

candidate = Path(os.environ["CANDIDATE_ROOT"])
fitz = types.ModuleType("fitz")
fitz.open = lambda *args, **kwargs: None
sys.modules.setdefault("fitz", fitz)
sys.modules.setdefault("requests", types.ModuleType("requests"))
brain = types.ModuleType("skills.brain_manager.action")
brain.switch_brain_mode = lambda *args, **kwargs: {}
sys.modules["skills.brain_manager.action"] = brain

runner = runpy.run_path(str(candidate / "scripts/ops/run_test_suite.py"))
assert runner["RUNTIME_DIR"] == candidate / ".runtime"
assert runner["resolve_runtime_output"](".runtime/v2.json") == Path(".runtime/v2.json")

smoke = runpy.run_path(str(candidate / "scripts/ops/smoke_test_full.py"))
assert smoke["RUNTIME_DIR"] == candidate / ".runtime"
assert smoke["MUTABLE_STATIC_DIR"] == candidate / "static"
assert smoke["_output_path"]("legacy.json") == Path("legacy.json")

acceptance = runpy.run_path(str(candidate / "scripts/ops/magi_acceptance_gate.py"))
assert acceptance["DEFAULT_JSON_OUT"] == candidate / ".runtime/magi_acceptance_latest.json"

commercial = runpy.run_path(str(candidate / "scripts/ops/commercial_readiness_live.py"))
assert commercial["RUNTIME_DIR"] == candidate / ".runtime"

skill_smoke = runpy.run_path(str(candidate / "scripts/ops/skill_realworld_smoke.py"))
assert skill_smoke["REPORT_DIR"] == candidate / "static/reports"

integration = runpy.run_path(str(candidate / "scripts/ops/integration_smoke.py"))
assert integration["REPORT_DIR"] == candidate / "static"

smart = runpy.run_path(str(candidate / "scripts/ops/smart_model_router_live.py"))
assert smart["RUNTIME_DIR"] == candidate / ".runtime"

apple = runpy.run_path(str(candidate / "scripts/tests/apple_intelligence_smoke_test.py"))
assert apple["REPORT_DIR"] == candidate / "reports"

nightly = runpy.run_path(str(candidate / "scripts/nightly_council.py"))
assert Path(nightly["LOG_FILE"]) == candidate / "daemon.log"
assert Path(nightly["STATUS_FILE"]) == candidate / "static/magi_status.json"
print(json.dumps({"ok": True}))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == {"ok": True}

    environment["MAGI_ROOT"] = str(candidate)
    tunnel = subprocess.run(
        ["bash", str(candidate / "scripts/cloudflare_tunnel.sh")],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert f"LOG={candidate / 'logs' / 'cloudflared.log'}" in tunnel.stdout
    assert f"AGENT_DIR={candidate / '.agent'}" in tunnel.stdout


def test_v3_skill_purifier_execute_is_fail_closed(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    _copy_candidate(candidate, ("scripts/ops/purify_magi_skills.py",))
    (candidate / "skills" / "unlisted-skill").mkdir(parents=True)
    marker = candidate / "skills" / "unlisted-skill" / "SKILL.md"
    marker.write_text("immutable", encoding="utf-8")
    shared = tmp_path / "shared"
    environment = _v3_environment(candidate, shared)
    before = _snapshot(candidate)

    blocked = subprocess.run(
        [sys.executable, "-B", str(candidate / "scripts/ops/purify_magi_skills.py"), "--execute"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert blocked.returncode == 2
    assert json.loads(blocked.stdout.splitlines()[-1])["error"] == "immutable_v3_release"
    assert marker.is_file()
    assert _snapshot(candidate) == before

    for name in (
        "MAGI_AGENT_DIR",
        "MAGI_DATA_DIR",
        "MAGI_EXPORTS_DIR",
        "MAGI_MUTABLE_STATIC_DIR",
        "MAGI_ROOT",
        "MAGI_ROOT_DIR",
        "MAGI_RUNTIME_DIR",
        "MAGI_SHARED_STATE_DIR",
        "MAGI_V3_RELEASE_ID",
    ):
        environment.pop(name, None)
    legacy = subprocess.run(
        [sys.executable, "-B", str(candidate / "scripts/ops/purify_magi_skills.py")],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(legacy.stdout.splitlines()[-1])
    assert Path(payload["report_path"]).is_relative_to(candidate / "static")
