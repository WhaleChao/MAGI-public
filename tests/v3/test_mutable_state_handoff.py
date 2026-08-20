from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from magi_v3.mutable_state_handoff import (
    ExactContext,
    MutableStateHandoffError,
    STATE_SPECS,
    execute_handoff,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "v3_mutable_state_handoff.py"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _context() -> ExactContext:
    return ExactContext(
        release_id="v3-20260717-rc-test",
        release_manifest_sha256=HASH_A,
        deployment_manifest_sha256=HASH_B,
        cutover_plan_sha256=HASH_C,
    )


def _payload(spec_id: str, encoding: str, *, marker: str = "initial") -> bytes:
    if encoding == "csv":
        return f"name,address\n{spec_id},{marker}-synthetic-address\n".encode("utf-8")
    value = {"state": spec_id, "marker": marker, "secret": "PRIVATE-BUSINESS-CONTENT"}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return encoded + (b"\n" if encoding == "jsonl" else b"")


def _seed_source(root: Path, *, include_optional: bool = True) -> dict[str, bytes]:
    written: dict[str, bytes] = {}
    for spec in STATE_SPECS:
        if not spec.required and not include_optional:
            continue
        path = root / spec.source_relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _payload(spec.state_id, spec.encoding)
        path.write_bytes(data)
        written[spec.state_id] = data
    return written


def _run(
    tmp_path: Path,
    *,
    action: str,
    source: Path,
    target: Path,
    receipt_name: str,
    staging_name: str | None = None,
    refresh: bool = False,
    expected: str | None = None,
):
    return execute_handoff(
        action=action,  # type: ignore[arg-type]
        source_root=source,
        target_shared_root=target,
        receipt_path=tmp_path / "receipts" / receipt_name,
        staging_root=(tmp_path / staging_name) if staging_name else None,
        context=_context(),
        refresh=refresh,
        expected_target_snapshot_sha256=expected,
    )


def test_allowlist_is_exact_and_covers_audited_p0_p1_state() -> None:
    state_ids = {spec.state_id for spec in STATE_SPECS}
    assert len(STATE_SPECS) == len(state_ids) == 29
    assert {
        "obsidian_wiki",
        "obsidian_vault",
        "obsidian_ingest",
        "obsidian_index",
        "transcript_sync",
        "transcript_manual_queue",
        "laf_portal_retry",
        "laf_seed_skip",
        "laf_processed_email",
        "market_watchlist",
        "market_data_cache",
        "market_performance",
        "bookmark_batch",
        "discord_channel_map",
        "discord_last_channel",
        "telegram_channel",
        "telegram_topic_map",
        "telegram_poll_offset",
        "poa_chat",
        "hearing_reminder",
        "file_review_processed_email",
        "payment_registry",
        "payment_proof_registry",
        "judgments_export",
        "cortex_cursor",
        "debt_address_bank_json",
        "debt_address_company_json",
        "debt_address_bank_csv",
        "debt_address_company_csv",
    } == state_ids
    assert {spec.state_id for spec in STATE_SPECS if spec.required} == {
        "laf_processed_email",
        "file_review_processed_email",
        "payment_registry",
        "payment_proof_registry",
        "judgments_export",
    }
    assert len({spec.target_relative for spec in STATE_SPECS}) == len(STATE_SPECS)
    assert all("*" not in spec.source_relative and "*" not in spec.target_relative for spec in STATE_SPECS)
    assert all(not spec.source_relative.endswith("/") for spec in STATE_SPECS)


def test_dry_run_writes_only_private_receipt_and_lists_optional_degradation(tmp_path: Path) -> None:
    source = tmp_path / "v2"
    target = tmp_path / "v3-shared"
    _seed_source(source, include_optional=False)

    payload, receipt_sha = _run(
        tmp_path,
        action="dry-run",
        source=source,
        target=target,
        receipt_name="dry-run.json",
    )

    receipt = tmp_path / "receipts" / "dry-run.json"
    assert not target.exists()
    assert receipt.is_file()
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert payload["status"] == "dry_run"
    assert payload["ready"] is True
    assert payload["degraded"] is True
    assert payload["degraded_state_ids"] == sorted(
        spec.state_id for spec in STATE_SPECS if not spec.required
    )
    assert payload["target_before_snapshot_sha256"] != payload["target_snapshot_sha256"]
    assert len(receipt_sha) == 64
    serialized = receipt.read_text(encoding="utf-8")
    assert "PRIVATE-BUSINESS-CONTENT" not in serialized
    assert str(source) not in serialized
    assert str(target) not in serialized
    assert payload["contains_business_payload"] is False
    assert payload["contains_source_or_target_paths"] is False


def test_prepare_atomically_copies_only_allowlist_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "v2"
    target = tmp_path / "v3-shared"
    original = _seed_source(source)
    ignored = source / ".agent" / "not-allowlisted.json"
    ignored.write_text('{"do_not_copy": true}', encoding="utf-8")

    first, _ = _run(
        tmp_path,
        action="prepare",
        source=source,
        target=target,
        receipt_name="prepare-1.json",
        staging_name="stage-1",
    )
    assert first["status"] == "prepared"
    assert first["degraded"] is False
    assert not (tmp_path / "stage-1").exists()
    assert len(list(path for path in target.rglob("*") if path.is_file())) == len(STATE_SPECS)
    assert not any(path.name == ignored.name for path in target.rglob("*"))
    for spec in STATE_SPECS:
        path = target / spec.target_relative
        assert path.read_bytes() == original[spec.state_id]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert all(row["status"] == "copied" for row in first["states"])

    second, _ = _run(
        tmp_path,
        action="prepare",
        source=source,
        target=target,
        receipt_name="prepare-2.json",
        staging_name="stage-2",
    )
    assert not (tmp_path / "stage-2").exists()
    assert all(row["status"] == "unchanged" for row in second["states"])
    assert second["target_before_snapshot_sha256"] == second["target_snapshot_sha256"]
    assert second["target_snapshot_sha256"] == first["target_snapshot_sha256"]


def test_debt_address_handoff_preserves_existing_entries_and_v3_writes_only_shared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api import debt_document_generator as debt

    source = tmp_path / "v2"
    shared = tmp_path / "v3-shared"
    release = tmp_path / "sealed-release"
    release_address_dir = release / "integrations/debt_robot/document"
    release_address_dir.mkdir(parents=True)
    (release / "release-manifest.json").write_text("{}\n", encoding="utf-8")
    for name in ("01_A.py", "02_B.py", "03_C.py", "04_D.py", "05_E.py", "06_F.py"):
        source_module = ROOT / "integrations/debt_robot" / name
        target_module = release / "integrations/debt_robot" / name
        shutil.copy2(source_module, target_module)
    for name in ("A.docx", "B.docx", "C.docx", "D.docx"):
        shutil.copy2(
            ROOT / "integrations/debt_robot/document" / name,
            release_address_dir / name,
        )
    supplement_runtime = release / "src/supplement_core/__init__.py"
    supplement_runtime.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "src/supplement_core/__init__.py", supplement_runtime)
    supplement_template = release / "data/templates/D_supplement.docx"
    supplement_template.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "data/templates/D_supplement.docx", supplement_template)
    _seed_source(source)
    bank_json = source / "integrations/debt_robot/document/all adress - bank.json"
    bank_json.write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {"name": "既有合成銀行", "address": "合成市既有路一號"}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    company_csv = source / "integrations/debt_robot/document/all adress - company.csv"
    company_csv.write_text(
        "公司名,地址\n既有合成公司,合成市公司路二號\n",
        encoding="utf-8",
    )

    prepared, _ = _run(
        tmp_path,
        action="prepare",
        source=source,
        target=shared,
        receipt_name="debt-address-prepare.json",
        staging_name="debt-address-stage",
    )
    assert prepared["ready"] is True

    monkeypatch.setattr(debt, "_MAGI_ROOT", str(release))
    monkeypatch.setattr(debt, "_ROBOT_SOURCE_DIR", str(release / "integrations/debt_robot"))
    monkeypatch.setattr(debt, "_ROBOT_DOCUMENT_DIR", str(release_address_dir))
    monkeypatch.setattr(debt, "_TEMPLATE_DIR", str(release_address_dir))
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-debt-address-test")
    monkeypatch.setenv("MAGI_V3_SHARED_STATE_DIR", str(shared))
    monkeypatch.setenv("MAGI_SHARED_STATE_DIR", str(shared))
    monkeypatch.setenv(
        "MAGI_DEBT_ADDRESS_BOOK_DIR", str(shared / "debt/address-book")
    )

    options = debt.get_address_options()
    assert debt.get_robot_source_status()["ok"] is True
    assert {row["name"] for row in options["banks"]} >= {"既有合成銀行"}
    assert {row["name"] for row in options["companies"]} >= {"既有合成公司"}
    assert debt.save_address_to_csv(
        "新增合成銀行", "合成市新增路三號", "bank"
    )
    refreshed = debt.get_address_options()
    assert {row["name"] for row in refreshed["banks"]} >= {
        "既有合成銀行",
        "新增合成銀行",
    }
    assert not any(release_address_dir.glob("all adress - *"))
    application = debt.generate_application(
        {
            "name": "合成聲請人",
            "address": "合成市聲請路四號",
            "lawyer_name": "合成律師",
            "asset_total": 100,
            "debt_total": 200,
            "max_creditor_bank": "既有合成銀行",
            "application_court": "臺灣臺北地方法院",
        }
    )
    output = shared / "exports/debt/generated-application.docx"
    output.parent.mkdir(parents=True)
    application.save(output)
    assert output.is_file()
    assert not (release / "exports").exists()


