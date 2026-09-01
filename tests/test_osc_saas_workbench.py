import json
from pathlib import Path

import pytest


def test_osc_refresh_hard_exit_flushes_before_native_teardown() -> None:
    from scripts.ops import osc_events_refresh

    events: list[object] = []

    class Stream:
        def __init__(self, name: str):
            self.name = name

        def flush(self) -> None:
            events.append(self.name)

    osc_events_refresh._hard_exit_after_flush(
        0,
        stdout=Stream("stdout"),
        stderr=Stream("stderr"),
        exit_fn=lambda code: events.append(("exit", code)),
    )

    assert events == ["stdout", "stderr", ("exit", 0)]


def test_quality_check_blocks_prompt_leak_and_internal_case_number():
    from api.osc.saas_workbench import quality_check

    result = quality_check(
        {
            "case_number": "114年度訴字第123號",
            "reason": "損害賠償",
            "text": "<|channel>thought\nOSC-2026-001 應准許。",
        }
    )

    assert result["pass"] is False
    codes = {x["code"] for x in result["issues"]}
    assert "prompt_or_reasoning_leak" in codes
    assert "internal_case_number" in codes
    assert result["legal_workflow"]["enabled"] is True
    assert result["legal_workflow"]["agent"]["key"] == "pleading_review_agent"


def test_client_packet_uses_debt_checklist():
    from api.osc.saas_workbench import build_client_packet

    def fake_exec(*args, **kwargs):
        return ({}, None)

    result = build_client_packet(
        fake_exec,
        {"client_name": "王小明", "reason": "消債更生", "case_number": "2026-0001"},
    )

    assert result["ok"] is True
    assert "債權人清冊" in result["copy_text"]
    assert result["portal_mode"] == "packet_only"


def test_conflict_check_flags_opponent_records():
    from api.osc.saas_workbench import conflict_check

    def fake_exec(sql, params=(), fetch="all"):
        if "FROM opponents" in sql:
            return ([{"id": 1, "case_number": "2025-0001", "opponent_name": "張三", "notes": ""}], None)
        return ([], None)

    result = conflict_check(fake_exec, {"opponent_name": "張三"})

    assert result["risk"] == "high"
    assert result["matches"][0]["side"] == "opponent"
    assert result["matches"][0]["actions"][0]["act"] == "saas-opponent-edit"


def test_intake_runtime_record_is_local_jsonl(tmp_path, monkeypatch):
    from api.osc import saas_workbench

    monkeypatch.setattr(saas_workbench, "INTAKE_PATH", tmp_path / "intake.jsonl")

    def fake_exec(*args, **kwargs):
        return ([], None)

    result = saas_workbench.record_intake(
        fake_exec,
        {"client_name": "李四", "case_reason": "損害賠償", "summary": "電話諮詢"},
        actor="tester",
    )

    assert result["ok"] is True
    assert Path(saas_workbench.INTAKE_PATH).exists()
    assert saas_workbench.recent_intakes(1)[0]["client_name"] == "李四"


def test_saas_overview_exposes_ten_capabilities(monkeypatch, tmp_path):
    from api.osc import draft_learning, saas_workbench

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "learning.jsonl")
    monkeypatch.setattr(saas_workbench, "INTAKE_PATH", tmp_path / "intake.jsonl")
    monkeypatch.setattr(saas_workbench, "ONBOARDING_PATH", tmp_path / "onboarding.json")
    monkeypatch.setattr(saas_workbench, "NOTIFICATION_PREFS_PATH", tmp_path / "notify.json")
    monkeypatch.setattr(saas_workbench, "WORKFLOW_TEMPLATES_PATH", tmp_path / "workflow.json")

    def fake_exec(*args, **kwargs):
        sql = args[0]
        if "COUNT(*) AS c" in sql:
            return ({"c": 0}, None)
        return ([], None)

    result = saas_workbench.build_saas_overview(fake_exec)

    assert result["ok"] is True
    assert len(result["capabilities"]) == 14
    assert {x["key"] for x in result["capabilities"]} >= {
        "learning_center",
        "quality_gate",
        "risk_dashboard",
        "conflict_check",
        "nerv_status_page",
        "operations_report",
        "onboarding_checklist",
        "notification_preferences",
        "workflow_templates",
        "diagnostics_export",
    }
    assert result["readiness"]["mode"] == "single_host"
    assert result["readiness"]["status_page"]["url"] == "/status"
    assert "多租戶" in result["readiness"]["not_needed"]
    assert "公開上傳入口" in result["readiness"]["not_needed"]
    assert {x["key"] for x in result["readiness"]["checks"]} >= {"nerv_status", "not_needed_scope"}
    assert result["integration"]["principle"].startswith("這裡集中顯示常用資訊")
    assert all(x.get("owner") and x.get("source") and x.get("role") for x in result["capabilities"])
    target_tabs = {
        x["target_tab"]
        for x in result["integration"]["items"]
        if x.get("target_tab")
    }
    for item in result["integration"]["items"]:
        for target in item.get("target_tabs") or []:
            target_tabs.add(target["tab"])
    assert target_tabs >= {"todos", "clients", "documents", "drafts", "saasTimelineSection"}
    timeline = next(x for x in result["capabilities"] if x["key"] == "document_timeline")
    assert timeline["primary_action"]["act"] == "saas-section-jump"
    assert timeline["primary_action"]["section"] == "saasTimelineSection"
    assert timeline["secondary_actions"][0]["tab"] == "documents"
    assert timeline["title"] == "文件證據時間線"
    nerv = next(x for x in result["capabilities"] if x["key"] == "nerv_status_page")
    assert nerv["primary_action"]["act"] == "open-url"
    assert "對外資料" in {x["title"] for x in result["capabilities"]}
    assert result["onboarding"]["summary"]["required"] >= 1
    assert result["notification_preferences"]["prefs"]["system_health"] == "system_only"
    assert result["workflow_templates"]["count"] >= 4
    assert result["workflow_templates"]["legal_workflow_agents"]
    assert result["workflow_templates"]["practice_profiles"]
    assert result["workflow_templates"]["reference"]["import_mode"] == "conceptual_patterns_only"
    assert result["ai_governance"]["policies"]
    assert result["task_boards"]["refresh"]["interval_hours"] == 6
    assert "MAGI 事務統計" in result["operations_text"]


