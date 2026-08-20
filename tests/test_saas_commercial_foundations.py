from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from flask import Flask

from api import saas_audit
from api.durable_rate_limit import (
    DurableRateLimiter,
    check_rate_limit,
    hash_client_identity,
    inspect_rate_limit_storage,
)
from api import request_guards


def test_durable_rate_limit_is_shared_between_instances_and_resets(tmp_path: Path) -> None:
    now = [120.0]
    database = tmp_path / "rate.sqlite3"
    first = DurableRateLimiter(
        database,
        limits={"webhook": 2, "api": 3},
        window_seconds=60,
        clock=lambda: now[0],
    )
    second = DurableRateLimiter(
        database,
        limits={"webhook": 2, "api": 3},
        window_seconds=60,
        clock=lambda: now[0],
    )

    assert first.check("webhook", "tenant-a\0client-1").rejected is False
    assert second.check("webhook", "tenant-a\0client-1").rejected is False
    blocked = first.check("webhook", "tenant-a\0client-1")
    assert blocked.rejected is True
    assert blocked.retry_after == 60

    now[0] = 181.0
    reset = second.check("webhook", "tenant-a\0client-1")
    assert reset.rejected is False
    assert reset.count == 1
    assert database.stat().st_mode & 0o077 == 0


def test_durable_rate_limit_stores_no_raw_client_identity(tmp_path: Path) -> None:
    database = tmp_path / "rate.sqlite3"
    identity = "tenant-secret\0203.0.113.42"
    limiter = DurableRateLimiter(database, clock=lambda: 1000.0)
    assert limiter.check("api", identity).rejected is False

    raw = database.read_bytes()
    assert identity.encode() not in raw
    assert b"203.0.113.42" not in raw
    assert len(hash_client_identity(identity)) == 64


def test_rate_limit_storage_failure_is_fail_closed_when_required(tmp_path: Path) -> None:
    real = tmp_path / "real.sqlite3"
    real.write_bytes(b"")
    link = tmp_path / "rate.sqlite3"
    link.symlink_to(real)
    limiter = DurableRateLimiter(link, limits={"api": 5})

    decision = check_rate_limit("api", "client", limiter=limiter, fail_closed=True)
    assert decision.rejected is True
    assert decision.backend == "unavailable_fail_closed"
    assert decision.reason == "durable_storage_unavailable"


def test_rate_limit_readiness_verifies_database_not_only_filename(tmp_path: Path) -> None:
    database = tmp_path / "rate.sqlite3"
    limiter = DurableRateLimiter(database, clock=lambda: 1000.0)
    assert limiter.check("api", "client").rejected is False
    assert inspect_rate_limit_storage(database)["status"] == "verified"

    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    database.write_bytes(b"not-a-sqlite-database")
    report = inspect_rate_limit_storage(database)
    assert report["ok"] is False
    assert report["status"] == "invalid"
    assert report["issue"]


def test_audit_chain_seals_legacy_prefix_and_redacts_sensitive_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit.jsonl"
    legacy = {"action": "legacy", "metadata": {"safe": True}}
    path.write_text(json.dumps(legacy, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(saas_audit, "AUDIT_PATH", path)

    event = saas_audit.append_audit_event(
        "case.update",
        resource_type="case",
        resource_id="2026-0001",
        metadata={"api_token": "do-not-store", "nested": {"password": "secret"}},
    )
    report = saas_audit.verify_audit_chain(path)

    assert report["ok"] is True
    assert report["status"] == "verified"
    assert report["legacy_events"] == 1
    assert report["chained_events"] == 1
    assert event["sequence"] == 1
    assert str(event["previous_hash"]).startswith("legacy:")
    assert event["metadata"]["api_token"] == "[redacted]"
    assert event["metadata"]["nested"]["password"] == "[redacted]"
    assert "do-not-store" not in path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o077 == 0


def test_audit_chain_concurrent_append_has_one_contiguous_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(saas_audit, "AUDIT_PATH", path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        events = list(pool.map(lambda number: saas_audit.append_audit_event(f"event.{number}"), range(30)))

    report = saas_audit.verify_audit_chain(path)
    assert report["ok"] is True
    assert report["chained_events"] == 30
    assert sorted(int(event["sequence"]) for event in events) == list(range(1, 31))


def test_audit_tampering_is_detected_and_future_append_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(saas_audit, "AUDIT_PATH", path)
    saas_audit.append_audit_event("first")
    saas_audit.append_audit_event("second")

    rows = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[0])
    payload["action"] = "tampered"
    rows[0] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    report = saas_audit.verify_audit_chain(path)
    assert report["ok"] is False
    assert "event hash mismatch" in report["issue"]
    with pytest.raises(OSError, match="audit chain verification failed"):
        saas_audit.append_audit_event("third")


def test_protected_mutations_receive_start_and_completion_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        request_guards,
        "append_audit_event",
        lambda action, **kwargs: events.append((action, kwargs)) or {"action": action},
    )
    app = Flask("commercial-mutation-audit")
    app.config.update(TESTING=True, SECRET_KEY="test")
    request_guards.install_request_guards(app, logging.getLogger("test.mutation.audit"))

    @app.post("/api/private-change")
    def private_change():
        return {"ok": True}, 201

    response = app.test_client().post("/api/private-change")

    assert response.status_code == 201
    assert [action for action, _kwargs in events] == [
        "http.mutation.started",
        "http.mutation.completed",
    ]
    assert events[0][1]["resource_id"] == events[1][1]["resource_id"]
    assert events[1][1]["metadata"]["status_code"] == 201


def test_public_webhook_alias_cannot_amplify_mutation_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        request_guards,
        "append_audit_event",
        lambda action, **_kwargs: events.append(action) or {"action": action},
    )
    app = Flask("commercial-public-webhook")
    app.config.update(TESTING=True, SECRET_KEY="test")
    request_guards.install_request_guards(app, logging.getLogger("test.public.webhook"))

    @app.post("/")
    def callback():
        return "OK"

    assert app.test_client().post("/").status_code == 200
    assert events == []


def test_formal_saas_fails_closed_before_mutation_when_audit_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAGI_SAAS_MODE", "1")
    monkeypatch.setenv("MAGI_TENANT_ID", "tenant-test")

    def unavailable(*_args, **_kwargs):
        raise OSError("audit sink unavailable")

    monkeypatch.setattr(request_guards, "append_audit_event", unavailable)
    app = Flask("commercial-audit-fail-closed")
    app.config.update(TESTING=True, SECRET_KEY="test")
    request_guards.install_request_guards(app, logging.getLogger("test.audit.closed"))
    side_effects: list[str] = []

    @app.post("/api/private-change")
    def private_change():
        side_effects.append("performed")
        return {"ok": True}

    response = app.test_client().post("/api/private-change")

    assert response.status_code == 503
    assert side_effects == []
