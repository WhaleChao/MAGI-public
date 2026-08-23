"""Regression tests for file-review notification aggregation."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import types
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


MODULE_PATH = Path(__file__).resolve().parent.parent / "skills" / "file-review-orchestrator" / "action.py"


def _load_action_module():
    name = f"file_review_action_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch("builtins.print"), patch.object(sys, "argv", [str(MODULE_PATH)]), patch(
        "api.runtime_paths.get_skill_python", return_value=Path(sys.executable)
    ), patch("api.product_runtime.apply_product_runtime_env", return_value={}):
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module


def _portal_receipt(module, batch: str, count: int) -> dict:
    return module.portal_download_snapshot(
        [
            {
                "status": "downloadable",
                "rowid": f"{batch}-{index}",
                "upddt": "20260818190000",
            }
            for index in range(count)
        ],
        observed_at="2026-08-18T19:20:00+08:00",
    )


def _download_signature_receipt(
    module, *, processed=(), verified_existing=(), mismatch_deferred=()
) -> dict:
    processed_hashes = module.normalize_signature_hashes(processed)
    verified_hashes = module.normalize_signature_hashes(verified_existing)
    handled_hashes = module.normalize_signature_hashes(
        [*processed_hashes, *verified_hashes]
    )
    mismatch_deferred_hashes = module.normalize_signature_hashes(
        mismatch_deferred
    )
    return {
        "portal_download_receipt_schema": module.PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
        "processed_portal_signature_hashes": processed_hashes,
        "processed_portal_signature_set_hash": module.signature_set_hash(
            processed_hashes
        ),
        "verified_existing_portal_signature_hashes": verified_hashes,
        "verified_existing_portal_signature_set_hash": module.signature_set_hash(
            verified_hashes
        ),
        "handled_portal_signature_hashes": handled_hashes,
        "handled_portal_signature_set_hash": module.signature_set_hash(
            handled_hashes
        ),
        "mismatch_deferred_portal_signature_hashes": mismatch_deferred_hashes,
        "mismatch_deferred_portal_signature_set_hash": module.signature_set_hash(
            mismatch_deferred_hashes
        ),
    }


def test_portal_download_receipt_is_alias_stable_and_pii_free():
    from magi_v3.file_review_receipts import (
        canonical_portal_download_signature,
        portal_download_snapshot,
    )

    raw = {
        "rowid": "opaque-row-17",
        "no": "synthetic-row-number",
        "yyidno": "synthetic-private-case",
        "c60yyidno": "synthetic-private-case-public",
        "clnm": "synthetic-private-party",
        "status": "D",
        "applydt": "20260818120000",
        "downlimit": "20260831120000",
        "upddt": "20260818190000",
        "updated_at": "20260818190000",
        "updtime": "20260818190000",
        "limitdt": "20260830120000",
    }
    public_aliases = {
        "status": "downloadable",
        "rowid": "opaque-row-17",
        "no": "synthetic-row-number",
        "case_number": "synthetic-private-case",
        "c60yyidno": "synthetic-private-case-public",
        "party": "synthetic-private-party",
        "status_code": "D",
        "applydt": "20260818120000",
        "deadline": "20260831120000",
        "upddt": "20260818190000",
        "updated_at": "20260818190000",
        "updtime": "20260818190000",
        "limitdt": "20260830120000",
    }

    assert canonical_portal_download_signature(raw) == canonical_portal_download_signature(
        public_aliases
    )
    changed_batch = {**public_aliases, "upddt": "20260818193000"}
    assert canonical_portal_download_signature(
        changed_batch
    ) != canonical_portal_download_signature(public_aliases)
    receipt = portal_download_snapshot(
        [public_aliases], observed_at="2026-08-18T19:20:00+08:00"
    )
    serialized = json.dumps(receipt, ensure_ascii=False)
    assert "synthetic-private-case" not in serialized
    assert "synthetic-private-party" not in serialized
    assert "synthetic-private-case-public" not in serialized
    assert "synthetic-row-number" not in serialized
    assert "opaque-row-17" not in serialized
    assert len(receipt["portal_download_signature_hashes"]) == 1


@pytest.mark.parametrize(
    ("raw_field", "public_field", "raw_value", "public_value"),
    [
        ("downlimit", "deadline", "20260831120000", "20260831120000"),
        ("dlmdate", "deadline", "20260831120000", "20260831120000"),
        ("payedate", "deadline", "20260831120000", "20260831120000"),
        ("paylimitdt", "pay_deadline", "20260830120000", "20260830120000"),
        ("limitdt", "pay_deadline", "20260830120000", "20260830120000"),
        ("upddt", "updated_at", "20260818190000", "20260818190000"),
        ("updated_at", "updated_at", "20260818190000", "20260818190000"),
        ("updtime", "updated_at", "20260818190000", "20260818190000"),
        ("result", "result_text", "synthetic result", "synthetic result"),
        ("p_status", "p_status", "pending", "PENDING"),
        ("payment", "payment_flag", "y", "Y"),
        ("isdown", "isdown", "n", "N"),
    ],
)
def test_portal_signature_raw_public_aliases_match_when_each_alias_is_singular(
    raw_field, public_field, raw_value, public_value
):
    from magi_v3.file_review_receipts import canonical_portal_download_signature

    raw = {
        "rowid": "opaque-row-alias",
        "status": "D",
        "applydt": "20260818120000",
        raw_field: raw_value,
    }
    public = {
        "rowid": "opaque-row-alias",
        "status_code": "D",
        "applydt": "20260818120000",
        public_field: public_value,
    }
    assert canonical_portal_download_signature(raw)
    assert canonical_portal_download_signature(raw) == canonical_portal_download_signature(public)


def test_portal_signature_uses_stable_result_before_renderer_specific_row_text():
    from magi_v3.file_review_receipts import canonical_portal_download_signature

    raw = {
        "rowid": "opaque-rendered-row",
        "status": "3",
        "applydt": "20260818120000",
        "result": "synthetic stable court result",
        "row_text": "raw table renderer synthetic stable court result",
    }
    public = {
        "rowid": "opaque-rendered-row",
        "status_code": "3",
        "applydt": "20260818120000",
        "result_text": "synthetic stable court result",
        "row_text": "public probe renderer | synthetic stable court result | download",
    }

    signature = canonical_portal_download_signature(raw)
    assert signature
    assert signature == canonical_portal_download_signature(public)
    assert signature != canonical_portal_download_signature(
        {**public, "result_text": "synthetic changed court result"}
    )


def test_portal_signature_falls_back_to_row_text_only_without_result():
    from magi_v3.file_review_receipts import canonical_portal_download_signature

    row = {
        "rowid": "opaque-row-text-fallback",
        "status_code": "3",
        "applydt": "20260818120000",
        "row_text": "synthetic fallback rendering",
    }
    signature = canonical_portal_download_signature(row)

    assert signature
    assert signature == canonical_portal_download_signature(
        {**row, "result_text": "", "result": ""}
    )
    assert signature != canonical_portal_download_signature(
        {**row, "row_text": "synthetic changed fallback rendering"}
    )


def test_portal_signature_uses_c60yyidno_as_fallback_identity_without_rowid_or_no():
    from magi_v3.file_review_receipts import canonical_portal_download_signature

    raw = {
        "c60yyidno": "synthetic-c60-identity",
        "applydt": "20260818120000",
    }
    public = {
        "case_number": "synthetic-c60-identity",
        "applydt": "20260818120000",
    }
    assert canonical_portal_download_signature(raw)
    assert canonical_portal_download_signature(raw) == canonical_portal_download_signature(public)


def test_portal_probe_expired_button_is_not_downloadable_but_live_button_is():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewManager,
    )

    row = {"status": "3", "statusnm": "法院回覆同意"}
    assert (
        FileReviewManager._classify_portal_row_status(
            row,
            row_text="下載期限已過 線上下載",
            has_download=True,
        )
        == "expired"
    )
    assert (
        FileReviewManager._classify_portal_row_status(
            row,
            row_text="下載期限：115/12/31 線上下載",
            has_download=True,
        )
        == "downloadable"
    )


def test_simple_mariadb_exposes_fetch_one_compatibility():
    module = _load_action_module()
    db = module._SimpleMariaDB({})
    expected = {"id": 7, "court_name": "臺灣高等法院"}
    db.execute = lambda query, params=None, fetch=None: expected if fetch == "one" else None

    class _Cursor:
        def execute(self, query, params):
            assert "SELECT" in query

        def fetchone(self):
            return expected

    class _Connection:
        closed = False

        def cursor(self, *_args):
            return _Cursor()

        def close(self):
            self.closed = True

    connection = _Connection()
    db.get_connection = lambda: connection

    assert db.fetch_one("SELECT id, court_name FROM cases", ()) == expected
    assert connection.closed is True


def test_sealed_release_defers_payment_queue_binding_until_queue_access(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-synthetic-sealed")
    monkeypatch.setenv("MAGI_ROOT_DIR", str(tmp_path))
    monkeypatch.delenv("MAGI_FILE_REVIEW_STATE_DIR", raising=False)
    monkeypatch.delenv("MAGI_PAYMENT_PROOF_UPLOAD_QUEUE_PATH", raising=False)
    monkeypatch.delenv("MAGI_SHARED_STATE_DIR", raising=False)
    monkeypatch.delenv("MAGI_V3_SHARED_STATE_DIR", raising=False)

    module = _load_action_module()

    from magi_v3.external_inputs import ExternalInputError

    with pytest.raises(ExternalInputError):
        module._payment_proof_queue_read_unlocked()


def test_laf_notifier_fallback_log_uses_mutable_runtime(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.line_notifier import LAFNotifier

    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    notifier = object.__new__(LAFNotifier)
    notifier._log_local("test delivery")

    log_path = tmp_path / "notifications" / "laf_notifications.log"
    assert log_path.is_file()
    assert "test delivery" in log_path.read_text(encoding="utf-8")
    assert not (
        Path(__file__).resolve().parent.parent
        / "casper_ecosystem"
        / "law_firm_orchestrators"
        / "laf_notifications.log"
    ).exists()


def test_scheduled_check_defer_preserves_last_verified_portal_proof(tmp_path, monkeypatch):
    module = _load_action_module()
    state = tmp_path / "file_review_auto_state.json"
    proof = {
        "ok": True,
        "portal_verified": True,
        "portal_raw_row_count": 275,
        "portal_case_count": 213,
    }
    state.write_text(json.dumps({"result": proof}), encoding="utf-8")
    monkeypatch.setenv("MAGI_FILE_REVIEW_AUTO_STATE", str(state))

    module._publish_scheduled_check_state(
        {
            "success": True,
            "status": "deferred",
            "deferred": True,
            "skipped": True,
            "steps": {
                "check_emails": {
                    "success": True,
                    "portal_probe_ok": False,
                    "portal_probe_deferred": True,
                },
                "download_payment_slips": {
                    "success": True,
                    "deferred": True,
                },
                "download": {
                    "success": True,
                    "deferred": True,
                },
            },
        }
    )

    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["result"] == proof
    assert payload["last_observation"]["degraded"] is True
    assert payload["last_observation"]["portal_probe_deferred"] is True
    assert payload["phase"] == "scheduled_check_complete"


def test_payment_proof_portal_busy_is_durably_queued_and_retried(tmp_path, monkeypatch):
    module = _load_action_module()
    queue_path = tmp_path / "payment-proof-upload-queue.json"
    store_dir = tmp_path / "payment-proof-pending-files"
    image_path = tmp_path / "discord-temp.png"
    image_path.write_bytes(b"payment-proof-image")
    monkeypatch.setattr(module, "PAYMENT_PROOF_UPLOAD_QUEUE_PATH", queue_path)
    monkeypatch.setattr(module, "PAYMENT_PROOF_UPLOAD_STORE_DIR", store_dir)

    parsed = {
        "court_code": "TTD",
        "court_name": "臺東簡易庭",
        "year": "114",
        "case_type": "東原簡",
        "case_number": "18",
        "raw_case_id": "114.東原簡.000018",
        "amount": "200",
    }
    fake_manager = types.SimpleNamespace(
        parse_payment_screenshot=lambda _path: dict(parsed),
        _parse_payment_text=lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(module, "_ensure_imports", lambda: types.SimpleNamespace(FileReviewManager=fake_manager))
    monkeypatch.setattr(
        module,
        "cmd_upload_payment_proof",
        lambda **_kwargs: {
            "success": True,
            "status": "deferred",
            "deferred": True,
            "reason": "file_review_portal_busy",
        },
    )

    result = module.cmd_upload_payment_proof_from_image(str(image_path), notify=False)
    assert result["success"] is True
    assert result["queued"] is True
    assert "不需要重新上傳" in result["message"]
    snapshot = json.loads(queue_path.read_text(encoding="utf-8"))
    assert list(snapshot["jobs"]) == [result["job_id"]]
    stored_path = Path(snapshot["jobs"][result["job_id"]]["file_path"])
    assert stored_path.read_bytes() == b"payment-proof-image"

    # Simulate Discord cleaning its temporary attachment and a later worker run.
    image_path.unlink()
    notices = []
    monkeypatch.setattr(module, "_notify", lambda text, *_args, **_kwargs: notices.append(text))
    monkeypatch.setattr(
        module,
        "cmd_upload_payment_proof",
        lambda **_kwargs: {
            "success": True,
            "result": "Uploaded",
            "proof_receipt_committed": True,
            "message": "✅ 繳費憑證已上傳 — TTD 114年東原簡字第18號",
        },
    )
    monkeypatch.setattr(module.time, "time", lambda: 9_999_999_999.0)
    drained = module.cmd_process_payment_proof_queue(notify=True, max_items=3)

    assert drained["success"] is True
    assert drained["processed_count"] == 1
    assert drained["pending_count"] == 0
    assert json.loads(queue_path.read_text(encoding="utf-8"))["jobs"] == {}
    assert not stored_path.exists()
    assert notices == ["✅ 繳費憑證已上傳 — TTD 114年東原簡字第18號"]


def test_payment_proof_queue_deduplicates_same_file_and_case(tmp_path, monkeypatch):
    module = _load_action_module()
    monkeypatch.setattr(module, "PAYMENT_PROOF_UPLOAD_QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(module, "PAYMENT_PROOF_UPLOAD_STORE_DIR", tmp_path / "store")
    image = tmp_path / "same.png"
    image.write_bytes(b"same")
    info = {
        "court_code": "TPD",
        "year": "115",
        "case_type": "訴",
        "case_number": "123",
        "raw_case_id": "115.訴.000123",
    }

    first = module._enqueue_payment_proof_upload(image_path=str(image), info=info)
    second = module._enqueue_payment_proof_upload(image_path=str(image), info=info)

    assert first["job_id"] == second["job_id"]
    assert len(module._payment_proof_queue_snapshot()["jobs"]) == 1


def test_payment_proof_dedup_rejects_legacy_case_only_and_new_sha(tmp_path):
    module = _load_action_module()
    case_id = "synthetic.case"
    sha_a = "a" * 64
    sha_b = "b" * 64

    legacy = {case_id: {"uploaded_at": "2026-01-01T00:00:00"}}
    assert module._payment_proof_registry_matches(legacy, case_id, sha_a) is False

    current = {
        case_id: {
            "proof_schema": module.PAYMENT_PROOF_SCHEMA,
            "file_sha256": sha_a,
            "payment_event_id": "event-a",
        }
    }
    assert module._payment_proof_registry_matches(current, case_id, sha_a, "event-a") is True
    assert module._payment_proof_registry_matches(current, case_id, sha_b, "event-a") is False
    assert module._payment_proof_registry_matches(current, case_id, sha_a, "event-b") is False


def test_payment_proof_registry_upsert_preserves_multiple_occurrences():
    module = _load_action_module()
    registry = {}
    module._payment_proof_registry_upsert(
        registry,
        "synthetic.case",
        {"proof_schema": module.PAYMENT_PROOF_SCHEMA, "file_sha256": "a" * 64},
    )
    module._payment_proof_registry_upsert(
        registry,
        "synthetic.case",
        {"proof_schema": module.PAYMENT_PROOF_SCHEMA, "file_sha256": "b" * 64},
    )
    assert len(registry["synthetic.case"]["proofs"]) == 2
    assert module._payment_proof_registry_matches(registry, "synthetic.case", "a" * 64)
    assert module._payment_proof_registry_matches(registry, "synthetic.case", "b" * 64)


def test_payment_proof_queue_keeps_nonterminal_success(tmp_path, monkeypatch):
    module = _load_action_module()
    queue_path = tmp_path / "queue.json"
    store_dir = tmp_path / "store"
    stored = store_dir / "proof.png"
    stored.parent.mkdir()
    stored.write_bytes(b"proof")
    queue_path.write_text(
        json.dumps({"version": 1, "jobs": {"job": {
            "job_id": "job", "status": "pending", "attempts": 0,
            "next_attempt_at": 0, "file_path": str(stored),
            "file_sha256": module._payment_proof_file_sha256(str(stored)),
            "court_code": "TPD", "year": "115", "case_type": "訴",
            "case_number": "1", "client_name": "",
        }}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "PAYMENT_PROOF_UPLOAD_QUEUE_PATH", queue_path)
    monkeypatch.setattr(module, "cmd_upload_payment_proof", lambda **_kwargs: {
        "success": True, "result": "Skipped", "message": "legacy synthetic result",
    })

    result = module.cmd_process_payment_proof_queue(notify=False, max_items=1)
    saved = json.loads(queue_path.read_text(encoding="utf-8"))
    assert result["pending_count"] == 1
    assert saved["jobs"]["job"]["attempts"] == 1
    assert stored.exists()


def test_payment_proof_job_identity_includes_event_and_pay_id():
    module = _load_action_module()
    assert module._payment_proof_event_identity({"pay_id": "pay-a"}) == "pay-a"
    assert module._payment_proof_dedup_key("case", "a" * 64, "pay-a") != module._payment_proof_dedup_key("case", "a" * 64, "pay-b")


def test_payment_proof_queue_keeps_same_sha_different_events_separate(tmp_path, monkeypatch):
    module = _load_action_module()
    monkeypatch.setattr(module, "PAYMENT_PROOF_UPLOAD_QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(module, "PAYMENT_PROOF_UPLOAD_STORE_DIR", tmp_path / "store")
    image = tmp_path / "same.png"
    image.write_bytes(b"same-proof")
    base = {"court_code": "TPD", "year": "115", "case_type": "訴", "case_number": "1", "raw_case_id": "115.訴.000001"}
    first = module._enqueue_payment_proof_upload(image_path=str(image), info={**base, "pay_id": "pay-a"})
    second = module._enqueue_payment_proof_upload(image_path=str(image), info={**base, "pay_id": "pay-b"})
    again = module._enqueue_payment_proof_upload(image_path=str(image), info={**base, "pay_id": "pay-a"})
    assert first["job_id"] != second["job_id"]
    assert first["job_id"] == again["job_id"]
    assert len(module._payment_proof_queue_snapshot()["jobs"]) == 2


def test_payment_proof_atomic_registry_write_and_event_tokens(tmp_path, monkeypatch):
    module = _load_action_module()
    registry_path = tmp_path / "payment_proof_registry.json"
    module._write_payment_proof_registry_atomic(
        str(registry_path),
        {"synthetic.case": {"proof_schema": module.PAYMENT_PROOF_SCHEMA, "file_sha256": "a" * 64, "payment_event_id": "pay-a"}},
    )
    assert json.loads(registry_path.read_text(encoding="utf-8"))["synthetic.case"]["payment_event_id"] == "pay-a"
    assert not list(tmp_path.glob("*.tmp"))
    monkeypatch.setattr(module, "get_payment_proof_registry_path", lambda _folder: registry_path)
    tokens = module._load_payment_proof_case_tokens(str(tmp_path))
    assert tokens == {"syntheticcase|pay-a"}
    assert module._payment_proof_event_uploaded("synthetic.case", str(tmp_path), "pay-a")
    assert not module._payment_proof_event_uploaded("other.case", str(tmp_path), "pay-a")
    assert module._portal_item_has_uploaded_proof({"case_number": "synthetic.case", "pay_id": "pay-a"}, tokens)
    assert not module._portal_item_has_uploaded_proof({"case_number": "synthetic.case", "pay_id": "pay-b"}, tokens)
    assert not module._portal_item_has_uploaded_proof({"case_number": "other.case", "pay_id": "pay-a"}, tokens)
    assert not module._portal_item_has_uploaded_proof({"pay_id": "pay-a"}, tokens)


def test_payment_proof_command_legacy_and_event_aware_dedup(tmp_path, monkeypatch):
    module = _load_action_module()
    registry_path = tmp_path / "payment_proof_registry.json"
    registry_path.write_text(json.dumps({"115.訴.000001": {"uploaded_at": "legacy"}}), encoding="utf-8")
    image = tmp_path / "proof.png"
    image.write_bytes(b"new-proof")
    calls = []

    class FakeManager:
        def __init__(self, **_kwargs): pass
        def login(self): return True
        def navigate_to_file_review(self): pass
        def upload_payment_proof(self, case_info, _path):
            calls.append(dict(case_info))
            return "Uploaded"
        def close(self): pass

    class Lock:
        acquired = True
        def release(self): pass

    monkeypatch.setattr(module, "_acquire_file_review_portal_lock", lambda _owner: Lock())
    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(module, "_get_credentials", lambda _cfg: {"username": "u", "password": "p", "download_folder": str(tmp_path)})
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(module, "_ensure_imports", lambda: types.SimpleNamespace(FileReviewManager=FakeManager))
    monkeypatch.setattr(module, "_resolve_court_code", lambda value: value)
    monkeypatch.setattr(module, "get_payment_proof_registry_path", lambda _folder: registry_path)
    monkeypatch.setattr(module, "_notify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys.modules.setdefault("skills.ops.dedup_db", types.SimpleNamespace()), "is_done", lambda *_args, **_kwargs: False, raising=False)
    monkeypatch.setattr(sys.modules["skills.ops.dedup_db"], "mark_done", lambda *_args, **_kwargs: True, raising=False)

    first = module.cmd_upload_payment_proof("TPD", "115", "訴", "1", file_path=str(image), notify=False, payment_event_id="pay-a")
    second = module.cmd_upload_payment_proof("TPD", "115", "訴", "1", file_path=str(image), notify=False, payment_event_id="pay-b")
    third = module.cmd_upload_payment_proof("TPD", "115", "訴", "1", file_path=str(image), notify=False, payment_event_id="pay-b")
    assert first["result"] == "Uploaded" and first["proof_receipt_committed"] is True
    assert second["result"] == "Uploaded"
    assert third["result"] == "exact_duplicate_verified"
    assert [item["payment_event_id"] for item in calls] == ["pay-a", "pay-b"]

    monkeypatch.setattr(module, "_write_payment_proof_registry_atomic", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic registry failure")))
    failed = module.cmd_upload_payment_proof("TPD", "115", "訴", "1", file_path=str(image), notify=False, payment_event_id="pay-c")
    assert failed["success"] is False
    assert failed["result"] == "registry_not_committed"
    assert failed["proof_receipt_committed"] is False

    queue_path = tmp_path / "queue-after-registry-failure.json"
    queue_file = tmp_path / "queued-proof.png"
    queue_file.write_bytes(b"queued-proof")
    queue_path.write_text(json.dumps({"version": 1, "jobs": {"job": {
        "job_id": "job", "status": "pending", "attempts": 0, "next_attempt_at": 0,
        "file_path": str(queue_file), "file_sha256": module._payment_proof_file_sha256(str(queue_file)),
    }}}), encoding="utf-8")
    monkeypatch.setattr(module, "PAYMENT_PROOF_UPLOAD_QUEUE_PATH", queue_path)
    monkeypatch.setattr(module, "cmd_upload_payment_proof", lambda **_kwargs: failed)
    drained = module.cmd_process_payment_proof_queue(notify=False, max_items=1)
    assert drained["pending_count"] == 1
    assert queue_file.exists()


def test_payment_proof_registry_commit_failure_is_nonterminal(tmp_path, monkeypatch):
    module = _load_action_module()
    queue_path = tmp_path / "queue.json"
    stored = tmp_path / "stored.png"
    stored.write_bytes(b"proof")
    queue_path.write_text(json.dumps({"version": 1, "jobs": {"job": {
        "job_id": "job", "status": "pending", "attempts": 0, "next_attempt_at": 0,
        "file_path": str(stored), "file_sha256": module._payment_proof_file_sha256(str(stored)),
    }}}), encoding="utf-8")
    monkeypatch.setattr(module, "PAYMENT_PROOF_UPLOAD_QUEUE_PATH", queue_path)
    monkeypatch.setattr(module, "cmd_upload_payment_proof", lambda **_kwargs: {
        "success": False, "result": "registry_not_committed", "deferred": True,
        "reason": "payment_proof_registry_write_failed", "proof_receipt_committed": False,
    })
    result = module.cmd_process_payment_proof_queue(notify=False, max_items=1)
    assert result["pending_count"] == 1
    assert stored.exists()


def test_payment_proof_batch_uses_event_aware_registry(tmp_path, monkeypatch):
    module = _load_action_module()
    image = tmp_path / "截圖.png"
    image.write_bytes(b"batch-proof")
    registry_path = tmp_path / "payment_proof_registry.json"
    registry_path.write_text(json.dumps({"115.訴.000001": {"uploaded_at": "legacy"}}), encoding="utf-8")
    calls = []

    parsed = {
        "court_code": "TPD", "court_name": "synthetic", "year": "115",
        "case_type": "訴", "case_number": "1", "raw_case_id": "115.訴.000001",
        "pay_id": "pay-batch", "amount": "100",
    }

    class FakeManager:
        @staticmethod
        def parse_payment_screenshot(_path): return dict(parsed)
        def __init__(self, **_kwargs): pass
        def login(self): return True
        def navigate_to_file_review(self): pass
        def upload_payment_proof(self, case_info, _path): calls.append(dict(case_info)); return "Uploaded"
        def close(self): pass

    class Lock:
        acquired = True
        def release(self): pass

    monkeypatch.setattr(module, "_acquire_file_review_portal_lock", lambda _owner: Lock())
    monkeypatch.setattr(module, "_ensure_imports", lambda: types.SimpleNamespace(FileReviewManager=FakeManager))
    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(module, "_get_credentials", lambda _cfg: {"username": "u", "password": "p", "download_folder": str(tmp_path)})
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(module, "get_payment_proof_registry_path", lambda _folder: registry_path)
    monkeypatch.setattr(module, "_notify", lambda *_args, **_kwargs: None)
    result = module.cmd_upload_payment_proofs_batch(str(tmp_path), notify=False)
    assert result["success"] is True
    assert calls[0]["payment_event_id"] == "pay-batch"
    saved = json.loads(registry_path.read_text(encoding="utf-8"))["115.訴.000001"]
    assert saved["proof_schema"] == module.PAYMENT_PROOF_SCHEMA
    assert saved["file_sha256"] == module._payment_proof_file_sha256(str(image))


def test_payment_proof_portal_event_matching_is_exact_and_fail_closed():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager
    assert FileReviewManager._payment_proof_event_matches_row({"rowid": "r-a", "p_payid": "p-a"}, "p-a")
    assert not FileReviewManager._payment_proof_event_matches_row({"rowid": "r-a", "p_payid": "p-a"}, "p-b")
    assert FileReviewManager._payment_proof_event_matches_row({"rowid": "r-a"}, "")
    assert not FileReviewManager._payment_proof_event_matches_row({}, "p-a")


def test_ready_to_download_items_expose_case_identity_without_internal_paths():
    module = _load_action_module()
    info = types.SimpleNamespace(
        court_case_no="115年度訴字第1號",
        laf_case_no="1150001-A-001",
        application_no="A-9",
        client_name="王小明",
        court="臺灣花蓮地方法院",
    )

    items = module._ready_to_download_items(types.SimpleNamespace(ready_to_download=[info]))

    assert items == [
        {
            "court_case_no": "115年度訴字第1號",
            "laf_case_no": "1150001-A-001",
            "application_no": "A-9",
            "client_name": "王小明",
            "court": "臺灣花蓮地方法院",
        }
    ]


def test_recent_download_activity_ignores_exists_skip(tmp_path):
    module = _load_action_module()
    job_dir = tmp_path / "_bg_jobs"
    job_dir.mkdir()

    skip_job = {
        "success": True,
        "finished_at": datetime.now().isoformat(),
        "result": {
            "items": [
                {
                    "party": "張裕和",
                    "court_case_no": "114.易.000321",
                    "file": "ebook_ROW003.zip",
                    "action": "exists_skip",
                }
            ]
        },
    }
    copied_job = {
        "success": True,
        "finished_at": datetime.now().isoformat(),
        "result": {
            "items": [
                {
                    "party": "[當事人H]",
                    "court_case_no": "115.原金訴.000044",
                    "file": "卷宗A.pdf",
                    "dst": "/tmp/卷宗A.pdf",
                    "action": "copied",
                },
                {
                    "party": "[當事人H]",
                    "court_case_no": "115.原金訴.000044",
                    "file": "卷宗B.pdf",
                    "dst": "/tmp/卷宗B.pdf",
                    "action": "copied",
                },
            ]
        },
    }

    (job_dir / "download_skip.json").write_text(json.dumps(skip_job, ensure_ascii=False), encoding="utf-8")
    (job_dir / "download_copy.json").write_text(json.dumps(copied_job, ensure_ascii=False), encoding="utf-8")

    with patch.object(module, "BG_JOB_DIR", str(job_dir)):
        records = module._load_recent_download_activity(days=7)

    assert len(records) == 1
    assert records[0]["party"] == "[當事人H]"
    assert records[0]["case_number"] == "115.原金訴.000044"
    assert records[0]["detail"] == "已下載卷宗（2 份）"


def test_recent_activity_backlog_is_seeded_then_only_new_items_surface(tmp_path, monkeypatch):
    module = _load_action_module()
    seen_dedup = set()
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(
            is_done=lambda _category, key: key in seen_dedup,
            mark_done=lambda _category, key, metadata=None: seen_dedup.add(key),
        ),
    )
    download_folder = str(tmp_path)
    base_record = {
        "processed_at": datetime.now() - timedelta(minutes=30),
        "party": "張裕和",
        "case_number": "114.易.000321",
        "detail": "已下載卷宗（3 份）",
        "count": 3,
        "source": "download_job",
        "artifact_type": "review_download",
        "key": "download_20260320_023957_577560.json",
    }

    first = module._filter_unnotified_recent_activity(
        [base_record], download_folder, "recent_review_download_activity"
    )
    assert first == []

    second = module._filter_unnotified_recent_activity(
        [base_record], download_folder, "recent_review_download_activity"
    )
    assert second == []

    new_record = dict(base_record)
    new_record["processed_at"] = datetime.now()
    new_record["detail"] = "已下載卷宗（1 份）"
    new_record["count"] = 1
    new_record["key"] = "download_20260320_120000_test.json"

    fresh = module._filter_unnotified_recent_activity(
        [new_record], download_folder, "recent_review_download_activity"
    )
    assert len(fresh) == 1
    assert fresh[0]["detail"] == "已下載卷宗（1 份）"

    module._mark_recent_activity_notified(
        fresh, download_folder, "recent_review_download_activity"
    )
    after_mark = module._filter_unnotified_recent_activity(
        [new_record], download_folder, "recent_review_download_activity"
    )
    assert after_mark == []


def test_portal_probe_error_is_business_readable():
    module = _load_action_module()

    text = module._format_portal_probe_error(
        {
            "error": "list_page_verification_failed",
            "error_detail": {
                "page_check": {
                    "has_list_markers": False,
                    "has_table": False,
                    "tr_count": 0,
                    "body_preview": "",
                },
                "frame_diagnostics": [
                    {
                        "frame_name": "",
                        "frame_url": "https://ola.judicial.gov.tw/",
                        "body_preview": "會員登入 驗證碼 密碼",
                    }
                ],
            },
        }
    )

    assert "入口列表沒有正確載入" in text
    assert "會員登入 驗證碼 密碼" in text
    assert "{" not in text
    assert "frame_diagnostics" not in text


def test_portal_item_display_party_uses_db_case_folder_for_typos():
    module = _load_action_module()

    class FakeDb:
        def execute(self, query, params=None, fetch=None):
            if params and params[0] == "115年度原訴字第000036號":
                return {
                    "case_number": "2026-0034",
                    "court_case_number": "115年度原訴字第000036號",
                    "client_name": "陳文眀",
                    "folder_path": "/案件/法扶案件/刑事/2026-0034-陳文明-一審-傷害",
                }
            return None

    party = module._display_party_for_case_item(
        {
            "party": "陳文眀",
            "court_case_no": "115年度原訴字第000036號",
        },
        db=FakeDb(),
        cache={},
    )

    assert party == "陳文明"


def test_recent_activity_block_uses_folder_name_for_display_typos():
    module = _load_action_module()

    lines = module._format_recent_activity_block(
        "最近卷宗下載",
        [
            {
                "processed_at": datetime.now(),
                "party": "李秀瑛",
                "case_number": "115年度勞簡字第1號",
                "folder_path": "/案件/法扶案件/行政/2026-0045-李秀英-一審-勞工保險爭議/09_閱卷/卷宗.pdf",
                "detail": "已下載卷宗（1 份）",
            }
        ],
    )

    rendered = "\n".join(lines)
    assert "李秀英｜115年度勞簡字第1號" in rendered
    assert "李秀瑛" not in rendered


def test_ola_error_page_is_labeled_and_treated_as_transient():
    module = _load_action_module()

    result = {
        "error": "navigate_failed",
        "error_code": "ola_error_page",
        "error_detail": "https://ola.judicial.gov.tw/judrf/lssologinchk.htm",
    }

    text = module._format_portal_probe_error(result)

    assert "法院入口回傳錯誤頁" in text
    assert module._is_transient_portal_probe_failure(result) is True


def test_chrome_error_document_is_retried_and_missing_menu_is_transient():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewManager,
    )

    module = _load_action_module()

    assert FileReviewManager._looks_like_ola_error_page(
        "", "", "chrome-error://chromewebdata/"
    )
    assert module._is_transient_portal_probe_failure(
        {
            "error": "navigate_failed",
            "error_code": "review_menu_not_found",
        }
    )


def test_portal_probe_failure_alerts_only_after_streak(tmp_path, monkeypatch):
    module = _load_action_module()

    monkeypatch.setenv("MAGI_FILE_REVIEW_PORTAL_FAILURE_NOTIFY_STREAK", "3")
    result = {
        "success": False,
        "error": "navigate_failed",
        "error_code": "ola_error_page",
    }

    first = module._record_portal_probe_state(str(tmp_path), result)
    second = module._record_portal_probe_state(str(tmp_path), result)
    third = module._record_portal_probe_state(str(tmp_path), result)

    assert first["failure_streak"] == 1
    assert first["should_alert"] is False
    assert second["failure_streak"] == 2
    assert second["should_alert"] is False
    assert third["failure_streak"] == 3
    assert third["should_alert"] is True

    cleared = module._record_portal_probe_state(str(tmp_path), {"success": True})
    assert cleared["failure_streak"] == 0
    assert not (tmp_path / ".portal_probe_failure_state.json").exists()


def test_court_pickup_portal_row_does_not_become_pending_payment(tmp_path):
    module = _load_action_module()
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "result_text": "鑫源企業社請至本院閱覽紙本卷宗，不另製發繳費單。",
        "party": "鑫源企業社",
        "court_case_no": "115年度聲字第123號",
        "rowid": "CP001",
        "applydt": _roc_compact(-1),
    }

    assert module._portal_item_is_court_pickup_ready(item) is True
    assert module._portal_item_is_actionable_pending(item) is False

    collapsed = module._collapse_portal_items([item], download_folder=str(tmp_path))

    assert collapsed["court_pickup_count"] == 1
    assert collapsed["court_pickup_history_count"] == 0
    assert collapsed["pending_payment_count"] == 0
    assert collapsed["items"][0]["status"] == "court_pickup"


def test_old_portal_court_pickup_rows_are_history_not_notifications(tmp_path):
    module = _load_action_module()
    item = {
        "status": "court_pickup",
        "status_name": "法院回覆同意",
        "result_text": "請至本院閱覽紙本卷宗，不另製發繳費單。",
        "party": "王有烈等",
        "court_case_no": "108年度基簡字第000686號",
        "rowid": "CP-OLD",
        "applydt": _roc_compact(-90),
    }

    collapsed = module._collapse_portal_items([item], download_folder=str(tmp_path))

    assert collapsed["court_pickup_count"] == 0
    assert collapsed["court_pickup_history_count"] == 1
    assert collapsed["count"] == 0
    assert collapsed["items"] == []


def test_completed_court_pickup_text_is_not_actionable(tmp_path):
    module = _load_action_module()
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "result_text": "已到院閱卷",
        "party": "李家榛",
        "court_case_no": "108年度基簡字第000883號",
        "rowid": "CP-DONE",
        "applydt": _roc_compact(-1),
    }

    assert module._portal_item_is_court_pickup_ready(item) is False
    assert module._portal_item_is_actionable_pending(item) is False


def test_downloaded_registry_does_not_suppress_downloadable_case_by_default(tmp_path, monkeypatch):
    module = _load_action_module()
    (tmp_path / "downloaded_registry.json").write_text(
        json.dumps({
            "卷宗_劉信義.pdf": {
                "yyidno": "115.原侵重訴.000001",
                "case_info": {
                    "artifact_type": "review_download",
                    "showyyidno": "115年度原侵重訴字第000001號",
                    "case_number": "115.原侵重訴.000001",
                },
            },
            "繳費單_劉信義.pdf": {
                "yyidno": "115.原侵重訴.000001",
                "case_info": {"artifact_type": "payment_slip"},
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    item = {
        "status": "downloadable",
        "party": "劉信義",
        "case_number": "115年度原侵重訴字第000001號",
        "rowid": "DL001",
    }

    monkeypatch.delenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", raising=False)

    assert module._filter_not_yet_downloaded([item], str(tmp_path)) == [item]

    monkeypatch.setenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "1")
    assert module._filter_not_yet_downloaded([item], str(tmp_path)) == []


def test_recent_verified_row_is_suppressed_even_without_portal_download_date(
    tmp_path, monkeypatch
):
    module = _load_action_module()
    row = {
        "status": "downloadable",
        "case_number": "114年度原上訴字第160號",
        "rowid": "1117279",
        "isdown": "",
        "downdt": "",
        "upddt": "",
    }
    fields = (
        "rowid", "no", "yyidno", "showyyidno", "c60yyidno", "isdown",
        "downdt", "upddt", "updated_at", "updtime", "limitdt", "paylimitdt",
    )
    signature = "|".join(
        f"{name}={str(row.get(name) or '').strip()}" for name in fields
    )
    (tmp_path / "clicked_rowids.json").write_text(
        json.dumps(
            {
                "1117279": {
                    "first_clicked": datetime.now().isoformat(),
                    "last_clicked": datetime.now().isoformat(),
                    "row_signature": signature,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP", "1")

    assert module._filter_not_yet_downloaded([row], str(tmp_path)) == []

    changed = dict(row, upddt="2026/08/13 04:00")
    assert module._filter_not_yet_downloaded([changed], str(tmp_path)) == [changed]


def test_portal_downloadable_review_folder_archive_skip_is_legacy_opt_in(tmp_path, monkeypatch):
    module = _load_action_module()

    class FakeManager:
        def _case_review_folder_has_files(self, case_info):
            return case_info.get("party") == "蘇建和"

    item = {
        "status": "downloadable",
        "party": "蘇建和",
        "case_number": "114年度重上更二字第000095號",
        "rowid": "DL002",
    }

    monkeypatch.delenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", raising=False)
    collapsed = module._collapse_portal_items(
        [item],
        download_folder=str(tmp_path),
        file_review_manager=FakeManager(),
    )

    assert collapsed["downloadable_raw_count"] == 1
    assert collapsed["downloadable_skipped_count"] == 0
    assert collapsed["downloadable_count"] == 1
    assert collapsed["items"] == [item]

    monkeypatch.setenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "1")
    collapsed = module._collapse_portal_items(
        [item],
        download_folder=str(tmp_path),
        file_review_manager=FakeManager(),
    )

    assert collapsed["downloadable_skipped_count"] == 1
    assert collapsed["downloadable_count"] == 0
    assert collapsed["items"] == []


def test_portal_pending_payment_skips_when_review_files_already_archived(tmp_path):
    module = _load_action_module()
    item = {
        "status": "pending_payment",
        "paystatus": "0",
        "status_name": "法院回覆同意",
        "result_text": "請於【115/05/11 上午】以後至法院領取",
        "row_text": "待繳費，請於法院回覆結果下載繳費單繳費",
        "party": "鑫源企業社",
        "court_case_no": "114年度建字第000016號",
        "case_number": "114.建.000016",
        "rowid": "1059435",
        "payid": "31001961342172",
        "archived_review_files": True,
    }

    collapsed = module._collapse_portal_items([item], download_folder=str(tmp_path))

    assert collapsed["pending_payment_count"] == 0
    assert collapsed["count"] == 0
    assert collapsed["items"] == []


def test_portal_pending_payment_uses_manager_archive_lookup_fields(tmp_path):
    module = _load_action_module()
    seen = {}

    class FakeManager:
        def _case_review_folder_has_files(self, case_info):
            seen.update(case_info)
            return (
                case_info.get("showyyidno") == "114年度建字第000016號"
                and case_info.get("clnm") == "鑫源企業社"
            )

    item = {
        "status": "pending_payment",
        "paystatus": "0",
        "status_name": "法院回覆同意",
        "row_text": "待繳費，請於法院回覆結果下載繳費單繳費",
        "party": "鑫源企業社",
        "court_case_no": "114年度建字第000016號",
        "case_number": "114.建.000016",
        "rowid": "1059435",
        "applydt": _roc_compact(-1),
    }

    collapsed = module._collapse_portal_items(
        [item],
        download_folder=str(tmp_path),
        file_review_manager=FakeManager(),
    )

    assert seen["showyyidno"] == "114年度建字第000016號"
    assert seen["clnm"] == "鑫源企業社"
    assert seen["yyidno"] == "114.建.000016"
    assert collapsed["pending_payment_count"] == 0
    assert collapsed["items"] == []


def test_portal_pending_payment_retries_when_registry_file_is_missing(tmp_path):
    module = _load_action_module()
    (tmp_path / "payment_registry.json").write_text(
        json.dumps(
            {
                "case:115原交易21:林建豐": {
                    "case_number": "115.原交易.000021",
                    "yyidno": "115.原交易.000021",
                    "party": "林建豐",
                    "files": ["繳費單_林建豐_115.原交易.000021.pdf"],
                    "processed_at": "2026-06-09T14:56:58",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "row_text": "待繳費，請於法院回覆結果下載繳費單繳費",
        "party": "林建豐",
        "court_case_no": "115年度原交易字第000021號",
        "case_number": "115.原交易.000021",
        "rowid": "1075000",
        "applydt": _roc_compact(-1),
    }

    collapsed = module._collapse_portal_items([item], download_folder=str(tmp_path))

    assert collapsed["pending_payment_count"] == 1
    assert collapsed["count"] == 1
    assert collapsed["items"] == [item]


def test_file_review_manager_court_pickup_row_is_not_pending_payment():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    row_json = {
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "鑫源企業社請至本院閱覽紙本卷宗，不另製發繳費單。",
        "clnm": "鑫源企業社",
        "yyidno": "115聲123",
    }

    assert FileReviewManager._is_court_pickup_row(row_json, "") is True
    assert FileReviewManager._is_pending_payment_row(row_json, "") is False


def test_file_review_manager_pending_payment_wins_over_online_download_marker():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    row_json = {
        "paystatus": "1",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "請將聲請複製電子卷證費用新台幣200元整於待繳費連結繳費後，書記官確認後會將可進行【線上下載】。",
        "clnm": "林建豐",
        "yyidno": "115.原交易.000021",
    }

    assert (
        FileReviewManager._classify_portal_row_status(
            row_json,
            row_text="線上下載",
            has_download=True,
        )
        == "pending_payment"
    )


def test_live_ola_pending_payment_codes_are_not_misread_as_paid(tmp_path):
    module = _load_action_module()
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    row_text = (
        "法院回覆：請將聲請複製電子卷證費用新台幣200元於待繳費連結繳費。 "
        "法院回覆同意 待繳費，請於法院回覆結果下載繳費單繳費"
    )
    row_json = {
        "paystatus": "1",
        "p_status": "",
        "payment": "Y",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "請於待繳費連結繳費",
    }
    item = {
        "status": "pending_payment",
        "paystatus": "1",
        "p_status": "",
        "payment_flag": "Y",
        "status_code": "3",
        "status_name": "法院回覆同意",
        "result_text": row_json["result"],
        "row_text": row_text,
        "case_number": "115.訴.000530",
        "court_case_no": "115年度訴字第000530號",
        "rowid": "1117289",
        "applydt": _roc_compact(-1),
        "pay_deadline": _roc_compact(13),
    }

    assert FileReviewManager._is_pending_payment_row(row_json, row_text) is True
    assert module._portal_item_is_paid(item) is False
    collapsed = module._collapse_portal_items([item], download_folder=str(tmp_path))
    assert collapsed["pending_payment_count"] == 1
    assert collapsed["items"] == [item]


def test_live_ola_paid_row_ignores_historical_pending_instruction(tmp_path):
    module = _load_action_module()
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    row_text = "法院回覆：請於待繳費連結繳費。 法院回覆同意 已繳費"
    row_json = {
        "paystatus": "2",
        "p_status": "Y",
        "payment": "N",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "請於待繳費連結繳費",
    }
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "p_status": "Y",
        "payment_flag": "N",
        "status_code": "3",
        "status_name": "法院回覆同意",
        "result_text": row_json["result"],
        "row_text": row_text,
        "case_number": "114.附民.001289",
        "court_case_no": "114年度附民字第001289號",
        "rowid": "1113881",
        "applydt": _roc_compact(-7),
    }

    assert FileReviewManager._is_pending_payment_row(row_json, row_text) is False
    assert module._portal_item_is_paid(item) is True
    collapsed = module._collapse_portal_items([item], download_folder=str(tmp_path))
    assert collapsed["pending_payment_count"] == 0


def test_portal_payment_scan_chain_keeps_generated_slip_once_and_suppresses_paid(tmp_path):
    module = _load_action_module()
    generated = {
        "status": "pending_payment", "p_status": "Y", "paystatus": "2",
        "status_code": "3", "status_name": "法院回覆同意", "row_text": "待繳費",
        "rowid": "synthetic-slip", "case_number": "115年度訴字第000123號",
        "applydt": _roc_compact(-1), "pay_deadline": _roc_compact(3),
    }
    paid = {**generated, "rowid": "synthetic-paid", "row_text": "已繳費"}

    chain = module._portal_payment_scan_chain([generated, dict(generated)], download_folder=str(tmp_path))
    paid_chain = module._portal_payment_scan_chain([paid], download_folder=str(tmp_path))

    assert chain["probe_candidate_count"] == 2
    assert chain["coverage_candidate_count"] == 1
    assert chain["pending_payment_count"] == 1
    assert chain["notification_queue_count"] == 1
    assert paid_chain["coverage_candidate_count"] == 0
    assert paid_chain["notification_queue_count"] == 0


def test_payment_dedup_is_per_portal_occurrence_not_entire_case(tmp_path):
    module = _load_action_module()
    old_pdf = tmp_path / "繳費單_old.pdf"
    old_pdf.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "payment_registry.json").write_text(
        json.dumps({
            "rowid:OLD-ROW": {
                "rowid": "OLD-ROW",
                "case_number": "115.訴.000530",
                "files": [old_pdf.name],
                "file_paths": [str(old_pdf)],
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    item = {
        "status": "pending_payment",
        "paystatus": "1",
        "payment_flag": "Y",
        "status_name": "法院回覆同意",
        "row_text": "待繳費，請於法院回覆結果下載繳費單繳費",
        "case_number": "115.訴.000530",
        "court_case_no": "115年度訴字第000530號",
        "rowid": "NEW-ROW",
        "applydt": _roc_compact(-1),
        "pay_deadline": _roc_compact(13),
    }

    assert module._is_portal_payment_notice_seen(item, str(tmp_path)) is False
    assert module._collapse_portal_items([item], download_folder=str(tmp_path))["pending_payment_count"] == 1


def test_years_old_ola_pending_history_is_not_actionable(tmp_path):
    module = _load_action_module()
    item = {
        "status": "pending_payment",
        "paystatus": "0",
        "status_name": "法院回覆同意",
        "row_text": "待繳費，請於法院回覆結果下載繳費單繳費",
        "case_number": "108.訴.000496",
        "court_case_no": "108年度訴字第000496號",
        "rowid": "HISTORY-ROW",
        "applydt": "1080628",
    }

    assert module._portal_item_is_actionable_pending(item) is True
    assert module._portal_item_is_recent_payment(item) is False
    assert module._collapse_portal_items([item], download_folder=str(tmp_path))["pending_payment_count"] == 0


def test_general_review_download_payment_print_guard_defaults_off(monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    monkeypatch.delenv("MAGI_FILE_REVIEW_GENERAL_DOWNLOAD_PRINT_PAYMENT_SLIPS", raising=False)

    assert FileReviewManager._allow_payment_slip_print_during_general_download() is False


def test_general_review_download_payment_print_guard_can_be_enabled(monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    monkeypatch.setenv("MAGI_FILE_REVIEW_GENERAL_DOWNLOAD_PRINT_PAYMENT_SLIPS", "true")

    assert FileReviewManager._allow_payment_slip_print_during_general_download() is True


def test_check_and_download_available_guards_general_payment_slip_prints():
    source = (
        Path(__file__).resolve().parents[1]
        / "casper_ecosystem"
        / "law_firm_orchestrators"
        / "file_review_automation.py"
    ).read_text(encoding="utf-8")
    block = source.split("    def check_and_download_available", 1)[1].split(
        "    def _handle_download_popup", 1
    )[0]

    assert "MAGI_FILE_REVIEW_GENERAL_DOWNLOAD_PRINT_PAYMENT_SLIPS" in block
    assert "allow_payment_slip_print = self._allow_payment_slip_print_during_general_download()" in block
    assert block.count("if not allow_payment_slip_print:") >= 2
    assert block.count("一般閱卷下載不自動列印繳費單") >= 2


def test_download_popup_has_bounded_boundary_grace_and_real_control_contract():
    source = (
        Path(__file__).resolve().parents[1]
        / "casper_ecosystem"
        / "law_firm_orchestrators"
        / "file_review_automation.py"
    ).read_text(encoding="utf-8")
    block = source.split("    def _handle_download_popup", 1)[1].split(
        "    def _handle_download_confirmation_dialog", 1
    )[0]

    assert 'MAGI_FILE_REVIEW_POPUP_READY_TIMEOUT_SEC", "180"' in block
    assert 'MAGI_FILE_REVIEW_POPUP_GRACE_TIMEOUT_SEC", "15"' in block
    assert "寬限複核已確認檔案列表完成" in block
    assert "popup_timeout + popup_grace_timeout" in block
    assert "//*[@title='下載']" in block
    assert "//tr[@id='trdata']//button[@title='下載']" in block
    assert "_popup_source_has_download_controls" in block
    assert "內嵌視窗 DOM 備援判定" in block


def test_popup_rendered_data_row_is_ready_even_if_visibility_probe_is_false():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    rendered = """
    <table id="tablecontext"><tbody><tr id="trdata">
      <td>卷證.pdf</td><td><button title="下載"><i class="fa fa-download"></i></button></td>
    </tr></tbody></table>
    """
    header_only = '<button title="單檔批次下載"><i class="fa fa-download"></i></button>'

    assert FileReviewManager._popup_source_has_download_controls(rendered) is True
    assert FileReviewManager._popup_source_has_download_controls(header_only) is False


def test_late_download_detection_uses_pre_move_snapshot_not_server_mtime():
    source = (
        Path(__file__).resolve().parents[1]
        / "casper_ecosystem"
        / "law_firm_orchestrators"
        / "file_review_automation.py"
    ).read_text(encoding="utf-8")
    block = source.split("最後保險：", 1)[1].split("檢測到 {len(candidates)}", 1)[0]

    assert "_late_was_new = self._download_file_changed_since_snapshot" in block
    assert "_late_was_new" in block
    assert "self._is_valid_review_download_artifact(fp)" in block


def test_file_review_download_logs_do_not_serialize_portal_rows_or_case_number():
    source = (
        Path(__file__).resolve().parents[1]
        / "casper_ecosystem"
        / "law_firm_orchestrators"
        / "file_review_automation.py"
    ).read_text(encoding="utf-8")

    assert '取得 Row JSON: {row_json}' not in source
    assert "(補)更新案件資料: {case_info.get('showyyidno')}" not in source
    assert "已取得案件列資料" in source


def test_file_review_manager_court_pickup_wins_over_online_download_marker():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    row_json = {
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "請至本院閱覽紙本卷宗，不另製發繳費單。",
        "clnm": "鑫源企業社",
        "yyidno": "115聲123",
    }

    assert (
        FileReviewManager._classify_portal_row_status(
            row_json,
            row_text="線上下載",
            has_download=True,
        )
        == "court_pickup"
    )


def test_file_review_manager_waiting_or_denied_rows_are_not_court_pickup():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    waiting = {
        "status": "2",
        "statusnm": "待法院回覆",
        "result": "尚未回覆",
    }
    denied = {
        "status": "4",
        "statusnm": "法院回覆不同意",
        "result": "不同意聲請，原因【已到院閱卷】",
    }
    completed = {
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "已到院閱卷",
    }

    assert FileReviewManager._is_court_pickup_row(waiting, "聲請閱卷") is False
    assert FileReviewManager._is_court_pickup_row(denied, "") is False
    assert FileReviewManager._is_court_pickup_row(completed, "") is False


def test_payment_check_notice_stays_quiet_when_portal_has_no_pending_payment():
    module = _load_action_module()

    assert module._should_emit_payment_check_notice(
        pay_hits=7,
        pay_notified=0,
        portal_pending=0,
        portal_pending_changed=True,
        portal_probe_ok=True,
    ) is False


def test_payment_check_notice_emits_only_for_actionable_payment_work():
    module = _load_action_module()

    assert module._should_emit_payment_check_notice(
        pay_hits=0,
        pay_notified=0,
        portal_pending=2,
        portal_pending_changed=True,
        portal_probe_ok=True,
    ) is True
    assert module._should_emit_payment_check_notice(
        pay_hits=1,
        pay_notified=0,
        portal_pending=0,
        portal_pending_changed=False,
        portal_probe_ok=False,
    ) is False
    assert module._should_emit_payment_check_notice(
        pay_hits=0,
        pay_notified=1,
        portal_pending=0,
        portal_pending_changed=False,
        portal_probe_ok=True,
    ) is True


def test_payment_check_notice_stays_quiet_when_portal_probe_is_safely_deferred():
    module = _load_action_module()

    assert module._should_emit_payment_check_notice(
        pay_hits=1,
        pay_notified=0,
        portal_pending=0,
        portal_pending_changed=False,
        portal_probe_ok=False,
        portal_deferred=True,
    ) is False


def test_empty_check_warning_requires_user_visible_warning_text():
    module = _load_action_module()

    assert module._should_emit_empty_check_warning(
        user_visible_warning="",
        notify_empty=True,
    ) is False
    assert module._should_emit_empty_check_warning(
        user_visible_warning="⚠️ Gmail token 需要重新授權",
        notify_empty=True,
    ) is True
    assert module._should_emit_empty_check_warning(
        user_visible_warning="⚠️ Gmail token 需要重新授權",
        notify_empty=False,
    ) is False


def test_review_check_notice_ignores_download_button_until_archived():
    module = _load_action_module()

    assert module._should_emit_review_check_notice(
        download_email_hits=0,
        pickup_email_hits=0,
        ready_to_download_count=0,
        portal_downloadable=1,
        portal_downloadable_changed=True,
        portal_pickup=0,
        portal_pickup_changed=False,
        scan_errors=0,
        portal_failure_alert=False,
    ) is False


def test_review_check_notice_ignores_download_email_until_archived():
    module = _load_action_module()

    assert module._should_emit_review_check_notice(
        download_email_hits=1,
        pickup_email_hits=0,
        ready_to_download_count=1,
        portal_downloadable=0,
        portal_downloadable_changed=False,
        portal_pickup=0,
        portal_pickup_changed=False,
        scan_errors=0,
        portal_failure_alert=False,
    ) is False


def test_review_check_notice_emits_for_pickup_and_health_issues():
    module = _load_action_module()

    assert module._should_emit_review_check_notice(
        download_email_hits=0,
        pickup_email_hits=1,
        ready_to_download_count=0,
        portal_downloadable=0,
        portal_downloadable_changed=False,
        portal_pickup=0,
        portal_pickup_changed=False,
        scan_errors=0,
        portal_failure_alert=False,
    ) is True
    assert module._should_emit_review_check_notice(
        download_email_hits=0,
        pickup_email_hits=0,
        ready_to_download_count=0,
        portal_downloadable=0,
        portal_downloadable_changed=False,
        portal_pickup=0,
        portal_pickup_changed=False,
        scan_errors=1,
        portal_failure_alert=False,
    ) is True


def test_download_notice_email_is_not_processed_until_download_archive(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    class _Exec:
        def __init__(self, data):
            self.data = data

        def execute(self):
            return self.data

    class _Messages:
        def __init__(self, message):
            self.message = message

        def list(self, **kwargs):
            return _Exec({"messages": [{"id": "msg-download"}]})

        def get(self, **kwargs):
            return _Exec(self.message)

    class _Users:
        def __init__(self, message):
            self.message = message

        def messages(self):
            return _Messages(self.message)

    class _Gmail:
        def __init__(self, message):
            self.message = message

        def users(self):
            return _Users(self.message)

    body = "法院完成線上交付核閱通知，可線上下載。案號：115年度原交易字第21號。"
    message = {
        "payload": {
            "headers": [
                {"name": "Subject", "value": "法院完成線上交付核閱通知 115年度原交易字第21號"},
                {"name": "From", "value": "noreply@judicial.gov.tw"},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")},
        }
    }
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = _Gmail(message)

    result = mgr._scan_and_process_emails("線上下載", "download")

    assert result["hits"] == 1
    assert len(mgr.ready_to_download) == 1
    assert "msg-download" not in mgr.processed_emails
    assert not (tmp_path / "processed_emails.json").exists()


def test_process_emails_dedupes_same_message_across_queries(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    class _Exec:
        def __init__(self, data):
            self.data = data

        def execute(self):
            return self.data

    class _Messages:
        def __init__(self, message):
            self.message = message

        def list(self, **kwargs):
            return _Exec({"messages": [{"id": "msg-payment"}]})

        def get(self, **kwargs):
            return _Exec(self.message)

    class _Users:
        def __init__(self, message):
            self.message = message

        def messages(self):
            return _Messages(self.message)

    class _Gmail:
        def __init__(self, message):
            self.message = message

        def users(self):
            return _Users(self.message)

    body = "法院回覆閱卷聲請結果通知（含繳費單）。案號：115年度原交易字第21號。繳費期限：2026/07/01。附件：繳費單。"
    message = {
        "payload": {
            "headers": [
                {"name": "Subject", "value": "法院回覆閱卷聲請結果通知（含繳費單）"},
                {"name": "From", "value": "noreply@judicial.gov.tw"},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")},
        }
    }
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = _Gmail(message)
    downloads = []
    monkeypatch.setattr(
        mgr,
        "_download_email_attachments",
        lambda msg_id, message=None: downloads.append(msg_id) or [str(tmp_path / "payment.pdf")],
    )
    monkeypatch.setattr(mgr, "notify_payment_needed", lambda info: False)

    result = mgr.process_emails()

    assert result["payment_hits"] == 1
    assert downloads == ["msg-payment"]
    assert "msg-payment" not in mgr.processed_emails


def test_process_emails_detects_nested_payment_attachment_from_subject_and_filename(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    class _Exec:
        def __init__(self, data):
            self.data = data

        def execute(self):
            return self.data

    class _Attachments:
        def get(self, **kwargs):
            assert kwargs["id"] == "att-payment"
            return _Exec({"data": base64.urlsafe_b64encode(b"%PDF-1.4\nnested payment\n").decode("ascii")})

    class _Messages:
        def __init__(self, message):
            self.message = message

        def list(self, **kwargs):
            return _Exec({"messages": [{"id": "msg-nested-payment"}]})

        def get(self, **kwargs):
            return _Exec(self.message)

        def attachments(self):
            return _Attachments()

    class _Users:
        def __init__(self, message):
            self.message = message

        def messages(self):
            return _Messages(self.message)

    class _Gmail:
        def __init__(self, message):
            self.message = message

        def users(self):
            return _Users(self.message)

    html = """
        <table>
          <tr><td>對象法院</td><td>臺灣臺東地方法院</td></tr>
          <tr><td>當事人</td><td>林建豐</td></tr>
          <tr><td>案號</td><td>115.原交易.000021</td></tr>
          <tr><td>繳費期限</td><td>2026/07/01</td></tr>
        </table>
    """
    message = {
        "snippet": "法院回覆結果，附件為規費繳款單。",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "法院回覆閱卷聲請結果通知（含繳費單）"},
                {"name": "From", "value": "noreply@judicial.gov.tw"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/html",
                            "body": {"data": base64.urlsafe_b64encode(html.encode("utf-8")).decode("ascii")},
                        }
                    ],
                },
                {
                    "filename": "繳費單_林建豐_115.原交易.000021.pdf",
                    "mimeType": "application/pdf",
                    "body": {"attachmentId": "att-payment"},
                },
            ],
        },
    }
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = _Gmail(message)
    notified = []
    monkeypatch.setattr(mgr, "notify_payment_needed", lambda info: notified.append(info) or True)

    result = mgr.process_emails()

    assert result["payment_hits"] == 1
    assert result["payment_notified"] == 1
    assert "msg-nested-payment" in mgr.processed_emails
    assert notified and notified[0].court_case_no == "115年度原交易字第21號"
    assert notified[0].client_name == "林建豐"
    assert Path(notified[0].files[0]).name == "繳費單_林建豐_115.原交易.000021.pdf"


def test_process_emails_text_only_gmail_payment_notice_does_not_block_pdf_dedup(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    assert FileReviewManager._is_payment_notice_text("線 上 列 印 繳 費 單 通 知 信") is True

    class _Exec:
        def __init__(self, data):
            self.data = data

        def execute(self):
            return self.data

    class _Messages:
        def __init__(self, message):
            self.message = message

        def list(self, **kwargs):
            return _Exec({"messages": [{"id": "msg-gmail-text-payment"}]})

        def get(self, **kwargs):
            return _Exec(self.message)

    class _Users:
        def __init__(self, message):
            self.message = message

        def messages(self):
            return _Messages(self.message)

    class _Gmail:
        def __init__(self, message):
            self.message = message

        def users(self):
            return _Users(self.message)

    body = "對象法院 臺灣高等法院 當事人 李秀花 聲請方式 複製電子卷證 案號 115.原聲再.000002 案由 違反公職人員選罷法 回覆內容 請至待繳費下載繳費單"
    message = {
        "snippet": "請至待繳費下載繳費單",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "法院回覆閱卷聲請結果通知（含繳費單）"},
                {"name": "From", "value": "noreply@judicial.gov.tw"},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")},
        },
    }
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = _Gmail(message)
    monkeypatch.setattr(mgr, "_download_email_attachments", lambda *_args, **_kwargs: [])
    text_notices = []
    monkeypatch.setattr(mgr, "notify_payment_needed", lambda info: text_notices.append(info) or True)
    portal_seen = []

    def fake_portal_download():
        portal_seen.extend((item.case_number, list(item.source_message_ids)) for item in mgr.pending_payment_notices)
        for item in mgr.pending_payment_notices:
            for msg_id in item.source_message_ids:
                mgr.processed_emails.add(msg_id)
        return {"attempted": 1, "downloaded": 1, "notified": 1, "errors": []}

    monkeypatch.setattr(mgr, "_download_queued_payment_notices_from_portal", fake_portal_download)

    result = mgr.process_emails()

    assert result["payment_hits"] == 1
    assert result["payment_notified"] == 2
    assert result["payment_portal_attempts"] == 1
    assert result["payment_portal_downloaded"] == 1
    assert result["payment_portal_notified"] == 1
    assert "msg-gmail-text-payment" in mgr.processed_emails
    assert portal_seen == [("115年度原聲再字第2號", ["msg-gmail-text-payment"])]
    assert len(text_notices) == 1
    assert text_notices[0].allow_text_without_pdf is True
    assert "gmail_payment_text:115年度原聲再字第2號" in mgr.notified_cases
    assert "web_payment:115年度原聲再字第2號" not in mgr.notified_cases


def test_text_only_payment_notice_alerts_even_when_portal_download_is_deferred(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = object()
    sent = []
    monkeypatch.setattr(mgr, "notify_payment_needed", lambda info: sent.append(info) or True)
    monkeypatch.setattr(mgr, "_download_email_attachments", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(mgr, "_get_email_body", lambda _payload: "案號 115.訴.000123 請至待繳費下載繳費單")

    class _Exec:
        def __init__(self, data):
            self.data = data

        def execute(self):
            return self.data

    class _Messages:
        def list(self, **_kwargs):
            return _Exec({"messages": [{"id": "msg-deferred"}]})

        def get(self, **_kwargs):
            return _Exec({
                "snippet": "請至待繳費下載繳費單",
                "payload": {"headers": [
                    {"name": "Subject", "value": "法院回覆閱卷聲請結果通知（含繳費單）"},
                    {"name": "From", "value": "noreply@judicial.gov.tw"},
                ]},
            })

    class _Users:
        def messages(self):
            return _Messages()

    class _Gmail:
        def users(self):
            return _Users()

    mgr.gmail_service = _Gmail()
    stats = mgr._scan_and_process_emails("query", "auto", set())

    assert stats["payment_hits"] == 1
    assert stats["payment_notified"] == 1
    assert len(sent) == 1
    assert "msg-deferred" not in mgr.processed_emails
    assert len(mgr.pending_payment_notices) == 1
    assert "gmail_payment_text:115年度訴字第123號" in mgr.notified_cases


def test_unparseable_payment_notice_still_alerts_and_closes_after_delivery(tmp_path, monkeypatch):
    """A confirmed payment email may not silently vanish when its case id changed format."""
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    class _Exec:
        def __init__(self, data):
            self.data = data

        def execute(self):
            return self.data

    class _Messages:
        def list(self, **_kwargs):
            return _Exec({"messages": [{"id": "msg-unparsed-payment"}]})

        def get(self, **_kwargs):
            return _Exec({
                "snippet": "請至待繳費下載繳費單",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "法院回覆閱卷聲請結果通知（含繳費單）"},
                        {"name": "From", "value": "noreply@judicial.gov.tw"},
                    ],
                    "body": {
                        "data": base64.urlsafe_b64encode(
                            "法院通知：請於期限內下載並繳費".encode("utf-8")
                        ).decode("ascii")
                    },
                },
            })

    class _Users:
        def messages(self):
            return _Messages()

    class _Gmail:
        def users(self):
            return _Users()

    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = _Gmail()
    monkeypatch.setattr(mgr, "_download_email_attachments", lambda *_a, **_k: [])
    sent = []
    monkeypatch.setattr(mgr, "notify_payment_needed", lambda info: sent.append(info) or True)

    result = mgr.process_emails()

    assert result["payment_hits"] == 1
    assert result["payment_notified"] == 1
    assert len(sent) == 1
    assert sent[0].allow_text_without_pdf is True
    assert sent[0].court_case_no == "案號待辨識（請查看法院繳費通知信）"
    assert "msg-unparsed-payment" in mgr.processed_emails
    assert "gmail_payment_text:msg-unparsed-payment" in mgr.notified_cases


def test_download_email_attachments_reuses_same_payload_file(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    payload = b"%PDF-1.4\nsame payment slip\n"
    message = {
        "payload": {
            "parts": [
                {
                    "filename": "259420307417.pdf",
                    "mimeType": "application/octet-stream",
                    "body": {
                        "data": base64.urlsafe_b64encode(payload).decode("ascii"),
                    },
                }
            ]
        }
    }
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = object()

    first = mgr._download_email_attachments("msg-payment", message)
    second = mgr._download_email_attachments("msg-payment", message)

    assert first == second
    assert first == [str(tmp_path / "259420307417.pdf")]
    assert sorted(p.name for p in tmp_path.glob("259420307417*.pdf")) == ["259420307417.pdf"]


def test_portal_notify_state_can_record_zero_pending_without_notification(tmp_path):
    module = _load_action_module()
    state_path = tmp_path / ".portal_notify_state.json"

    module._save_portal_notify_state(
        str(state_path),
        portal_downloadable=6,
        portal_pickup=29,
        portal_pending=0,
    )

    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["portal_downloadable"] == 6
    assert data["portal_court_pickup"] == 29
    assert data["portal_pending"] == 0


def test_recent_activity_fingerprint_ignores_processed_at_for_same_download():
    module = _load_action_module()
    base = {
        "source": "download_job",
        "artifact_type": "review_download",
        "party": "林建豐",
        "case_number": "115年度原交易字第21號",
        "detail": "已下載卷宗（2 份）",
        "count": 2,
    }

    first = dict(base, processed_at="2026-06-23T10:00:00")
    second = dict(base, processed_at="2026-06-23T10:05:00")

    assert module._recent_activity_fingerprint(first) == module._recent_activity_fingerprint(second)


def test_payment_pdf_notification_uses_file_caption_without_duplicate_text_push(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewInfo,
        FileReviewManager,
    )

    pdf = tmp_path / "441403005422.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        court="臺灣花蓮地方法院",
        client_name="林建豐",
        court_case_no="115年度原交易字第21號",
        status="待繳費",
        payment_deadline="2026-06-30",
        files=[str(pdf)],
    )

    sent_files = []

    class FakeNotifier:
        def notify_admin_with_files(self, text, file_paths, **kwargs):
            sent_files.append((text, file_paths, kwargs))
            return True

        def notify_admin(self, *_args, **_kwargs):
            raise AssertionError("text fallback should not run after file delivery")

    def fail_text_push(*_args, **_kwargs):
        raise AssertionError("red_phone text push should not run before successful file delivery")

    fake_red_phone = types.SimpleNamespace(
        send_telegram_push_with_status=fail_text_push,
        send_discord_bot_file=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("discord file fallback should not run after file delivery")
        ),
    )
    fake_line_notifier = types.SimpleNamespace(LAFNotifier=lambda: FakeNotifier())
    fake_dedup_db = types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False)
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_red_phone)
    monkeypatch.setitem(sys.modules, "line_notifier", fake_line_notifier)
    monkeypatch.setitem(sys.modules, "skills.ops.dedup_db", fake_dedup_db)
    monkeypatch.setattr(
        "casper_ecosystem.law_firm_orchestrators.line_notifier.LAFNotifier",
        lambda: FakeNotifier(),
    )

    assert mgr.notify_payment_needed(info) is True

    assert len(sent_files) == 1
    text, file_paths, kwargs = sent_files[0]
    assert text.startswith("💰 繳費單通知")
    assert "林建豐 - 115年度原交易字第21號" in text
    assert len(file_paths) == 1
    sent_path = Path(file_paths[0])
    assert sent_path != pdf
    assert sent_path.name == "繳費單_林建豐_115年度原交易字第21號.pdf"
    assert sent_path.read_bytes() == pdf.read_bytes()
    assert kwargs["topic_key"] == "filereview_payment"


def test_expired_party_only_payment_dismissal_cannot_silence_a_new_case(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False),
    )
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.dismissed_payments = {
        "web_payment:dismissed:吳倩茹": {
            "dismissed_at": (datetime.now() - timedelta(days=100)).isoformat(),
            "keyword": "吳倩茹",
            "reason": "舊案已繳費",
        }
    }

    assert mgr._is_payment_dismissed(
        "web_payment:case:114年度附民字第1289號:吳倩茹",
        "web_payment:114年度附民字第1289號",
        party="吳倩茹",
    ) is False


def test_recent_party_only_payment_dismissal_is_a_short_lived_safety_net(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False),
    )
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.dismissed_payments = {
        "web_payment:dismissed:吳倩茹": {
            "dismissed_at": (datetime.now() - timedelta(hours=2)).isoformat(),
            "keyword": "吳倩茹",
            "reason": "剛確認已繳費",
        }
    }

    assert mgr._is_payment_dismissed(
        "web_payment:case:114年度附民字第1289號:吳倩茹",
        "web_payment:114年度附民字第1289號",
        party="吳倩茹",
    ) is True


def test_case_scoped_payment_dismissal_survives_format_changes(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False),
    )
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.dismissed_payments = {
        "web_payment:case:114附民1289:吳倩茹": {
            "dismissed_at": "2026-04-10T11:29:40",
            "keyword": "吳倩茹",
            "reason": "本案已繳費",
        }
    }

    assert mgr._is_payment_dismissed(
        "web_payment:case:114年度附民字第001289號:吳倩茹",
        "web_payment:114年度附民字第001289號",
        party="吳倩茹",
    ) is True


def test_notify_payment_needed_delivers_new_pdf_even_when_payment_proof_exists(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewInfo,
        FileReviewManager,
    )

    pdf = tmp_path / "441403005422.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setenv("MAGI_V3_SHARED_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MAGI_SHARED_STATE_DIR", str(tmp_path))
    proof_registry = tmp_path / "file-review" / "downloads" / "payment_proof_registry.json"
    proof_registry.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MAGI_PAYMENT_PROOF_REGISTRY_PATH", str(proof_registry))
    monkeypatch.setenv(
        "MAGI_PAYMENT_REGISTRY_PATH",
        str(tmp_path / "file-review" / "downloads" / "payment_registry.json"),
    )
    proof_registry.write_text(
        json.dumps(
            {
                "115.上訴.003543": {
                    "uploaded_at": "2026-06-30T14:32:49",
                    "court_code": "TPH",
                    "file": "discord_d528d01e2e9b4c6b8b8567bb2f25e38b.png",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        court="臺灣高等法院",
        client_name="游秀鈴",
        court_case_no="115年度上訴字第3543號",
        status="待繳費",
        payment_deadline="2026-07-02",
        files=[str(pdf)],
    )

    delivered = []

    class FakeNotifier:
        def notify_admin_with_files(self, text, paths, **kwargs):
            delivered.append((text, paths, kwargs))
            return True

        def notify_admin(self, *_args, **_kwargs):
            raise AssertionError("successful PDF delivery must not send fallback text")

    fake_line_notifier = types.SimpleNamespace(LAFNotifier=lambda: FakeNotifier())
    fake_red_phone = types.SimpleNamespace(
        send_telegram_push_with_status=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("successful PDF delivery must not send red_phone text")
        ),
        send_discord_bot_file=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("successful PDF delivery must not send Discord fallback")
        ),
    )
    monkeypatch.setitem(sys.modules, "line_notifier", fake_line_notifier)
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_red_phone)
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False),
    )

    assert mgr.notify_payment_needed(info) is True
    assert len(delivered) == 1
    assert "仍交付本輪新取得" in delivered[0][0]
    assert mgr._payment_pdf_already_delivered(str(pdf)) is True


def test_payment_slip_download_sends_files_without_duplicate_summary_text(tmp_path, monkeypatch):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_凡江_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    text_notifications = []
    file_notifications = []

    class FakeManager:
        def __init__(self, **_kwargs):
            pass

        def login(self):
            return True

        def navigate_to_file_review(self):
            return None

        def download_all_payment_slips(self, max_days=14, target_case_number=None):
            return [
                {
                    "party": "凡江",
                    "case_number": "115年度原交易字第21號",
                    "all_paths": [str(pdf)],
                }
            ]

        def close(self):
            return None

    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "_get_credentials",
        lambda _cfg: {
            "username": "u",
            "password": "p",
            "download_folder": str(tmp_path),
        },
    )
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(module, "_ensure_imports", lambda: types.SimpleNamespace(FileReviewManager=FakeManager))
    monkeypatch.setattr(module, "_is_valid_payment_pdf_file", lambda path: path == str(pdf))
    monkeypatch.setattr(module, "_notify", lambda *args, **kwargs: text_notifications.append((args, kwargs)))
    monkeypatch.setattr(module, "_notify_file", lambda *args, **kwargs: file_notifications.append((args, kwargs)) or True)
    monkeypatch.setattr(module, "_mark_payment_file_delivered", lambda *args, **kwargs: None)

    result = module.cmd_download_payment_slips(notify=True)

    assert result["success"] is True
    assert result["count"] == 1
    assert text_notifications == []
    assert len(file_notifications) == 1
    args, kwargs = file_notifications[0]
    sent_path = Path(args[0])
    assert sent_path != pdf
    assert sent_path.name == "繳費單_凡江_115年度原交易字第21號.pdf"
    assert sent_path.read_bytes() == pdf.read_bytes()
    assert kwargs["topic_key"] == "filereview_payment"
    assert kwargs["caption"].startswith("💰 繳費單 PDF 下載完成")


def test_payment_slip_download_stays_quiet_when_no_pending_slips(tmp_path, monkeypatch):
    module = _load_action_module()
    text_notifications = []
    file_notifications = []

    class FakeManager:
        def __init__(self, **_kwargs):
            pass

        def login(self):
            return True

        def navigate_to_file_review(self):
            return None

        def download_all_payment_slips(self, max_days=14, target_case_number=None):
            return []

        def close(self):
            return None

    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "_get_credentials",
        lambda _cfg: {
            "username": "u",
            "password": "p",
            "download_folder": str(tmp_path),
        },
    )
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(module, "_ensure_imports", lambda: types.SimpleNamespace(FileReviewManager=FakeManager))
    monkeypatch.setattr(module, "_notify", lambda *args, **kwargs: text_notifications.append((args, kwargs)))
    monkeypatch.setattr(module, "_notify_file", lambda *args, **kwargs: file_notifications.append((args, kwargs)) or True)

    result = module.cmd_download_payment_slips(notify=True)

    assert result["success"] is True
    assert result["count"] == 0
    assert result["sent"] == 0
    assert result["failed"] == 0
    assert result["delivery"]["suppressed_noop"] is True
    assert text_notifications == []
    assert file_notifications == []


def test_payment_pdf_delivery_failure_is_pending_not_delivered(tmp_path, monkeypatch):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_凡江_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(module, "_notify_file", lambda *args, **kwargs: {"ok": False, "errors": ["tg_down"]})

    result = module._send_payment_pdf_files(
        [str(pdf)],
        download_folder=str(tmp_path),
        caption_prefix="💰 繳費單 PDF 下載完成",
        notify=True,
    )

    assert result["sent"] == 0
    assert result["failed"] == 1
    assert module._payment_file_already_delivered(str(pdf), str(tmp_path)) is False
    state = json.loads((tmp_path / ".payment_pdf_delivery_state.json").read_text(encoding="utf-8"))
    assert state["sent_files"] == {}
    assert len(state["pending_files"]) == 1
    pending = next(iter(state["pending_files"].values()))
    assert pending["attempts"] == 1
    assert pending["last_error"] == "notify_file_returned_false"


def test_existing_payment_pdf_is_delivered_despite_old_text_notice(tmp_path, monkeypatch):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_林建豐_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "notified_cases.json").write_text(
        json.dumps(
            {"web_payment:case:115原交易21:林建豐": "2026-05-26T14:01:34"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(module, "_notify_file", lambda *args, **kwargs: calls.append((args, kwargs)) or True)

    result = module._send_payment_pdf_files(
        [str(pdf)],
        download_folder=str(tmp_path),
        caption_prefix="💰 繳費單 PDF 下載完成",
        notify=True,
        notice_keys_by_path={str(pdf): ["web_payment:case:115原交易21:林建豐"]},
    )

    assert result["sent"] == 1
    assert result["failed"] == 0
    assert result["notice_seen"] == 0
    assert len(calls) == 1


def test_notify_file_rejects_false_status_dict_and_uses_fallback(tmp_path, monkeypatch):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_凡江_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    fallback_calls = []

    class FakeNotifier:
        def notify_admin_with_files(self, *_args, **_kwargs):
            return {"ok": False, "delivered": False}

    fake_line_notifier = types.SimpleNamespace(LAFNotifier=lambda: FakeNotifier())
    fake_red_phone = types.SimpleNamespace(
        send_file_admin=lambda *_args, **_kwargs: {"ok": False, "errors": ["tg_down"]},
        send_discord_bot_file=lambda *args, **kwargs: fallback_calls.append((args, kwargs)) or True,
    )
    monkeypatch.setitem(sys.modules, "line_notifier", fake_line_notifier)
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_red_phone)

    assert module._notify_file(str(pdf), caption="test", topic_key="filereview_payment") is True
    assert len(fallback_calls) == 1


def test_scheduled_check_runs_payment_scan_before_download(monkeypatch):
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "payment-scan", 1)
    calls = []
    download_env = {}
    portal_env = {}

    monkeypatch.setenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "1")
    monkeypatch.setenv("MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP", "1")
    monkeypatch.setenv("MAGI_FILE_REVIEW_CHECK_WITH_PORTAL", "0")

    def fake_check_emails(**_kwargs):
        calls.append("check_emails")
        portal_env["during_check"] = module.os.environ.get("MAGI_FILE_REVIEW_CHECK_WITH_PORTAL")
        return {
            "success": True,
            "portal_probe_ok": True,
            "portal_probe_deferred": False,
            "portal_pending_payment_count": 1,
            "portal_downloadable_count": 1,
            "ready_to_download_count": 0,
            "download_hits": 0,
            **portal_receipt,
        }

    monkeypatch.setattr(module, "cmd_check_emails", fake_check_emails)
    monkeypatch.setattr(
        module,
        "cmd_download_payment_slips",
        lambda **kwargs: calls.append("download_payment_slips") or {"success": True, "delivery": {"sent": 0, "failed": 0}},
    )

    def fake_download_sync(**kwargs):
        calls.append("download")
        download_env["case_level"] = module.os.environ.get("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP")
        download_env["button_level"] = module.os.environ.get("MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP")
        return {
            "success": True,
            "downloaded_count": 0,
            "verified_existing_count": 1,
            **_download_signature_receipt(
                module,
                verified_existing=portal_receipt[
                    "portal_download_signature_hashes"
                ],
            ),
        }

    monkeypatch.setattr(module, "cmd_download_sync", fake_download_sync)

    result = module.cmd_scheduled_check(notify=True)

    assert result["success"] is True
    assert calls == ["check_emails", "download_payment_slips", "download"]
    assert portal_env == {"during_check": "1"}
    assert download_env == {"case_level": "0", "button_level": "1"}
    assert module.os.environ.get("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP") == "1"
    assert module.os.environ.get("MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP") == "1"
    assert module.os.environ.get("MAGI_FILE_REVIEW_CHECK_WITH_PORTAL") == "0"


def test_scheduled_check_rejects_unaccounted_portal_downloadables(monkeypatch):
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "unaccounted", 7)
    monkeypatch.setattr(
        module,
        "cmd_check_emails",
        lambda **_kwargs: {
            "success": True,
            "portal_probe_ok": True,
            "portal_probe_deferred": False,
            "portal_pending_payment_count": 0,
            "portal_downloadable_count": 7,
            "ready_to_download_count": 0,
            "download_hits": 0,
            **portal_receipt,
        },
    )
    monkeypatch.setattr(
        module,
        "cmd_download_sync",
        lambda **_kwargs: {
            "success": True,
            "downloaded_count": 0,
            **_download_signature_receipt(module),
        },
    )

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is False
    assert result["failed_steps"] == ["download"]
    download = result["steps"]["download"]
    assert download["reason"] == "portal_downloadable_not_reconciled"
    assert download["expected_portal_downloadable_count"] == 7
    assert download["accounted_portal_downloadable_count"] == 0
    assert download["download_reconciliation_verified"] is False


def test_scheduled_check_accepts_verified_existing_portal_downloadables(monkeypatch):
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "existing", 7)
    monkeypatch.setattr(
        module,
        "cmd_check_emails",
        lambda **_kwargs: {
            "success": True,
            "portal_probe_ok": True,
            "portal_probe_deferred": False,
            "portal_pending_payment_count": 0,
            "portal_downloadable_count": 7,
            "ready_to_download_count": 0,
            "download_hits": 0,
            **portal_receipt,
        },
    )
    monkeypatch.setattr(
        module,
        "cmd_download_sync",
        lambda **_kwargs: {
            "success": True,
            "downloaded_count": 0,
            "verified_existing_count": 7,
            **_download_signature_receipt(
                module,
                verified_existing=portal_receipt[
                    "portal_download_signature_hashes"
                ],
            ),
        },
    )

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is True
    download = result["steps"]["download"]
    assert download["expected_portal_downloadable_count"] == 7
    assert download["accounted_portal_downloadable_count"] == 7
    assert download["download_reconciliation_verified"] is True


def test_scheduled_check_accounts_exact_cross_case_cooldown_as_safe_deferral(
    monkeypatch,
):
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "cross-case-cooldown", 7)
    expected = portal_receipt["portal_download_signature_hashes"]
    handled = expected[:5]
    deferred = expected[5:]
    monkeypatch.setattr(
        module,
        "cmd_check_emails",
        lambda **_kwargs: {
            "success": True,
            "portal_probe_ok": True,
            "portal_probe_deferred": False,
            "portal_pending_payment_count": 0,
            "portal_downloadable_count": 7,
            "ready_to_download_count": 0,
            "download_hits": 0,
            **portal_receipt,
        },
    )
    monkeypatch.setattr(
        module,
        "cmd_download_sync",
        lambda **_kwargs: {
            "success": True,
            "status": "deferred",
            "deferred": True,
            "reason": "court_payload_identity_mismatch",
            **_download_signature_receipt(
                module,
                verified_existing=handled,
                mismatch_deferred=deferred,
            ),
        },
    )

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is True
    assert result["status"] == "deferred"
    download = result["steps"]["download"]
    assert download["download_reconciliation_verified"] is True
    assert download["accounted_portal_downloadable_count"] == 7
    assert download["handled_portal_signature_hashes"] == handled
    assert download["mismatch_deferred_portal_signature_hashes"] == deferred
    assert download["accounted_portal_signature_hashes"] == expected


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_reason",
        "not_deferred",
        "overlap",
        "bad_set_hash",
        "invalid_raw",
        "extra_valid",
    ],
)
def test_cross_case_cooldown_receipt_fails_closed_when_not_exact(mutation):
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "cross-case-invalid", 2)
    expected = portal_receipt["portal_download_signature_hashes"]
    email = {"portal_downloadable_count": 2, **portal_receipt}
    download = {
        "success": True,
        "status": "deferred",
        "deferred": True,
        "reason": "court_payload_identity_mismatch",
        **_download_signature_receipt(
            module,
            verified_existing=expected[:1],
            mismatch_deferred=expected[1:],
        ),
    }
    if mutation == "wrong_reason":
        download["reason"] = "download_time_budget_exhausted"
    elif mutation == "not_deferred":
        download["deferred"] = False
    elif mutation == "overlap":
        download.update(
            _download_signature_receipt(
                module,
                verified_existing=expected,
                mismatch_deferred=expected[1:],
            )
        )
    elif mutation == "bad_set_hash":
        download["mismatch_deferred_portal_signature_set_hash"] = "0" * 64
    elif mutation == "invalid_raw":
        download["mismatch_deferred_portal_signature_hashes"] = [
            *expected[1:],
            "not-a-signature",
        ]
    else:
        extra = _portal_receipt(module, "cross-case-extra", 1)[
            "portal_download_signature_hashes"
        ]
        combined = sorted([*expected[1:], *extra])
        download["mismatch_deferred_portal_signature_hashes"] = combined
        download["mismatch_deferred_portal_signature_set_hash"] = (
            module.signature_set_hash(combined)
        )

    reconciled = module._reconcile_scheduled_download(email, download)

    assert reconciled["accounted_portal_downloadable_count"] == 0
    assert reconciled["download_reconciliation_verified"] is False


def test_reconciliation_accounted_count_is_exact_valid_signature_intersection():
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "intersection", 7)
    expected = portal_receipt["portal_download_signature_hashes"]
    extra = _portal_receipt(module, "extra", 2)["portal_download_signature_hashes"]
    handled = [*expected[:5], *extra]
    download = {
        "success": True,
        "downloaded_count": 7,
        "verified_existing_count": 0,
        **_download_signature_receipt(module, processed=handled),
    }
    email = {
        "portal_downloadable_count": 7,
        **portal_receipt,
    }

    reconciled = module._reconcile_scheduled_download(email, download)

    assert reconciled["accounted_portal_downloadable_count"] == 5
    assert reconciled["download_reconciliation_verified"] is False
    assert reconciled["success"] is False


@pytest.mark.parametrize(
    "corrupt_side",
    ["expected", "handled"],
)
def test_reconciliation_invalid_signature_contract_accounts_zero(corrupt_side):
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "invalid-contract", 2)
    handled = portal_receipt["portal_download_signature_hashes"]
    email = {
        "portal_downloadable_count": 2,
        **portal_receipt,
    }
    download = {
        "success": True,
        "downloaded_count": 2,
        **_download_signature_receipt(module, processed=handled),
    }
    if corrupt_side == "expected":
        email["portal_download_signature_set_hash"] = "0" * 64
    else:
        download["handled_portal_signature_set_hash"] = "0" * 64

    reconciled = module._reconcile_scheduled_download(email, download)

    assert reconciled["accounted_portal_downloadable_count"] == 0
    assert reconciled["download_reconciliation_verified"] is False
    assert reconciled["success"] is False


@pytest.mark.parametrize(
    "field",
    [
        "portal_download_signature_hashes",
        "processed_portal_signature_hashes",
        "verified_existing_portal_signature_hashes",
        "handled_portal_signature_hashes",
    ],
)
@pytest.mark.parametrize(
    "mutation",
    ["invalid_extra", "duplicate", "uppercase", "non_list"],
)
def test_reconciliation_rejects_noncanonical_raw_signature_lists(field, mutation):
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "raw-schema", 2)
    expected = portal_receipt["portal_download_signature_hashes"]
    email = {
        "portal_downloadable_count": 2,
        **portal_receipt,
    }
    download = {
        "success": True,
        **_download_signature_receipt(
            module,
            processed=expected[:1],
            verified_existing=expected[1:],
        ),
    }
    target = email if field == "portal_download_signature_hashes" else download
    raw = list(target[field])
    if mutation == "invalid_extra":
        target[field] = [*raw, "not-a-signature"]
    elif mutation == "duplicate":
        target[field] = [*raw, raw[0]]
    elif mutation == "uppercase":
        target[field] = [raw[0].upper(), *raw[1:]]
    else:
        target[field] = tuple(raw)

    reconciled = module._reconcile_scheduled_download(email, download)

    assert reconciled["accounted_portal_downloadable_count"] == 0
    assert reconciled["download_reconciliation_verified"] is False
    assert reconciled["success"] is False


def test_reconciliation_rejects_declared_handled_union_mismatch():
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "declared-mismatch", 2)
    expected = portal_receipt["portal_download_signature_hashes"]
    email = {
        "portal_downloadable_count": 2,
        **portal_receipt,
    }
    download = {
        "success": True,
        **_download_signature_receipt(
            module,
            processed=expected[:1],
            verified_existing=expected[1:],
        ),
    }
    download["handled_portal_signature_hashes"] = expected[:1]
    download["handled_portal_signature_set_hash"] = module.signature_set_hash(
        expected[:1]
    )

    reconciled = module._reconcile_scheduled_download(email, download)

    assert reconciled["accounted_portal_downloadable_count"] == 0
    assert reconciled["download_reconciliation_verified"] is False
    assert reconciled["success"] is False


@pytest.mark.parametrize("raw_count", ["2", True, -1])
def test_reconciliation_rejects_noncanonical_expected_count(raw_count):
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "invalid-count", 2)
    expected = portal_receipt["portal_download_signature_hashes"]
    email = {
        "portal_downloadable_count": raw_count,
        **portal_receipt,
    }
    download = {
        "success": True,
        **_download_signature_receipt(module, processed=expected),
    }

    reconciled = module._reconcile_scheduled_download(email, download)

    assert reconciled["expected_portal_downloadable_count"] == 0
    assert reconciled["accounted_portal_downloadable_count"] == 0
    assert reconciled["download_reconciliation_verified"] is False
    assert reconciled["success"] is False


def test_reconciliation_valid_zero_count_still_requires_both_receipt_contracts():
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "valid-zero", 0)
    email = {
        "portal_downloadable_count": 0,
        **portal_receipt,
    }
    valid = module._reconcile_scheduled_download(
        email,
        {
            "success": True,
            **_download_signature_receipt(module),
        },
    )
    invalid = module._reconcile_scheduled_download(email, {"success": True})

    assert valid["download_reconciliation_verified"] is True
    assert valid["success"] is True
    assert invalid["download_reconciliation_verified"] is False
    assert invalid["success"] is False


def test_scheduled_fallback_does_not_compete_with_fresh_primary_owner(tmp_path, monkeypatch):
    module = _load_action_module()
    state_path = tmp_path / "file_review_auto_state.json"
    state_path.write_text(
        json.dumps(
            {
                "updated_at": module.datetime.now().isoformat(timespec="seconds"),
                "pid": module.os.getpid(),
                "phase": "cycle_complete",
                "result": {"portal_verified": True, "downloaded_count": 2},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_FILE_REVIEW_AUTO_STATE", str(state_path))
    monkeypatch.setenv("MAGI_FILE_REVIEW_FALLBACK_ONLY", "1")
    monkeypatch.setattr(
        module,
        "cmd_check_emails",
        lambda **_kwargs: pytest.fail("fresh primary owner must prevent duplicate Gmail scan"),
    )

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is True
    assert result["status"] == "delegated"
    assert result["owner"] == "file_review_auto"
    assert result["owner_state"]["portal_verified"] is True


def test_scheduled_fallback_runs_when_primary_owner_state_is_stale(tmp_path, monkeypatch):
    module = _load_action_module()
    state_path = tmp_path / "file_review_auto_state.json"
    state_path.write_text(
        json.dumps(
            {
                "updated_at": "2020-01-01T00:00:00",
                "pid": module.os.getpid(),
                "phase": "cycle_complete",
                "result": {"portal_verified": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_FILE_REVIEW_AUTO_STATE", str(state_path))
    monkeypatch.setenv("MAGI_FILE_REVIEW_FALLBACK_ONLY", "1")
    monkeypatch.setattr(
        module,
        "cmd_check_emails",
        lambda **_kwargs: {
            "success": True,
            "portal_probe_ok": True,
            "portal_probe_deferred": False,
            "portal_pending_payment_count": 0,
            "portal_downloadable_count": 0,
            "ready_to_download_count": 0,
            "download_hits": 0,
            **_portal_receipt(module, "stale-owner-empty", 0),
        },
    )

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is True
    assert result["status"] == "done"


def test_scheduled_fallback_takes_over_when_primary_owner_is_unhealthy(tmp_path, monkeypatch):
    module = _load_action_module()
    state_path = tmp_path / "file_review_auto_state.json"
    state_path.write_text(
        json.dumps(
            {
                "updated_at": module.datetime.now().isoformat(timespec="seconds"),
                "pid": module.os.getpid(),
                "phase": "cycle_complete",
                "result": {"ok": False, "portal_verified": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_FILE_REVIEW_AUTO_STATE", str(state_path))
    monkeypatch.setattr(
        module,
        "cmd_check_emails",
        lambda **_kwargs: {
            "success": True,
            "portal_probe_ok": True,
            "portal_probe_deferred": False,
            "portal_pending_payment_count": 0,
            "portal_downloadable_count": 0,
            "ready_to_download_count": 0,
            "download_hits": 0,
            **_portal_receipt(module, "unhealthy-owner-empty", 0),
        },
    )

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is True
    assert result["status"] == "done"


def test_gmail_scan_can_defer_portal_payment_to_single_owner(monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    manager = FileReviewManager.__new__(FileReviewManager)
    manager.gmail_service = object()
    manager.ready_to_download = []
    manager.pending_payment_notices = []
    manager.log = lambda *_args, **_kwargs: None
    manager.process_auto_drafts = lambda: None
    manager._scan_and_process_emails = lambda *_args, **_kwargs: {
        "hits": 0,
        "notified": 0,
        "download_hits": 0,
        "pickup_hits": 0,
        "payment_hits": 0,
        "payment_notified": 0,
        "errors": [],
    }
    manager._download_queued_payment_notices_from_portal = lambda: pytest.fail(
        "Gmail owner must not open a court-portal session"
    )
    monkeypatch.setenv("MAGI_FILE_REVIEW_EMAIL_SCAN_WITH_PORTAL_PAYMENT", "0")

    result = manager.process_emails()

    assert result["errors"] == []
    assert result["payment_portal_deferred_to_owner"] is True


def test_scheduled_check_skips_second_portal_login_when_probe_proves_no_downloads(monkeypatch):
    module = _load_action_module()
    calls = []
    monkeypatch.setattr(
        module,
        "cmd_check_emails",
        lambda **_kwargs: {
            "success": True,
            "portal_probe_ok": True,
            "portal_probe_deferred": False,
            "portal_pending_payment_count": 0,
            "portal_downloadable_count": 0,
            "ready_to_download_count": 0,
            "download_hits": 0,
            **_portal_receipt(module, "skip-second-login-empty", 0),
        },
    )
    monkeypatch.setattr(
        module,
        "cmd_download_payment_slips",
        lambda **_kwargs: calls.append("payment") or {"success": True, "count": 0},
    )
    monkeypatch.setattr(
        module,
        "cmd_download_sync",
        lambda **_kwargs: calls.append("download") or {"success": True},
    )

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is True
    assert calls == []
    assert result["steps"]["download_payment_slips"]["reason"] == "verified_no_pending_payment"
    assert result["steps"]["download"]["reason"] == "verified_no_download_signal"
    assert result["steps"]["download"]["downloaded_count"] == 0


def test_scheduled_check_publishes_authoritative_portal_state(tmp_path, monkeypatch):
    module = _load_action_module()
    state_path = tmp_path / "file_review_auto_state.json"
    monkeypatch.setenv("MAGI_FILE_REVIEW_AUTO_STATE", str(state_path))
    monkeypatch.setattr(
        module,
        "cmd_check_emails",
        lambda **_kwargs: {
            "success": True,
            "portal_probe_ok": True,
            "portal_probe_deferred": False,
            "portal_status_semantics": "ola-current-state-v2",
            "portal_raw_row_count": 25,
            "portal_case_count": 20,
            "portal_pending_payment_count": 0,
            "portal_downloadable_count": 0,
            "ready_to_download_count": 0,
            "download_hits": 0,
            "scan_errors": 0,
            "recent_unnotified_count": 0,
            **_portal_receipt(module, "authoritative-empty", 0),
        },
    )

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is True
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["phase"] == "scheduled_check_complete"
    assert state["result"]["ok"] is True
    assert state["result"]["portal_verified"] is True
    assert state["result"]["check"]["parsed"]["portal_status_semantics"] == "ola-current-state-v2"
    assert state["result"]["portal_raw_row_count"] == 25


def test_scheduled_check_fails_closed_without_authoritative_portal_probe(monkeypatch):
    module = _load_action_module()
    calls = []
    monkeypatch.setattr(
        module,
        "cmd_check_emails",
        lambda **_kwargs: {
            "success": True,
            "portal_probe_ok": False,
            "portal_probe_deferred": False,
            "portal_probe_error": "missing credentials",
            "portal_probe_error_code": "missing_credentials",
            "portal_failure_alert": False,
        },
    )
    monkeypatch.setattr(
        module,
        "cmd_download_payment_slips",
        lambda **_kwargs: calls.append("payment") or {"success": True},
    )
    monkeypatch.setattr(
        module,
        "cmd_download_sync",
        lambda **_kwargs: calls.append("download") or {"success": True},
    )

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is False
    assert calls == []
    assert result["steps"]["download_payment_slips"]["reason"] == "portal_probe_failed"
    assert result["steps"]["download"]["reason"] == "portal_probe_failed"
    assert result["steps"]["download"]["error"] == "missing credentials"


def test_scheduled_check_defers_one_off_transient_portal_failure(monkeypatch):
    module = _load_action_module()
    calls = []
    monkeypatch.setattr(
        module,
        "cmd_check_emails",
        lambda **_kwargs: {
            "success": True,
            "portal_probe_ok": False,
            "portal_probe_deferred": False,
            "portal_probe_error": "navigate_failed",
            "portal_probe_error_code": "navigate_failed",
            "portal_failure_streak": 1,
            "portal_failure_alert": False,
        },
    )
    monkeypatch.setattr(
        module,
        "cmd_download_payment_slips",
        lambda **_kwargs: calls.append("payment") or {"success": True},
    )
    monkeypatch.setattr(
        module,
        "cmd_download_sync",
        lambda **_kwargs: calls.append("download") or {"success": True},
    )

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is True
    assert result["status"] == "deferred"
    assert calls == []
    assert result["steps"]["download_payment_slips"]["reason"] == "portal_probe_transient_retry"
    assert result["steps"]["download"]["reason"] == "portal_probe_transient_retry"


def test_scheduled_check_fails_after_transient_portal_failure_threshold(monkeypatch):
    module = _load_action_module()
    monkeypatch.setattr(
        module,
        "cmd_check_emails",
        lambda **_kwargs: {
            "success": True,
            "portal_probe_ok": False,
            "portal_probe_deferred": False,
            "portal_probe_error": "navigate_failed",
            "portal_probe_error_code": "navigate_failed",
            "portal_failure_streak": 3,
            "portal_failure_alert": True,
        },
    )

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["steps"]["download"]["reason"] == "portal_probe_failed"


def test_download_fails_closed_when_popup_never_reaches_download_control(
    tmp_path, monkeypatch
):
    module = _load_action_module()

    class FakeManager:
        last_navigation_error_code = ""
        last_download_error_code = "popup_processing_timeout"
        last_download_error_detail = "popup content still processing after 120s"

        def __init__(self, **_kwargs):
            pass

        def login(self):
            return True

        def navigate_to_file_review(self):
            return True

        def check_and_download_available(self, target_case_number=None):
            return []

        def close(self):
            return None

    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "_get_credentials",
        lambda _cfg: {
            "username": "u",
            "password": "p",
            "download_folder": str(tmp_path),
        },
    )
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(
        module,
        "_ensure_imports",
        lambda: types.SimpleNamespace(FileReviewManager=FakeManager),
    )
    monkeypatch.setattr(module, "_eventlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_safe_flow_step_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_mark_notify_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_check_flow_cancelled", lambda *args, **kwargs: None)

    result = module.cmd_download.__wrapped__(notify=False)

    assert result["success"] is False
    assert result["error"] == "popup_processing_timeout"
    assert result["downloaded_count"] == 0


def test_download_nested_frame_timeout_is_deferred_before_threshold(
    tmp_path, monkeypatch
):
    module = _load_action_module()

    class FakeManager:
        last_navigation_error_code = ""
        last_download_error_code = "popup_nested_frame_timeout"
        last_download_error_detail = "nested v1 frame unavailable"
        last_download_error_events = [
            {
                "code": "popup_nested_frame_timeout",
                "detail": "nested v1 frame unavailable",
            }
        ]

        def __init__(self, **_kwargs):
            pass

        def login(self):
            return True

        def navigate_to_file_review(self):
            return True

        def check_and_download_available(self, target_case_number=None):
            return []

        def close(self):
            return None

    notices = []
    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "_get_credentials",
        lambda _cfg: {
            "username": "u",
            "password": "p",
            "download_folder": str(tmp_path),
        },
    )
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(
        module,
        "_ensure_imports",
        lambda: types.SimpleNamespace(FileReviewManager=FakeManager),
    )
    monkeypatch.setattr(module, "_eventlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_notify", lambda msg, *_a, **_k: notices.append(msg))
    monkeypatch.setattr(module, "_safe_flow_step_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_mark_notify_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_check_flow_cancelled", lambda *args, **kwargs: None)

    result = module.cmd_download.__wrapped__(notify=True)

    assert result["success"] is True
    assert result["status"] == "deferred"
    assert result["deferred"] is True
    assert result["reason"] == "download_retry_pending"
    assert result["unresolved_count"] == 1
    assert result["retry_streak"] == 1
    assert notices == []


def test_download_incomplete_with_verified_file_is_partial_and_retried(
    tmp_path, monkeypatch
):
    module = _load_action_module()
    downloaded = tmp_path / "verified-review.pdf"
    downloaded.write_bytes(b"%PDF-1.4\nfixture\n%%EOF\n")

    class FakeManager:
        last_navigation_error_code = ""
        last_download_error_code = "direct_download_incomplete"
        last_download_error_detail = "one control produced no complete PDF"
        last_download_error_events = [
            {
                "code": "direct_download_incomplete",
                "detail": "one control produced no complete PDF",
            }
        ]

        def __init__(self, **_kwargs):
            pass

        def login(self):
            return True

        def navigate_to_file_review(self):
            return True

        def check_and_download_available(self, target_case_number=None):
            return [str(downloaded)]

        def close(self):
            return None

    notices = []
    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "_get_credentials",
        lambda _cfg: {
            "username": "u",
            "password": "p",
            "download_folder": str(tmp_path),
        },
    )
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(
        module,
        "_ensure_imports",
        lambda: types.SimpleNamespace(FileReviewManager=FakeManager),
    )
    monkeypatch.setattr(module, "_eventlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_notify", lambda msg, *_a, **_k: notices.append(msg))
    monkeypatch.setattr(module, "_safe_flow_step_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_mark_notify_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_check_flow_cancelled", lambda *args, **kwargs: None)

    # A prior zero-download failure must be cleared by verifiable progress;
    # otherwise two more mixed sweeps would emit the false "no PDF" alert.
    module._record_download_failure_state(
        str(tmp_path), error_key="direct_download_incomplete"
    )
    result = module.cmd_download.__wrapped__(notify=True)

    assert result["success"] is True
    assert result["status"] == "partial"
    assert result["deferred"] is True
    assert result["downloaded_count"] == 1
    assert result["unresolved_count"] == 1
    assert result["retry_streak"] == 0
    assert not (tmp_path / ".download_failure_state.json").exists()
    assert notices == []


def test_clicked_row_registry_is_signature_bound_and_expires(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewManager,
    )

    monkeypatch.setenv("MAGI_FILE_REVIEW_ROW_RECHECK_MINUTES", "1440")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "1117279",
        "yyidno": "114年度原上訴字第160號",
        "isdown": "Y",
        "upddt": "2026/08/12 19:54",
    }
    mgr._register_rowid_clicked(
        "1117279",
        {"case_number": row["yyidno"], "_portal_row_json": row},
    )

    assert mgr._is_rowid_clicked("1117279", row_json=dict(row)) is True

    changed = dict(row, upddt="2026/08/13 08:00")
    assert mgr._is_rowid_clicked("1117279", row_json=changed) is False

    mgr._register_rowid_clicked(
        "1117279",
        {"case_number": row["yyidno"], "_portal_row_json": row},
    )
    mgr._clicked_rowids["1117279"]["last_clicked"] = (
        datetime.now() - timedelta(days=2)
    ).isoformat()
    assert mgr._is_rowid_clicked("1117279", row_json=row) is False


def test_clicked_row_registry_default_does_not_reclick_on_next_evening(
    tmp_path, monkeypatch
):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewManager,
    )

    monkeypatch.delenv("MAGI_FILE_REVIEW_ROW_RECHECK_MINUTES", raising=False)
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "stable-row",
        "yyidno": "synthetic-case",
        "isdown": "Y",
        "upddt": "2026/08/12 19:54",
    }
    mgr._register_rowid_clicked(
        "stable-row",
        {"case_number": row["yyidno"], "_portal_row_json": row},
    )
    mgr._clicked_rowids["stable-row"]["last_clicked"] = (
        datetime.now() - timedelta(days=2)
    ).isoformat()
    assert mgr._is_rowid_clicked("stable-row", row_json=row) is True

    # The bounded audit remains: even a portal that never changes its stable
    # signature is revalidated after thirty days.
    mgr._clicked_rowids["stable-row"]["last_clicked"] = (
        datetime.now() - timedelta(days=31)
    ).isoformat()
    assert mgr._is_rowid_clicked("stable-row", row_json=row) is False


def test_download_notice_receipt_suppresses_same_content_and_keeps_versions(
    tmp_path,
):
    module = _load_action_module()
    archived = tmp_path / "卷1.pdf"
    archived.write_bytes(b"%PDF-1.7\nfirst-version\n%%EOF\n")
    item = {
        "court_case_no": "synthetic-private-case",
        "file": "卷1.pdf",
        "dst": str(archived),
        "action": "moved",
    }

    first = module._prepare_download_notice(
        str(tmp_path), [item], [str(archived)], notify_requested=True
    )
    assert first["should_notify"] is True
    assert first["new_count"] == 1
    assert first["updated_count"] == 0
    module._complete_download_notice(str(tmp_path), first["event_digest"])

    repeated = module._prepare_download_notice(
        str(tmp_path), [item], [str(archived)], notify_requested=True
    )
    assert repeated["should_notify"] is False
    assert repeated["duplicate_count"] == 1

    ledger_path = tmp_path / module.DOWNLOAD_NOTICE_LEDGER_FILE
    ledger_text = ledger_path.read_text(encoding="utf-8")
    assert "synthetic-private-case" not in ledger_text
    assert "卷1.pdf" not in ledger_text
    assert str(tmp_path) not in ledger_text
    ledger = json.loads(ledger_text)
    history = next(iter(ledger["artifacts"].values()))["versions"]
    assert len(history) == 1
    assert ledger["pii_included"] is False
    assert ledger_path.stat().st_mode & 0o777 == 0o600


def test_download_notice_receipt_labels_changed_bytes_as_update(tmp_path):
    module = _load_action_module()
    archived = tmp_path / "卷1.pdf"
    archived.write_bytes(b"%PDF-1.7\nfirst-version\n%%EOF\n")
    first_item = {
        "court_case_no": "synthetic-case",
        "file": "卷1.pdf",
        "dst": str(archived),
        "action": "moved",
    }
    first = module._prepare_download_notice(
        str(tmp_path), [first_item], [str(archived)], notify_requested=True
    )
    module._complete_download_notice(str(tmp_path), first["event_digest"])

    archived.write_bytes(b"%PDF-1.7\nmaterially-updated-version\n%%EOF\n")
    update_item = {**first_item, "file": "卷1 (1).pdf"}
    updated = module._prepare_download_notice(
        str(tmp_path), [update_item], [str(archived)], notify_requested=True
    )
    assert updated["should_notify"] is True
    assert updated["new_count"] == 0
    assert updated["updated_count"] == 1
    assert updated["event_digest"] != first["event_digest"]

    ledger = json.loads(
        (tmp_path / module.DOWNLOAD_NOTICE_LEDGER_FILE).read_text(encoding="utf-8")
    )
    history = next(iter(ledger["artifacts"].values()))["versions"]
    assert len(history) == 2
    assert history[0]["content_sha256"] != history[1]["content_sha256"]


def test_download_notice_pending_delivery_is_retried_without_new_version(tmp_path):
    module = _load_action_module()
    archived = tmp_path / "卷2.pdf"
    archived.write_bytes(b"%PDF-1.7\nretry-me\n%%EOF\n")
    item = {
        "court_case_no": "synthetic-case",
        "file": "卷2.pdf",
        "dst": str(archived),
        "action": "moved",
    }
    first = module._prepare_download_notice(
        str(tmp_path), [item], [str(archived)], notify_requested=True
    )
    assert first["should_notify"] is True

    retry = module._prepare_download_notice(
        str(tmp_path), [item], [str(archived)], notify_requested=True
    )
    assert retry["should_notify"] is True
    assert retry["event_digest"] == first["event_digest"]
    assert retry["new_count"] == 1

    module._complete_download_notice(str(tmp_path), retry["event_digest"])
    final = module._prepare_download_notice(
        str(tmp_path), [item], [str(archived)], notify_requested=True
    )
    assert final["should_notify"] is False


def test_download_notice_disabled_run_does_not_consume_future_notice(tmp_path):
    module = _load_action_module()
    archived = tmp_path / "卷3.pdf"
    archived.write_bytes(b"%PDF-1.7\nnotify-later\n%%EOF\n")
    item = {
        "court_case_no": "synthetic-case",
        "file": "卷3.pdf",
        "dst": str(archived),
        "action": "moved",
    }
    silent = module._prepare_download_notice(
        str(tmp_path), [item], [str(archived)], notify_requested=False
    )
    assert silent["should_notify"] is False

    later = module._prepare_download_notice(
        str(tmp_path), [item], [str(archived)], notify_requested=True
    )
    assert later["should_notify"] is True
    assert later["new_count"] == 1


def test_download_notification_forwards_same_event_id_to_red_phone(monkeypatch):
    module = _load_action_module()
    captured = {}

    def send_status(_message, **kwargs):
        captured.update(kwargs)
        return {"telegram": True, "queued": False}

    monkeypatch.setitem(
        sys.modules,
        "skills.ops.red_phone",
        types.SimpleNamespace(send_telegram_push_with_status=send_status),
    )
    event_id = "a" * 64
    assert module._notify("📥 卷宗下載完成", event_id=event_id) is True
    assert captured["event_id"] == event_id
    assert captured["topic_key"] == "filereview"
    assert captured["source"] == "file_review_orchestrator"


def test_red_phone_event_id_dedup_is_shared_with_mirror(monkeypatch):
    from skills.ops import red_phone

    completed = set()
    send_calls = []
    mirror_events = []
    monkeypatch.setattr(
        red_phone,
        "_delivery_channel_done",
        lambda event_id, channel: (event_id, channel) in completed,
    )
    monkeypatch.setattr(
        red_phone,
        "_mark_delivery_channel_done",
        lambda event_id, channel: completed.add((event_id, channel)),
    )
    monkeypatch.setattr(red_phone, "_get_telegram_config", lambda: ("token", ["1"]))
    monkeypatch.setattr(
        red_phone, "_resolve_thread_id", lambda *_a, **_k: ("filereview", 0)
    )
    monkeypatch.setattr(red_phone, "_append_delivery_log", lambda *_a, **_k: None)
    monkeypatch.setattr(red_phone, "RED_PHONE_RETRY_COUNT", 0)

    def send_once(*_args, **_kwargs):
        send_calls.append(True)
        return {"ok_any": True, "acked": ["1"], "total": 1, "error": ""}

    def mirror(_message, **kwargs):
        mirror_events.append(kwargs["event_id"])
        return True

    monkeypatch.setattr(red_phone, "_send_telegram_once", send_once)
    monkeypatch.setattr(red_phone, "_mirror_to_discord", mirror)
    event_id = "b" * 64
    first = red_phone.send_telegram_push_with_status(
        "📥 卷宗下載完成", topic_key="filereview", event_id=event_id
    )
    second = red_phone.send_telegram_push_with_status(
        "📥 卷宗下載完成", topic_key="filereview", event_id=event_id
    )

    assert first["telegram"] is True and first["deduped"] is False
    assert second["telegram"] is True and second["deduped"] is True
    assert len(send_calls) == 1
    assert mirror_events == [event_id, event_id]
    assert (event_id, "telegram") in completed
    with pytest.raises(ValueError, match="event_id"):
        red_phone.send_telegram_push_with_status(
            "bad", event_id="0" * 63 + "G"
        )


def test_red_phone_outbox_dedups_by_event_not_same_message(monkeypatch):
    from skills.ops import red_phone

    outbox = []
    monkeypatch.setattr(red_phone, "_load_outbox", lambda: outbox)
    monkeypatch.setattr(
        red_phone,
        "_save_outbox",
        lambda rows: outbox.__setitem__(slice(None), [dict(row) for row in rows]),
    )
    monkeypatch.setattr(red_phone, "_append_delivery_log", lambda *_a, **_k: None)
    first_id = red_phone._enqueue_outbox(
        "same visible message", "info", "file_review_orchestrator", event_id="c" * 64
    )
    repeat_id = red_phone._enqueue_outbox(
        "same visible message", "info", "file_review_orchestrator", event_id="c" * 64
    )
    second_id = red_phone._enqueue_outbox(
        "same visible message", "info", "file_review_orchestrator", event_id="d" * 64
    )
    assert repeat_id == first_id
    assert second_id != first_id
    assert [row["event_id"] for row in outbox] == ["c" * 64, "d" * 64]


def test_download_notice_rejects_symlink_artifact_and_ledger(tmp_path):
    module = _load_action_module()
    actual = tmp_path / "actual.pdf"
    actual.write_bytes(b"%PDF-1.7\nactual\n%%EOF\n")
    linked = tmp_path / "linked.pdf"
    linked.symlink_to(actual)
    item = {
        "court_case_no": "synthetic-case",
        "file": "linked.pdf",
        "dst": str(linked),
        "action": "moved",
    }
    with pytest.raises(RuntimeError, match="final_artifact_invalid"):
        module._collect_download_notice_artifacts(
            [item], [str(linked)], download_folder=str(tmp_path)
        )

    hostile = tmp_path / "hostile-ledger.json"
    hostile.write_text("{}", encoding="utf-8")
    ledger_path = tmp_path / module.DOWNLOAD_NOTICE_LEDGER_FILE
    ledger_path.symlink_to(hostile)
    direct_item = {**item, "file": "actual.pdf", "dst": str(actual)}
    with pytest.raises(RuntimeError, match="ledger_not_regular"):
        module._prepare_download_notice(
            str(tmp_path), [direct_item], [str(actual)], notify_requested=True
        )


def test_download_notice_rejects_symlink_root_and_outside_regular_file(tmp_path):
    module = _load_action_module()
    real_root = tmp_path / "downloads"
    real_root.mkdir()
    alias_root = tmp_path / "downloads-alias"
    alias_root.symlink_to(real_root, target_is_directory=True)
    inside = real_root / "inside.pdf"
    inside.write_bytes(b"%PDF-1.7\ninside\n%%EOF\n")
    item = {
        "court_case_no": "synthetic-case",
        "file": "inside.pdf",
        "dst": str(inside),
        "action": "moved",
    }
    with pytest.raises(RuntimeError, match="root_not_canonical"):
        module._prepare_download_notice(
            str(alias_root), [item], [str(inside)], notify_requested=True
        )

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.7\noutside\n%%EOF\n")
    outside_item = {**item, "file": "outside.pdf", "dst": str(outside)}
    with pytest.raises(RuntimeError, match="final_artifact_invalid"):
        module._collect_download_notice_artifacts(
            [outside_item], [str(outside)], download_folder=str(real_root)
        )


def test_download_notice_rejects_source_and_final_artifact_mismatch(tmp_path):
    module = _load_action_module()
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    source = download_root / "卷4.pdf"
    source.write_bytes(b"%PDF-1.7\nsource-version\n%%EOF\n")
    source_receipt = {
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "size": source.stat().st_size,
    }
    case_root = tmp_path / "case"
    case_root.mkdir()
    final = case_root / "卷4.pdf"
    final.write_bytes(b"%PDF-1.7\nwrong-final-version\n%%EOF\n")
    item = {
        "court_case_no": "synthetic-case",
        "folder": str(case_root),
        "file": "卷4.pdf",
        "dst": str(final),
        "action": "moved",
    }
    with pytest.raises(RuntimeError, match="final_artifact_mismatch"):
        module._prepare_download_notice(
            str(download_root),
            [item],
            [str(source)],
            notify_requested=True,
            content_receipts={str(source): source_receipt, "卷4.pdf": source_receipt},
        )


def test_download_notice_accepts_matching_final_archive_receipt(tmp_path):
    module = _load_action_module()
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    source = download_root / "卷5.pdf"
    source.write_bytes(b"%PDF-1.7\nimmutable-version\n%%EOF\n")
    source_receipt = {
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "size": source.stat().st_size,
    }
    case_root = tmp_path / "case"
    case_root.mkdir()
    final = case_root / "卷5.pdf"
    final.write_bytes(source.read_bytes())
    item = {
        "court_case_no": "synthetic-case",
        "folder": str(case_root),
        "file": "卷5.pdf",
        "dst": str(final),
        "action": "moved",
    }
    notice = module._prepare_download_notice(
        str(download_root),
        [item],
        [str(source)],
        notify_requested=True,
        content_receipts={str(source): source_receipt, "卷5.pdf": source_receipt},
    )
    assert notice["valid"] is True
    assert notice["should_notify"] is True
    assert notice["new_count"] == 1


def test_red_phone_receipt_database_failure_is_not_accepted(monkeypatch):
    from skills.ops import red_phone

    send_calls = []
    mirror_calls = []
    monkeypatch.setattr(red_phone, "_get_telegram_config", lambda: ("token", ["1"]))
    monkeypatch.setattr(
        red_phone, "_resolve_thread_id", lambda *_a, **_k: ("filereview", 0)
    )
    monkeypatch.setattr(red_phone, "_append_delivery_log", lambda *_a, **_k: None)
    monkeypatch.setattr(red_phone, "RED_PHONE_RETRY_COUNT", 0)
    monkeypatch.setattr(
        red_phone,
        "_send_telegram_once",
        lambda *_a, **_k: send_calls.append(True)
        or {"ok_any": True, "acked": ["1"], "total": 1, "error": ""},
    )
    monkeypatch.setattr(
        red_phone,
        "_mirror_to_discord",
        lambda *_a, **_k: mirror_calls.append(True) or True,
    )
    event_id = "e" * 64

    fake_dedup_db = types.SimpleNamespace(
        is_done=lambda *_a, **_k: (_ for _ in ()).throw(
            ConnectionError("offline")
        ),
        mark_done=lambda *_a, **_k: False,
    )
    monkeypatch.setitem(sys.modules, "skills.ops.dedup_db", fake_dedup_db)
    with pytest.raises(RuntimeError, match="receipt_unavailable"):
        red_phone.send_telegram_push_with_status(
            "receipt preflight", event_id=event_id
        )
    assert send_calls == []

    fake_dedup_db.is_done = lambda *_a, **_k: False
    with pytest.raises(RuntimeError, match="receipt_not_persisted"):
        red_phone.send_telegram_push_with_status(
            "receipt commit", event_id=event_id
        )
    assert send_calls == [True]
    assert mirror_calls == []


def test_red_phone_discord_receipt_failure_is_not_accepted(monkeypatch):
    from skills.ops import red_phone

    monkeypatch.setenv("MAGI_DC_MIRROR_ENABLED", "1")
    monkeypatch.setattr(red_phone, "_delivery_channel_done", lambda *_a: False)
    monkeypatch.setattr(red_phone, "_send_discord_bot_message", lambda *_a, **_k: True)
    monkeypatch.setattr(
        red_phone,
        "_mark_delivery_channel_done",
        lambda *_a: (_ for _ in ()).throw(
            RuntimeError("notification_channel_receipt_not_persisted")
        ),
    )
    with pytest.raises(RuntimeError, match="receipt_not_persisted"):
        red_phone._mirror_to_discord(
            "📥 卷宗下載完成",
            topic_key="filereview",
            source="file_review_orchestrator",
            event_id="f" * 64,
        )


def test_single_case_identity_mismatch_is_quarantined_and_auto_retried(
    tmp_path, monkeypatch
):
    module = _load_action_module()

    # A previously accumulated generic download streak must not turn a
    # safely quarantined wrong-case court response into a recurring red alert.
    for _ in range(3):
        module._record_download_failure_state(
            str(tmp_path), error_key="case_identity_mismatch"
        )

    class FakeManager:
        last_navigation_error_code = ""
        last_download_error_code = "case_identity_mismatch"
        last_download_error_detail = "late artifact belongs to another portal row"
        last_download_error_events = [
            {
                "code": "case_identity_mismatch",
                "detail": "late artifact belongs to another portal row",
            }
        ]
        last_download_processed_signature_hashes = set()
        last_download_verified_existing_signature_hashes = set()
        last_download_mismatch_deferred_signature_hashes = {"a" * 64}

        def __init__(self, **_kwargs):
            pass

        def login(self):
            return True

        def navigate_to_file_review(self):
            return True

        def check_and_download_available(self, target_case_number=None):
            return []

        def close(self):
            return None

    notices = []
    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "_get_credentials",
        lambda _cfg: {
            "username": "u",
            "password": "p",
            "download_folder": str(tmp_path),
        },
    )
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(
        module,
        "_ensure_imports",
        lambda: types.SimpleNamespace(FileReviewManager=FakeManager),
    )
    monkeypatch.setattr(module, "_eventlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_notify", lambda msg, *_a, **_k: notices.append(msg))
    monkeypatch.setattr(module, "_safe_flow_step_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_mark_notify_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_check_flow_cancelled", lambda *args, **kwargs: None)

    result = module.cmd_download.__wrapped__(notify=True)

    assert result["success"] is True
    assert result["status"] == "deferred"
    assert result["deferred"] is True
    assert result["reason"] == "court_payload_identity_mismatch"
    assert result["unresolved_count"] == 1
    assert result["retry_streak"] == 0
    assert result["mismatch_deferred_portal_signature_hashes"] == ["a" * 64]
    assert result["mismatch_deferred_portal_signature_set_hash"] == (
        module.signature_set_hash(["a" * 64])
    )
    assert "已隔離且未歸檔" in result["message"]
    assert not (tmp_path / ".download_failure_state.json").exists()
    assert notices == []


def test_case_identity_mismatch_dominates_same_sweep_transient_noise(
    tmp_path, monkeypatch
):
    module = _load_action_module()

    for _ in range(3):
        module._record_download_failure_state(
            str(tmp_path), error_key="direct_download_incomplete"
        )

    class FakeManager:
        last_navigation_error_code = ""
        last_download_error_code = "case_identity_mismatch"
        last_download_error_detail = "late artifact belongs to another portal row"
        last_download_error_events = [
            {
                "code": "direct_download_incomplete",
                "detail": "download control produced no complete PDF",
            },
            {
                "code": "case_identity_mismatch",
                "detail": "late artifact belongs to another portal row",
            },
        ]

        def __init__(self, **_kwargs):
            pass

        def login(self):
            return True

        def navigate_to_file_review(self):
            return True

        def check_and_download_available(self, target_case_number=None):
            return []

        def close(self):
            pass

    fake_mod = types.SimpleNamespace(FileReviewManager=FakeManager)
    monkeypatch.setattr(module, "_ensure_imports", lambda: fake_mod)
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: object())
    monkeypatch.setattr(
        module,
        "_get_credentials",
        lambda _cfg: {
            "username": "u",
            "password": "p",
            "download_folder": str(tmp_path),
        },
    )
    notices = []
    monkeypatch.setattr(module, "_notify", lambda msg, *_a, **_k: notices.append(msg))
    monkeypatch.setattr(module, "_safe_flow_step_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_mark_notify_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_check_flow_cancelled", lambda *args, **kwargs: None)

    result = module.cmd_download.__wrapped__(notify=True)

    assert result["success"] is True
    assert result["status"] == "deferred"
    assert result["reason"] == "court_payload_identity_mismatch"
    assert result["unresolved_count"] == 1
    assert result["retry_streak"] == 0
    assert not (tmp_path / ".download_failure_state.json").exists()
    assert notices == []


def test_download_zero_new_files_emits_verified_existing_receipt(
    tmp_path, monkeypatch
):
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "direct-existing", 7)

    class FakeManager:
        last_navigation_error_code = ""
        last_download_error_code = ""
        last_download_error_detail = ""
        last_download_error_events = []
        last_download_deferred = False
        last_download_deferred_reason = ""
        last_download_total_count = 7
        last_download_processed_count = 7

        def __init__(self, **_kwargs):
            self._last_archive_report = {}
            self._last_smart_skipped_files = [
                {"file": f"existing-{index}.pdf", "existing_path": str(tmp_path)}
                for index in range(7)
            ]
            self.last_download_processed_signature_hashes = set()
            self.last_download_verified_existing_signature_hashes = set(
                portal_receipt["portal_download_signature_hashes"]
            )

        def login(self):
            return True

        def navigate_to_file_review(self):
            return True

        def check_and_download_available(self, target_case_number=None):
            return []

        def close(self):
            return None

    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "_get_credentials",
        lambda _cfg: {
            "username": "u",
            "password": "p",
            "download_folder": str(tmp_path),
        },
    )
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(
        module,
        "_ensure_imports",
        lambda: types.SimpleNamespace(FileReviewManager=FakeManager),
    )
    monkeypatch.setattr(module, "_eventlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_safe_flow_step_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_mark_notify_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_check_flow_cancelled", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_cleanup_all_download_folders", lambda *_args: None)

    result = module.cmd_download.__wrapped__(notify=False)

    assert result["success"] is True
    assert result["downloaded_count"] == 0
    assert result["verified_existing_count"] == 7
    assert result["accounted_downloadable_count"] == 7
    assert result["processed_count"] == 7
    assert result["total_count"] == 7
    assert result["remaining_count"] == 0
    assert result["verified_existing_portal_signature_hashes"] == portal_receipt[
        "portal_download_signature_hashes"
    ]
    assert result["handled_portal_signature_set_hash"] == module.signature_set_hash(
        portal_receipt["portal_download_signature_hashes"]
    )


def test_preclick_smart_skip_records_verified_existing_portal_signature():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewManager,
    )

    manager = object.__new__(FileReviewManager)
    manager.last_download_verified_existing_signature_hashes = set()

    manager._record_verified_existing_portal_signature("row-signature-1")
    manager._record_verified_existing_portal_signature("row-signature-1")
    manager._record_verified_existing_portal_signature("")

    assert manager.last_download_verified_existing_signature_hashes == {
        "row-signature-1"
    }


def test_cross_case_cooldown_records_separate_anonymous_portal_signature():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewManager,
    )

    manager = object.__new__(FileReviewManager)
    manager.last_download_mismatch_deferred_signature_hashes = set()

    manager._record_mismatch_deferred_portal_signature("row-signature-1")
    manager._record_mismatch_deferred_portal_signature("row-signature-1")
    manager._record_mismatch_deferred_portal_signature("")

    assert manager.last_download_mismatch_deferred_signature_hashes == {
        "row-signature-1"
    }


def test_download_time_budget_exhaustion_is_deferred_not_complete(
    tmp_path, monkeypatch
):
    module = _load_action_module()

    class FakeManager:
        last_navigation_error_code = ""
        last_download_error_code = ""
        last_download_error_detail = ""
        last_download_error_events = []
        last_download_deferred = True
        last_download_deferred_reason = "download_time_budget_exhausted"
        last_download_total_count = 8
        last_download_processed_count = 4

        def __init__(self, **_kwargs):
            self._last_archive_report = {}
            self._last_smart_skipped_files = []

        def login(self):
            return True

        def navigate_to_file_review(self):
            return True

        def check_and_download_available(self, target_case_number=None):
            return []

        def close(self):
            return None

    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "_get_credentials",
        lambda _cfg: {
            "username": "u",
            "password": "p",
            "download_folder": str(tmp_path),
        },
    )
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(
        module,
        "_ensure_imports",
        lambda: types.SimpleNamespace(FileReviewManager=FakeManager),
    )
    monkeypatch.setattr(module, "_eventlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_safe_flow_step_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_mark_notify_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_check_flow_cancelled", lambda *args, **kwargs: None)

    result = module.cmd_download.__wrapped__(notify=False)

    assert result["success"] is True
    assert result["status"] == "deferred"
    assert result["deferred"] is True
    assert result["reason"] == "download_time_budget_exhausted"
    assert result["processed_count"] == 4
    assert result["total_count"] == 8
    assert result["remaining_count"] == 4


def test_download_incomplete_alerts_only_after_consecutive_threshold(
    tmp_path, monkeypatch
):
    module = _load_action_module()
    monkeypatch.setenv("MAGI_FILE_REVIEW_DOWNLOAD_FAILURE_NOTIFY_STREAK", "3")

    first = module._record_download_failure_state(
        str(tmp_path), error_key="direct_download_incomplete"
    )
    second = module._record_download_failure_state(
        str(tmp_path), error_key="direct_download_incomplete"
    )
    third = module._record_download_failure_state(
        str(tmp_path), error_key="direct_download_incomplete"
    )

    assert first["failure_streak"] == 1 and first["should_alert"] is False
    assert second["failure_streak"] == 2 and second["should_alert"] is False
    assert third["failure_streak"] == 3 and third["should_alert"] is True


def _write_scheduled_fixture(tmp_path: Path) -> tuple[Path, Path]:
    fixture = tmp_path / "fixture"
    (fixture / "portal").mkdir(parents=True)
    (fixture / "state").mkdir()
    (fixture / "runtime").mkdir()
    (fixture / ".magi-v3-schedule-fixture").write_text(
        "job_file_review_check\n", encoding="utf-8"
    )
    (fixture / "portal" / "卷證A.pdf").write_bytes(b"fixture-review-pdf")
    (fixture / "portal" / "卷證A-重複.pdf").write_bytes(b"fixture-review-pdf")
    provider = fixture / "file-review-provider.json"
    provider.write_text(
        json.dumps(
            {
                "schema": "magi.v3.file-review-scheduled-fixture/v1",
                "emails": [
                    {"kind": "downloadable", "case_number": "2026-0001"},
                    {"kind": "willingness_inquiry", "case_number": ""},
                ],
                "portal_files": ["portal/卷證A.pdf", "portal/卷證A-重複.pdf"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return fixture, provider


def test_scheduled_fixture_runs_formal_child_to_dynamic_terminal_receipt(tmp_path):
    fixture, provider = _write_scheduled_fixture(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "MAGI_DISABLE_SKILL_VENV_REEXEC": "1",
            "MAGI_DISABLE_NOTIFICATIONS": "1",
            "MAGI_V3_SCHEDULE_ADAPTER": "real_entrypoint_fixture_v1",
            "MAGI_V3_SCHEDULE_DRY_RUN": "1",
            "MAGI_V3_SCHEDULE_FIXTURE_ROOT": str(fixture),
            "MAGI_FILE_REVIEW_SCHEDULE_FIXTURE_PATH": str(provider),
            "MAGI_FILE_REVIEW_STATE_DIR": str(fixture / "state"),
            "MAGI_RUNTIME_DIR": str(fixture / "runtime"),
            "MAGI_ROOT_DIR": str(MODULE_PATH.parents[2]),
            "PYTHONPATH": str(MODULE_PATH.parents[2]),
            "PYTHONNOUSERSITE": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--task", 'scheduled_check {"notify":false}'],
        env=env,
        cwd=str(MODULE_PATH.parents[2]),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    payload = json.loads(completed.stdout)
    steps = payload["steps"]
    assert steps["check_emails"]["willingness_inquiries_excluded"] == 1
    assert steps["download"]["downloaded_count"] == 1
    assert steps["download"]["duplicate_count"] == 1
    assert steps["download"]["child_terminal"] is True
    assert steps["download"]["child_status"] == "done"
    assert steps["download"]["pid"] != os.getpid()
    receipts = list((fixture / "state" / "formal-receipts").glob("*.json"))
    assert len(receipts) >= 5
    receipt_ids = {
        json.loads(path.read_text(encoding="utf-8"))["receipt_id"] for path in receipts
    }
    assert len(receipt_ids) == len(receipts)


def test_scheduled_fixture_rejects_fake_formal_handler(tmp_path, monkeypatch):
    module = _load_action_module()
    fixture, provider = _write_scheduled_fixture(tmp_path)
    monkeypatch.setenv("MAGI_V3_SCHEDULE_ADAPTER", "real_entrypoint_fixture_v1")
    monkeypatch.setenv("MAGI_V3_SCHEDULE_DRY_RUN", "1")
    monkeypatch.setenv("MAGI_V3_SCHEDULE_FIXTURE_ROOT", str(fixture))
    monkeypatch.setenv("MAGI_FILE_REVIEW_SCHEDULE_FIXTURE_PATH", str(provider))
    monkeypatch.setattr(module, "cmd_check_emails", lambda **_kwargs: {"success": True})

    with pytest.raises(RuntimeError, match="formal handler rejected"):
        module.cmd_scheduled_check(notify=False)


def test_file_review_check_summaries_are_quiet_cron_only():
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert '_notify(section_msg, True, topic_key="quiet_cron")' in source
    assert "effective_topic = section_topic" not in source


def test_file_review_cron_uses_complete_scheduled_check():
    source = (MODULE_PATH.parent.parent.parent / "scripts" / "seed_cron_jobs.py").read_text(encoding="utf-8")

    assert '"id": "job_file_review_check"' in source
    assert '"scheduled_check"' in source
    assert '"--task", "download")' not in source


def test_scheduled_check_bounds_download_inside_outer_cron_envelope(monkeypatch):
    module = _load_action_module()
    portal_receipt = _portal_receipt(module, "budget", 1)
    observed = {}

    def invoke(name, _handler, **_kwargs):
        if name == "check_emails":
            return {
                "success": True,
                "portal_probe_ok": True,
                "portal_downloadable_count": 1,
                "portal_pending_payment_count": 0,
                **portal_receipt,
            }
        if name == "download":
            observed["budget"] = os.environ.get(
                "MAGI_FILE_REVIEW_DOWNLOAD_MAX_RUNTIME_SEC"
            )
            return {
                "success": True,
                "status": "done",
                "deferred": False,
                "downloaded_count": 1,
                **_download_signature_receipt(
                    module,
                    processed=portal_receipt["portal_download_signature_hashes"],
                ),
            }
        return {"success": True, "status": "done", "deferred": False}

    monkeypatch.delenv("MAGI_FILE_REVIEW_DOWNLOAD_MAX_RUNTIME_SEC", raising=False)
    monkeypatch.setattr(module, "_scheduled_check_fixture_provider", lambda: None)
    monkeypatch.setattr(module, "_invoke_scheduled_formal_handler", invoke)
    monkeypatch.setattr(module, "_publish_scheduled_check_state", lambda _result: None)

    result = module.cmd_scheduled_check(notify=False)

    assert result["success"] is True
    assert observed["budget"] == "540"
    assert "MAGI_FILE_REVIEW_DOWNLOAD_MAX_RUNTIME_SEC" not in os.environ


def test_download_resume_cursor_persists_only_hashes(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewManager,
    )

    manager = object.__new__(FileReviewManager)
    manager.download_folder = str(tmp_path)
    manager.log = lambda _message: None
    raw_case = "115年度測字第123號-測試當事人"
    button_hash = FileReviewManager._download_resume_hash("卷宗一.pdf")

    manager._mark_download_resume_button_completed(raw_case, button_hash)

    cursor_path = tmp_path / ".file_review_download_resume.json"
    cursor_text = cursor_path.read_text(encoding="utf-8")
    assert raw_case not in cursor_text
    assert "測試當事人" not in cursor_text
    assert manager._download_resume_completed_buttons(raw_case) == {button_hash}

    manager._clear_download_resume_state()
    assert not cursor_path.exists()


def _roc_compact(days_from_now: int = 3) -> str:
    dt = datetime.now() + timedelta(days=days_from_now)
    return f"{dt.year - 1911:03d}{dt.month:02d}{dt.day:02d}"


def test_file_review_download_expiry_requires_explicit_portal_label():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewManager,
    )

    assert FileReviewManager._is_download_deadline_expired_text(
        "已於115/05/29超過下載期限"
    )
    assert not FileReviewManager._is_download_deadline_expired_text(
        "下載期限：115/12/31"
    )
    assert not FileReviewManager._is_download_deadline_expired_text(
        "繳費期限已過，但卷宗仍可線上下載"
    )


def test_processed_payment_registry_retries_pdf_without_delivery_receipt(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    pdf = tmp_path / "繳費單_吳志炳_114.原交易.000049.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "995588",
        "yyidno": "114.原交易.000049",
        "showyyidno": "114年度原交易字第000049號",
        "clnm": "吳志炳",
        "paylimitdt": _roc_compact(3),
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }
    mgr.payment_registry = {
        "rowid:995588": {
            "processed_at": "2026-04-10T14:04:02",
            "yyidno": "114.原交易.000049",
            "case_number": "114.原交易.000049",
            "rowid": "995588",
            "party": "吳志炳",
            "files": [pdf.name],
            "file_paths": [str(pdf)],
        }
    }

    delivered = []
    with patch.object(mgr, "notify_payment_needed", side_effect=lambda info: delivered.append(info) or True):
        assert mgr._notify_payment_if_needed(row, case_info={"party": "吳志炳"}, file_paths=None) is True

    assert delivered and delivered[0].files == [str(pdf)]
    saved = json.loads((tmp_path / "notified_cases.json").read_text(encoding="utf-8"))
    assert "web_payment:case:114原交易49:吳志炳" in saved


def test_processed_payment_registry_skips_only_after_pdf_delivery_receipt(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    pdf = tmp_path / "繳費單_測試_115.訴.000001.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "delivery-receipt-row",
        "yyidno": "115.訴.000001",
        "showyyidno": "115年度訴字第1號",
        "clnm": "測試",
        "paylimitdt": _roc_compact(3),
    }
    mgr.payment_registry = {
        "rowid:delivery-receipt-row": {
            "rowid": "delivery-receipt-row",
            "yyidno": "115.訴.000001",
            "party": "測試",
            "files": [pdf.name],
            "file_paths": [str(pdf)],
        }
    }
    mgr._record_payment_pdf_delivery(str(pdf), sent=True, caption="verified")

    with patch.object(mgr, "notify_payment_needed", side_effect=AssertionError("must not resend delivered PDF")):
        assert mgr._notify_payment_if_needed(row, case_info={"party": "測試"}, file_paths=None) is True

    saved = json.loads((tmp_path / "notified_cases.json").read_text(encoding="utf-8"))
    assert "web_payment:case:115訴1:測試" in saved


def test_payment_notice_requires_actual_pdf_before_dedup(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "lin-row",
        "yyidno": "115.原交易.000021",
        "showyyidno": "115年度原交易字第21號",
        "clnm": "林建豐",
        "paylimitdt": _roc_compact(3),
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }

    assert mgr._notify_payment_if_needed(row, case_info={"party": "林建豐"}, file_paths=[]) is False
    notified_path = tmp_path / "notified_cases.json"
    if notified_path.exists():
        saved = json.loads(notified_path.read_text(encoding="utf-8"))
        assert "web_payment:115年度原交易字第21號" not in saved
        assert "web_payment:case:115原交易21:林建豐" not in saved


def test_recent_payment_activity_retries_when_pdf_not_delivered(tmp_path):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_林建豐_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    record = {
        "processed_at": datetime.now(),
        "party": "林建豐",
        "case_number": "115年度原交易字第000021號",
        "detail": "已下載繳費單（1 份）",
        "count": 1,
        "artifact_type": "payment_slip",
        "source": "payment_registry",
        "key": "rowid:1075000",
        "file_paths": [str(pdf)],
    }
    fp = module._recent_activity_fingerprint(record)
    (tmp_path / ".recent_activity_notified.json").write_text(
        json.dumps({
            "version": 1,
            "recent_payment_activity": {fp: "2026-06-22T10:30:41"},
            "recent_review_download_activity": {},
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    fresh = module._filter_unnotified_recent_activity(
        [record],
        str(tmp_path),
        "recent_payment_activity",
    )

    assert fresh == [record]


def test_recent_payment_activity_does_not_skip_legacy_case_only_proof(tmp_path, monkeypatch):
    module = _load_action_module()
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False),
    )
    pdf = tmp_path / "441403005422.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "payment_proof_registry.json").write_text(
        json.dumps(
            {
                "115.上訴.003543": {
                    "uploaded_at": "2026-06-30T14:32:49",
                    "court_code": "TPH",
                    "file": "discord_d528d01e2e9b4c6b8b8567bb2f25e38b.png",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "payment_registry.json").write_text(
        json.dumps(
            {
                "case:115上訴3543": {
                    "processed_at": datetime.now().isoformat(),
                    "case_number": "115.上訴.003543",
                    "party": "游秀鈴",
                    "file_paths": [str(pdf)],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert len(module._load_recent_payment_activity(str(tmp_path), days=7)) == 1

    monkeypatch.setattr(
        module,
        "_payment_pdf_text",
        lambda _path: "規費繳款單\n案 號：115 年 上訴 字 003543 號\n應繳款人：喬政翔",
    )

    assert len(module._load_recent_unregistered_payment_pdfs(str(tmp_path), days=2)) == 1


def test_payment_pdf_scan_reads_only_explicit_manual_import_root(monkeypatch, tmp_path):
    module = _load_action_module()
    managed = tmp_path / "managed"
    manual = tmp_path / "manual-import"
    managed.mkdir()
    manual.mkdir()
    pdf = manual / "manual-payment.pdf"
    pdf.write_bytes(b"%PDF-1.4\nsynthetic\n")
    monkeypatch.setenv("MAGI_FILE_REVIEW_IMPORT_DIRS", str(manual))
    monkeypatch.setattr(module, "_payment_pdf_text", lambda _path: "規費繳款單\n案號：115年訴字第000123號")
    monkeypatch.setattr(module, "_payment_proof_already_uploaded", lambda *_args: False)

    records = module._load_recent_unregistered_payment_pdfs(str(managed), days=2)

    assert len(records) == 1
    assert records[0]["source"] == "payment_pdf_scan"
    assert records[0]["artifact_type"] == "payment_slip"
    assert records[0]["case_number"].endswith("000123號")
    assert module._payment_pdf_scan_roots(str(managed)) == [str(managed), str(manual)]


def test_payment_notice_rejects_pdf_extension_with_non_pdf_payload(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    bad_pdf = tmp_path / "繳費單_梁志祥_114.交上易.000014.pdf"
    bad_pdf.write_text('{"messageText":"銷帳編號取號失敗"}', encoding="utf-8")
    ignored_dir = tmp_path / "_ignored_downloads" / "20260622"
    ignored_dir.mkdir(parents=True)
    quarantined_pdf = ignored_dir / "繳費單_梁志祥_114.交上易.000014.invalid_artifact.pdf"
    quarantined_pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "liang-row",
        "yyidno": "114.交上易.000014",
        "showyyidno": "114年度交上易字第000014號",
        "clnm": "梁志祥",
        "paylimitdt": _roc_compact(3),
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }

    with patch.object(mgr, "notify_payment_needed", side_effect=AssertionError("must not notify invalid PDF")):
        assert mgr._notify_payment_if_needed(
            row,
            case_info={"party": "梁志祥", "case_number": "114.交上易.000014"},
            file_paths=[str(bad_pdf)],
        ) is False
        assert mgr._notify_payment_if_needed(
            row,
            case_info={"party": "梁志祥", "case_number": "114.交上易.000014"},
            file_paths=[str(quarantined_pdf)],
        ) is False

    module = _load_action_module()
    assert module._is_valid_payment_pdf_file(str(quarantined_pdf)) is False

    notified_path = tmp_path / "notified_cases.json"
    if notified_path.exists():
        saved = json.loads(notified_path.read_text(encoding="utf-8"))
        assert "web_payment:case:114交上易14:梁志祥" not in saved


def test_legacy_unpadded_text_notice_does_not_suppress_new_pdf(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    (tmp_path / "notified_cases.json").write_text(
        json.dumps({"web_payment:114年度交上易字第14號": "2026-06-10T16:08:26"}, ensure_ascii=False),
        encoding="utf-8",
    )
    pdf = tmp_path / "繳費單_梁志祥_114.交上易.000014.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "1086357",
        "yyidno": "114.交上易.000014",
        "showyyidno": "114年度交上易字第000014號",
        "clnm": "梁志祥",
        "paylimitdt": _roc_compact(3),
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }

    assert "web_payment:case:114交上易14" in mgr.notified_cases
    with patch.object(mgr, "notify_payment_needed", return_value=True) as notify:
        assert mgr._notify_payment_if_needed(
            row,
            case_info={"party": "梁志祥", "case_number": "114.交上易.000014"},
            file_paths=[str(pdf)],
        ) is True
    notify.assert_called_once()


def test_notify_payment_needed_without_pdf_is_not_delivery(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewInfo, FileReviewManager

    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        client_name="林建豐",
        court="臺灣臺東地方法院",
        court_case_no="115年度原交易字第999999號",
        status="待繳費",
        payment_deadline="",
        files=[],
    )

    assert mgr.notify_payment_needed(info) is False


def test_gmail_payment_notice_downloads_portal_pdf_before_marking_email_processed(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewInfo, FileReviewManager

    pdf = tmp_path / "繳費單_李秀花_115年度原聲再字第2號.pdf"
    pdf.write_bytes(b"%PDF-1.4\nportal payment slip\n")
    marked = []
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(
            is_done=lambda *_args, **_kwargs: False,
            mark_done=lambda category, key, metadata=None: marked.append((category, key, metadata)) or True,
        ),
    )

    mgr = FileReviewManager(username="u", password="p", download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        client_name="李秀花",
        court="臺灣高等法院花蓮分院",
        court_case_no="115年度原聲再字第2號",
        status="待繳費",
        payment_deadline="",
        files=[],
        message_id="msg-portal-payment",
        source_message_ids=["msg-portal-payment"],
    )
    assert mgr._queue_pending_payment_notice(info) is True
    monkeypatch.setattr(mgr, "login", lambda: setattr(mgr, "logged_in", True) or True)
    monkeypatch.setattr(mgr, "navigate_to_file_review", lambda: True)
    download_calls = []
    monkeypatch.setattr(
        mgr,
        "download_all_payment_slips",
        lambda **kwargs: download_calls.append(kwargs) or [{
            "case_number": "115年度原聲再字第2號",
            "party": "李秀花",
            "court": "臺灣高等法院花蓮分院",
            "rowid": "row-1",
            "payid": "pay-1",
            "pdf_path": str(pdf),
            "all_paths": [str(pdf)],
        }],
    )
    delivered = []
    monkeypatch.setattr(mgr, "notify_payment_needed", lambda notice: delivered.append(notice) or True)

    stats = mgr._download_queued_payment_notices_from_portal()

    assert stats["attempted"] == 1
    assert stats["downloaded"] == 1
    assert stats["notified"] == 1
    assert "msg-portal-payment" in mgr.processed_emails
    assert download_calls == [{"max_days": 14, "target_case_number": "115年度原聲再字第2號"}]
    assert delivered and delivered[0].files == [str(pdf)]
    assert any(key.startswith("web_payment:case:115原聲再2") for _cat, key, _meta in marked)


def test_payment_slip_download_uses_review_list_frame_helper(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    class _Driver:
        def implicitly_wait(self, _timeout):
            return None

        def find_element(self, *_args, **_kwargs):
            raise AssertionError("download_all_payment_slips should use _open_review_list_v1")

        def execute_script(self, script, *_args):
            if "function getRowJson" in str(script):
                return []
            return None

    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.driver = _Driver()
    opened = []
    monkeypatch.setattr(mgr, "_open_review_list_v1", lambda: opened.append(True) or True)

    result = mgr.download_all_payment_slips(max_days=14, target_case_number="113年度訴字第253號")

    assert result == []
    assert opened == [True]


def test_payment_slip_download_retries_registry_and_notice_without_pdf(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators import file_review_automation as fra

    row = {
        "rowid": "missing-pdf-row",
        "yyidno": "115.原交易.000021",
        "showyyidno": "115年度原交易字第000021號",
        "clnm": "測試當事人",
        "paylimitdt": _roc_compact(3),
        "paystatus": "1",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }

    class _Switch:
        def default_content(self):
            return None

    class _Driver:
        switch_to = _Switch()

        def implicitly_wait(self, _timeout):
            return None

        def execute_script(self, script, *_args):
            if "function getRowJson" in str(script):
                return [{"idx": 0, "row_text": "法院回覆同意｜待繳費", "row_json": row}]
            if "var rows = document.querySelectorAll" in str(script):
                return object()
            return None

    mgr = fra.FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.driver = _Driver()
    mgr.payment_registry = {
        "rowid:missing-pdf-row": {
            "rowid": "missing-pdf-row",
            "files": [],
            "file_paths": [],
        }
    }
    mgr.notified_cases.add("web_payment:rowid:missing-pdf-row")
    monkeypatch.setattr(mgr, "_open_review_list_v1", lambda: True)
    monkeypatch.setattr(mgr, "_switch_to_review_list_v1", lambda: True)
    monkeypatch.setattr(mgr, "_is_fee_exempt_case", lambda **_kwargs: False)
    # This unit test proves that stale registry/notice state cannot suppress a
    # fresh PDF download.  Do not let it discover unrelated files on the LIVE
    # NAS: that turns a deterministic test into a slow external-I/O probe.
    monkeypatch.setattr(mgr, "_find_existing_payment_slip_files", lambda _row_json: [])
    monkeypatch.setattr(fra.time, "sleep", lambda _seconds: None)

    def _download(_row_elem, _row_json, folder, _before):
        pdf = Path(folder) / "繳費單.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        return [pdf.name]

    monkeypatch.setattr(mgr, "_download_payment_slip_direct", _download)

    result = mgr.download_all_payment_slips(max_days=14)

    assert len(result) == 1
    assert result[0]["already_existed"] is False
    assert Path(result[0]["pdf_path"]).is_file()


def test_portal_action_flags_are_not_payment_proof(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "paystatus": "1",
        "payment": "Y",
        "p_status": "Y",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "請下載繳費單繳費",
    }

    assert mgr._has_payment_proof_uploaded(row) is False


def test_notify_payment_needed_does_not_treat_queued_text_as_delivered(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewInfo,
        FileReviewManager,
    )

    pdf = tmp_path / "繳費單_凡江_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        client_name="凡江",
        court="臺灣花蓮地方法院",
        court_case_no="115年度原交易字第21號",
        status="待繳費",
        payment_deadline="2026-07-01",
        files=[str(pdf)],
    )

    class FakeNotifier:
        def notify_admin_with_files(self, *_args, **_kwargs):
            return False

    fake_line_notifier = types.SimpleNamespace(LAFNotifier=lambda: FakeNotifier())
    fake_red_phone = types.SimpleNamespace(
        send_telegram_push_with_status=lambda *_args, **_kwargs: {
            "telegram": False,
            "delivered": False,
            "queued": True,
            "outbox_id": "queued-1",
        }
    )
    monkeypatch.setitem(sys.modules, "line_notifier", fake_line_notifier)
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_red_phone)
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False),
    )

    assert mgr.notify_payment_needed(info) is False


def test_notify_payment_needed_respects_suppress_notify(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewInfo,
        FileReviewManager,
    )

    pdf = tmp_path / "繳費單_凡江_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        client_name="凡江",
        court="臺灣花蓮地方法院",
        court_case_no="115年度原交易字第21號",
        status="待繳費",
        payment_deadline="2026-07-01",
        files=[str(pdf)],
    )

    class FakeNotifier:
        def notify_admin_with_files(self, *_args, **_kwargs):
            raise AssertionError("suppressed notification should not send files")

        def notify_admin(self, *_args, **_kwargs):
            raise AssertionError("suppressed notification should not send text")

    fake_line_notifier = types.SimpleNamespace(LAFNotifier=lambda: FakeNotifier())
    fake_red_phone = types.SimpleNamespace(
        send_telegram_push_with_status=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("suppressed notification should not use red_phone")
        ),
        send_discord_bot_file=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("suppressed notification should not use Discord fallback")
        ),
    )
    monkeypatch.setenv("MAGI_FILE_REVIEW_SUPPRESS_NOTIFY", "1")
    monkeypatch.setitem(sys.modules, "line_notifier", fake_line_notifier)
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_red_phone)
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False),
    )

    assert mgr.notify_payment_needed(info) is False


def test_archived_payment_slip_suppresses_repeat_download_and_reseeds_registry(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    case_folder = tmp_path / "2025-0133-吳志炳-一審-公共危險"
    review_folder = case_folder / "02_閱卷資料" / "20260515"
    review_folder.mkdir(parents=True)
    pdf = review_folder / "繳費單_吳志炳_114.原交易.000049.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    mgr = FileReviewManager(download_folder=str(tmp_path / "downloads"), headless=True)
    mgr._resolve_case_folder = lambda info: str(case_folder)

    row = {
        "rowid": "fresh-rowid",
        "yyidno": "114.原交易.000049",
        "showyyidno": "114年度原交易字第000049號",
        "clnm": "吳志炳",
        "paylimitdt": _roc_compact(3),
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }

    assert mgr._is_payment_processed(row) is True
    entry = mgr.payment_registry["rowid:fresh-rowid"]
    assert entry["file_paths"] == [str(pdf)]
    assert entry["party"] == "吳志炳"
    assert not (tmp_path / "downloads" / "notified_cases.json").exists()


def test_portal_pending_payment_retries_after_legacy_text_notice(tmp_path):
    module = _load_action_module()
    (tmp_path / "notified_cases.json").write_text(
        json.dumps({"web_payment:114年度原交易字第000049號": "2026-04-10T14:04:02"}, ensure_ascii=False),
        encoding="utf-8",
    )
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "result_text": "待繳費",
        "row_text": "法院回覆同意｜待繳費，請下載繳費單繳費",
        "party": "吳志炳",
        "court_case_no": "114年度原交易字第000049號",
        "rowid": "995588",
        "pay_deadline": _roc_compact(3),
    }

    groups = module._filter_urgent_pending_payments(
        [item],
        days=14,
        download_folder=str(tmp_path),
    )

    assert groups == {"overdue": [], "urgent": [item], "unknown": []}


def test_portal_pending_payment_retries_after_unpadded_text_notice(tmp_path):
    module = _load_action_module()
    (tmp_path / "notified_cases.json").write_text(
        json.dumps({"web_payment:114年度交上易字第14號": "2026-06-10T16:08:26"}, ensure_ascii=False),
        encoding="utf-8",
    )
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "result_text": "待繳費",
        "row_text": "法院回覆同意｜待繳費，請下載繳費單繳費",
        "party": "梁志祥",
        "court_case_no": "114年度交上易字第000014號",
        "case_number": "114.交上易.000014",
        "pay_deadline": _roc_compact(3),
    }

    groups = module._filter_urgent_pending_payments(
        [item],
        days=14,
        download_folder=str(tmp_path),
    )

    assert groups == {"overdue": [], "urgent": [item], "unknown": []}


def test_portal_pending_payment_waits_for_pdf_delivery_receipt(tmp_path):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_吳志炳_114.原交易.000049.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    registry_path = Path(module.get_payment_registry_path(str(tmp_path)))
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps({
            "rowid:995588": {
                "case_number": "114.原交易.000049",
                "party": "吳志炳",
                "files": [pdf.name],
                "file_paths": [str(pdf)],
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "result_text": "待繳費",
        "row_text": "法院回覆同意｜待繳費，請下載繳費單繳費",
        "party": "吳志炳",
        "court_case_no": "114年度原交易字第000049號",
        "rowid": "995588",
        "pay_deadline": _roc_compact(3),
    }

    groups = module._filter_urgent_pending_payments(
        [item],
        days=14,
        download_folder=str(tmp_path),
    )

    assert groups == {"overdue": [], "urgent": [item], "unknown": []}

    module._mark_payment_file_delivered(str(pdf), str(tmp_path), caption="test")
    groups = module._filter_urgent_pending_payments(
        [item],
        days=14,
        download_folder=str(tmp_path),
    )

    assert groups == {"overdue": [], "urgent": [], "unknown": []}


def test_payment_download_error_is_retryable_after_cooldown(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    mgr = FileReviewManager(download_folder=str(tmp_path / "downloads"), headless=True)
    monkeypatch.setattr(mgr, "_find_existing_payment_slip_files", lambda row_json: [])
    monkeypatch.setenv("MAGI_EEFILE_PAYMENT_ERROR_COOLDOWN_HOURS", "0")
    row = {
        "rowid": "retry-row",
        "yyidno": "115.原交易.000021",
        "showyyidno": "115年度原交易字第000021號",
        "clnm": "喬○翔",
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }

    mgr._mark_payment_download_error(row, reason="payment_slip_download_no_pdf", files=[])

    assert mgr.payment_registry["rowid:retry-row"]["status"] == "invalid_download_cooldown"
    assert mgr._is_payment_processed(row) is False


def test_file_review_archive_success_does_not_also_stage():
    source = (
        Path(__file__).resolve().parents[1]
        / "casper_ecosystem"
        / "law_firm_orchestrators"
        / "file_review_automation.py"
    ).read_text(encoding="utf-8")
    loop = source.split("for fp in remaining_files:", 1)[1].split("# stage", 1)[0]

    assert 'if res.get("ok"):' in loop
    assert "continue" in loop


def test_review_filename_case_binding_rejects_cross_case_assignment():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    filename = "士林刑事 114附民1289_卷1_P1_102_1785814752.pdf"

    mismatch = FileReviewManager._review_artifact_case_binding(
        filename,
        {
            "case_number": "114.原訴.000084",
            "showyyidno": "114年度原訴字第000084號",
            "party": "黃珊珊",
        },
    )
    verified = FileReviewManager._review_artifact_case_binding(
        filename,
        {
            "case_number": "114.附民.001289",
            "showyyidno": "114年度附民字第1289號",
            "party": "吳倩茹",
        },
    )

    assert mismatch["status"] == "mismatch"
    assert mismatch["filename_identity"] == ["114", "附民", "1289"]
    assert mismatch["expected_identities"] == [["114", "原訴", "84"]]
    assert verified["status"] == "verified"


def test_review_download_requires_pdf_trailer_for_production_sized_file(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    partial = tmp_path / "士林刑事 114附民1289_卷1_P1_102.pdf"
    partial.write_bytes(b"%PDF-1.4\n" + b"x" * (70 * 1024))
    complete = tmp_path / "士林刑事 114附民1289_卷1_P1_102_1.pdf"
    complete.write_bytes(b"%PDF-1.4\n" + b"x" * (70 * 1024) + b"\n%%EOF\n")
    manager = FileReviewManager(download_folder=str(tmp_path), headless=True)

    assert manager._is_valid_review_download_artifact(str(partial)) is False
    assert manager._is_valid_review_download_artifact(str(complete)) is True


def test_popup_download_reconciliation_preserves_mixed_button_failure():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    late_count, unresolved = FileReviewManager._reconcile_popup_attempt_counts(
        ["direct"], verified_path_count=1, verified_button_count=0
    )
    assert late_count == 1
    assert unresolved == []

    late_count, unresolved = FileReviewManager._reconcile_popup_attempt_counts(
        ["direct"], verified_path_count=1, verified_button_count=1
    )
    assert late_count == 0
    assert unresolved == ["direct"]

    late_count, unresolved = FileReviewManager._reconcile_popup_attempt_counts(
        ["window"], verified_path_count=0, verified_button_count=0
    )
    assert late_count == 0
    assert unresolved == ["window"]


def test_archive_hard_gate_quarantines_filename_case_mismatch(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    download_root = tmp_path / "downloads"
    download_root.mkdir()
    source = download_root / "士林刑事 114附民1289_卷1_P1_102.pdf"
    source.write_bytes(b"%PDF-1.4\n" + b"x" * (70 * 1024) + b"\n%%EOF\n")
    wrong_case_folder = tmp_path / "2026-0059-吳志炳-一審-公共危險"
    wrong_case_folder.mkdir()

    manager = FileReviewManager(download_folder=str(download_root), headless=True)
    wrong_meta = {
        "court": "HLD",
        "case_number": "114.原交易.000049",
        "showyyidno": "114年度原交易字第000049號",
        "party": "吳志炳",
    }
    manager._last_download_meta_by_file = {str(source): wrong_meta, source.name: wrong_meta}
    manager._resolve_case_folder = lambda _info: str(wrong_case_folder)

    manager._archive_to_case_folders([str(source)], [wrong_meta])

    quarantined = list((download_root / "_case_mismatch_downloads").rglob("*.pdf"))
    assert len(quarantined) == 1
    assert not source.exists()
    assert not list(wrong_case_folder.rglob("*.pdf"))
    assert manager.last_download_error_code == "case_identity_mismatch"
    assert manager._last_archive_report["items"][0]["action"] == "blocked_case_identity_mismatch"


def test_case_mismatch_quarantine_suppresses_same_unchanged_portal_row(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    monkeypatch.setenv("MAGI_CASE_MISMATCH_RETRY_MINUTES", "120")
    manager = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "1099999",
        "yyidno": "114附民001289",
        "showyyidno": "114年度附民字第001289號",
        "downdt": "",
    }
    meta = {
        "_portal_rowid": "1099999",
        "_portal_row_json": row,
        "case_number": row["yyidno"],
        "showyyidno": row["showyyidno"],
    }
    binding = {
        "filename_identity": ["115", "訴", "530"],
        "expected_identities": [["114", "附民", "1289"]],
    }

    manager._remember_case_mismatch_suppression(meta, binding)

    assert manager._is_case_mismatch_suppressed(meta, row) is True
    saved = json.loads((tmp_path / ".case_mismatch_suppressions.json").read_text(encoding="utf-8"))
    assert saved["entries"]["rowid:1099999"]["filename_identity"] == ["115", "訴", "530"]


def test_case_mismatch_suppression_retries_when_portal_row_changes(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    manager = FileReviewManager(download_folder=str(tmp_path), headless=True)
    original_row = {"rowid": "1099999", "yyidno": "114附民001289", "downdt": ""}
    meta = {"_portal_rowid": "1099999", "_portal_row_json": original_row, "case_number": "114附民001289"}
    manager._remember_case_mismatch_suppression(meta, {"filename_identity": ["115", "訴", "530"]})

    changed_row = dict(original_row, downdt="2026-08-08 09:00:00")
    assert manager._is_case_mismatch_suppressed(meta, changed_row) is False
    assert manager._case_mismatch_suppressions == {}


def test_case_mismatch_default_cooldown_is_one_day_and_row_update_still_retries(
    tmp_path, monkeypatch
):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    monkeypatch.delenv("MAGI_CASE_MISMATCH_RETRY_MINUTES", raising=False)
    manager = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {"rowid": "1099998", "yyidno": "114東原簡000018", "downdt": ""}
    meta = {
        "_portal_rowid": row["rowid"],
        "_portal_row_json": row,
        "case_number": row["yyidno"],
    }

    manager._remember_case_mismatch_suppression(
        meta, {"filename_identity": ["114", "原上訴", "160"]}
    )

    entry = manager._case_mismatch_suppressions["rowid:1099998"]
    recorded = datetime.fromisoformat(entry["recorded_at"])
    suppressed_until = datetime.fromisoformat(entry["suppress_until"])
    assert suppressed_until - recorded >= timedelta(hours=23, minutes=59)
    assert manager._is_case_mismatch_suppressed(meta, row) is True
    assert manager._is_case_mismatch_suppressed(meta, dict(row, downdt="2026-08-12 19:00:00")) is False


def test_resolved_cross_row_mismatch_is_reconciled_after_correct_archive(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    manager = FileReviewManager(download_folder=str(tmp_path), headless=True)
    detail = (
        "download filename identity ['115', '訴', '530'] "
        "does not match portal row [['114', '附民', '1289']]"
    )
    manager._case_identity_mismatch_events = [
        {
            "filename_identity": ["115", "訴", "530"],
            "expected_identities": [["114", "附民", "1289"]],
            "quarantine_path": str(tmp_path / "quarantined.pdf"),
            "error_detail": detail,
        }
    ]
    manager.last_download_error_events = [
        {"code": "case_identity_mismatch", "detail": detail}
    ]
    manager.last_download_error_code = "case_identity_mismatch"
    manager.last_download_error_detail = detail
    manager._last_archive_report = {
        "items": [
            {
                "file": "北高行行政_115訴530本院卷_P1-130.pdf",
                "action": "copied",
            }
        ]
    }

    assert manager._reconcile_resolved_case_identity_mismatches() == 1
    assert manager._case_identity_mismatch_events == []
    assert manager.last_download_error_events == []
    assert manager.last_download_error_code == ""
    assert manager.last_download_error_detail == ""


def test_unresolved_cross_row_mismatch_remains_blocking(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    manager = FileReviewManager(download_folder=str(tmp_path), headless=True)
    detail = (
        "download filename identity ['115', '訴', '530'] "
        "does not match portal row [['114', '附民', '1289']]"
    )
    mismatch = {
        "filename_identity": ["115", "訴", "530"],
        "expected_identities": [["114", "附民", "1289"]],
        "quarantine_path": str(tmp_path / "quarantined.pdf"),
        "error_detail": detail,
    }
    manager._case_identity_mismatch_events = [mismatch]
    manager.last_download_error_events = [
        {"code": "case_identity_mismatch", "detail": detail}
    ]
    manager.last_download_error_code = "case_identity_mismatch"
    manager.last_download_error_detail = detail
    manager._last_archive_report = {
        "items": [
            {
                "file": "士林刑事 114附民1289_卷1_P1_102.pdf",
                "action": "copied",
            }
        ]
    }

    assert manager._reconcile_resolved_case_identity_mismatches() == 0
    assert manager._case_identity_mismatch_events == [mismatch]
    assert manager.last_download_error_events == [
        {"code": "case_identity_mismatch", "detail": detail}
    ]
    assert manager.last_download_error_code == "case_identity_mismatch"


def test_chrome_epoch_suffix_normalizes_for_review_dedup():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    assert FileReviewManager._strip_chrome_suffix(
        "士林刑事 114附民1289_卷1_P1_102_1785814752.pdf"
    ) == "士林刑事 114附民1289_卷1_P1_102.pdf"


def test_review_popup_uses_newest_visible_dialog_iframe(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators import file_review_automation as module

    FileReviewManager = module.FileReviewManager
    monkeypatch.setattr(
        module,
        "By",
        type("FakeBy", (), {"CSS_SELECTOR": "css selector"}),
    )

    class FakeFrame:
        def __init__(self, name, displayed=True):
            self.name = name
            self._displayed = displayed

        def is_displayed(self):
            return self._displayed

    class FakeDialog:
        def __init__(self, frame, displayed=True, width=640, height=480):
            self.frame = frame
            self._displayed = displayed
            self.size = {"width": width, "height": height}

        def is_displayed(self):
            return self._displayed

        def find_elements(self, *_args):
            return [self.frame]

    hidden_old = FakeDialog(FakeFrame("hidden-old"), displayed=False)
    visible_old = FakeDialog(FakeFrame("visible-old"))
    visible_new = FakeDialog(FakeFrame("visible-new"))

    class FakeDriver:
        def find_elements(self, *_args):
            return [hidden_old, visible_old, visible_new]

    manager = FileReviewManager(download_folder=str(tmp_path), headless=True)
    manager.driver = FakeDriver()

    assert manager._visible_review_dialog_iframe().name == "visible-new"