def test_saas_workbench_template_has_actionable_entry_links():
    html = Path("templates/partials/osc/saasWorkbench.html").read_text(encoding="utf-8")

    assert "資料來源與處理入口" in html
    assert 'id="saasReadinessGrid"' in html
    assert 'id="saasOnboardingSection"' in html
    assert 'id="saasNotificationSection"' in html
    assert 'id="saasWorkflowSection"' in html
    assert 'id="saasGovernanceSection"' in html
    assert 'id="saasTaskBoardSection"' in html
    assert 'id="saasOscTodoBody"' in html
    assert 'id="saasCalendarEventBody"' in html
    assert "事件待辦" in html
    assert "OSC 建立待辦" in html
    assert "行事曆事件" in html
    assert "功能整合關係" not in html
    assert "管理工具" in html
    assert "事務總覽" not in html
    assert "諮詢／接案追蹤" in html
    assert "所務" not in html
    assert "事務所營運" not in html
    assert "工作台" not in html
    assert "面板" not in html
    assert "漏斗" not in html
    assert "重命名" not in html
    assert "對外文件產生包" not in html
    assert "當事人入口" not in html
    assert "客戶入口" not in html
    assert "資料包" not in html
    assert 'id="saasTimelineSection"' in html
    for tab in ["cases", "clients", "todos", "laf", "documents", "drafts"]:
        assert f'data-tab="{tab}"' in html
    assert 'href="https://calendar.google.com/calendar/u/0/r"' in html
    assert 'target="_blank" rel="noopener noreferrer"' in html


def test_saas_tools_are_embedded_in_dashboard_not_separate_nav():
    osc = Path("templates/osc.html").read_text(encoding="utf-8")
    dashboard = Path("templates/partials/osc/dashboard.html").read_text(encoding="utf-8")

    assert 'data-tab="saasWorkbench"' not in osc
    assert 'include "partials/osc/saasWorkbench.html"' in dashboard


def test_dashboard_laf_case_labels_are_consistent():
    html = Path("templates/partials/osc/dashboard.html").read_text(encoding="utf-8")

    assert "未結法扶案件" in html
    assert "已結法扶案件" in html
    assert "未結法扶</div>" not in html


def test_operations_report_separates_total_active_and_closing_pending(monkeypatch, tmp_path):
    from api.osc import draft_learning, saas_workbench

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "learning.jsonl")
    monkeypatch.setattr(saas_workbench, "INTAKE_PATH", tmp_path / "intake.jsonl")

    def fake_exec(sql, params=(), fetch="one"):
        if "COUNT(*) AS c FROM cases" in sql and "WHERE" not in sql:
            return ({"c": 182}, None)
        if "COUNT(*) AS c FROM cases" in sql and "NOT (" in sql and "LIKE '%結案中%'" in sql:
            return ({"c": 143}, None)
        if "COUNT(*) AS c FROM cases" in sql and "LIKE '%結案中%'" in sql:
            return ({"c": 1}, None)
        if "COUNT(*) AS c FROM cases" in sql and "LOWER(COALESCE(status, ''))" in sql:
            return ({"c": 38}, None)
        if "COUNT(*) AS c" in sql:
            return ({"c": 0}, None)
        return ([], None)

    result = saas_workbench.build_operations_report(fake_exec)

    assert result["total_cases"] == 182
    assert result["active_cases"] == 143
    assert result["closed_cases"] == 38
    assert result["closing_pending_cases"] == 1
    assert result["pending_review_todos"] == 0


