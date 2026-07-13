from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_transcript_action():
    action_path = Path(__file__).resolve().parents[1] / "skills" / "transcript-downloader" / "action.py"
    spec = importlib.util.spec_from_file_location("transcript_downloader_action_for_test", action_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_transcript_summary_lists_only_cases_with_downloads():
    mod = _load_transcript_action()
    msg, summary = mod._summarize_download_results(
        {
            "cases": [
                {
                    "success": True,
                    "client_name": "董碧雲",
                    "court_case_number": "114年度司執消債清字第000181號",
                    "files": [],
                },
                {
                    "success": True,
                    "client_name": "陳瀚",
                    "court_case_number": "115年度原上訴字第000091號",
                    "files": [
                        "/tmp/06163123.003.pdf",
                        "/tmp/01134746.003.pdf",
                        "/tmp/10101010.003.pdf",
                        "/tmp/20202020.003.pdf",
                        "/tmp/30303030.003.pdf",
                    ],
                },
                {
                    "success": True,
                    "client_name": "張偉銘",
                    "court_case_number": "114年度原訴字第000024號",
                    "files": [],
                },
            ]
        }
    )

    assert "5 份，1 案有新檔 / 掃描 3 案" in msg
    assert "陳瀚｜115年度原上訴字第000091號（5 份）" in msg
    assert "06163123.003.pdf" in msg
    assert "30303030.003.pdf" in msg
    assert "董碧雲" not in msg
    assert "張偉銘" not in msg
    assert summary["downloaded_count"] == 5
    assert summary["downloaded_cases_count"] == 1
    assert summary["scanned_cases_count"] == 3
    assert [case["client_name"] for case in summary["cases"]] == ["陳瀚"]


def test_transcript_summary_keeps_all_downloaded_files_by_default():
    mod = _load_transcript_action()
    files = [f"/tmp/file_{idx:02d}.pdf" for idx in range(15)]
    msg, summary = mod._summarize_download_results(
        {
            "cases": [
                {
                    "success": True,
                    "client_name": "測試當事人",
                    "court_case_number": "115年度測字第1號",
                    "files": files,
                }
            ]
        }
    )

    assert "file_00.pdf" in msg
    assert "file_14.pdf" in msg
    assert "其餘" not in msg
    assert summary["cases"][0]["files"] == files


def test_transcript_summary_uses_case_folder_for_display_name_typos():
    mod = _load_transcript_action()
    msg, summary = mod._summarize_download_results(
        {
            "cases": [
                {
                    "success": True,
                    "case_number": "2026-0045",
                    "client_name": "李秀瑛",
                    "court_case_number": "115年度勞簡字第1號",
                    "folder_path": "/案件/法扶案件/行政/2026-0045-李秀英-一審-勞工保險爭議",
                    "files": ["/tmp/transcript.pdf"],
                }
            ]
        }
    )

    assert "李秀英｜115年度勞簡字第1號（1 份）" in msg
    assert "李秀瑛" not in msg
    assert summary["cases"][0]["client_name"] == "李秀英"


def test_transcript_batched_sync_keeps_progress_for_report_but_not_notification_topic():
    mod = _load_transcript_action()

    note = mod._format_transcript_batch_note(
        {"batched": True, "selected_cases": 24, "eligible_cases": 72},
        {"cycle_scanned_cases": 48, "eligible_cases": 72},
    )

    assert "本輪分批掃描：24/72 案" in note
    assert "目前 cycle 已掃 48/72 案" in note
    assert "尚餘 24 案" in note
    assert mod._transcript_notify_topic({"batched": True}, {"downloaded_count": 0}) == "quiet_cron"
    assert mod._should_notify_transcript_success({"downloaded_count": 0, "failed_cases_count": 0}) is False


def test_transcript_success_notification_only_for_new_files_or_actionable_items():
    mod = _load_transcript_action()

    assert mod._should_notify_transcript_success({"downloaded_count": 1, "failed_cases_count": 0}) is True
    assert mod._should_notify_transcript_success({"downloaded_count": 0, "downloaded_cases_count": 1}) is True
    assert mod._should_notify_transcript_success({"downloaded_count": 0, "failed_cases_count": 2}) is True
    assert mod._should_notify_transcript_success({"downloaded_count": 0}, md5_warning="MD5 掃描逾時") is True


def test_transcript_sync_report_writes_latest(tmp_path, monkeypatch):
    mod = _load_transcript_action()
    monkeypatch.setattr(mod, "TRANSCRIPT_SYNC_RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(mod, "TRANSCRIPT_SYNC_STATE_PATH", tmp_path / "state.json")

    latest = mod._write_transcript_sync_report(
        {"success": True, "batched": True, "selected_cases": 24, "eligible_cases": 72, "cases": []},
        {"downloaded_count": 0, "scanned_cases_count": 24},
        "msg",
    )

    latest_path = Path(latest)
    assert latest_path.exists()
    data = latest_path.read_text(encoding="utf-8")
    assert '"batched": true' in data
    assert '"selected_cases": 24' in data
    assert '"last_batch_latest_path"' in (tmp_path / "state.json").read_text(encoding="utf-8")


def test_cmd_sync_suppresses_zero_download_success_notification(tmp_path, monkeypatch):
    mod = _load_transcript_action()
    calls: list[tuple[str, bool, str]] = []

    class FakeDownloader:
        def __init__(self, *args, **kwargs):
            pass

        def rename_all_transcripts(self):
            return None

        def close(self):
            return None

    class FakeImports:
        CourtRecordDownloader = FakeDownloader

    monkeypatch.setattr(mod, "TRANSCRIPT_SYNC_RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(mod, "TRANSCRIPT_SYNC_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(mod, "TRANSCRIPT_SYNC_LOCK_PATH", tmp_path / "lock.json")
    monkeypatch.setattr(mod, "_eventlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_safe_flow_step_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_ensure_local_cases_schema", lambda: None)
    monkeypatch.setattr(mod, "_load_config", lambda: {})
    monkeypatch.setattr(
        mod,
        "_get_credentials",
        lambda _cfg: {"username": "u", "password": "p", "download_folder": str(tmp_path)},
    )
    monkeypatch.setattr(mod, "_check_flow_cancelled", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_acquire_sync_lock", lambda: (True, ""))
    monkeypatch.setattr(mod, "_release_sync_lock", lambda: None)
    monkeypatch.setattr(mod, "_ensure_imports", lambda: FakeImports)
    monkeypatch.setattr(mod, "_get_db_manager", lambda _cfg: object())
    monkeypatch.setattr(
        mod,
        "_download_sync_batch",
        lambda *_args, **_kwargs: {
            "success": True,
            "batched": True,
            "selected_cases": 24,
            "eligible_cases": 77,
            "sync_status": {
                "cycle_scanned_cases": 18,
                "eligible_cases": 77,
                "last_cycle_completed_at": "2026-06-30T06:03:27.043131",
            },
            "cases": [{"success": True, "client_name": "測試", "court_case_number": "115年度測字第1號", "files": []}],
        },
    )
    monkeypatch.setattr(mod, "_notify", lambda text, flag=True, topic_key="transcript": calls.append((text, flag, topic_key)))

    out = mod.cmd_sync(rename=True, notify=True, run_md5_scan=False)

    assert out["success"] is True
    assert out["downloaded_count"] == 0
    assert out["notified"] is False
    assert out["notify_suppressed_reason"] == "no_new_transcripts"
    assert calls == []
