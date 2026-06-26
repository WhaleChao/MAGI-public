import importlib.util
import sys
import types
from pathlib import Path


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
    assert mod._callback_succeeded({"success": True}) is True