def test_operations_report_includes_actionable_business_detail_rows():
    from api.osc import saas_workbench

    def fake_exec(sql, params=(), fetch="one"):
        if "COUNT(*) AS c" in sql:
            return ({"c": 1}, None)
        if "FROM cases" in sql and "SELECT case_number" in sql:
            return ([{"case_number": "2026-0001", "client_name": "王小明", "status": "已結案", "legal_aid_status": "已結案，待報結"}], None)
        if "FROM case_todos" in sql and "SELECT id" in sql:
            return ([{"id": 9, "case_number": "2026-0002", "client_name": "林小華", "todo_date": "2026-07-13", "description": "原期限：2026-07-01／原類型：陳報\n補正資料"}], None)
        return ([], None)

    result = saas_workbench.build_operations_report(fake_exec)

    assert result["closing_pending_items"][0]["case_number"] == "2026-0001"
    assert result["pending_review_items"][0]["description"].endswith("補正資料")


def test_operations_report_separates_laf_attention_from_branch_review():
    from api.osc import saas_workbench

    def fake_exec(sql, params=(), fetch="one"):
        if "COUNT(*) AS c" in sql:
            return ({"c": 0}, None)
        if "COALESCE(legal_aid_approval_status, '') IN ('待轉入', '已補件待轉入', '已轉入')" in sql:
            return (
                [
                    {
                        "case_number": "2025-0119",
                        "client_name": "林文忠",
                        "status": "已結案",
                        "legal_aid_status": "已結案",
                        "legal_aid_approval_status": "已轉入",
                    }
                ],
                None,
            )
        if "WHERE" in sql and "LIKE '%待報結%'" in sql and "SELECT case_number" in sql:
            return (
                [
                    {
                        "case_number": "2025-0045",
                        "client_name": "郭麗卿",
                        "status": "已結案",
                        "legal_aid_status": "已結案，待送出",
                        "legal_aid_approval_status": "待補件",
                    }
                ],
                None,
            )
        return ([], None)

    result = saas_workbench.build_operations_report(fake_exec)

    assert result["laf_attention_cases"] == 1
    assert result["laf_attention_items"][0]["legal_aid_approval_status"] == "待補件"
    assert result["laf_branch_pending_cases"] == 1
    assert result["laf_branch_pending_items"][0]["legal_aid_approval_status"] == "已轉入"


def test_risk_dashboard_marks_source_module():
    from api.osc import saas_workbench

    def fake_exec(sql, params=(), fetch="all"):
        if "FROM case_todos" in sql:
            return (
                [
                    {
                        "id": 1,
                        "case_number": "2026-0001",
                        "client_name": "王小明",
                        "todo_type": "開庭",
                        "todo_date": "2026-05-01",
                        "description": "準備資料",
                        "status": "",
                    }
                ],
                None,
            )
        return ([], None)

    result = saas_workbench.build_risk_dashboard(fake_exec, limit=5)

    assert result["items"][0]["owner"] == "OSC 建立待辦"
    assert result["items"][0]["target_tab"] == "todos"
    assert {x["act"] for x in result["items"][0]["actions"]} == {"saas-todo-edit", "saas-todo-complete"}


def test_task_boards_split_calendar_imports_from_osc_todos():
    from api.osc import saas_workbench

    def fake_exec(sql, params=(), fetch="all"):
        if "FROM case_todos" in sql and "NOT LIKE 'gcal_import" in sql:
            assert "COALESCE(todo_type, '') <> '行事曆事件'" in sql
            return (
                [
                    {
                        "id": 1,
                        "case_number": "2026-0001",
                        "client_name": "王小明",
                        "todo_type": "補正",
                        "todo_date": "2026-05-20",
                        "todo_time": "",
                        "description": "OSC 從法院通知建立",
                        "status": "pending",
                        "source_file": "法院通知.pdf",
                    }
                ],
                None,
            )
        if "FROM case_todos" in sql and "LIKE 'gcal_import" in sql:
            assert "COALESCE(todo_type, '')='行事曆事件'" in sql
            return (
                [
                    {
                        "id": 2,
                        "case_number": "2026-0002",
                        "client_name": "",
                        "todo_type": "開會",
                        "todo_date": "2026-05-21",
                        "todo_time": "10:00",
                        "description": "同事手動日曆事件",
                        "status": "pending",
                        "source_file": "gcal_import",
                    },
                    {
                        "id": 4,
                        "case_number": "2026-0004",
                        "client_name": "",
                        "todo_type": "行事曆事件",
                        "todo_date": "2026-05-23",
                        "todo_time": "11:00",
                        "description": "本地行事曆事件待辦",
                        "status": "pending",
                        "source_file": "",
                    }
                ],
                None,
            )
        if "FROM calendar_events" in sql:
            return (
                [
                    {
                        "id": 3,
                        "case_number": "2026-0003",
                        "title": "開庭",
                        "start_date": "2026-05-22 09:30:00",
                        "description": "第一法庭",
                        "location": "花蓮地院",
                    }
                ],
                None,
            )
        return ([], None)

    result = saas_workbench.build_task_boards(fake_exec)

    assert result["refresh"]["interval_hours"] == 6
    assert result["osc_todos"]["count"] == 1
    assert result["osc_todos"]["items"][0]["source"] == "case_todos"
    assert result["calendar_events"]["source_counts"] == {"calendar_events": 1, "gcal_import": 1, "calendar_todo": 1}
    assert {x["source"] for x in result["calendar_events"]["items"]} == {"calendar_events", "gcal_import", "calendar_todo"}


