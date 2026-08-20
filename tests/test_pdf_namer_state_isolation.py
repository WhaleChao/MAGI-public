from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = ROOT / "skills" / "pdf-namer"


def _copy_candidate(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate" / "skills" / "pdf-namer"
    candidate.mkdir(parents=True)
    for source in SOURCE_SKILL.glob("*.py"):
        shutil.copy2(source, candidate / source.name)
    # A legacy seed verifies read compatibility without permitting backwrites.
    (candidate / "training_data.json").write_text(
        json.dumps([{"filename": "legacy.pdf", "category": "seed"}]),
        encoding="utf-8",
    )
    (candidate / "_corrections.json").write_text("[]\n", encoding="utf-8")
    return candidate


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _candidate_env(candidate: Path, state: Path, case_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "MAGI_V3_STATE_DIR": str(state),
            "MAGI_SHARED_STATE_DIR": str(state),
            "MAGI_V3_SHARED_STATE_DIR": str(state),
            "MAGI_PDF_NAMER_STATE_DIR": str(state / "pdf-namer"),
            "MAGI_PDF_NAMER_CASE_INDEX": str(
                state / "pdf-namer" / "_case_index.json"
            ),
            "MAGI_CASE_ROOT": str(case_root),
            "MAGI_PDF_NAMER_LOAD_DOTENV": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join((str(candidate), str(ROOT))),
        }
    )
    return env