def test_invalid_debt_address_csv_is_rejected_before_handoff_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v2"
    target = tmp_path / "v3-shared"
    _seed_source(source)
    broken = source / "integrations/debt_robot/document/all adress - bank.csv"
    broken.write_text("name\nmissing-address\n", encoding="utf-8")

    with pytest.raises(MutableStateHandoffError, match="debt_address_bank_csv"):
        _run(
            tmp_path,
            action="dry-run",
            source=source,
            target=target,
            receipt_name="invalid-debt-address.json",
        )
    assert not target.exists()


def test_required_missing_fails_closed_before_receipt_or_target_write(tmp_path: Path) -> None:
    source = tmp_path / "v2"
    target = tmp_path / "v3-shared"
    _seed_source(source)
    required = next(spec for spec in STATE_SPECS if spec.required)
    (source / required.source_relative).unlink()

    with pytest.raises(MutableStateHandoffError, match=required.state_id):
        _run(
            tmp_path,
            action="prepare",
            source=source,
            target=target,
            receipt_name="missing.json",
            staging_name="stage-missing",
        )
    assert not target.exists()
    assert not (tmp_path / "receipts" / "missing.json").exists()


def test_source_symlink_and_special_file_are_rejected_without_reading(tmp_path: Path) -> None:
    source = tmp_path / "v2"
    target = tmp_path / "v3-shared"
    _seed_source(source)
    first = STATE_SPECS[0]
    path = source / first.source_relative
    path.unlink()
    path.symlink_to(source / next(spec for spec in STATE_SPECS if spec.required).source_relative)
    with pytest.raises(MutableStateHandoffError, match="symlink"):
        _run(
            tmp_path,
            action="dry-run",
            source=source,
            target=target,
            receipt_name="symlink.json",
        )

    path.unlink()
    os.mkfifo(path)
    with pytest.raises(MutableStateHandoffError, match="not regular"):
        _run(
            tmp_path,
            action="dry-run",
            source=source,
            target=target,
            receipt_name="fifo.json",
        )