def test_document_timeline_reuses_document_actions():
    from api.osc import saas_workbench

    def fake_exec(sql, params=(), fetch="all"):
        if "FROM document_index" in sql:
            return (
                [
                    {
                        "id": 3,
                        "case_number": "2026-0001",
                        "file_name": "準備書狀.pdf",
                        "file_path": "/tmp/準備書狀.pdf",
                        "subfolder_name": "我方歷次書狀",
                        "reason": "",
                        "party": "",
                        "modified_date": "2026-05-11 10:00:00",
                    }
                ],
                None,
            )
        return ([], None)

    result = saas_workbench.build_document_timeline(fake_exec)

    assert result["items"][0]["actions"][0]["act"] == "doc-open"
    assert result["items"][0]["actions"][1]["act"] == "doc-copy"


def test_saas_generated_edit_actions_have_dispatch_handlers():
    root = Path(__file__).resolve().parents[1]
    events_js = (root / "static/osc/osc-events.js").read_text(encoding="utf-8")
    for act in [
        "saas-todo-edit",
        "saas-todo-complete",
        "saas-cal-edit",
        "saas-laf-detail",
        "saas-laf-status",
        "saas-case-edit",
        "saas-client-edit",
        "saas-opponent-edit",
    ]:
        assert f'if (act === "{act}")' in events_js
    for fn in [
        "reloadSaasOnboarding",
        "saveSaasNotificationPrefs",
        "downloadSaasDiagnosticPack",
        "copySaasOpsReport",
    ]:
        assert fn in events_js or fn in (root / "static/osc/tabs/saas.js").read_text(encoding="utf-8")


def test_high_coverage_event_refresh_is_seeded():
    from magi_v3.external_inputs import load_bound_cron_jobs

    root = Path(__file__).resolve().parents[1]
    jobs = list(load_bound_cron_jobs(root, missing_source_default=False).jobs)
    job = next(x for x in jobs if x.get("id") == "job_osc_events_refresh")

    drive_job = next(x for x in jobs if x.get("id") == "job_drive_case_sync_bidirectional")
    assert drive_job["cron"] == "1 */6 * * *"
    assert "drive_case_sync_worker.py" in drive_job["command"]
    assert "--timeout-sec 900" in drive_job["command"]
    assert "--priority-upcoming-days 21" in drive_job["command"]
    assert "--no-downloads" in drive_job["command"]
    assert "--no-uploads" in drive_job["command"]
    all_drive_job = next(x for x in jobs if x.get("id") == "job_drive_case_sync_all_files")
    assert all_drive_job["cron"] == "12,32,52 * * * *"
    assert "drive_case_sync_worker.py" in all_drive_job["command"]
    assert "--direct-all-cases" in all_drive_job["command"]
    assert "--direct-all-case-limit 1" in all_drive_job["command"]
    assert "--timeout-sec 6000" in all_drive_job["command"]
    assert "--inventory-timeout-sec 5400" in all_drive_job["command"]
    assert all_drive_job["timeout_sec"] == 6300
    assert "MAGI_DRIVE_SYNC_UNVERIFIED_EXISTING_ARE_OK=0" in all_drive_job["command"]
    assert job["cron"] == "35 */2 * * *"
    assert job["enabled"] is True
    assert "osc_events_refresh.py" in job["command"]
    assert "OSC_EVENTS_REFRESH_CALENDAR_LIMIT=500" in job["command"]
    assert "OSC_EVENTS_REFRESH_GCAL_PUSH_LIMIT=200" in job["command"]
    assert "OSC_PDF_CALENDAR_FILENAME_SWEEP_LIMIT=50000" in job["command"]
    assert "OSC_PDF_CALENDAR_FINAL_DOC_PRIORITY_CASE_LIMIT=2000" in job["command"]
    assert "OSC_PDF_CALENDAR_TARGET_TIMEOUT_SEC=180" in job["command"]
    assert "OSC_PDF_CALENDAR_BULK_TEXT_ENABLE=1" in job["command"]
    assert "OSC_PDF_CALENDAR_FULL_TEXT_SCAN=1" in job["command"]
    assert "OSC_PDF_CALENDAR_FULL_TEXT_ALL_CASES=1" in job["command"]
    assert "OSC_PDF_CALENDAR_FULL_TEXT_SCAN_LIMIT=10000" in job["command"]
    assert "OSC_EVENTS_REFRESH_PDF_LIMIT=10000" in job["command"]
    assert "OSC_EVENTS_REFRESH_PDF_CASE_BATCH=500" in job["command"]
    assert "OSC_EVENTS_REFRESH_SCAN_BUDGET_SEC=3600" in job["command"]
    assert "OSC_EVENTS_REFRESH_TRANSCRIPT_LIMIT=120" in job["command"]
    assert "TRANSCRIPT_TODO_PDF_TIMEOUT_SEC=60" in job["command"]
    assert "OSC_TRANSCRIPT_TODO_TIMEOUT_SEC=600" in job["command"]
    assert "OSC_EVENTS_REFRESH_SOURCE_AUDIT_DRIVE_REMEDIATE_ENABLE=1" in job["command"]
    assert "--skip-drive-sync" in job["command"]
    assert "--skip-transcript-todos" not in job["command"]
    assert "--skip-calendar-audit" not in job["command"]
    assert "--skip-calendar-source-audit" not in job["command"]
    assert "--skip-share-repair" in job["command"]
    assert "快刷" in job["desc"]
    assert job["no_catchup"] is True