def test_candidate_import_is_side_effect_free_and_prefers_explicit_state(tmp_path: Path) -> None:
    candidate = _copy_candidate(tmp_path)
    v3_state = tmp_path / "runtime-state"
    explicit_state = tmp_path / "explicit-state"
    case_root = tmp_path / "cases"
    case_root.mkdir()
    before = _tree_digest(candidate)

    env = _candidate_env(candidate, v3_state, case_root)
    env["MAGI_PDF_NAMER_STATE_DIR"] = str(explicit_state)
    code = """
import sys
sys.modules['sentence_transformers'] = None
import state_paths
assert state_paths.pdf_namer_state_dir() == state_paths.Path(__import__('os').environ['MAGI_PDF_NAMER_STATE_DIR'])
import action
import training_loader
import smart_filer
import nightly_train
import naming_rules
import nightly_layout
import rag_feedback
import rename_watcher
import layout_extractor
import naming_validator
import vision_parser
assert str(action.JOB_DIR).startswith(__import__('os').environ['MAGI_PDF_NAMER_STATE_DIR'])
"""
    subprocess.run(
        [sys.executable, "-B", "-c", code],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert _tree_digest(candidate) == before
    assert not explicit_state.exists(), "imports must not create runtime state"
    assert not v3_state.exists(), "explicit state must take precedence without side effects"


def test_v2_without_state_environment_keeps_legacy_skill_directory(tmp_path: Path) -> None:
    candidate = _copy_candidate(tmp_path)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(candidate)
    env.pop("MAGI_PDF_NAMER_STATE_DIR", None)
    env.pop("MAGI_V3_STATE_DIR", None)
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import state_paths; assert state_paths.pdf_namer_state_dir() == state_paths.SKILL_DIR",
        ],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_isolated_runtime_refuses_release_tree_write_target(tmp_path: Path) -> None:
    candidate = _copy_candidate(tmp_path)
    before = _tree_digest(candidate)
    env = os.environ.copy()
    env.update(
        {
            "MAGI_PDF_NAMER_STATE_DIR": str(candidate / "runtime-state"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(candidate),
        }
    )
    code = """
import state_paths
try:
    state_paths.prepare_write(state_paths.state_path('blocked.json'))
except RuntimeError:
    pass
else:
    raise AssertionError('release-tree state write was not rejected')
"""
    subprocess.run(
        [sys.executable, "-B", "-c", code],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert _tree_digest(candidate) == before


def test_isolated_runtime_rejects_nested_directory_and_file_symlinks(tmp_path: Path) -> None:
    candidate = _copy_candidate(tmp_path)
    state = tmp_path / "runtime-state"
    outside = tmp_path / "outside"
    state.mkdir()
    outside.mkdir()
    (state / "logs").symlink_to(outside, target_is_directory=True)
    (state / "training_data.json").symlink_to(outside / "training_data.json")
    before = _tree_digest(candidate)
    env = os.environ.copy()
    env.update(
        {
            "MAGI_PDF_NAMER_STATE_DIR": str(state),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(candidate),
        }
    )
    code = """
import state_paths
for relative in ('logs/nightly_train.log', 'training_data.json'):
    try:
        state_paths.prepare_write(state_paths.state_path(relative))
    except RuntimeError:
        pass
    else:
        raise AssertionError(f'symlinked state path was accepted: {relative}')
"""
    subprocess.run(
        [sys.executable, "-B", "-c", code],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert not any(outside.iterdir())
    assert _tree_digest(candidate) == before


def test_v3_writes_and_nightly_dry_run_leave_candidate_tree_immutable(tmp_path: Path) -> None:
    candidate = _copy_candidate(tmp_path)
    v3_state = tmp_path / "runtime-state"
    case_root = tmp_path / "empty-cases"
    case_root.mkdir()
    before = _tree_digest(candidate)
    env = _candidate_env(candidate, v3_state, case_root)

    exercise = """
import json
import os
import sys
sys.modules['sentence_transformers'] = None
import training_loader
training_loader._append_to_training_data({'filename': 'runtime.pdf', 'category': 'runtime'})
training_loader._cache_rules([])
training_loader._save_learning_local('old.pdf', '法院通知', 'new.pdf', 1.0, '')
import action
action._write_job('isolation', {'status': 'dry_run'})
action.build_filename_learning_rules(case_root=os.environ['MAGI_CASE_ROOT'], min_token_count=1)
import smart_filer
smart_filer._save_filing_log({'timestamp': '2026-07-16T00:00:00', 'filed': []})
smart_filer.CASE_ROOT = os.environ['MAGI_CASE_ROOT']
smart_filer.build_case_index(force_rebuild=True)
import nightly_train
nightly_train._auto_adjust_filing_threshold({'metrics': {'date_total': 5, 'date_accuracy_pct': 90, 'party_accuracy_pct': 80}})
import rename_watcher
rename_watcher.save_snapshot({})
learning = {
    'detected_at': '2026-07-16T00:00:00',
    'new_filename': 'new.pdf',
    'old_filename': 'old.pdf',
    'case': '2026-0001',
    'subfolder': '法院通知或程序裁定',
    'parties': [],
    'corrections': {'date': {'from': '', 'to': '20260716'}},
}
rename_watcher.append_correction(learning)
rename_watcher.log_rename(learning)
import rag_feedback
rag_feedback.rag_engine.log_feedback('runtime feedback text', '2026-0001', '法院通知', 'rag.pdf')
"""
    subprocess.run(
        [sys.executable, "-B", "-c", exercise],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    subprocess.run(
        [
            sys.executable,
            "-B",
            str(candidate / "nightly_train.py"),
            "--max-files",
            "1",
            "--dry-run",
            "--report-only",
        ],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert _tree_digest(candidate) == before
    pdf_state = v3_state / "pdf-namer"
    training = json.loads((pdf_state / "training_data.json").read_text(encoding="utf-8"))
    expected_training = ["runtime.pdf", "rag.pdf"]
    if not os.environ.get("MAGI_V3_RELEASE_ID"):
        expected_training.insert(0, "legacy.pdf")
    assert [item["filename"] for item in training] == expected_training
    assert (pdf_state / "_bg_jobs" / "file_isolation.json").is_file()
    assert (pdf_state / "_case_index.json").is_file()
    assert (pdf_state / "_corrections.json").is_file()
    assert (pdf_state / "_filing_log.json").is_file()
    assert (pdf_state / "_learned_filename_rules.json").is_file()
    assert (pdf_state / "_pending_learns.json").is_file()
    assert (pdf_state / "_rename_snapshot.json").is_file()
    assert (pdf_state / "_rename_log.json").is_file()
    assert (pdf_state / "_threshold_state.json").is_file()
    assert (pdf_state / "db_rules_cache.json").is_file()
    assert (pdf_state / "logs" / "nightly_train.log").is_file()