def test_target_symlink_and_world_readable_target_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "v2"
    target = tmp_path / "v3-shared"
    _seed_source(source)
    spec = STATE_SPECS[0]
    target_path = target / spec.target_relative
    target_path.parent.mkdir(parents=True)
    target_path.symlink_to(source / spec.source_relative)
    with pytest.raises(MutableStateHandoffError, match="symlink"):
        _run(
            tmp_path,
            action="dry-run",
            source=source,
            target=target,
            receipt_name="target-link.json",
        )

    target_path.unlink()
    target_path.write_bytes(_payload(spec.state_id, spec.encoding))
    target_path.chmod(0o644)
    with pytest.raises(MutableStateHandoffError, match="safe regular"):
        _run(
            tmp_path,
            action="dry-run",
            source=source,
            target=target,
            receipt_name="target-mode.json",
        )


def test_different_target_requires_refresh_with_exact_old_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "v2"
    target = tmp_path / "v3-shared"
    _seed_source(source)
    initial, _ = _run(
        tmp_path,
        action="prepare",
        source=source,
        target=target,
        receipt_name="initial.json",
        staging_name="stage-initial",
    )
    spec = next(spec for spec in STATE_SPECS if spec.state_id == "payment_registry")
    changed = _payload(spec.state_id, spec.encoding, marker="changed")
    (source / spec.source_relative).write_bytes(changed)

    audit, _ = _run(
        tmp_path,
        action="dry-run",
        source=source,
        target=target,
        receipt_name="conflict-audit.json",
    )
    assert audit["ready"] is False
    assert audit["target_before_snapshot_sha256"] == initial["target_snapshot_sha256"]
    audit_row = next(row for row in audit["states"] if row["state_id"] == spec.state_id)
    assert audit_row["status"] == "conflict"

    with pytest.raises(MutableStateHandoffError, match="explicit refresh"):
        _run(
            tmp_path,
            action="prepare",
            source=source,
            target=target,
            receipt_name="refused.json",
            staging_name="stage-refused",
        )
    assert (target / spec.target_relative).read_bytes() != changed
    assert not (target / STATE_SPECS[-1].target_relative).is_symlink()

    with pytest.raises(MutableStateHandoffError, match="precondition failed"):
        _run(
            tmp_path,
            action="prepare",
            source=source,
            target=target,
            receipt_name="wrong-hash.json",
            staging_name="stage-wrong-hash",
            refresh=True,
            expected="d" * 64,
        )
    assert (target / spec.target_relative).read_bytes() != changed

    refreshed, _ = _run(
        tmp_path,
        action="prepare",
        source=source,
        target=target,
        receipt_name="refreshed.json",
        staging_name="stage-refreshed",
        refresh=True,
        expected=initial["target_snapshot_sha256"],
    )
    assert (target / spec.target_relative).read_bytes() == changed
    row = next(row for row in refreshed["states"] if row["state_id"] == spec.state_id)
    assert row["status"] == "refreshed"
    assert refreshed["target_before_snapshot_sha256"] == initial["target_snapshot_sha256"]
    assert refreshed["target_snapshot_sha256"] != initial["target_snapshot_sha256"]


