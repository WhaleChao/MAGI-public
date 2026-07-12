from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "ops" / "agent_readiness_gate.py"
REQUIRED_CAPABILITY_TO_TOOL = {
    "cases.manage": "api/blueprints/osc_cases.py:osc_cases_api",
    "clients.manage": "api/blueprints/osc_cases.py:osc_clients_api",
    "calendar.events": "api/blueprints/osc_cases.py:osc_calendar_events_api",
    "todos.manage": "api/blueprints/osc_cases.py:osc_todos_api",
    "documents.finalize": "api/blueprints/osc_cases.py:osc_documents_finalize_api",
    "files.upload": "api/blueprints/osc_cases.py:osc_file_upload_api",
    "nas.mount_guard": "api/nas_mount_guard.py:ensure_nas_mounts",
    "drive.upload": "api/osc/drive_case_sync.py:execute_nas_to_drive_uploads",
    "laf.portal_draft": "skills/laf-orchestrator/action.py:task_portal_action",
    "file_review.apply": "skills/file-review-orchestrator/action.py:cmd_apply",
    "transcript.sync": "skills/transcript-downloader/action.py:cmd_sync",
    "transcription.audio": "skills/apple/apple_intelligence.py:transcribe_audio",
    "translation.document": "skills/translator/action.py:translate_core",
    "ocr.consensus": "skills/engine/ocr/consensus.py:run_consensus",
    "legal_statutes.search": "skills/statutes-vdb/action.py:task_search",
    "judgments.collect": "skills/judgment-collector/action.py:collect",
    "research.fetch": "skills/research-brief/action.py:task_fetch",
    "drafting.generate": "api/blueprints/osc_cases.py:osc_drafts_generate_api",
    "accounting.transactions": "api/blueprints/osc_accounting.py:osc_accounting_transactions_api",
    "quotation.manage": "api/blueprints/osc_cases.py:osc_quotations_api",
    "memory.capture_rules": "api/domains/memory_flow.py:maybe_capture_user_rules",
    "obsidian.writeback": "skills/obsidian/action.py:task_writeback",
    "realtime.query": "skills/engine/realtime_data_gateway.py:handle_realtime_query",
    "web.upload_task": "api/blueprints/web_runtime.py:_run_direct_web_upload_text_task",
    "models.live_gate": "scripts/ops/model_live_gate.py:build_report",
    "system.acceptance": "scripts/ops/magi_acceptance_gate.py:run_acceptance",
    "backup.restore": "skills/ops/database/backup_restore.py:run_restore",
    "notifications.telegram_delivery": "skills/ops/red_phone.py:send_telegram_push_with_status",
}
REQUIRED_DOMAINS = {capability_id.split(".", 1)[0] for capability_id in REQUIRED_CAPABILITY_TO_TOOL}


def _load_gate():
    spec = importlib.util.spec_from_file_location("agent_readiness_gate_test", GATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capability(*, side_effect: list[str] | None = None) -> dict:
    return {
        "id": "demo.reader",
        "domain": "demo",
        "intent": "Read a public readiness signal.",
        "tool": "skills/demo_tool.py:run",
        "side_effect": side_effect or ["read_only"],
        "verify": "Require a boolean success field.",
        "human_handling": "Ask an operator to resolve unavailable inputs.",
    }


def _write_catalog(tmp_path: Path, capabilities: list[dict]) -> Path:
    tool = tmp_path / "skills" / "demo_tool.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("def run():\n    return True\n", encoding="utf-8")
    catalog = tmp_path / "config" / "agent_capabilities.json"
    catalog.parent.mkdir()
    catalog.write_text(json.dumps({"schema_version": 1, "capabilities": capabilities}), encoding="utf-8")
    return catalog


def test_repository_catalog_is_strict_ready_and_compact():
    gate = _load_gate()

    report = gate.run_gate(strict=True)
    catalog = json.loads((ROOT / "config" / "agent_capabilities.json").read_text(encoding="utf-8"))
    catalog_by_id = {item["id"]: item for item in catalog["capabilities"]}

    assert report["ok"] is True
    assert report["compact"] is True
    assert report["summary"]["capability_count"] >= 30
    assert report["summary"]["error_count"] == 0
    assert report["summary"]["warning_count"] == 0
    assert REQUIRED_DOMAINS <= set(report["domains"])
    assert report["summary"]["domain_count"] >= len(REQUIRED_DOMAINS)
    assert all(set(item) == {"id", "domain", "risk", "ready", "issues"} for item in report["capabilities"])
    assert all(item["ready"] is True for item in report["capabilities"])
    for capability_id, tool in REQUIRED_CAPABILITY_TO_TOOL.items():
        capability = catalog_by_id[capability_id]
        assert capability["tool"] == tool
        assert capability["intent"]
        assert capability["verify"]
        assert capability.get("rollback") or capability.get("human_handling")


def test_high_risk_missing_verify_is_hard_failure(tmp_path):
    gate = _load_gate()
    capability = _capability(side_effect=["external_write"])
    capability.pop("verify")
    catalog = _write_catalog(tmp_path, [capability])

    report = gate.run_gate(capabilities_path=catalog, root=tmp_path)

    assert report["ok"] is False
    assert {item["code"] for item in report["issues"]} == {"missing_verify"}
    assert report["issues"][0]["severity"] == "error"


def test_high_risk_missing_recovery_is_hard_failure(tmp_path):
    gate = _load_gate()
    capability = _capability(side_effect=["db_write"])
    capability.pop("human_handling")
    catalog = _write_catalog(tmp_path, [capability])

    report = gate.run_gate(capabilities_path=catalog, root=tmp_path)

    assert report["ok"] is False
    assert {item["code"] for item in report["issues"]} == {"missing_recovery"}
    assert report["issues"][0]["severity"] == "error"


def test_low_risk_missing_contract_is_warning_until_strict(tmp_path):
    gate = _load_gate()
    capability = _capability()
    capability.pop("verify")
    capability.pop("human_handling")
    catalog = _write_catalog(tmp_path, [capability])

    relaxed = gate.run_gate(capabilities_path=catalog, root=tmp_path)
    strict = gate.run_gate(capabilities_path=catalog, root=tmp_path, strict=True)

    assert relaxed["ok"] is True
    assert relaxed["summary"]["warning_count"] == 2
    assert strict["ok"] is False
    assert strict["capabilities"][0]["ready"] is False


def test_missing_tool_and_private_content_fail_closed(tmp_path):
    gate = _load_gate()
    capability = _capability()
    capability["tool"] = "skills/missing.py:run"
    capability["intent"] = "Use api_key=not-public while reading a signal."
    catalog = _write_catalog(tmp_path, [capability])

    report = gate.run_gate(capabilities_path=catalog, root=tmp_path)

    codes = {item["code"] for item in report["issues"]}
    assert report["ok"] is False
    assert {"tool_source_missing", "private_content"} <= codes


def test_cli_writes_compact_json_and_strict_exit_code(tmp_path, capsys):
    gate = _load_gate()
    capability = _capability()
    capability.pop("verify")
    catalog = _write_catalog(tmp_path, [capability])
    output = tmp_path / "reports" / "agent_readiness.json"

    code = gate.main(
        [
            "--root",
            str(tmp_path),
            "--capabilities",
            str(catalog),
            "--json-out",
            str(output),
        ]
    )

    stdout_report = json.loads(capsys.readouterr().out)
    written_report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert written_report == stdout_report
    assert written_report["summary"]["warning_count"] == 1

    assert gate.main(["--root", str(tmp_path), "--capabilities", str(catalog), "--strict"]) == 1
