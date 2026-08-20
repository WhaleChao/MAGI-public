import hashlib
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
    assert mod._callback_succeeded({"ok": True}, route="dispatch") is False
    assert mod._callback_succeeded(
        {"ok": True, "created_case": True, "created_case_id": "case-1"},
        route="dispatch",
    ) is True


def test_v3_bound_output_paths_override_legacy_cli_paths(monkeypatch, tmp_path):
    state = tmp_path / "shared" / "static" / "laf_state.json"
    pending = tmp_path / "shared" / "runtime" / "laf_pending.json"
    monkeypatch.setenv("MAGI_LAF_GMAIL_STATE_PATH", str(state))
    monkeypatch.setenv("MAGI_LAF_GMAIL_PENDING_PATH", str(pending))
    mod = _load_scan_module(monkeypatch)

    assert mod._output_path(
        "MAGI_LAF_GMAIL_STATE_PATH", "/legacy/release/static/state.json", mod.DEFAULT_STATE_PATH
    ) == state
    assert mod._output_path(
        "MAGI_LAF_GMAIL_PENDING_PATH", "/legacy/release/.runtime/pending.json", mod.DEFAULT_PENDING_PATH
    ) == pending


def _case_info(message_id="msg-1"):
    return SimpleNamespace(
        message_id=message_id,
        subject="法扶案件通知",
        laf_case_number="1150529-W-002",
        client_name="測試人",
        notification_type="派案通知",
    )


class _FakeDurableDB:
    def __init__(self):
        self.message_ids = set()
        self.records = []

    def check_laf_email_exists(self, message_id):
        return message_id in self.message_ids

    def add_laf_email_record(self, record):
        self.records.append(dict(record))
        self.message_ids.add(record["gmail_message_id"])


def test_default_run_is_dry_run_and_writes_pending_report(monkeypatch, tmp_path):
    mod = _load_scan_module(monkeypatch)
    callbacks = []
    marked = []
    closed = []

    class FakeOrchestrator:
        def __init__(self, dry_run=False):
            self.dry_run = dry_run
            self.config = {}
            self.db = _FakeDurableDB()

        def close(self):
            closed.append(True)

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
    assert closed == [True]
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["cases"]["msg-1"]["status"] == "pending_dry_run"


def test_apply_callback_failure_is_not_marked_processed(monkeypatch, tmp_path):
    mod = _load_scan_module(monkeypatch)
    marked = []
    closed = []

    class FakeOrchestrator:
        def __init__(self, dry_run=False):
            self.dry_run = dry_run
            self.config = {}
            self.db = _FakeDurableDB()

        def close(self):
            closed.append(True)

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
    assert result["cleanup_ok"] is True
    assert closed == [True]
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["failure_count"] == 1
    assert data["cases"]["msg-1"]["status"] == "failed_callback"


def test_apply_callback_success_marks_processed_and_clears_pending(monkeypatch, tmp_path):
    mod = _load_scan_module(monkeypatch)
    marked = []
    closed = []
    durable_db = _FakeDurableDB()

    class FakeOrchestrator:
        def __init__(self, dry_run=False):
            self.dry_run = dry_run
            self.config = {}
            self.db = durable_db

        def close(self):
            closed.append(True)

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
    assert result["cleanup_ok"] is True
    assert durable_db.check_laf_email_exists("msg-1") is True
    assert closed == [True]
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["cases"] == {}


def test_dispatch_without_created_case_proof_is_not_marked_processed(monkeypatch, tmp_path):
    mod = _load_scan_module(monkeypatch)
    marked = []
    durable_db = _FakeDurableDB()

    class FakeOrchestrator:
        def __init__(self, dry_run=False):
            self.dry_run = dry_run
            self.config = {}
            self.db = durable_db

        def close(self):
            pass

        def _resolve_email_route(self, _case_info, _notification_type):
            return "dispatch"

        def on_new_email(self, _case_info):
            return {"ok": True, "created_case": False}

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
            max_results=80,
            apply=True,
            dry_run=False,
            json_out=str(tmp_path / "state.json"),
            pending_out=str(pending),
        )
    )

    assert result["ok"] is False
    assert result["marked_processed"] == 0
    assert marked == []
    assert durable_db.message_ids == set()
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["cases"]["msg-1"]["status"] == "failed_callback"


def test_dispatch_success_persists_created_case_id_before_marking(monkeypatch, tmp_path):
    mod = _load_scan_module(monkeypatch)
    marked = []
    durable_db = _FakeDurableDB()

    class FakeOrchestrator:
        def __init__(self, dry_run=False):
            self.dry_run = dry_run
            self.config = {}
            self.db = durable_db

        def close(self):
            pass

        def _resolve_email_route(self, _case_info, _notification_type):
            return "dispatch"

        def on_new_email(self, _case_info):
            return {
                "ok": True,
                "created_case": True,
                "created_case_id": "case-123",
                "case_number": "2026-0123",
            }

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

    result = mod.run_once(
        SimpleNamespace(
            max_results=80,
            apply=True,
            dry_run=False,
            json_out=str(tmp_path / "state.json"),
            pending_out=str(tmp_path / "pending.json"),
        )
    )

    assert result["ok"] is True
    assert result["marked_processed"] == 1
    assert marked == ["msg-1"]
    assert durable_db.records[0]["created_case_id"] == "case-123"


