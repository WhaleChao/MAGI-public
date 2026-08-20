from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys

from scripts.v3_validation.actual_route_replay import (
    SCRIPT_PATH,
    _external_storage_roots,
    _is_installed_release_root,
    _worker_environment,
    _worker_live_read_roots,
    _worker_runtime,
    run_actual_route_replay,
)


def test_installed_candidate_root_is_allowed_for_read_only_replay(tmp_path: Path) -> None:
    from scripts.v3_validation import actual_route_replay as replay

    candidate = replay.REPO_ROOT.resolve(strict=True)
    site_packages = tmp_path.resolve(strict=True)
    assert _worker_live_read_roots((candidate, site_packages)) == (
        candidate,
        site_packages,
    )


def test_only_direct_installed_release_root_qualifies(tmp_path: Path) -> None:
    live_root = tmp_path / "MAGI"
    installed = live_root / "releases" / "v3-test"
    mutable_runtime = live_root / "runtime" / "candidate"
    nested_release_path = installed / "nested"
    for path in (installed, mutable_runtime, nested_release_path):
        path.mkdir(parents=True, exist_ok=True)

    assert _is_installed_release_root(installed, live_root) is True
    assert _is_installed_release_root(mutable_runtime, live_root) is False
    assert _is_installed_release_root(nested_release_path, live_root) is False


