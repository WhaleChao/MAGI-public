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


def test_transcript_batched_sync_reports_progress_even_without_downloads():
    mod = _load_transcript_action()

    note = mod._format_transcript_batch_note(
        {"batched": True, "selected_cases": 24, "eligible_cases": 72},
        {"cycle_scanned_cases": 48, "eligible_cases": 72},
    )

    assert "本輪分批掃描：24/72 案" in note
    assert "目前 cycle 已掃 48/72 案" in note
    assert "尚餘 24 案" in note
    assert mod._transcript_notify_topic({"batched": True}, {"downloaded_count": 0}) == "transcript"


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