def test_result_download_retry_requires_observable_durable_queue(monkeypatch):
    mod = _load_scan_module(monkeypatch)
    db = _FakeDurableDB()
    case_info = _case_info("result-message")
    case_info.notification_type = "結案轉入通知"

    missing_queue = SimpleNamespace(
        db=db,
        _load_pending_portal_downloads=lambda: {},
        _portal_retry_item_is_pending=lambda item: bool(item),
    )
    ok, error = mod._persist_durable_success(
        missing_queue,
        case_info,
        route="result_download",
        callback_result={
            "ok": True,
            "route": "result_download",
            "laf_case_number": case_info.laf_case_number,
            "retry_queued": True,
            "retry_queue_token": "synthetic-receipt",
        },
    )
    assert ok is False
    assert error == "durable_portal_retry_not_observable_after_queue"
    assert db.records == []

    queued = SimpleNamespace(
        db=db,
        _load_pending_portal_downloads=lambda: {
            case_info.laf_case_number: {
                "status": "pending_retry",
                "queue_token": "synthetic-receipt",
            }
        },
        _portal_retry_item_is_pending=lambda item: item.get("status") == "pending_retry",
    )
    ok, error = mod._persist_durable_success(
        queued,
        case_info,
        route="result_download",
        callback_result={
            "ok": True,
            "route": "result_download",
            "laf_case_number": case_info.laf_case_number,
            "retry_queued": True,
            "retry_queue_token": "synthetic-receipt",
        },
    )
    assert ok is True
    assert error == ""
    assert db.records[-1]["status"] == "pending_download"


def test_result_download_retry_receipt_must_match_durable_queue(monkeypatch):
    mod = _load_scan_module(monkeypatch)
    db = _FakeDurableDB()
    case_info = _case_info("result-message-mismatch")
    orchestrator = SimpleNamespace(
        db=db,
        _load_pending_portal_downloads=lambda: {
            case_info.laf_case_number: {
                "status": "pending_retry",
                "queue_token": "newer-event",
            }
        },
        _portal_retry_item_is_pending=lambda item: item.get("status") == "pending_retry",
    )

    ok, error = mod._persist_durable_success(
        orchestrator,
        case_info,
        route="result_download",
        callback_result={
            "ok": True,
            "route": "result_download",
            "laf_case_number": case_info.laf_case_number,
            "retry_queued": True,
            "retry_queue_token": "older-event",
        },
    )

    assert ok is False
    assert error == "durable_portal_retry_receipt_mismatch"
    assert db.records == []


