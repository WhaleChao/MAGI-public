import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "laf_gmail_dispatch_scan.py"


def _load_scan_module(monkeypatch):
    laf_orch_mod = types.ModuleType("casper_ecosystem.law_firm_orchestrators.laf_orchestrator")
    laf_orch_mod.LAFOrchestrator = object
    laf_mod = types.ModuleType("laf")
    laf_mod.LAFGmailMonitor = object
    monkeypatch.setitem(sys.modules, "casper_ecosystem", types.ModuleType("casper_ecosystem"))
    monkeypatch.setitem(sys.modules, "casper_ecosystem.law_firm_orchestrators", types.ModuleType("law_firm_orchestrators"))
    monkeypatch.setitem(sys.modules, "casper_ecosystem.law_firm_orchestrators.laf_orchestrator", laf_orch_mod)
    monkeypatch.setitem(sys.modules, "laf", laf_mod)

    spec = importlib.util.spec_from_file_location("laf_gmail_dispatch_scan_test", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_callback_failure_is_not_processed_success(monkeypatch):
    mod = _load_scan_module(monkeypatch)

    assert mod._callback_succeeded(False) is False
    assert mod._callback_succeeded(None) is False
    assert mod._callback_succeeded({"success": False, "error": "submit failed"}) is False
    assert mod._callback_succeeded({"ok": False}) is False
    assert mod._callback_succeeded({"handled": False}) is False
    assert mod._callback_succeeded({"processed": False}) is False
    assert mod._callback_succeeded({"success": True}) is True


def _case_info(message_id="msg-1"):
    return SimpleNamespace(
        message_id=message_id,
        subject="法扶案件通知",
        laf_case_number="1150529-W-002",
        client_name="測試人",
        notification_type="派案通知",
    )


def test_default_run_is_dry_run_and_writes_pending_report(monkeypatch, tmp_path):
    mod = _load_scan_module(monkeypatch)
    callbacks = []
    marked = []

    class FakeOrchestrator:
        def __init__(self, dry_run=False):
            self.dry_run = dry_run
            self.config = {}

        def _resolve_email_route(self, _case_info, _notification_type):
            return "laf_case"

        def on_new_email(self, case_info):
            callbacks.append(case_info)
            return True

    class FakeMonitor:
        def __init__(self, *_args, **_kwargs):
            pass

        def authenticate(self):
            return True

        def check_emails(self, **_kwargs):
            return [_case_info()]

        def mark_laf_processed(self, message_id):
            marked.append(message_id)

    monkeypatch.setattr(mod, "LAFOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(mod, "LAFGmailMonitor", FakeMonitor)
    monkeypatch.delenv("MAGI_LAF_GMAIL_APPLY", raising=False)
    pending = tmp_path / "pending.json"

    result = mod.run_once(
        SimpleNamespace(
            credentials="credentials.json",
            token="token.pickle",
            days=1,
            max_results=80,
            apply=False,
            dry_run=False,
            json_out=str(tmp_path / "state.json"),
            pending_out=str(pending),
        )
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert callbacks == []
    assert marked == []
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["cases"]["msg-1"]["status"] == "pending_dry_run"


def test_apply_callback_failure_is_not_marked_processed(monkeypatch, tmp_path):
    mod = _load_scan_module(monkeypatch)
    marked = []

    class FakeOrchestrator:
        def __init__(self, dry_run=False):
            self.dry_run = dry_run
            self.config = {}

        def _resolve_email_route(self, _case_info, _notification_type):
            return "laf_case"

        def on_new_email(self, _case_info):
            return {"ok": False, "error": "callback failed"}

    class FakeMonitor:
        def __init__(self, *_args, **_kwargs):
            pass

        def authenticate(self):
            return True

        def check_emails(self, **_kwargs):
            return [_case_info()]

        def mark_laf_processed(self, message_id):
            marked.append(message_id)

    monkeypatch.setattr(mod, "LAFOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(mod, "LAFGmailMonitor", FakeMonitor)
    pending = tmp_path / "pending.json"

    result = mod.run_once(
        SimpleNamespace(
            credentials="credentials.json",
            token="token.pickle",
            days=1,
            max_results=80,
            apply=True,
            dry_run=False,
            json_out=str(tmp_path / "state.json"),
            pending_out=str(pending),
        )
    )

    assert result["dry_run"] is False
    assert result["marked_processed"] == 0
    assert marked == []
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["failure_count"] == 1
    assert data["cases"]["msg-1"]["status"] == "failed_callback"


def test_apply_callback_success_marks_processed_and_clears_pending(monkeypatch, tmp_path):
    mod = _load_scan_module(monkeypatch)
    marked = []

    class FakeOrchestrator:
        def __init__(self, dry_run=False):
            self.dry_run = dry_run
            self.config = {}

        def _resolve_email_route(self, _case_info, _notification_type):
            return "laf_case"

        def on_new_email(self, _case_info):
            return {"ok": True}

    class FakeMonitor:
        def __init__(self, *_args, **_kwargs):
            pass

        def authenticate(self):
            return True

        def check_emails(self, **_kwargs):
            return [_case_info()]

        def mark_laf_processed(self, message_id):
            marked.append(message_id)

    monkeypatch.setattr(mod, "LAFOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(mod, "LAFGmailMonitor", FakeMonitor)
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"cases": {"msg-1": {"status": "failed_callback"}}}), encoding="utf-8")

    result = mod.run_once(
        SimpleNamespace(
            credentials="credentials.json",
            token="token.pickle",
            days=1,
            max_results=80,
            apply=True,
            dry_run=False,
            json_out=str(tmp_path / "state.json"),
            pending_out=str(pending),
        )
    )

    assert result["marked_processed"] == 1
    assert marked == ["msg-1"]
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["cases"] == {}