def test_full_calendar_governance_gets_a_distinct_complete_scan_budget():
    from magi_v3.external_inputs import ExternalInputError, load_bound_cron_jobs
    from scripts.seed_cron_jobs import business_jobs
    from scripts.ops.osc_events_refresh import _pdf_scan_worker_timeout_sec, parse_args

    root = Path(__file__).resolve().parents[1]
    try:
        jobs = list(load_bound_cron_jobs(root, missing_source_default=False).jobs)
    except ExternalInputError:
        # Public source checkouts intentionally do not publish a private
        # runtime cron snapshot. Validate the authoritative generator instead.
        jobs = business_jobs(root)
    job = next(x for x in jobs if x.get("id") == "job_osc_todo_governance")
    assert "OSC_PDF_CALENDAR_BUDGET_SEC=7200" in job["command"]
    assert "OSC_PDF_CALENDAR_TARGET_TIMEOUT_SEC=180" in job["command"]
    assert "--skip-drive-sync" not in job["command"]

    args = parse_args(["--scan-time-budget-sec", "7200"])
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("OSC_PDF_CALENDAR_BUDGET_SEC", "7200")
        assert _pdf_scan_worker_timeout_sec(args) == 7230


def test_frequent_refresh_defers_full_corpus_without_governance_budget():
    from scripts.ops.osc_events_refresh import (
        _full_corpus_budget_eligible,
        _pdf_scan_worker_timeout_sec,
        parse_args,
    )

    args = parse_args(["--scan-time-budget-sec", "3600"])
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("OSC_PDF_CALENDAR_BUDGET_SEC", "360")
        patch.setenv("OSC_PDF_CALENDAR_FULL_TEXT_SCAN", "1")
        patch.setenv("OSC_PDF_CALENDAR_FULL_TEXT_ALL_CASES", "1")
        assert _full_corpus_budget_eligible(360, 3600) is False
        assert _pdf_scan_worker_timeout_sec(args) == 390


