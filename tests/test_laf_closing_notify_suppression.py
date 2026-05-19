import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORCH_PATH = ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "laf_orchestrator.py"


spec = importlib.util.spec_from_file_location("laf_orchestrator_for_notify_test", ORCH_PATH)
laf_orchestrator = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(laf_orchestrator)


class DummyNotifier:
    def __init__(self):
        self.messages = []

    def notify_admin(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        return True


def test_closing_suppress_notify_blocks_success_message(monkeypatch):
    orch = laf_orchestrator.LAFOrchestrator(dry_run=False)
    notifier = DummyNotifier()
    orch._notifier = notifier
    orch.laf_config = {"username": "test-user", "password": "test-password"}
    orch._log_event = lambda *args, **kwargs: None

    fake_module = types.SimpleNamespace(_export_file_to_static=lambda *args, **kwargs: {})
    monkeypatch.setitem(sys.modules, "laf_automation_v2", fake_module)

    class FakeAutomation:
        last_debug_artifact = {}
        last_upload_result = {}

        def login(self):
            return True

        def save_closing_report_draft(self, **kwargs):
            return True

    orch._get_automation = lambda: FakeAutomation()
    ok = orch.execute_portal_closing(
        "1150101-W-001",
        {"meeting_count": 1, "contact_count": 0, "inq_count": 0, "court_count": 0, "review_count": 1, "document_count": 1},
        {},
        upload_files=[],
        client_name="測試當事人",
        suppress_notify=True,
    )

    assert ok is True
    assert notifier.messages == []
