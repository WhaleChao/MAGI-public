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

