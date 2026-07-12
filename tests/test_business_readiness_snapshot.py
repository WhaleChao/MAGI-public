import json
from datetime import datetime

from scripts.ops.business_readiness_snapshot import build_snapshot, main


def test_snapshot_surfaces_hidden_business_blockers(tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".runtime").mkdir()
    (tmp_path / "static" / "laf_portal_new_files_latest.json").write_text(
        '{"portal_still_missing": 2}', encoding="utf-8"
    )
    (tmp_path / ".agent" / "laf_pending_portal_downloads.json").write_text(
        '{"items":[{"status":"manual_review"},{"status":"pending_retry"}]}', encoding="utf-8"
    )
    (tmp_path / "static" / "file_review_auto_state.json").write_text(
        '{"result":{"ok":true,"reason":"auto_download_disabled"}}', encoding="utf-8"
    )

    result = build_snapshot(
        root=tmp_path,
        env={"NVIDIA_NIM_ENABLE": "0"},
        exec_fn=lambda *_args, **_kwargs: ([], None),
        now=datetime(2026, 7, 11, 12, 0),
        mlx_available=False,
        whisper_cli="/opt/homebrew/bin/whisper",
    )

    assert result["state"] == "attention"
    assert result["items"]["法扶附件"]["label"] == "2份欠檔"
    assert len(result["items"]["法扶附件"]["retry_items"]) == 2
    assert result["items"]["閱卷下載"]["label"] == "僅掃描未下載"
    assert result["items"]["錄音轉文字"]["label"] == "CPU備援"


def test_snapshot_is_green_when_automation_and_engines_are_ready(tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".runtime").mkdir()
    (tmp_path / "static" / "laf_portal_new_files_latest.json").write_text(
        '{"portal_still_missing": 0}', encoding="utf-8"
    )
    (tmp_path / ".agent" / "laf_pending_portal_downloads.json").write_text(
        '{"items":[]}', encoding="utf-8"
    )
    (tmp_path / "static" / "file_review_auto_state.json").write_text(
        '{"result":{"ok":true,"downloaded_count":0}}', encoding="utf-8"
    )
    (tmp_path / ".runtime" / "heavy_fallback_live_latest.json").write_text(
        '{"success":true}', encoding="utf-8"
    )

    result = build_snapshot(
        root=tmp_path,
        env={
            "MAGI_FILE_REVIEW_AUTO_DOWNLOAD": "1",
            "NVIDIA_NIM_ENABLE": "1",
            "NVIDIA_NIM_MODEL": "nvidia/nemotron-3-super-120b-a12b",
        },
        exec_fn=lambda *_args, **_kwargs: ([], None),
        now=datetime.now(),
        mlx_available=True,
        whisper_cli="",
    )

    assert result["ok"] is True
    assert result["state"] == "ok"
    assert result["items"]["閱卷下載"]["label"] == "自動下載正常"
    assert result["items"]["NVIDIA重型"]["state"] == "ok"


def test_snapshot_surfaces_failed_file_review_background_job(tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".runtime").mkdir()
    jobs = tmp_path / "skills" / "file-review-orchestrator" / "_bg_jobs"
    jobs.mkdir(parents=True)
    (tmp_path / "static" / "laf_portal_new_files_latest.json").write_text(
        '{"portal_still_missing": 0}', encoding="utf-8"
    )
    (tmp_path / ".agent" / "laf_pending_portal_downloads.json").write_text(
        '{"items":[]}', encoding="utf-8"
    )
    (tmp_path / "static" / "file_review_auto_state.json").write_text(
        '{"result":{"ok":true}}', encoding="utf-8"
    )
    (jobs / "download_latest.json").write_text(
        '{"status":"failed","success":false}', encoding="utf-8"
    )

    result = build_snapshot(
        root=tmp_path,
        env={"MAGI_FILE_REVIEW_AUTO_DOWNLOAD": "1"},
        exec_fn=lambda *_args, **_kwargs: ([], None),
        now=datetime(2026, 7, 11, 12, 0),
        mlx_available=True,
        whisper_cli="",
    )

    assert result["items"]["閱卷下載"]["state"] == "attention"
    assert result["items"]["閱卷下載"]["label"] == "下載工作失敗"