def test_worker_uses_candidate_venv_with_site_processing_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    formal = os.environ.get("MAGI_V3_ROUTE_CERTIFYING") == "1"
    if not formal:
        monkeypatch.delenv("MAGI_V3_ROUTE_CERTIFYING", raising=False)
        monkeypatch.setenv("PYTHONPATH", str(Path.home() / "Library" / "Python"))
    worker_python, python_roots = _worker_runtime()
    environment = _worker_environment(
        tmp_path,
        worker_python=worker_python,
        python_roots=python_roots,
    )
    probe = subprocess.run(
        [
            str(worker_python),
            "-S",
            "-c",
            (
                "import flask, flask_login, json, jsonschema, os, sys; "
                "print(json.dumps({'path':sys.path,'home':os.environ['HOME'],"
                "'flask':flask.__file__,'login':flask_login.__file__,"
                "'jsonschema':jsonschema.__file__}))"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
    payload = json.loads(probe.stdout)
    live_runtime = str(
        Path.home() / "Library" / "Application Support" / "MAGI" / "runtime"
    )
    user_site = str(Path.home() / "Library" / "Python")
    if formal:
        assert Path(worker_python).absolute() == Path(
            os.environ["MAGI_V3_PYTHON_RUNTIME"]
        ).absolute()
    else:
        declared_runtime = os.environ.get("MAGI_V3_PYTHON_RUNTIME", "").strip()
        venv_root = (
            Path(declared_runtime).expanduser().absolute().parent.parent.resolve()
            if declared_runtime
            else (Path(__file__).resolve().parents[2] / "venv").resolve()
        )
        assert Path(worker_python).absolute().is_relative_to(venv_root)
    assert payload["home"] == str(tmp_path / "home")
    assert all(user_site not in entry for entry in payload["path"])
    if formal or not os.environ.get("MAGI_V3_PYTHON_RUNTIME", "").strip():
        assert all(live_runtime not in entry for entry in payload["path"])
    if formal:
        assert all(
            any(
                Path(payload[name]).resolve().is_relative_to(root)
                for root in python_roots[1:]
            )
            for name in ("flask", "login", "jsonschema")
        )
    else:
        assert all(
            Path(payload[name]).resolve().is_relative_to(venv_root)
            for name in ("flask", "login", "jsonschema")
        )
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONSAFEPATH"] == "1"


def test_all_347_routes_receive_machine_readable_actual_replay_dispositions(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")

    assert evidence["execution_passed"] is True
    assert evidence["coverage_complete"] is False
    assert evidence["passed"] is False
    assert evidence["workload"] == "346_route_contract_replay"
    assert evidence["inventory_counts"] == {"5002": 280, "5003": 67, "total": 347}
    route_summary = evidence["route_summary"]
    assert route_summary["pinned_routes"] == 347
    assert route_summary["routes_with_machine_disposition"] == 347
    assert 0 < route_summary["fully_actual_handler_replayed_routes"] < 347
    assert route_summary["routes_with_remaining_gap"] == (
        347 - route_summary["fully_actual_handler_replayed_routes"]
    )
    assert route_summary["routes_with_handler_dispatch_evidence"] >= (
        route_summary["fully_actual_handler_replayed_routes"]
    )
    methods = evidence["route_method_summary"]
    assert methods["pinned_route_methods"] == 431
    assert methods["reviewed_route_methods"] == 431
    assert methods["actual_handler_passed"] == methods["representative_success_path_passed"]
    assert methods["validation_guard_only"] > 0
    assert methods["handler_dispatch_passed"] == (
        methods["representative_success_path_passed"]
        + methods["validation_guard_only"]
    )
    assert methods["actual_handler_failed"] == 0
    assert methods["remaining_route_methods"] == (
        methods["pinned_route_methods"] - methods["representative_success_path_passed"]
    )
    assert sum(methods["dispositions"].values()) == 431

    dispositions = evidence["route_method_dispositions"]
    signatures = {
        (row["service"], row["rule"], row["method"], row["endpoint"])
        for row in dispositions
    }
    assert len(dispositions) == len(signatures) == 431
    counts = Counter(row["disposition"] for row in dispositions)
    assert counts == Counter(methods["dispositions"])
    assert counts["blocked_unreviewed"] == 0
    assert counts["missing_actual_handler_case"] > 0
    assert counts["actual_handler_passed"] == methods["representative_success_path_passed"]
    assert counts["validation_guard_only"] == methods["validation_guard_only"]
    assert counts["fixture_contract_only"] == 0

    fixture_promotions = {
        (row["service"], row["rule"], row["method"], row["endpoint"]): row
        for row in dispositions
        if (row["service"], row["rule"]) in {
            ("5002", "/api/osc/chat"),
            ("5003", "/shortcut/pdf_text"),
        }
    }
    assert set(fixture_promotions) == {
        ("5002", "/api/osc/chat", "POST", "web_runtime.osc_chat_api"),
        ("5003", "/shortcut/pdf_text", "POST", "api_shortcut_pdf_text"),
    }
    assert all(
        row["disposition"] == "actual_handler_passed"
        and row["branch_class"] == "representative_success_path"
        and row["representative_success_path_passed"] is True
        for row in fixture_promotions.values()
    )

    unsafe_guard_rows = [
        row
        for row in dispositions
        if row.get("side_effect_class") in {"external_commit", "destructive"}
        and row.get("branch_class") == "validation_guard_only"
    ]
    assert unsafe_guard_rows
    assert all(row["disposition"] == "validation_guard_only" for row in unsafe_guard_rows)


def test_actual_handlers_are_dispatched_with_bound_outcomes_not_auth_or_404_shortcuts(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")

    assert evidence["actual_handler_cases"]
    assert all(case["passed"] for case in evidence["actual_handler_cases"])
    assert all(case["actual_handler_dispatched"] for case in evidence["actual_handler_cases"])
    assert all(not case["auth_or_not_found_status_used_as_proof"] for case in evidence["actual_handler_cases"])
    assert all(
        case["branch_class"] in {"validation_guard_only", "representative_success_path"}
        and case["validation_guard_only"]
        is (case["branch_class"] == "validation_guard_only")
        and case["representative_success_path"]
        is (case["branch_class"] == "representative_success_path")
        for case in evidence["actual_handler_cases"]
    )
    assert {case["observed_status"] for case in evidence["actual_handler_cases"]} <= {200, 204, 302, 400}
    hearing_cases = {
        (case["rule"], case["method"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["rule"].startswith("/api/osc/hearing-conflicts")
    }
    assert set(hearing_cases) == {
        (
            "/api/osc/hearing-conflicts",
            "GET",
            "osc_cases.osc_hearing_conflicts_api",
        ),
        (
            "/api/osc/hearing-conflicts/check",
            "POST",
            "osc_cases.osc_hearing_conflicts_check_api",
        ),
        (
            "/api/osc/hearing-conflicts/generate",
            "POST",
            "osc_cases.osc_hearing_conflicts_generate_api",
        ),
        (
            "/api/osc/hearing-conflicts/download",
            "GET",
            "osc_cases.osc_hearing_conflicts_download_api",
        ),
    }
    assert all(
        case["passed"] is True
        and case["actual_handler_dispatched"] is True
        and case["observed_status"] == 200
        for case in hearing_cases.values()
    )
    assert {case["side_effect_guard"]["fixture"] for case in hearing_cases.values()} == {
        "bounded_empty_schedule_projection",
        "tentative_candidate_fail_closed",
        "sandboxed_manual_generation_adapter",
        "tenant_bound_document_id_download",
    }
    assert all(
        case["side_effect_guard"][name] == 0
        for case in hearing_cases.values()
        for name in (
            "fixture_database_calls",
            "database_mutations",
            "socket.connect",
            "socket.bind",
            "socket.create_connection",
            "socket.getaddrinfo",
            "subprocess.Popen",
        )
    )
    new_read_batches = {
        (case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["rule"] in {
            "/api/live-validation",
            "/api/ops/process-monitor",
            "/ops/process-monitor",
            "/api/iron_dome/hash",
            "/api/iron_dome/patterns",
            "/api/iron_dome/status",
        }
    }
    assert set(new_read_batches) == {
        ("/api/live-validation", "admin_runtime.api_live_validation"),
        ("/api/ops/process-monitor", "web_runtime.process_monitor_api"),
        ("/ops/process-monitor", "web_runtime.process_monitor_page"),
        ("/api/iron_dome/hash", "iron_dome_hash"),
        ("/api/iron_dome/patterns", "iron_dome_patterns"),
        ("/api/iron_dome/status", "iron_dome_status"),
    }
    assert all(
        case["side_effect_guard"]
        == {
            "fixture_database_calls": 0,
            "statement_kinds": [],
            "database_mutations": 0,
            "fixture": "in_memory_dependencies",
            "network_attempts": 0,
            "subprocess_attempts": 0,
            "live_state_attempts": 0,
            "writes_outside_sandbox": 0,
            "mutations_outside_sandbox": 0,
            "external_storage_access_attempts": 0,
        }
        for case in new_read_batches.values()
    )
    promoted_fixture_cases = {
        (case["service"], case["rule"], case["method"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["rule"] in {"/api/osc/chat", "/shortcut/pdf_text"}
    }
    assert set(promoted_fixture_cases) == {
        ("5002", "/api/osc/chat", "POST", "web_runtime.osc_chat_api"),
        ("5003", "/shortcut/pdf_text", "POST", "api_shortcut_pdf_text"),
    }
    assert {
        case["side_effect_guard"]["fixture"]
        for case in promoted_fixture_cases.values()
    } == {"in_memory_orchestrator", "temporary_pdf_in_memory_reader"}
    assert all(
        case["passed"] is True
        and case["branch_class"] == "representative_success_path"
        for case in promoted_fixture_cases.values()
    )
    assert evidence["osc_golden_flow"]["passed"] is True
    assert evidence["osc_golden_flow"]["expected_outcomes_sha256"] == evidence["osc_golden_flow"][
        "observed_outcomes_sha256"
    ]
    operational = evidence["operational_golden_flow"]
    assert operational["passed"] is True
    assert operational["case_count"] == 5
    assert operational["expected_outcomes_sha256"] == operational["observed_outcomes_sha256"]
    assert all(case["passed"] and case["actual_handler_dispatched"] for case in operational["cases"])
    assert {case["domain"] for case in operational["cases"]} == {
        "nas_file_workflows",
        "office_document_workflows",
        "provider_and_session_integrations",
    }


def test_osc_transactional_crud_batch_dispatches_success_and_journals_exact_sql(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    cases = {
        (case["rule"], case["method"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case.get("side_effect_guard", {}).get("fixture")
        == "transactional_sqlite_operation_journal"
        and case["endpoint"].startswith("osc_cases.")
        and case["endpoint"].startswith("osc_cases.")
    }
    expected: dict[tuple[str, str, str], tuple[list[str], list[str]]] = {
        ("/api/osc/case-reason-templates", "POST", "osc_cases.osc_case_reason_templates_api"):
            (["INSERT", "INSERT"], ["case_reason_templates", "activity_logs"]),
        ("/api/osc/activity-logs", "POST", "osc_cases.osc_activity_logs_api"):
            (["INSERT"], ["activity_logs"]),
        ("/api/osc/user-settings", "POST", "osc_cases.osc_user_settings_api"):
            (["INSERT", "INSERT"], ["user_settings", "activity_logs"]),
        ("/api/osc/memory-keywords", "POST", "osc_cases.osc_memory_keywords_api"):
            (["INSERT", "INSERT"], ["memory_keywords", "activity_logs"]),
        ("/api/osc/opponents", "POST", "osc_cases.osc_opponents_api"):
            (["INSERT", "INSERT"], ["opponents", "activity_logs"]),
        ("/api/osc/document-keywords", "POST", "osc_cases.osc_document_keywords_api"):
            (["INSERT"], ["document_keywords"]),
        ("/api/osc/quotation-templates", "POST", "osc_cases.osc_quotation_templates_api"):
            (["INSERT"], ["quotation_templates"]),
        ("/api/osc/calendar/events", "POST", "osc_cases.osc_calendar_events_api"):
            (["INSERT"], ["calendar_events"]),
        ("/api/osc/clients", "POST", "osc_cases.osc_clients_api"):
            (["INSERT"], ["clients"]),
        ("/api/osc/meetings", "POST", "osc_cases.osc_meetings_api"):
            (["INSERT"], ["meetings"]),
        ("/api/osc/todos", "POST", "osc_cases.osc_todos_api"):
            (["SELECT", "INSERT"], ["case_todos", "case_todos"]),
        ("/api/osc/activity-logs/<int:row_id>", "DELETE", "osc_cases.osc_activity_log_detail_api"):
            (["DELETE"], ["activity_logs"]),
    }
    detail_specs = (
        ("/api/osc/case-reason-templates/<int:row_id>", "osc_cases.osc_case_reason_template_detail_api", "case_reason_templates", True),
        ("/api/osc/user-settings/<int:row_id>", "osc_cases.osc_user_setting_detail_api", "user_settings", True),
        ("/api/osc/memory-keywords/<path:case_number>/<path:hotkey>", "osc_cases.osc_memory_keyword_detail_api", "memory_keywords", True),
        ("/api/osc/opponents/<int:row_id>", "osc_cases.osc_opponent_detail_api", "opponents", True),
        ("/api/osc/document-keywords/<int:row_id>", "osc_cases.osc_document_keyword_detail_api", "document_keywords", False),
        ("/api/osc/quotation-templates/<int:row_id>", "osc_cases.osc_quotation_template_detail_api", "quotation_templates", False),
        ("/api/osc/calendar/events/<int:row_id>", "osc_cases.osc_calendar_event_detail_api", "calendar_events", False),
        ("/api/osc/clients/<row_id>", "osc_cases.osc_client_detail_api", "clients", False),
        ("/api/osc/meetings/<int:row_id>", "osc_cases.osc_meeting_detail_api", "meetings", False),
        ("/api/osc/todos/<int:row_id>", "osc_cases.osc_todo_detail_api", "case_todos", False),
    )
    for rule, endpoint, table, logs_activity in detail_specs:
        suffix_kinds = ["INSERT"] if logs_activity else []
        suffix_tables = ["activity_logs"] if logs_activity else []
        put_kinds = ["UPDATE", *suffix_kinds]
        put_tables = [table, *suffix_tables]
        if endpoint in {
            "osc_cases.osc_calendar_event_detail_api",
            "osc_cases.osc_todo_detail_api",
        }:
            put_kinds.append("SELECT")
            put_tables.append(table)
        if endpoint == "osc_cases.osc_todo_detail_api":
            put_kinds.append("SELECT")
            put_tables.append(table)
        expected[(rule, "PUT", endpoint)] = (put_kinds, put_tables)
        expected[(rule, "DELETE", endpoint)] = (["DELETE", *suffix_kinds], [table, *suffix_tables])

    assert len(cases) == len(expected) == 32
    assert set(cases) == set(expected)
    for key, case in cases.items():
        kinds, tables = expected[key]
        guard = case["side_effect_guard"]
        assert case["passed"] is True
        assert case["actual_handler_dispatched"] is True
        assert case["observed_status"] == 200
        assert case["contract"]["kind"] == "json_deep_subset"
        assert case["contract"]["observed"]["ok"] is True
        assert guard["statement_kinds"] == kinds
        assert guard["statement_tables"] == tables
        assert guard["fixture_database_calls"] == len(kinds)
        assert guard["sqlite_operation_journal_rows"] == len(kinds)
        assert guard["database_mutations"] == sum(kind != "SELECT" for kind in kinds)
        assert all(guard[name] == 0 for name in (
            "network_attempts",
            "subprocess_attempts",
            "live_state_attempts",
            "writes_outside_sandbox",
            "mutations_outside_sandbox",
        ))


def test_secondary_osc_write_batch_binds_db_and_file_transcripts(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    transactional = {
        (case["rule"], case["method"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case.get("side_effect_guard", {}).get("fixture")
        == "transactional_sqlite_operation_journal"
        and not case["endpoint"].startswith("osc_cases.")
    }
    expected: dict[tuple[str, str, str], tuple[list[str], list[str]]] = {}
    settings = (
        ("/api/osc/settings", "osc_settings.osc_settings_api", "settings"),
        ("/api/osc/courts", "osc_settings.osc_courts_api", "courts"),
        ("/api/osc/legal-aid-branches", "osc_settings.osc_legal_aid_branches_api", "legal_aid_branches"),
    )
    for rule, endpoint, table in settings:
        expected[(rule, "POST", endpoint)] = (["INSERT", "INSERT"], [table, "activity_logs"])
    settings_details = (
        ("/api/osc/settings/<path:setting_key>", "osc_settings.osc_setting_detail_api", "settings"),
        ("/api/osc/courts/<int:row_id>", "osc_settings.osc_court_detail_api", "courts"),
        ("/api/osc/legal-aid-branches/<int:row_id>", "osc_settings.osc_legal_aid_branch_detail_api", "legal_aid_branches"),
    )
    for rule, endpoint, table in settings_details:
        expected[(rule, "PUT", endpoint)] = (["UPDATE", "INSERT"], [table, "activity_logs"])
        expected[(rule, "DELETE", endpoint)] = (["DELETE", "INSERT"], [table, "activity_logs"])
    accounting = (
        ("/api/osc/accounting/transactions", "osc_accounting.osc_accounting_transactions_api", "case_transactions"),
        ("/api/osc/accounting/defaults", "osc_accounting.osc_accounting_defaults_api", "expense_defaults"),
        ("/api/osc/accounting/recurring", "osc_accounting.osc_accounting_recurring_api", "recurring_expenses"),
    )
    for rule, endpoint, table in accounting:
        expected[(rule, "POST", endpoint)] = (["INSERT"], [table])
        detail_rule = rule + "/<int:row_id>"
        detail_endpoint = endpoint.replace("_api", "_detail_api").replace(
            "osc_accounting_transactions_detail", "osc_accounting_transaction_detail"
        ).replace("osc_accounting_defaults_detail", "osc_accounting_default_detail").replace(
            "osc_accounting_recurring_detail", "osc_accounting_recurring_detail"
        )
        expected[(detail_rule, "PUT", detail_endpoint)] = (["UPDATE"], [table])
        expected[(detail_rule, "DELETE", detail_endpoint)] = (["DELETE"], [table])
    expected[(
        "/api/osc/accounting/recurring/<int:row_id>/sync-generated",
        "POST",
        "osc_accounting.osc_accounting_recurring_sync_generated_api",
    )] = (["SELECT", "UPDATE", "SELECT"], ["recurring_expenses", "case_transactions", "case_transactions"])
    expected[(
        "/api/osc/accounting/import/google-sheet",
        "POST",
        "osc_accounting.osc_accounting_google_sheet_import_api",
    )] = ([], [])
    expected[(
        "/api/osc/accounting/monthly-bonus",
        "POST",
        "osc_accounting.osc_accounting_monthly_bonus_api",
    )] = ([], [])
    expected[(
        "/api/osc/debt/supplement-checklist",
        "POST",
        "osc_debt.debt_supplement_checklist",
    )] = (["INSERT", "INSERT"], ["case_checklists", "case_checklists"])
    expected[("/api/osc/debt/validate", "POST", "osc_debt.debt_validate")] = ([], [])
    expected[("/api/osc/gcal/auth/start", "POST", "osc_gcal.gcal_auth_start")] = ([], [])
    expected[("/api/osc/gcal/sync", "POST", "osc_gcal.gcal_sync")] = ([], [])

    assert len(transactional) == len(expected) == 25
    assert set(transactional) == set(expected)
    for key, case in transactional.items():
        kinds, tables = expected[key]
        guard = case["side_effect_guard"]
        assert case["passed"] is True
        assert case["actual_handler_dispatched"] is True
        assert case["observed_status"] == 200
        assert case["contract"]["observed"]["ok"] is True
        assert guard["statement_kinds"] == kinds
        assert guard["statement_tables"] == tables
        assert guard["sqlite_operation_journal_rows"] == len(kinds)
        assert guard["database_mutations"] == sum(kind != "SELECT" for kind in kinds)
        assert all(guard[name] == 0 for name in (
            "network_attempts", "subprocess_attempts", "live_state_attempts",
            "writes_outside_sandbox", "mutations_outside_sandbox",
        ))

    file_cases = {
        (case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case.get("side_effect_guard", {}).get("fixture") == "sandbox_file_transcript"
    }
    expected_files = {
        ("/api/osc/debt/address-data", "osc_debt.debt_address_data"): (1, 0, 0),
        ("/api/osc/debt/generate", "osc_debt.debt_generate_document"): (1, 0, 0),
        ("/api/osc/debt/batch-generate", "osc_debt.debt_batch_generate"): (1, 0, 0),
        ("/api/osc/debt/auto-import", "osc_debt.debt_auto_import"): (0, 0, 0),
        ("/api/osc/debt/merge-pdf", "osc_debt.debt_merge_pdf"): (1, 0, 0),
        ("/api/osc/gcal/disconnect", "osc_gcal.gcal_disconnect"): (0, 1, 0),
    }
    assert set(file_cases) == set(expected_files)
    for key, case in file_cases.items():
        added, removed, changed = expected_files[key]
        guard = case["side_effect_guard"]
        transcript = guard["file_transcript"]
        assert case["passed"] is True
        assert case["observed_status"] == 200
        assert case["contract"]["observed"]["ok"] is True
        assert len(transcript["added"]) == added
        assert len(transcript["removed"]) == removed
        assert len(transcript["changed"]) == changed
        assert len(transcript["root_sha256"]) == 64
        assert all(len(value) == 64 for value in transcript["before_sha256"].values())
        assert all(len(value) == 64 for value in transcript["after_sha256"].values())
        if key == ("/api/osc/debt/merge-pdf", "osc_debt.debt_merge_pdf"):
            assert len(transcript["ephemeral_removed"]) == 1
            assert transcript["ephemeral_removed"][0]["path"].startswith("uploads/")
            assert len(transcript["ephemeral_removed"][0]["sha256"]) == 64
        assert all(guard[name] == 0 for name in (
            "network_attempts", "subprocess_attempts", "live_state_attempts",
            "writes_outside_sandbox", "mutations_outside_sandbox",
        ))


def test_next_osc_read_only_batch_uses_actual_handlers_and_select_only_fixture(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    expected = {
        ("/api/osc/settings", "osc_settings.osc_settings_api"),
        ("/api/osc/settings/<path:setting_key>", "osc_settings.osc_setting_detail_api"),
        ("/api/osc/courts", "osc_settings.osc_courts_api"),
        ("/api/osc/legal-aid-branches", "osc_settings.osc_legal_aid_branches_api"),
        ("/api/osc/case-reason-templates", "osc_cases.osc_case_reason_templates_api"),
        ("/api/osc/activity-logs", "osc_cases.osc_activity_logs_api"),
        ("/api/osc/user-settings", "osc_cases.osc_user_settings_api"),
        ("/api/osc/memory-keywords", "osc_cases.osc_memory_keywords_api"),
        ("/api/osc/opponents", "osc_cases.osc_opponents_api"),
        ("/api/osc/pdf-generation-log", "osc_cases.osc_pdf_generation_log_api"),
        ("/api/osc/document-templates", "osc_cases.osc_document_templates_api"),
        ("/api/osc/document-keywords", "osc_cases.osc_document_keywords_api"),
        ("/api/osc/document-replacements", "osc_cases.osc_document_replacements_api"),
        ("/api/osc/quotation-templates", "osc_cases.osc_quotation_templates_api"),
        ("/api/osc/clients", "osc_cases.osc_clients_api"),
        ("/api/osc/meetings", "osc_cases.osc_meetings_api"),
        ("/api/osc/todos", "osc_cases.osc_todos_api"),
    }
    cases = {
        (case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["method"] == "GET" and (case["rule"], case["endpoint"]) in expected
    }

    assert set(cases) == expected
    assert all(case["passed"] and case["actual_handler_dispatched"] for case in cases.values())
    assert all(case["observed_status"] == 200 for case in cases.values())
    assert all(
        case["side_effect_guard"]
        == {
            "fixture_database_calls": 1,
            "statement_kinds": ["SELECT"],
            "database_mutations": 0,
            "fixture": "select_only_in_memory",
            "network_attempts": 0,
            "subprocess_attempts": 0,
            "live_state_attempts": 0,
            "writes_outside_sandbox": 0,
            "mutations_outside_sandbox": 0,
            "external_storage_access_attempts": 0,
        }
        for case in cases.values()
    )


def test_next_osc_detail_batch_dispatches_nonempty_rows_with_select_only_guards(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    expected = {
        (
            "/api/osc/accounting/transactions/<int:row_id>",
            "osc_accounting.osc_accounting_transaction_detail_api",
        ),
        (
            "/api/osc/accounting/defaults/<int:row_id>",
            "osc_accounting.osc_accounting_default_detail_api",
        ),
        (
            "/api/osc/accounting/recurring/<int:row_id>",
            "osc_accounting.osc_accounting_recurring_detail_api",
        ),
        ("/api/osc/courts/<int:row_id>", "osc_settings.osc_court_detail_api"),
        (
            "/api/osc/legal-aid-branches/<int:row_id>",
            "osc_settings.osc_legal_aid_branch_detail_api",
        ),
        ("/api/osc/activity-logs/<int:row_id>", "osc_cases.osc_activity_log_detail_api"),
        (
            "/api/osc/case-reason-templates/<int:row_id>",
            "osc_cases.osc_case_reason_template_detail_api",
        ),
        ("/api/osc/calendar/events/<int:row_id>", "osc_cases.osc_calendar_event_detail_api"),
        ("/api/osc/clients/<row_id>", "osc_cases.osc_client_detail_api"),
        (
            "/api/osc/document-keywords/<int:row_id>",
            "osc_cases.osc_document_keyword_detail_api",
        ),
        (
            "/api/osc/document-replacements/<int:row_id>",
            "osc_cases.osc_document_replacement_detail_api",
        ),
        (
            "/api/osc/document-templates/<int:row_id>",
            "osc_cases.osc_document_template_detail_api",
        ),
        ("/api/osc/meetings/<int:row_id>", "osc_cases.osc_meeting_detail_api"),
        (
            "/api/osc/memory-keywords/<path:case_number>/<path:hotkey>",
            "osc_cases.osc_memory_keyword_detail_api",
        ),
        ("/api/osc/opponents/<int:row_id>", "osc_cases.osc_opponent_detail_api"),
        (
            "/api/osc/pdf-generation-log/<int:row_id>",
            "osc_cases.osc_pdf_generation_log_detail_api",
        ),
        (
            "/api/osc/quotation-templates/<int:row_id>",
            "osc_cases.osc_quotation_template_detail_api",
        ),
        ("/api/osc/quotations/<row_id>", "osc_cases.osc_quotation_detail_api"),
        ("/api/osc/todos/<int:row_id>", "osc_cases.osc_todo_detail_api"),
        ("/api/osc/user-settings/<int:row_id>", "osc_cases.osc_user_setting_detail_api"),
    }
    cases = {
        (case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["method"] == "GET" and (case["rule"], case["endpoint"]) in expected
    }
    expected_guard = {
        "fixture_database_calls": 1,
        "statement_kinds": ["SELECT"],
        "database_mutations": 0,
        "fixture": "select_only_in_memory",
        "network_attempts": 0,
        "subprocess_attempts": 0,
        "live_state_attempts": 0,
        "writes_outside_sandbox": 0,
        "mutations_outside_sandbox": 0,
        "external_storage_access_attempts": 0,
    }

    assert set(cases) == expected
    assert all(case["passed"] and case["actual_handler_dispatched"] for case in cases.values())
    assert all(case["observed_status"] == 200 for case in cases.values())
    assert all(case["side_effect_guard"] == expected_guard for case in cases.values())
    assert all(
        case["contract"]["observed"]["item"]
        == {
            "id": "offline-row",
            "fixture": "select-only",
            "endpoint": case["endpoint"],
        }
        for case in cases.values()
    )


def test_third_safe_batch_binds_select_counts_or_proves_zero_io(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    expected = {
        ("/api/osc/accounting/transactions", "osc_accounting.osc_accounting_transactions_api"): (200, 1),
        ("/api/osc/accounting/defaults", "osc_accounting.osc_accounting_defaults_api"): (200, 1),
        ("/api/osc/accounting/recurring", "osc_accounting.osc_accounting_recurring_api"): (200, 1),
        ("/api/osc/accounting/summary", "osc_accounting.osc_accounting_summary_api"): (200, 2),
        ("/api/osc/quotations", "osc_cases.osc_quotations_api"): (200, 1),
        ("/api/osc/calendar/events", "osc_cases.osc_calendar_events_api"): (200, 1),
        ("/api/osc/laf", "osc_cases.osc_laf_api"): (200, 3),
        ("/api/osc/laf/cases", "osc_cases.osc_laf_cases_api"): (200, 1),
        ("/api/osc/checklists/case", "osc_cases.osc_case_checklist_get"): (200, 1),
        ("/api/osc/checklists/legal-aid", "osc_cases.osc_laf_checklist_get"): (200, 1),
        ("/api/osc/checklists/debt-required", "osc_cases.osc_laf_debt_required_get"): (400, 0),
        ("/api/osc/drafts/feedback", "osc_cases.osc_drafts_feedback_recent_api"): (200, 0),
        ("/api/osc/drafts/meta", "osc_cases.osc_drafts_meta_api"): (200, 0),
        ("/api/osc/archive-jobs/<job_id>", "osc_cases.osc_archive_job_status_api"): (200, 0),
        ("/api/osc/files/text", "osc_cases.osc_file_text_api"): (400, 0),
        ("/api/osc/debt/forms", "osc_debt.debt_forms_list"): (200, 0),
        ("/api/osc/debt/courts", "osc_debt.debt_courts_list"): (200, 0),
        ("/api/osc/debt/expense-reference", "osc_debt.debt_expense_reference"): (200, 0),
        ("/api/osc/debt/schema/<form_type>", "osc_debt.debt_form_schema"): (200, 0),
        ("/api/osc/pdf/info", "osc_pdf.osc_pdf_info_api"): (400, 0),
    }
    cases = {
        (case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["method"] == "GET" and (case["rule"], case["endpoint"]) in expected
    }

    assert set(cases) == set(expected)
    for key, case in cases.items():
        expected_status, expected_selects = expected[key]
        assert case["passed"] is True
        assert case["actual_handler_dispatched"] is True
        assert case["auth_or_not_found_status_used_as_proof"] is False
        assert case["observed_status"] == expected_status
        assert case["side_effect_guard"] == {
            "fixture_database_calls": expected_selects,
            "statement_kinds": ["SELECT"] * expected_selects,
            "database_mutations": 0,
            "fixture": "select_only_in_memory",
            "network_attempts": 0,
            "subprocess_attempts": 0,
            "live_state_attempts": 0,
            "writes_outside_sandbox": 0,
            "mutations_outside_sandbox": 0,
            "external_storage_access_attempts": 0,
        }


def test_fourth_safe_batch_uses_bound_in_memory_dependencies_or_select_only_sql(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    expected = {
        ("/api/nerv/skills", "admin_runtime.api_nerv_skills"): (0, "in_memory_dependencies"),
        ("/api/nerv/product-runtime", "admin_runtime.api_nerv_product_runtime"): (0, "in_memory_dependencies"),
        (
            "/api/nerv/skill-interview",
            "admin_runtime.api_nerv_skill_interview_status",
        ): (0, "in_memory_dependencies"),
        (
            "/api/skills/interview-history",
            "admin_runtime.api_skill_interview_history",
        ): (0, "in_memory_dependencies"),
        (
            "/api/skills/<skill_name>/versions",
            "admin_runtime.api_skill_versions",
        ): (0, "in_memory_dependencies"),
        ("/api/osc/saas/overview", "osc_cases.osc_saas_overview_api"): (0, "select_only_in_memory"),
        ("/api/osc/saas/timeline", "osc_cases.osc_saas_timeline_api"): (0, "select_only_in_memory"),
        ("/api/osc/saas/task-boards", "osc_cases.osc_saas_task_boards_api"): (0, "select_only_in_memory"),
        ("/api/osc/saas/onboarding", "osc_cases.osc_saas_onboarding_api"): (0, "select_only_in_memory"),
        (
            "/api/osc/saas/notification-prefs",
            "osc_cases.osc_saas_notification_prefs_api",
        ): (0, "select_only_in_memory"),
        (
            "/api/osc/saas/workflow-templates",
            "osc_cases.osc_saas_workflow_templates_api",
        ): (0, "select_only_in_memory"),
        (
            "/api/osc/saas/ai-governance",
            "osc_cases.osc_saas_ai_governance_api",
        ): (0, "select_only_in_memory"),
        (
            "/api/osc/saas/operations-report",
            "osc_cases.osc_saas_operations_report_api",
        ): (0, "select_only_in_memory"),
        (
            "/api/osc/saas/diagnostic-pack",
            "osc_cases.osc_saas_diagnostic_pack_api",
        ): (0, "select_only_in_memory"),
        ("/api/osc/documents", "osc_cases.osc_documents_api"): (2, "select_only_in_memory"),
        ("/api/osc/judgments", "osc_cases.osc_judgments_compat_api"): (0, "select_only_in_memory"),
        ("/api/osc/case-intelligence", "osc_cases.osc_case_intelligence_api"): (0, "select_only_in_memory"),
        (
            "/api/osc/cases/<row_id>/intelligence-snapshot",
            "osc_cases.osc_case_intelligence_for_case_api",
        ): (0, "select_only_in_memory"),
        (
            "/api/osc/clients/<row_id>/workbench",
            "osc_cases.osc_client_workbench_api",
        ): (2, "select_only_in_memory"),
        ("/api/osc/debt/cases", "osc_debt.debt_cases_list"): (1, "select_only_in_memory"),
    }
    cases = {
        (case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if (case["rule"], case["endpoint"]) in expected
    }

    assert set(cases) == set(expected)
    for key, case in cases.items():
        expected_selects, fixture = expected[key]
        assert case["passed"] is True
        assert case["actual_handler_dispatched"] is True
        assert case["auth_or_not_found_status_used_as_proof"] is False
        assert case["observed_status"] == 200
        assert case["side_effect_guard"] == {
            "fixture_database_calls": expected_selects,
            "statement_kinds": ["SELECT"] * expected_selects,
            "database_mutations": 0,
            "fixture": fixture,
            "network_attempts": 0,
            "subprocess_attempts": 0,
            "live_state_attempts": 0,
            "writes_outside_sandbox": 0,
            "mutations_outside_sandbox": 0,
            "external_storage_access_attempts": 0,
        }


def test_fifth_safe_batch_dispatches_validation_before_io_posts(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    expected = {
        ("/api/osc/quotations", "osc_cases.osc_quotations_api"),
        ("/api/osc/checklists/case", "osc_cases.osc_case_checklist_post"),
        ("/api/osc/checklists/legal-aid", "osc_cases.osc_laf_checklist_post"),
        ("/api/osc/checklists/debt-required/save", "osc_cases.osc_laf_debt_required_save"),
        ("/api/osc/forms/preview", "osc_cases.osc_forms_preview_api"),
        ("/api/osc/pdf/upload", "osc_pdf.osc_pdf_upload_api"),
        ("/api/osc/pdf/action", "osc_pdf.osc_pdf_action_api"),
    }
    cases = {
        (case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["method"] == "POST" and (case["rule"], case["endpoint"]) in expected
    }
    expected_guard = {
        "fixture_database_calls": 0,
        "statement_kinds": [],
        "database_mutations": 0,
        "fixture": "select_only_in_memory",
        "network_attempts": 0,
        "subprocess_attempts": 0,
        "live_state_attempts": 0,
        "writes_outside_sandbox": 0,
        "mutations_outside_sandbox": 0,
        "external_storage_access_attempts": 0,
    }

    assert set(cases) == expected
    assert all(case["passed"] and case["actual_handler_dispatched"] for case in cases.values())
    assert all(not case["auth_or_not_found_status_used_as_proof"] for case in cases.values())
    transactional_keys = {
        ("/api/osc/settings", "osc_settings.osc_settings_api"),
        ("/api/osc/courts", "osc_settings.osc_courts_api"),
        ("/api/osc/legal-aid-branches", "osc_settings.osc_legal_aid_branches_api"),
    }
    for key, case in cases.items():
        assert case["method"] == "POST"
        if key in transactional_keys:
            guard = case["side_effect_guard"]
            assert case["observed_status"] == 200
            assert guard["fixture"] == "transactional_sqlite_operation_journal"
            assert guard["statement_kinds"] == ["INSERT", "INSERT"]
            assert guard["database_mutations"] == 2
            assert all(guard[name] == 0 for name in (
                "network_attempts",
                "subprocess_attempts",
                "live_state_attempts",
                "writes_outside_sandbox",
                "mutations_outside_sandbox",
            ))
        else:
            assert case["observed_status"] == 400
            assert case["side_effect_guard"] == expected_guard


def test_sixth_safe_batch_dispatches_isolated_get_projections(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    expected = {
        ("5003", "/health", "health"): 200,
        ("5003", "/melchior/health", "api_melchior_health"): 200,
        ("5003", "/sages", "sages_status"): 200,
        ("5003", "/skills/knowledge/stats", "api_skill_knowledge_stats"): 200,
        ("5003", "/summarize/health", "api_summarize_health"): 200,
        (
            "5002",
            "/static/worldmonitor_reports",
            "dashboard_pages.worldmonitor_reports_redirect",
        ): 302,
        (
            "5002",
            "/static/worldmonitor_reports/",
            "dashboard_pages.worldmonitor_reports_redirect",
        ): 302,
        ("5002", "/worldmonitor", "dashboard_pages.worldmonitor_entry"): 302,
        ("5002", "/worldmonitor/", "dashboard_pages.worldmonitor_entry"): 302,
        ("5002", "/dashboard", "dashboard_pages.dashboard"): 302,
        ("5002", "/dashboard/legacy", "dashboard_pages.dashboard_legacy"): 302,
        (
            "5002",
            "/research/judgment-classifier",
            "dashboard_pages.research_judgment_classifier",
        ): 200,
        ("5002", "/dashboard/nerv", "dashboard_pages.magi_adjust"): 200,
        ("5002", "/nerv", "dashboard_pages.magi_adjust"): 200,
        ("5002", "/magi-adjust", "dashboard_pages.magi_adjust"): 200,
        ("5002", "/magi-settings", "dashboard_pages.magi_adjust"): 200,
        ("5002", "/golem", "dashboard_pages.golem_console"): 200,
        ("5002", "/dashboard/golem", "dashboard_pages.golem_console"): 200,
        ("5002", "/mobile/manifest.webmanifest", "dashboard_pages.mobile_manifest"): 200,
        ("5002", "/mobile/sw.js", "dashboard_pages.mobile_service_worker"): 200,
    }
    cases = {
        (case["service"], case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["method"] == "GET"
        and (case["service"], case["rule"], case["endpoint"]) in expected
    }
    expected_guard = {
        "fixture_database_calls": 0,
        "statement_kinds": [],
        "database_mutations": 0,
        "fixture": "in_memory_dependencies",
        "network_attempts": 0,
        "subprocess_attempts": 0,
        "live_state_attempts": 0,
        "writes_outside_sandbox": 0,
        "mutations_outside_sandbox": 0,
        "external_storage_access_attempts": 0,
    }

    assert set(cases) == set(expected)
    assert all(case["passed"] and case["actual_handler_dispatched"] for case in cases.values())
    assert all(not case["auth_or_not_found_status_used_as_proof"] for case in cases.values())
    assert all(case["observed_status"] == expected[key] for key, case in cases.items())
    assert all(case["side_effect_guard"] == expected_guard for case in cases.values())


def test_seventh_safe_batch_dispatches_sandboxed_get_projections(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    expected = {
        ("5002", "/api/codex-distributed/status", "admin_runtime.api_codex_distributed_status"),
        ("5002", "/api/drive-case-exclusions", "admin_runtime.api_drive_case_exclusions_list"),
        ("5002", "/api/live-log", "admin_runtime.api_live_log"),
        ("5002", "/api/nerv/heavy-runtime", "admin_runtime.api_nerv_heavy_runtime"),
        ("5002", "/api/nerv/skills/<skill_name>", "admin_runtime.api_nerv_skill_detail"),
        ("5002", "/api/status", "admin_runtime.api_status"),
        ("5002", "/health", "admin_runtime.health"),
        ("5002", "/app", "dashboard_pages.mobile_home"),
        ("5002", "/mobile", "dashboard_pages.mobile_home"),
        ("5002", "/app-admin", "dashboard_pages.mobile_admin"),
        ("5002", "/mobile-admin", "dashboard_pages.mobile_admin"),
        ("5002", "/dashboard/beginner", "dashboard_pages.dashboard_beginner"),
        ("5002", "/start", "dashboard_pages.dashboard_beginner"),
        ("5002", "/dashboard/status", "dashboard_pages.status_center"),
        ("5002", "/status", "dashboard_pages.status_center"),
        ("5002", "/dashboard/website", "dashboard_pages.dashboard_website"),
        ("5002", "/magi-research", "dashboard_pages.research_panel"),
        ("5002", "/research", "dashboard_pages.research_panel"),
        ("5002", "/intel", "dashboard_pages.intel_panel"),
        ("5002", "/mobile/config.json", "dashboard_pages.mobile_config_json"),
    }
    cases = {
        (case["service"], case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["method"] == "GET"
        and (case["service"], case["rule"], case["endpoint"]) in expected
    }
    expected_guard = {
        "fixture_database_calls": 0,
        "statement_kinds": [],
        "database_mutations": 0,
        "fixture": "in_memory_dependencies",
        "network_attempts": 0,
        "subprocess_attempts": 0,
        "live_state_attempts": 0,
        "writes_outside_sandbox": 0,
        "mutations_outside_sandbox": 0,
        "external_storage_access_attempts": 0,
    }

    assert set(cases) == expected
    assert all(case["passed"] and case["actual_handler_dispatched"] for case in cases.values())
    assert all(not case["auth_or_not_found_status_used_as_proof"] for case in cases.values())
    assert all(case["observed_status"] == 200 for case in cases.values())
    assert all(case["side_effect_guard"] == expected_guard for case in cases.values())


def test_eighth_safe_batch_dispatches_bound_handlers_with_zero_isolation_delta(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    expected = {
        ("GET", "/readyz", "admin_runtime.readyz"): (200, 0, "in_memory_dependencies"),
        ("GET", "/saas-readyz", "admin_runtime.saas_readyz"): (200, 0, "in_memory_dependencies"),
        (
            "GET",
            "/research/rss-preview",
            "dashboard_pages.research_rss_preview",
        ): (200, 0, "in_memory_dependencies"),
        ("GET", "/api/osc/cases", "osc_cases.osc_cases_api"): (200, 1, "select_only_in_memory"),
        ("POST", "/api/osc/cases", "osc_cases.osc_cases_api"): (400, 0, "select_only_in_memory"),
        ("GET", "/api/osc/insights", "osc_cases.osc_insights_api"): (200, 0, "select_only_in_memory"),
        ("POST", "/api/osc/insights", "osc_cases.osc_insights_api"): (400, 0, "select_only_in_memory"),
        (
            "GET",
            "/api/osc/insights/<insight_id>",
            "osc_cases.osc_insight_detail_api",
        ): (200, 0, "select_only_in_memory"),
        (
            "GET",
            "/api/osc/archive-wizard/preview",
            "osc_cases.osc_archive_wizard_preview_api",
        ): (200, 0, "select_only_in_memory"),
        (
            "POST",
            "/api/osc/archive-wizard/execute",
            "osc_cases.osc_archive_wizard_execute_api",
        ): (400, 0, "select_only_in_memory"),
        ("GET", "/api/osc/backups", "osc_cases.osc_backup_list"): (200, 0, "select_only_in_memory"),
        (
            "POST",
            "/api/osc/backups/<filename>/restore",
            "osc_cases.osc_backup_restore",
        ): (400, 0, "select_only_in_memory"),
        (
            "GET",
            "/api/osc/template-folder",
            "osc_cases.osc_template_folder_api",
        ): (200, 1, "select_only_in_memory"),
        ("GET", "/api/osc/debt/source-status", "osc_debt.debt_source_status"): (200, 0, "select_only_in_memory"),
        (
            "GET",
            "/api/osc/debt/import-candidates",
            "osc_debt.debt_import_candidates",
        ): (200, 0, "select_only_in_memory"),
        ("GET", "/api/osc/debt/address-data", "osc_debt.debt_address_data"): (200, 0, "select_only_in_memory"),
        (
            "GET",
            "/api/osc/debt/scan-evidence/<case_id>",
            "osc_debt.debt_scan_evidence",
        ): (400, 1, "select_only_in_memory"),
        (
            "GET",
            "/api/osc/accounting/import/google-sheet",
            "osc_accounting.osc_accounting_google_sheet_import_api",
        ): (200, 0, "select_only_in_memory"),
        (
            "GET",
            "/api/osc/accounting/monthly-bonus",
            "osc_accounting.osc_accounting_monthly_bonus_api",
        ): (200, 0, "select_only_in_memory"),
        (
            "GET",
            "/api/osc/accounting/monthly-bonus/xlsx",
            "osc_accounting.osc_accounting_monthly_bonus_xlsx_api",
        ): (200, 0, "select_only_in_memory"),
    }
    cases = {
        (case["method"], case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["service"] == "5002"
        and (case["method"], case["rule"], case["endpoint"]) in expected
    }

    assert set(cases) == set(expected)
    for key, case in cases.items():
        expected_status, expected_database_calls, fixture = expected[key]
        assert case["passed"] and case["actual_handler_dispatched"]
        assert not case["auth_or_not_found_status_used_as_proof"]
        assert case["observed_status"] == expected_status
        assert case["side_effect_guard"] == {
            "fixture_database_calls": expected_database_calls,
            "statement_kinds": ["SELECT"] * expected_database_calls,
            "database_mutations": 0,
            "fixture": fixture,
            "network_attempts": 0,
            "subprocess_attempts": 0,
            "live_state_attempts": 0,
            "writes_outside_sandbox": 0,
            "mutations_outside_sandbox": 0,
            "external_storage_access_attempts": 0,
        }


def test_ninth_safe_batch_dispatches_exports_dashboard_gcal_and_golem_with_zero_isolation_delta(
    tmp_path: Path,
) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    expected = {
        ("GET", "/api/golem/api-keys", "golem_console.golem_api_keys_api"): (200, 0, "in_memory_dependencies"),
        ("GET", "/api/golem/logs", "golem_console.golem_logs_api"): (200, 0, "in_memory_dependencies"),
        ("GET", "/api/golem/skills", "golem_console.golem_skills_api"): (200, 0, "in_memory_dependencies"),
        ("GET", "/api/golem/status", "golem_console.golem_status_api"): (200, 0, "in_memory_dependencies"),
        ("POST", "/api/golem/command", "golem_console.golem_command_api"): (400, 0, "in_memory_dependencies"),
        ("GET", "/api/osc/accounting/transactions/xlsx", "osc_accounting.osc_accounting_transactions_xlsx_api"): (200, 1, "select_only_in_memory"),
        ("GET", "/api/osc/cases/<row_id>", "osc_cases.osc_case_detail_api"): (200, 1, "select_only_in_memory"),
        ("GET", "/api/osc/cases/export-csv", "osc_cases.osc_cases_export_csv_api"): (200, 1, "select_only_in_memory"),
        ("GET", "/api/osc/cases/export-xlsx", "osc_cases.osc_cases_export_xlsx_api"): (200, 1, "select_only_in_memory"),
        ("GET", "/api/osc/clients/export-csv", "osc_cases.osc_clients_export_csv_api"): (200, 1, "select_only_in_memory"),
        ("GET", "/api/osc/dashboard", "osc_cases.osc_dashboard_api"): (200, 11, "select_only_in_memory"),
        ("GET", "/api/osc/gcal/status", "osc_gcal.gcal_status"): (200, 0, "select_only_in_memory"),
        ("GET", "/api/osc/cases/<row_id>/folder-path", "osc_cases.osc_case_folder_path_api"): (200, 1, "select_only_in_memory"),
        ("GET", "/api/osc/cases/<row_id>/file-search", "osc_cases.osc_case_file_search_api"): (200, 1, "select_only_in_memory"),
        ("GET", "/api/osc/cases/<row_id>/folder-browser", "osc_cases.osc_case_folder_browser_api"): (400, 1, "select_only_in_memory"),
        ("GET", "/api/osc/cases/<row_id>/workbench", "osc_cases.osc_case_workbench_api"): (200, 10, "select_only_in_memory"),
        ("GET", "/api/osc/cases/<row_id>/address-label", "osc_cases.osc_case_address_label"): (400, 0, "select_only_in_memory"),
        ("GET", "/api/osc/quotations/<row_id>/export-pdf", "osc_cases.osc_quotation_export_pdf"): (200, 1, "select_only_in_memory"),
        ("POST", "/api/osc/cases/import-csv", "osc_cases.osc_cases_import_csv_api"): (400, 0, "select_only_in_memory"),
        ("POST", "/api/osc/clients/import-csv", "osc_cases.osc_clients_import_csv_api"): (400, 0, "select_only_in_memory"),
    }
    cases = {
        (case["method"], case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["service"] == "5002"
        and (case["method"], case["rule"], case["endpoint"]) in expected
    }

    assert set(cases) == set(expected)
    for key, case in cases.items():
        expected_status, expected_database_calls, fixture = expected[key]
        assert case["passed"] and case["actual_handler_dispatched"]
        assert not case["auth_or_not_found_status_used_as_proof"]
        assert case["observed_status"] == expected_status
        assert case["side_effect_guard"] == {
            "fixture_database_calls": expected_database_calls,
            "statement_kinds": ["SELECT"] * expected_database_calls,
            "database_mutations": 0,
            "fixture": fixture,
            "network_attempts": 0,
            "subprocess_attempts": 0,
            "live_state_attempts": 0,
            "writes_outside_sandbox": 0,
            "mutations_outside_sandbox": 0,
            "external_storage_access_attempts": 0,
        }


def test_tenth_safe_batch_dispatches_server_pages_webhook_gets_and_meta_with_zero_isolation_delta(
    tmp_path: Path,
) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    expected = {
        ("/", "index"): (302, 0, "in_memory_dependencies"),
        ("/favicon.ico", "favicon"): (204, 0, "in_memory_dependencies"),
        ("/login", "login"): (200, 0, "in_memory_dependencies"),
        ("/logout", "logout"): (302, 0, "in_memory_dependencies"),
        ("/register", "register"): (200, 0, "in_memory_dependencies"),
        ("/mobile-app", "mobile_app_entry"): (302, 0, "in_memory_dependencies"),
        ("/osc", "osc_interface"): (200, 0, "in_memory_dependencies"),
        ("/osc/debt", "osc_debt_interface"): (200, 0, "in_memory_dependencies"),
        ("/lottery", "lottery.lottery_page"): (200, 0, "in_memory_dependencies"),
        ("/callback", "callback"): (200, 0, "in_memory_dependencies"),
        ("/line/webhook", "callback"): (200, 0, "in_memory_dependencies"),
        ("/api/osc/meta", "osc_cases.osc_meta_api"): (200, 20, "select_only_in_memory"),
    }
    cases = {
        (case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["service"] == "5002"
        and case["method"] == "GET"
        and (case["rule"], case["endpoint"]) in expected
    }

    assert set(cases) == set(expected)
    for key, case in cases.items():
        expected_status, expected_database_calls, fixture = expected[key]
        assert case["passed"] and case["actual_handler_dispatched"]
        assert not case["auth_or_not_found_status_used_as_proof"]
        assert case["observed_status"] == expected_status
        assert case["side_effect_guard"] == {
            "fixture_database_calls": expected_database_calls,
            "statement_kinds": ["SELECT"] * expected_database_calls,
            "database_mutations": 0,
            "fixture": fixture,
            "network_attempts": 0,
            "subprocess_attempts": 0,
            "live_state_attempts": 0,
            "writes_outside_sandbox": 0,
            "mutations_outside_sandbox": 0,
            "external_storage_access_attempts": 0,
        }


def test_eleventh_safe_batch_dispatches_only_synthetic_cache_state_and_sandbox_roots(
    tmp_path: Path,
) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")
    expected = {
        ("/api/memory/stats", "web_runtime.api_memory_stats"): "in_memory_dependencies",
        ("/api/nerv/remote-access", "admin_runtime.api_nerv_remote_access"): "in_memory_dependencies",
        ("/dashboard/nerv/api/health", "admin_runtime.nerv_api_health"): "in_memory_dependencies",
        ("/status/api/health", "admin_runtime.nerv_api_health"): "in_memory_dependencies",
        ("/api/osc/judgments_legacy", "web_runtime.osc_judgments_api"): "in_memory_dependencies",
        ("/api/osc/poll", "web_runtime.osc_poll_api"): "in_memory_dependencies",
        ("/exports/<path:filename>", "serve_exports"): "sandbox_file",
        ("/api/osc/folders/roots", "osc_files.osc_folder_roots_api"): "sandbox_paths",
    }
    cases = {
        (case["rule"], case["endpoint"]): case
        for case in evidence["actual_handler_cases"]
        if case["service"] == "5002"
        and case["method"] == "GET"
        and (case["rule"], case["endpoint"]) in expected
    }

    assert set(cases) == set(expected)
    for key, case in cases.items():
        assert case["passed"] and case["actual_handler_dispatched"]
        assert not case["auth_or_not_found_status_used_as_proof"]
        assert case["observed_status"] == 200
        assert case["side_effect_guard"] == {
            "fixture_database_calls": 0,
            "statement_kinds": [],
            "database_mutations": 0,
            "fixture": expected[key],
            "network_attempts": 0,
            "subprocess_attempts": 0,
            "live_state_attempts": 0,
            "writes_outside_sandbox": 0,
            "mutations_outside_sandbox": 0,
            "external_storage_access_attempts": 0,
        }

    remote = cases[("/api/nerv/remote-access", "admin_runtime.api_nerv_remote_access")]["contract"]["observed"]
    assert remote["hostname"] == "offline-host"
    assert remote["tailscale"] == {"status": "offline", "ip": "", "dns_name": ""}
    assert remote["cloudflare"] == {"status": "offline", "url": ""}
    assert all(
        cases[(rule, "admin_runtime.nerv_api_health")]["contract"]["observed"]["cached"] is True
        for rule in ("/dashboard/nerv/api/health", "/status/api/health")
    )


def test_real_tools_surface_and_offline_safety_are_evidence_bound(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")

    assert evidence["surface_verification"]["5003"] == {
        "service": "5003",
        "expected": 67,
        "actual": 67,
        "exact": True,
    }
    assert evidence["surface_verification"]["5002"]["exact_full_surface_loaded"] is False
    assert len(evidence["script_sha256"]) == 64
    assert set(evidence["handler_source_sha256"]) == {
        "api/tools_api.py",
        "api/blueprints/admin_runtime.py",
        "api/blueprints/dashboard_pages.py",
        "api/blueprints/golem_console.py",
        "api/blueprints/lottery.py",
        "api/blueprints/osc_accounting.py",
        "api/blueprints/osc_cases.py",
        "api/blueprints/osc_debt.py",
        "api/blueprints/osc_files.py",
        "api/blueprints/osc_gcal.py",
            "api/blueprints/osc_pdf.py",
            "api/blueprints/raziel.py",
            "api/blueprints/osc_settings.py",
            "api/blueprints/web_runtime.py",
            "api/webhooks/telegram.py",
        "api/debt_document_generator.py",
        "api/osc/utils.py",
        "api/server.py",
        "skills/labor-law-calculator/action.py",
        "skills/ops/iron_dome_sync.py",
    }
    assert all(len(value) == 64 for value in evidence["handler_source_sha256"].values())
    assert evidence["safety"]["safe_execution"] is True
    assert evidence["safety"]["worker"] == {
        "listener_started": False,
        "network_connections_performed": 0,
        "blocked_network_attempts": 1,
        "subprocess_attempts": 0,
        "live_state_attempts": 0,
        "writes_outside_sandbox": 0,
        "mutations_outside_sandbox": 0,
        "external_storage_access_attempts": 0,
        "external_storage_roots": [
            str(root) for root in _external_storage_roots()
        ],
        "external_storage_access_attempts": 0,
        "sandbox_only": True,
    }
    assert evidence["safety"]["nas_accessed"] is False
    assert evidence["safety"]["external_storage_access_attempts"] == 0
    assert evidence["safety"]["isolation_attempts"] == {
        "external_storage_access": 0
    }
    assert evidence["safety"]["golden_network_access_performed"] is False
    assert evidence["safety"]["golden_production_state_accessed"] is False
    assert evidence["safety"]["operational_network_access_performed"] is False
    assert evidence["safety"]["operational_provider_exchange_performed"] is False
    assert evidence["safety"]["operational_nas_mount_attempted"] is False
    assert evidence["safety"]["operational_production_state_accessed"] is False


def test_route_and_golden_blockers_remain_precise_instead_of_false_passing(tmp_path: Path) -> None:
    evidence = run_actual_route_replay(tmp_path / "route-replay")

    assert evidence["golden_flow_coverage"] == {
        "required_domains": [
            "osc_file_preview_download",
            "tools_read_only_operations_and_audit",
            "nas_file_workflows",
            "office_document_workflows",
            "provider_and_session_integrations",
        ],
        "covered_domains": [
            "osc_file_preview_download",
            "tools_read_only_operations_and_audit",
            "nas_file_workflows",
            "office_document_workflows",
            "provider_and_session_integrations",
        ],
        "missing_domains": [],
        "actual_handler_flows": 5,
        "actual_handler_route_methods": evidence["route_method_summary"]["actual_handler_passed"],
        "complete": True,
    }
    assert evidence["blockers"]["ROUTE_REPLAY_NOT_IMPLEMENTED"] == {
        "retained": True,
        "remaining_routes": evidence["route_summary"]["routes_with_remaining_gap"],
        "remaining_route_methods": evidence["route_method_summary"]["remaining_route_methods"],
        "reason": "not all pinned route-methods have reviewed, bound actual-handler replay cases",
    }
    assert evidence["blockers"]["GOLDEN_FLOW_COVERAGE_INCOMPLETE"] == {
        "retained": False,
        "missing_domains": [],
        "reason": "all required actual-handler golden-flow domains passed in sandbox",
    }
    assert len(evidence["evidence_sha256"]) == 64


def test_cli_exits_blocked_while_emitting_complete_gap_evidence(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--workspace", str(tmp_path / "cli-replay")],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    evidence = json.loads(result.stdout)
    assert evidence["execution_passed"] is True
    assert evidence["coverage_complete"] is False
    assert evidence["passed"] is False
    assert evidence["blockers"]["ROUTE_REPLAY_NOT_IMPLEMENTED"]["retained"] is True