def test_conflict_preflight_prevents_partial_publish(tmp_path: Path) -> None:
    source = tmp_path / "v2"
    target = tmp_path / "v3-shared"
    _seed_source(source)
    conflict = STATE_SPECS[0]
    target_path = target / conflict.target_relative
    target_path.parent.mkdir(parents=True)
    target_path.write_text('{"different": true}', encoding="utf-8")
    target_path.chmod(0o600)

    with pytest.raises(MutableStateHandoffError, match="explicit refresh"):
        _run(
            tmp_path,
            action="prepare",
            source=source,
            target=target,
            receipt_name="conflict.json",
            staging_name="stage-conflict",
        )
    assert sum(path.is_file() for path in target.rglob("*")) == 1
    assert not (tmp_path / "stage-conflict").exists()


def test_invalid_exact_context_and_overlapping_roots_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "v2"
    _seed_source(source)
    with pytest.raises(MutableStateHandoffError, match="exact digest"):
        execute_handoff(
            action="dry-run",
            source_root=source,
            target_shared_root=tmp_path / "target",
            receipt_path=tmp_path / "receipts" / "bad-context.json",
            context=ExactContext("v3-test", "bad", HASH_B, HASH_C),
        )
    with pytest.raises(MutableStateHandoffError, match="disjoint"):
        execute_handoff(
            action="dry-run",
            source_root=source,
            target_shared_root=source / "target",
            receipt_path=tmp_path / "receipts" / "overlap.json",
            context=_context(),
        )


def test_cli_prints_only_bounded_metadata_and_does_not_mutate_source(tmp_path: Path) -> None:
    source = tmp_path / "v2"
    target = tmp_path / "v3-shared"
    original = _seed_source(source)
    receipt = tmp_path / "receipts" / "cli.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "prepare",
            "--source-root",
            str(source),
            "--target-shared-root",
            str(target),
            "--receipt",
            str(receipt),
            "--staging-root",
            str(tmp_path / "cli-stage"),
            "--release-id",
            _context().release_id,
            "--release-manifest-sha256",
            HASH_A,
            "--deployment-manifest-sha256",
            HASH_B,
            "--cutover-plan-sha256",
            HASH_C,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert set(output) == {
        "degraded",
        "ok",
        "ready",
        "receipt_sha256",
        "status",
        "target_before_snapshot_sha256",
        "target_snapshot_sha256",
    }
    assert output["ok"] is True
    assert "PRIVATE-BUSINESS-CONTENT" not in result.stdout
    assert str(source) not in result.stdout
    for spec in STATE_SPECS:
        assert (source / spec.source_relative).read_bytes() == original[spec.state_id]