def test_snapshot_clears_old_missing_docs_failure_after_final_ruling_arrives(tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".runtime").mkdir()
    case_folder = tmp_path / "2025-0001-測試-消費者債務清理-清算"
    final_docs = case_folder / "10_判決書或終局裁定及處分"
    final_docs.mkdir(parents=True)
    (final_docs / "20260703民事裁定（主文：債務人不免責）.pdf").write_bytes(b"%PDF-1.4")
    job = {
        "ts": "2026-07-10T10:00:00",
        "status": "failed",
        "result": {
            "error": "missing_required_docs",
            "identity": {"case_number": "2025-0001", "case_folder": str(case_folder)},
        },
    }
    (tmp_path / ".runtime" / "laf_report_jobs.jsonl").write_text(
        json.dumps(job, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (tmp_path / "static" / "laf_portal_new_files_latest.json").write_text(
        '{"portal_still_missing": 0}', encoding="utf-8"
    )
    (tmp_path / ".agent" / "laf_pending_portal_downloads.json").write_text(
        '{"items":[]}', encoding="utf-8"
    )
    (tmp_path / "static" / "file_review_auto_state.json").write_text(
        '{"result":{"ok":true}}', encoding="utf-8"
    )

    result = build_snapshot(
        root=tmp_path,
        env={"MAGI_FILE_REVIEW_AUTO_DOWNLOAD": "1"},
        exec_fn=lambda *_args, **_kwargs: ([], None),
        now=datetime(2026, 7, 11, 12, 0),
        mlx_available=True,
        whisper_cli="",
    )

    assert result["items"]["案件回報"]["state"] != "attention"


def test_snapshot_command_does_not_turn_business_attention_into_cron_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "scripts.ops.business_readiness_snapshot.build_snapshot",
        lambda **_kwargs: {"ok": False, "state": "attention"},
    )
    output = tmp_path / "snapshot.json"

    assert main(["--json-out", str(output)]) == 0


def test_snapshot_surfaces_escalated_todo_reviews(tmp_path, monkeypatch):
    (tmp_path / "static").mkdir()
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".runtime").mkdir()
    (tmp_path / "static" / "laf_portal_new_files_latest.json").write_text(
        '{"portal_still_missing": 0}', encoding="utf-8"
    )
    (tmp_path / ".agent" / "laf_pending_portal_downloads.json").write_text(
        '{"items":[]}', encoding="utf-8"
    )
    (tmp_path / "static" / "file_review_auto_state.json").write_text(
        '{"result":{"ok":true}}', encoding="utf-8"
    )
    monkeypatch.setattr(
        "scripts.ops.business_readiness_snapshot._operations",
        lambda _exec_fn: {"closing_pending_cases": 7, "pending_review_todos": 6},
    )

    result = build_snapshot(
        root=tmp_path,
        env={"MAGI_FILE_REVIEW_AUTO_DOWNLOAD": "1"},
        now=datetime(2026, 7, 12, 12, 0),
        mlx_available=True,
        whisper_cli="",
    )

    assert result["items"]["案件回報"]["label"] == "7案回報／6項確認"
    assert result["items"]["案件回報"]["review_pending"] == 6


def test_snapshot_adds_readable_case_and_review_details(tmp_path, monkeypatch):
    (tmp_path / "static").mkdir()
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".runtime").mkdir()
    (tmp_path / "static" / "laf_portal_new_files_latest.json").write_text('{"portal_still_missing":0}', encoding="utf-8")
    (tmp_path / ".agent" / "laf_pending_portal_downloads.json").write_text('{"items":[]}', encoding="utf-8")
    (tmp_path / "static" / "file_review_auto_state.json").write_text('{"result":{"ok":true}}', encoding="utf-8")
    monkeypatch.setattr(
        "scripts.ops.business_readiness_snapshot._operations",
        lambda _exec_fn: {
            "closing_pending_cases": 1,
            "pending_review_todos": 1,
            "closing_pending_items": [
                {"case_number": "2026-0001", "client_name": "王小明", "status": "已結案", "legal_aid_status": "已結案，待報結"}
            ],
            "pending_review_items": [
                {
                    "case_number": "2026-0002",
                    "client_name": "林小華",
                    "todo_date": "2026-07-13",
                    "description": "【MAGI逾期治理：原待辦#9】\n原期限：2026-07-01／原類型：陳報\n尚無可驗證的完成證據\n補正資料\nMAGI分享連結：https://example.invalid",
                }
            ],
        },
    )

    result = build_snapshot(root=tmp_path, env={"MAGI_FILE_REVIEW_AUTO_DOWNLOAD": "1"}, mlx_available=True, whisper_cli="")
    item = result["items"]["案件回報"]

    assert item["pending_items"] == [{"case_number": "2026-0001", "client_name": "王小明", "status": "已結案，待報結"}]
    assert item["review_items"][0]["original_due_date"] == "2026-07-01"
    assert item["review_items"][0]["original_type"] == "陳報"
    assert item["review_items"][0]["summary"] == "補正資料"
    assert "example.invalid" not in json.dumps(item, ensure_ascii=False)


def test_snapshot_adds_laf_retry_case_details(tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".runtime").mkdir()
    (tmp_path / "static" / "laf_portal_new_files_latest.json").write_text('{"portal_still_missing":0}', encoding="utf-8")
    (tmp_path / ".agent" / "laf_pending_portal_downloads.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "case_number": "2026-0060",
                        "laf_case_number": "1150529-W-002",
                        "client_name": "林文俊",
                        "case_type": "民事",
                        "case_reason": "返還借款",
                        "status": "pending_retry",
                        "reason": "portal_not_listed",
                        "tries": 16,
                        "last_try_at": "2026-07-12T10:45:46",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "static" / "file_review_auto_state.json").write_text('{"result":{"ok":true}}', encoding="utf-8")

    result = build_snapshot(
        root=tmp_path,
        env={"MAGI_FILE_REVIEW_AUTO_DOWNLOAD": "1"},
        exec_fn=lambda *_args, **_kwargs: ([], None),
        mlx_available=True,
        whisper_cli="",
    )
    item = result["items"]["法扶附件"]

    assert item["label"] == "1案重試中"
    assert item["retry_items"][0]["case_number"] == "2026-0060"
    assert item["retry_items"][0]["reason"] == "法扶網站目前尚未列出可下載附件"
    assert item["retry_items"][0]["last_try_at"] == "2026-07-12 10:45:46"