def test_result_download_cross_chain_orders_queue_db_then_gmail_marker(monkeypatch, tmp_path):
    mod = _load_scan_module(monkeypatch)
    actions = []
    queue = {}

    class OrderedDB(_FakeDurableDB):
        def add_laf_email_record(self, record):
            actions.append("durable_email_record")
            super().add_laf_email_record(record)

    durable_db = OrderedDB()

    class FakeOrchestrator:
        def __init__(self, dry_run=False):
            self.dry_run = dry_run
            self.config = {}
            self.db = durable_db

        def close(self):
            pass

        def _resolve_email_route(self, _case_info, _notification_type):
            return "result_download"

        def on_new_email(self, case_info):
            actions.append("durable_portal_queue")
            queue[case_info.laf_case_number] = {
                "status": "pending_retry",
                "queue_token": "cross-chain-receipt",
            }
            return {
                "ok": True,
                "route": "result_download",
                "laf_case_number": case_info.laf_case_number,
                "retry_queued": True,
                "retry_queue_token": "cross-chain-receipt",
            }

        def _load_pending_portal_downloads(self):
            return dict(queue)

        @staticmethod
        def _portal_retry_item_is_pending(item):
            return item.get("status") == "pending_retry"

    class FakeMonitor:
        def __init__(self, *_args, **_kwargs):
            pass

        def authenticate(self):
            return True

        def check_emails(self, **_kwargs):
            return [_case_info("cross-chain-message")]

        def mark_laf_processed(self, _message_id):
            actions.append("gmail_processed_marker")

    monkeypatch.setattr(mod, "LAFOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(mod, "LAFGmailMonitor", FakeMonitor)

    result = mod.run_once(
        SimpleNamespace(
            max_results=1,
            apply=True,
            dry_run=False,
            json_out=str(tmp_path / "state.json"),
            pending_out=str(tmp_path / "pending.json"),
        )
    )

    assert result["ok"] is True
    assert result["marked_processed"] == 1
    assert actions == [
        "durable_portal_queue",
        "durable_email_record",
        "gmail_processed_marker",
    ]


def test_pending_assignment_thread_is_processed_oldest_first(monkeypatch, tmp_path):
    mod = _load_scan_module(monkeypatch)
    handled = []
    durable_db = _FakeDurableDB()
    original = _case_info("original")
    original.received_at = "2026-07-23 10:36:35"
    reply = _case_info("reply")
    reply.received_at = "2026-07-23 17:02:07"

    class FakeOrchestrator:
        def __init__(self, dry_run=False):
            self.dry_run = dry_run
            self.config = {}
            self.db = durable_db

        def close(self):
            pass

        def _resolve_email_route(self, _case_info, _notification_type):
            return "dispatch"

        def on_new_email(self, case_info):
            handled.append(case_info.message_id)
            return {
                "ok": True,
                "created_case": True,
                "created_case_id": "case-123",
                "case_number": "2026-0123",
            }

    class FakeMonitor:
        def __init__(self, *_args, **_kwargs):
            pass

        def authenticate(self):
            return True

        def check_emails(self, **_kwargs):
            return [reply, original]

        def mark_laf_processed(self, _message_id):
            pass

    monkeypatch.setattr(mod, "LAFOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(mod, "LAFGmailMonitor", FakeMonitor)

    result = mod.run_once(
        SimpleNamespace(
            max_results=80,
            apply=True,
            dry_run=False,
            json_out=str(tmp_path / "state.json"),
            pending_out=str(tmp_path / "pending.json"),
        )
    )

    assert result["ok"] is True
    assert handled == ["original", "reply"]


def test_reconcile_pending_clears_already_processed_entries(monkeypatch, tmp_path):
    mod = _load_scan_module(monkeypatch)
    pending = tmp_path / "pending.json"
    pending.write_text(
        json.dumps(
            {
                "cases": {
                    "done-message": {"message_id": "done-message", "status": "pending_dry_run"},
                    "new-message": {"message_id": "new-message", "status": "pending_dry_run"},
                }
            }
        ),
        encoding="utf-8",
    )

    class FakeMonitor:
        def _laf_message_already_processed(self, message_id, _check):
            return message_id == "done-message"

    report = mod._reconcile_pending_report(pending, FakeMonitor(), lambda _mid: False)

    assert report["pending_count"] == 1
    assert set(report["cases"]) == {"new-message"}


def test_schedule_provider_runs_real_willingness_route_without_creating_case(
    monkeypatch, tmp_path
):
    from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import (
        LAFOrchestrator as RealLAFOrchestrator,
    )
    mod = _load_scan_module(monkeypatch)

    provider = tmp_path / "gmail-provider.json"
    provider.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "message_id": "willingness-1",
                        "subject": "接案意願徵詢－消費者債務清理事件",
                        "notification_type": "接案意願徵詢",
                        "laf_case_number": "1150709-T-051",
                        "client_name": "",
                        "sender": "fixture@example.invalid",
                        "snippet": "請回覆是否願意接案",
                        "body": "本信僅為意願徵詢，不是正式派案通知。",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    durable = _FakeDurableDB()
    config = tmp_path / "laf-config.json"
    config_bytes = b"{}\n"
    config.write_bytes(config_bytes)
    monkeypatch.setenv("MAGI_CONFIG_PATH", str(config))
    monkeypatch.setenv("MAGI_CONFIG_SHA256", hashlib.sha256(config_bytes).hexdigest())

    def real_orchestrator_with_disposable_db(dry_run=False):
        instance = RealLAFOrchestrator(dry_run=dry_run)
        instance._db = durable
        return instance

    monkeypatch.setattr(mod, "LAFOrchestrator", real_orchestrator_with_disposable_db)
    monkeypatch.setenv("MAGI_V3_REALISM_SANDBOX", "1")
    monkeypatch.setenv("MAGI_V3_SCHEDULE_FIXTURE_ROOT", str(tmp_path))
    monkeypatch.setenv("MAGI_LAF_GMAIL_PROVIDER_FIXTURE", str(provider))

    result = mod.run_once(
        SimpleNamespace(
            max_results=80,
            apply=True,
            dry_run=False,
            json_out=str(tmp_path / "state.json"),
            pending_out=str(tmp_path / "pending.json"),
        )
    )

    assert result["ok"] is True
    assert result["seen"] == 1
    assert result["handled"] == 1
    assert result["marked_processed"] == 1
    assert result["cases"][0]["route"] == "willingness"
    assert durable.message_ids == {"willingness-1"}
    transcript = json.loads(
        (tmp_path / "gmail_provider_transcript.json").read_text(encoding="utf-8")
    )
    assert [row["action"] for row in transcript] == [
        "authenticate",
        "check_emails",
        "mark_laf_processed",
        "close",
    ]
