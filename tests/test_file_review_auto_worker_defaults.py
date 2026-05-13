from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_file_review_auto_worker_runs_and_downloads_by_default():
    src = (ROOT / "skills" / "ops" / "file_review_auto_worker.py").read_text(encoding="utf-8")
    assert 'MAGI_FILE_REVIEW_AUTO_RUN_ON_START", "1"' in src
    assert 'MAGI_FILE_REVIEW_AUTO_DOWNLOAD", "1"' in src
    assert 'MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", "1"' in src


def test_file_review_downloadable_probe_cross_checks_gmail_by_default():
    src = (ROOT / "skills" / "file-review-orchestrator" / "action.py").read_text(encoding="utf-8")
    assert 'MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", "1"' in src
