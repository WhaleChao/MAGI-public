import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _load_transcript_action():
    path = ROOT / "skills" / "transcript-downloader" / "action.py"
    spec = importlib.util.spec_from_file_location("transcript_rc241", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_events_resolve_to_mutable_agent_dir(monkeypatch, tmp_path):
    from api import tools_api

    release = tmp_path / "immutable-release"
    agent = tmp_path / "shared" / "agent"
    release.mkdir()
    release.chmod(0o555)
    monkeypatch.setattr(tools_api, "get_agent_dir", lambda: agent)

    event_path = Path(tools_api._resolve_tools_events_path())

    assert event_path == agent / "tools_runtime_events.jsonl"
    assert release not in event_path.parents
    event_path.parent.mkdir(parents=True)
    event_path.write_text("{}\n", encoding="utf-8")
    assert event_path.read_text(encoding="utf-8") == "{}\n"


def test_transcript_default_download_dir_is_outside_sealed_release(monkeypatch, tmp_path):
    from api.runtime_paths import get_transcript_download_dir

    release = tmp_path / "immutable-release"
    shared = tmp_path / "shared"
    monkeypatch.setenv("MAGI_ROOT_DIR", str(release))
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-test")
    monkeypatch.setenv("MAGI_SHARED_STATE_DIR", str(shared))
    monkeypatch.delenv("MAGI_TRANSCRIPT_DOWNLOAD_DIR", raising=False)

    resolved = get_transcript_download_dir()

    assert resolved == (shared / "transcript-downloads").resolve()
    assert release.resolve() not in resolved.parents


def test_transcript_batch_deduplicates_repeated_download_paths(monkeypatch):
    action = _load_transcript_action()

    class Downloader:
        _last_download_error = ""
        _last_no_new_files_reason = ""

        def cleanup_download_folder(self):
            return {}

        def login(self):
            return True

        def get_cases_from_db(self):
            return [
                SimpleNamespace(
                    case_number="2026-0001",
                    court_case_number="115年度訴字第1號",
                    client_name="測試",
                    court_name="臺灣測試地方法院",
                    case_type="刑事",
                    folder_path="cases/2026-0001",
                )
            ]

        def download_record(self, _case):
            return ["same.pdf", "same.pdf"]

        def move_to_case_folder(self, _case, files):
            assert files == ["same.pdf"]
            return [
                {
                    "status": "archived",
                    "archive_reference": "06_筆錄/same.pdf",
                    "sha256": "a" * 64,
                    "case_identity_match": True,
                    "readable": True,
                }
            ]

    monkeypatch.setattr(action, "_load_sync_state", lambda: {})
    monkeypatch.setattr(action, "_save_sync_state", lambda _state: None)
    result = action._download_sync_batch(Downloader(), batch_size=1, notify=False)

    assert result["files"] == ["06_筆錄/same.pdf"]
    assert result["cases"][0]["files"] == ["06_筆錄/same.pdf"]
    assert result["cases"][0]["archive_receipts"][0]["sha256"] == "a" * 64


def test_transcript_batch_quarantines_identity_mismatch_and_keeps_durable_retry(monkeypatch):
    action = _load_transcript_action()

    class Downloader:
        _last_download_error = ""
        _last_no_new_files_reason = ""

        def cleanup_download_folder(self):
            return {}

        def login(self):
            return True

        def get_cases_from_db(self):
            return [
                SimpleNamespace(
                    case_number="2026-0002",
                    court_case_number="115年度訴字第2號",
                    client_name="測試",
                    court_name="臺灣測試地方法院",
                    case_type="刑事",
                    folder_path="cases/2026-0002",
                )
            ]

        def download_record(self, _case):
            return ["wrong-case.pdf"]

        def move_to_case_folder(self, _case, _files):
            return [
                {
                    "status": "quarantined",
                    "reason": "case_identity_mismatch",
                    "quarantine_reference": "wrong-case.pdf",
                    "sha256": "b" * 64,
                    "case_identity_match": False,
                    "readable": True,
                }
            ]

    monkeypatch.setattr(action, "_load_sync_state", lambda: {})
    monkeypatch.setattr(action, "_save_sync_state", lambda _state: None)

    result = action._download_sync_batch(Downloader(), batch_size=1, notify=False)

    assert result["success"] is True
    assert result["status"] == "partial_retry_pending"
    assert result["retryable"] is True
    assert result["retry_pending_count"] == 1
    assert result["cases"][0]["status"] == "quarantined_retry_pending"
    assert result["cases"][0]["error"].startswith(
        "transcript_case_identity_quarantined:"
    )
    assert result["cases"][0]["files"] == []
    assert result["cases"][0]["archive_receipts"][0]["status"] == "quarantined"


def test_transcript_batch_keeps_unproven_quarantine_as_hard_failure(monkeypatch):
    action = _load_transcript_action()

    class Downloader:
        _last_download_error = ""
        _last_no_new_files_reason = ""

        def cleanup_download_folder(self):
            return {}

        def login(self):
            return True

        def get_cases_from_db(self):
            return [
                SimpleNamespace(
                    case_number="2026-0003",
                    court_case_number="115年度訴字第3號",
                    client_name="測試",
                    court_name="臺灣測試地方法院",
                    case_type="刑事",
                    folder_path="cases/2026-0003",
                )
            ]

        def download_record(self, _case):
            return ["unproven.pdf"]

        def move_to_case_folder(self, _case, _files):
            return [
                {
                    "status": "quarantined",
                    "reason": "case_identity_mismatch",
                    "quarantine_reference": "unproven.pdf",
                    "sha256": "not-a-valid-digest",
                    "case_identity_match": False,
                    "readable": True,
                }
            ]

    monkeypatch.setattr(action, "_load_sync_state", lambda: {})
    monkeypatch.setattr(action, "_save_sync_state", lambda _state: None)

    result = action._download_sync_batch(Downloader(), batch_size=1, notify=False)

    assert result["success"] is False
    assert result["error"] == "transcript batch failed for 1 case(s)"
    assert result["cases"][0]["status"] == "archive_failed"


def test_large_transcript_download_persists_privacy_safe_pdf_progress(monkeypatch):
    action = _load_transcript_action()
    snapshots = []

    class Downloader:
        _last_download_error = ""
        _last_no_new_files_reason = ""

        def __init__(self):
            self.log_callback = lambda _message: None

        def cleanup_download_folder(self):
            return {}

        def login(self):
            return True

        def get_cases_from_db(self):
            return [
                SimpleNamespace(
                    case_number="2026-0001",
                    court_case_number="115年度訴字第1號",
                    client_name="測試當事人",
                    court_name="臺灣測試地方法院",
                    case_type="刑事",
                    folder_path="cases/2026-0001",
                )
            ]

        def download_record(self, _case):
            self.log_callback("下載 PDF #2/58")
            return []

    monkeypatch.setattr(action, "_load_sync_state", lambda: {})
    monkeypatch.setattr(action, "_save_sync_state", lambda state: snapshots.append(copy.deepcopy(state)))

    result = action._download_sync_batch(Downloader(), batch_size=1, notify=False)

    assert result["success"] is True
    progress = [
        item["active_case_progress"]
        for item in snapshots
        if (item.get("active_case_progress") or {}).get("phase") == "downloading_pdfs"
    ]
    assert progress
    assert progress[-1]["pdf_index"] == 2
    assert progress[-1]["pdf_total"] == 58
    assert set(progress[-1]) == {
        "case_index", "selected_cases", "phase", "pdf_index", "pdf_total", "updated_at"
    }


def test_transcript_outer_phase_never_persists_case_identity(monkeypatch):
    action = _load_transcript_action()
    saved = []
    monkeypatch.setattr(action, "_load_sync_state", lambda: {"eligible_cases": 79})
    monkeypatch.setattr(action, "_save_sync_state", lambda state: saved.append(copy.deepcopy(state)))

    action._set_transcript_sync_phase("renaming_transcripts")

    assert saved[-1]["active_case_progress"]["phase"] == "renaming_transcripts"
    assert set(saved[-1]["active_case_progress"]) == {"phase", "updated_at"}


def test_storage_loss_never_generates_all_files_missing_followup():
    source = Path(__file__).parents[1] / "scripts" / "weekend_bookmark_batch.py"
    text = source.read_text(encoding="utf-8")

    assert 'str(stage.get("defer_reason") or "") == "storage_unavailable"' in text
    assert 'if storage_unavailable:' in text