def test_pdf_calendar_scan_timeout_does_not_wait_for_uninterruptible_worker(
    tmp_path, monkeypatch
):
    from scripts.ops import osc_events_refresh

    class StuckWorker:
        pid = 65432
        returncode = None

        def poll(self):
            return None

    popen_kwargs: list[dict] = []
    killed: list[tuple[int, int]] = []
    ticks = iter((0.0, 0.0, 6.0))

    def _popen(*_args, **kwargs):
        popen_kwargs.append(kwargs)
        return StuckWorker()

    monkeypatch.setattr(osc_events_refresh, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(osc_events_refresh, "_pdf_scan_worker_timeout_sec", lambda _args: 5)
    monkeypatch.setattr(osc_events_refresh.subprocess, "Popen", _popen)
    monkeypatch.setattr(osc_events_refresh.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(osc_events_refresh.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(osc_events_refresh.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = osc_events_refresh._run_pdf_calendar_scan_isolated(
        osc_events_refresh.parse_args([])
    )

    assert result["ok"] is False
    assert result["error"] == "pdf_scan_timeout:5s"
    assert result["timeout_isolated"] is True
    assert result["process_group_terminated"] is True
    assert result["worker_pid"] == 65432
    assert killed == [(65432, osc_events_refresh.signal.SIGKILL)]
    assert popen_kwargs[0]["start_new_session"] is True
    assert popen_kwargs[0]["close_fds"] is True


def test_transcript_todo_timeout_does_not_wait_for_uninterruptible_nas_probe(
    tmp_path, monkeypatch
):
    from scripts.ops import osc_events_refresh

    class StuckWorker:
        pid = 76543
        returncode = None

        def poll(self):
            return None

    popen_kwargs: list[dict] = []
    killed: list[tuple[int, int]] = []
    ticks = iter((0.0, 0.0, 6.0))

    def _popen(*_args, **kwargs):
        popen_kwargs.append(kwargs)
        return StuckWorker()

    monkeypatch.setattr(osc_events_refresh, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(osc_events_refresh.subprocess, "Popen", _popen)
    monkeypatch.setattr(osc_events_refresh.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(osc_events_refresh.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(osc_events_refresh.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = osc_events_refresh._run_transcript_todo_isolated(
        osc_events_refresh.parse_args([]), timeout_sec=5
    )

    assert result["ok"] is True
    assert result["deferred"] is True
    assert result["reason"] == "transcript_todo_timeout:5s"
    assert result["timeout_isolated"] is True
    assert result["process_group_terminated"] is True
    assert result["worker_pid"] == 76543
    assert killed == [(76543, osc_events_refresh.signal.SIGKILL)]
    assert popen_kwargs[0]["start_new_session"] is True
    assert popen_kwargs[0]["close_fds"] is True


def test_laf_condition_draft_has_portal_sized_timeout():
    from magi_v3.external_inputs import load_bound_cron_jobs

    root = Path(__file__).resolve().parents[1]
    jobs = list(load_bound_cron_jobs(root, missing_source_default=False).jobs)
    job = next(x for x in jobs if x.get("id") == "job_laf_condition_draft")

    assert job["timeout_sec"] == 1200


def test_calendar_source_drive_remediation_timeout_is_process_isolated(tmp_path, monkeypatch):
    from scripts.ops import osc_events_refresh

    class StuckWorker:
        pid = 43210
        returncode = None

        def poll(self):
            return None

    killed: list[tuple[int, int]] = []
    ticks = iter((0.0, 0.0, 6.0))
    monkeypatch.setattr(osc_events_refresh, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(osc_events_refresh.subprocess, "Popen", lambda *args, **kwargs: StuckWorker())
    monkeypatch.setattr(osc_events_refresh.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(osc_events_refresh.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(osc_events_refresh.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setenv("OSC_EVENTS_REFRESH_SOURCE_AUDIT_DRIVE_TIMEOUT_SEC", "5")
    args = osc_events_refresh.parse_args([])

    result = osc_events_refresh._run_calendar_gap_drive_remediation(
        [{"case_number": "2026-0001"}],
        args=args,
    )

    assert result["ok"] is False
    assert result["reason"] == "drive_remediation_timeout"
    assert result["timeout_isolated"] is True
    assert result["process_group_terminated"] is True
    assert killed == [(43210, osc_events_refresh.signal.SIGKILL)]


def test_already_running_refresh_preserves_last_completed_report(monkeypatch, tmp_path):
    from scripts.ops import osc_events_refresh

    latest = tmp_path / "osc_events_refresh_latest.json"
    invocation = tmp_path / "osc_events_refresh_invocation_latest.json"
    latest.write_text(
        json.dumps({"ok": True, "pdf_calendar_scan": {"ok": True}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(osc_events_refresh, "LATEST_PATH", latest)
    monkeypatch.setattr(osc_events_refresh, "INVOCATION_PATH", invocation)

    osc_events_refresh._write_already_running_result(
        {"ok": True, "status": "already_running"}
    )

    assert json.loads(latest.read_text(encoding="utf-8")) == {
        "ok": True,
        "pdf_calendar_scan": {"ok": True},
    }
    audit = json.loads(invocation.read_text(encoding="utf-8"))
    assert audit["status"] == "already_running"
    assert audit["canonical_latest_preserved"] is True


def test_already_running_refresh_honours_explicit_output(monkeypatch, tmp_path):
    from scripts.ops import osc_events_refresh

    latest = tmp_path / "osc_events_refresh_latest.json"
    invocation = tmp_path / "osc_events_refresh_invocation_latest.json"
    explicit = tmp_path / "caller.json"
    monkeypatch.setattr(osc_events_refresh, "LATEST_PATH", latest)
    monkeypatch.setattr(osc_events_refresh, "INVOCATION_PATH", invocation)

    osc_events_refresh._write_already_running_result(
        {"ok": True, "status": "already_running"},
        str(explicit),
    )

    assert json.loads(explicit.read_text(encoding="utf-8"))["status"] == "already_running"
    assert not latest.exists()
    assert not invocation.exists()


def test_calendar_drive_presync_timeout_is_process_isolated(tmp_path, monkeypatch):
    from scripts.ops import osc_events_refresh

    class StuckWorker:
        pid = 54321
        returncode = None

        def poll(self):
            return None

    killed: list[tuple[int, int]] = []
    ticks = iter((0.0, 0.0, 61.0))
    monkeypatch.setattr(osc_events_refresh, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(osc_events_refresh.subprocess, "Popen", lambda *args, **kwargs: StuckWorker())
    monkeypatch.setattr(osc_events_refresh.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(osc_events_refresh.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(osc_events_refresh.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    args = osc_events_refresh.parse_args(["--drive-sync-timeout-sec", "30"])

    result = osc_events_refresh._run_drive_case_sync_before_pdf(args)

    assert result["ok"] is False
    assert result["status"] == "timeout"
    assert result["timeout_isolated"] is True
    assert result["process_group_terminated"] is True
    assert killed == [(54321, osc_events_refresh.signal.SIGKILL)]


def test_calendar_drive_presync_accepts_completed_bounded_slice():
    from scripts.ops.osc_events_refresh import _drive_presync_bounded_partial_is_success

    assert _drive_presync_bounded_partial_is_success(
        75,
        {
            "execution_summary": {
                "attempted": 16,
                "downloaded": 16,
                "failed": 0,
                "pending_unverified": 0,
                "stopped_by_limit": True,
            }
        },
    ) is True


def test_calendar_drive_presync_rejects_partial_slice_with_real_failure():
    from scripts.ops.osc_events_refresh import _drive_presync_bounded_partial_is_success

    assert _drive_presync_bounded_partial_is_success(
        75,
        {
            "execution_summary": {
                "attempted": 16,
                "downloaded": 15,
                "failed": 1,
                "stopped_by_limit": True,
            }
        },
    ) is False


def test_pdf_calendar_scan_holds_case_file_operation_lock(monkeypatch):
    from api.domains import case_file_operation_lock
    from scripts.ops import osc_events_refresh

    released: list[bool] = []
    acquired: list[dict] = []
    monkeypatch.setenv("OSC_PDF_CALENDAR_EXCLUSIVE_LOCK", "1")
    monkeypatch.setattr(
        case_file_operation_lock,
        "acquire_case_file_operation_lock",
        lambda **kwargs: acquired.append(kwargs) or {"acquired": True},
    )
    monkeypatch.setattr(
        case_file_operation_lock,
        "release_case_file_operation_lock",
        lambda: released.append(True),
    )
    monkeypatch.setattr(
        osc_events_refresh,
        "_run_pdf_calendar_scan_isolated",
        lambda _args: {"ok": True, "scanned": 87},
    )

    result = osc_events_refresh._run_pdf_calendar_scan(osc_events_refresh.parse_args([]))

    assert result["ok"] is True
    assert result["case_file_operation_lock"]["acquired"] is True
    assert result["case_file_operation_lock"]["exclusive"] is True
    assert acquired[0]["owner"] == "osc_events_refresh:pdf_calendar_scan"
    assert released == [True]


def test_pdf_calendar_scan_default_is_read_only_and_does_not_wait_for_drive_lock(
    monkeypatch,
):
    from api.domains import case_file_operation_lock
    from scripts.ops import osc_events_refresh

    calls: list[dict] = []
    released: list[bool] = []
    monkeypatch.delenv("OSC_PDF_CALENDAR_EXCLUSIVE_LOCK", raising=False)
    monkeypatch.setattr(
        case_file_operation_lock,
        "acquire_case_file_operation_lock",
        lambda **kwargs: calls.append(kwargs)
        or {"acquired": True, "disabled": True},
    )
    monkeypatch.setattr(
        case_file_operation_lock,
        "release_case_file_operation_lock",
        lambda: released.append(True),
    )
    monkeypatch.setattr(
        osc_events_refresh,
        "_run_pdf_calendar_scan_isolated",
        lambda _args: {"ok": True, "scanned": 87},
    )

    result = osc_events_refresh._run_pdf_calendar_scan(
        osc_events_refresh.parse_args([])
    )

    assert result["ok"] is True
    assert result["case_file_operation_lock"]["exclusive"] is False
    assert result["case_file_operation_lock"]["read_only"] is True
    assert calls[0]["exclusive"] is False
    assert released == []


def test_pdf_calendar_scan_fails_closed_when_case_file_operation_lock_is_busy(monkeypatch):
    from api.domains import case_file_operation_lock
    from scripts.ops import osc_events_refresh

    monkeypatch.setenv("OSC_PDF_CALENDAR_EXCLUSIVE_LOCK", "1")
    monkeypatch.setenv("OSC_PDF_CALENDAR_LOCK_WAIT_SEC", "0")
    monkeypatch.setattr(
        case_file_operation_lock,
        "acquire_case_file_operation_lock",
        lambda **_kwargs: {"acquired": False, "active_owner": {"owner": "drive_sync"}},
    )

    result = osc_events_refresh._run_pdf_calendar_scan(osc_events_refresh.parse_args([]))

    assert result["ok"] is False
    assert result["reason"] == "case_file_operation_lock_busy"
    assert result["active_owner"]["owner"] == "drive_sync"


def test_calendar_source_drive_worker_returns_summary(monkeypatch):
    from api.osc import drive_case_sync
    from scripts.ops import osc_events_refresh

    monkeypatch.setattr(
        drive_case_sync,
        "run_priority_case_sync",
        lambda **kwargs: {
            "ok": True,
            "summary": {"cases": len(kwargs["case_numbers"])},
            "file_sync_plan": {"summary": {"downloads": 1}},
            "execution_result": {"summary": {"downloaded": 1}},
            "drive_folder_result": {"summary": {"matched": 1}},
            "output_paths": {"report": "/tmp/report.json"},
        },
    )
    args = osc_events_refresh.parse_args([])

    result = osc_events_refresh._run_calendar_gap_drive_remediation_in_process(
        ["2026-0001"],
        args=args,
    )

    assert result["ok"] is True
    assert result["execution_summary"]["downloaded"] == 1
    assert result["file_sync_summary"]["downloads"] == 1


def test_onboarding_and_notification_preferences_persist(tmp_path, monkeypatch):
    from api.osc import saas_workbench

    monkeypatch.setattr(saas_workbench, "ONBOARDING_PATH", tmp_path / "onboarding.json")
    monkeypatch.setattr(saas_workbench, "NOTIFICATION_PREFS_PATH", tmp_path / "notify.json")

    result = saas_workbench.update_onboarding_status({"key": "public_audit", "done": True}, actor="tester")
    assert result["ok"] is True
    assert any(x["key"] == "public_audit" and x["done"] for x in result["items"])

    prefs = saas_workbench.save_notification_preferences({"system_health": "silent", "laf_general": "system_only"})
    assert prefs["prefs"]["system_health"] == "silent"
    assert prefs["prefs"]["laf_general"] == "system_only"


def test_diagnostic_pack_is_redacted_and_complete(tmp_path, monkeypatch):
    from api.osc import draft_learning, saas_workbench

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "learning.jsonl")
    monkeypatch.setattr(saas_workbench, "INTAKE_PATH", tmp_path / "intake.jsonl")
    monkeypatch.setattr(saas_workbench, "ONBOARDING_PATH", tmp_path / "onboarding.json")
    monkeypatch.setattr(saas_workbench, "NOTIFICATION_PREFS_PATH", tmp_path / "notify.json")

    def fake_exec(sql, params=(), fetch="one"):
        if "COUNT(*) AS c" in sql:
            return ({"c": 0}, None)
        return ([], None)

    pack = saas_workbench.build_diagnostic_pack(fake_exec)
    assert pack["ok"] is True
    assert pack["scope"] == "single_host_magi"
    assert pack["redaction"].startswith("No secrets")
    assert "readiness" in pack and "notification_preferences" in pack and "ai_governance" in pack


@pytest.fixture
def saas_client(monkeypatch, tmp_path):
    from flask import Flask
    from flask_login import LoginManager, UserMixin
    from api.blueprints import osc_cases
    from api.osc import draft_learning, saas_workbench

    monkeypatch.setattr(draft_learning, "EVENTS_PATH", tmp_path / "learning.jsonl")
    monkeypatch.setattr(saas_workbench, "INTAKE_PATH", tmp_path / "intake.jsonl")
    monkeypatch.setattr(saas_workbench, "ONBOARDING_PATH", tmp_path / "onboarding.json")
    monkeypatch.setattr(saas_workbench, "NOTIFICATION_PREFS_PATH", tmp_path / "notify.json")
    monkeypatch.setattr(saas_workbench, "WORKFLOW_TEMPLATES_PATH", tmp_path / "workflow.json")

    def fake_exec(sql, params=(), fetch="all"):
        if "COUNT(*) AS c" in sql:
            return ({"c": 0}, None)
        return ([], None)

    monkeypatch.setattr(osc_cases, "_osc_exec", fake_exec)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.secret_key = "test-saas"
    lm = LoginManager()
    lm.init_app(app)

    class _User(UserMixin):
        id = "tester"

    @lm.user_loader
    def _load_user(_user_id):
        return _User()

    app.register_blueprint(osc_cases.osc_bp)
    return app.test_client()


def test_saas_routes_smoke(saas_client):
    resp = saas_client.get("/api/osc/saas/overview")
    assert resp.status_code == 200
    assert len(resp.get_json()["capabilities"]) == 14
    assert resp.get_json()["readiness"]["mode"] == "single_host"

    resp = saas_client.post(
        "/api/osc/saas/quality-check",
        json={"text": "<|channel>thought\nOSC-2026-001", "case_number": "114年度訴字第1號"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["pass"] is False

    resp = saas_client.post("/api/osc/saas/client-packet", json={"client_name": "王小明", "reason": "消債更生"})
    assert resp.status_code == 200
    assert "債權人清冊" in resp.get_json()["copy_text"]

    resp = saas_client.get("/api/osc/saas/task-boards")
    assert resp.status_code == 200
    assert resp.get_json()["refresh"]["interval_hours"] == 6

    resp = saas_client.get("/api/osc/saas/onboarding")
    assert resp.status_code == 200
    assert resp.get_json()["summary"]["required"] >= 1

    resp = saas_client.post("/api/osc/saas/notification-prefs", json={"system_health": "silent"})
    assert resp.status_code == 200
    assert resp.get_json()["prefs"]["system_health"] == "silent"

    resp = saas_client.get("/api/osc/saas/workflow-templates")
    assert resp.status_code == 200
    assert resp.get_json()["count"] >= 4

    resp = saas_client.get("/api/osc/saas/ai-governance")
    assert resp.status_code == 200
    assert resp.get_json()["policies"]

    resp = saas_client.get("/api/osc/saas/operations-report")
    assert resp.status_code == 200
    assert "MAGI 事務統計" in resp.get_json()["text"]

    resp = saas_client.get("/api/osc/saas/diagnostic-pack")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    assert resp.get_json()["redaction"].startswith("No secrets")
