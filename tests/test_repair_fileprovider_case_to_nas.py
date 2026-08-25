from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.ops import repair_fileprovider_case_to_nas as repair


def _fixture_tree(tmp_path: Path) -> Path:
    source = (
        tmp_path
        / "Library"
        / "CloudStorage"
        / "SynologyDrive-homes"
        / "01_案件"
        / "法扶案件"
        / "民事"
        / "2099-0001-隔離當事人"
    )
    (source / "01_法扶資料").mkdir(parents=True)
    (source / "04_我方歷次書狀").mkdir()
    (source / "01_法扶資料" / "document-a.pdf").write_bytes(b"pdf-fixture-a")
    (source / "04_我方歷次書狀" / "document-b.docx").write_bytes(b"docx-fixture-b")
    return source


def _identity() -> str:
    return hashlib.sha256(b"synthetic-case-identity").hexdigest()


def test_dry_run_seals_manifest_without_creating_destination(monkeypatch, tmp_path):
    source = _fixture_tree(tmp_path)
    destination = tmp_path / "nas" / "case"
    destination.parent.mkdir()
    monkeypatch.setattr(repair, "is_authoritative_nas_write_path", lambda _path: True)

    result = repair.repair_case_tree(
        source=source,
        destination=destination,
        expected_source_manifest_sha256="",
        case_identity_sha256=_identity(),
        receipt=None,
        apply=False,
    )

    assert result["status"] == "ready_not_executed"
    assert result["file_count"] == 2
    assert result["directory_count"] == 2
    assert result["total_bytes"] == len(b"pdf-fixture-a") + len(b"docx-fixture-b")
    assert destination.exists() is False


def test_apply_copies_exact_tree_preserves_source_and_writes_pii_free_receipt(
    monkeypatch, tmp_path
):
    source = _fixture_tree(tmp_path)
    destination = tmp_path / "nas" / "case"
    destination.parent.mkdir()
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(repair, "is_authoritative_nas_write_path", lambda _path: True)
    manifest = repair.build_tree_manifest(source)

    result = repair.repair_case_tree(
        source=source,
        destination=destination,
        expected_source_manifest_sha256=manifest["manifest_sha256"],
        case_identity_sha256=_identity(),
        receipt=receipt,
        apply=True,
    )

    assert result["status"] == "passed"
    assert result["source_preserved"] is True
    assert result["zero_overwrite"] is True
    assert repair.build_tree_manifest(source)["manifest_sha256"] == manifest["manifest_sha256"]
    assert repair.build_tree_manifest(destination)["manifest_sha256"] == manifest["manifest_sha256"]
    stored = json.loads(receipt.read_text(encoding="utf-8"))
    assert stored == result
    receipt_text = receipt.read_text(encoding="utf-8")
    assert "隔離當事人" not in receipt_text
    assert "document-a" not in receipt_text
    assert str(source) not in receipt_text
    assert str(destination) not in receipt_text


def test_apply_rejects_manifest_drift_before_copy(monkeypatch, tmp_path):
    source = _fixture_tree(tmp_path)
    destination = tmp_path / "nas" / "case"
    destination.parent.mkdir()
    monkeypatch.setattr(repair, "is_authoritative_nas_write_path", lambda _path: True)

    with pytest.raises(repair.RepairError, match="source_manifest_mismatch"):
        repair.repair_case_tree(
            source=source,
            destination=destination,
            expected_source_manifest_sha256="1" * 64,
            case_identity_sha256=_identity(),
            receipt=None,
            apply=True,
        )

    assert destination.exists() is False


def test_apply_never_overwrites_existing_destination(monkeypatch, tmp_path):
    source = _fixture_tree(tmp_path)
    destination = tmp_path / "nas" / "case"
    destination.mkdir(parents=True)
    marker = destination / "existing.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(repair, "is_authoritative_nas_write_path", lambda _path: True)

    with pytest.raises(repair.RepairError, match="destination_already_exists"):
        repair.repair_case_tree(
            source=source,
            destination=destination,
            expected_source_manifest_sha256=repair.build_tree_manifest(source)["manifest_sha256"],
            case_identity_sha256=_identity(),
            receipt=None,
            apply=True,
        )

    assert marker.read_text(encoding="utf-8") == "keep"


def test_source_change_during_copy_fails_and_cleans_owned_stage(monkeypatch, tmp_path):
    source = _fixture_tree(tmp_path)
    destination = tmp_path / "nas" / "case"
    destination.parent.mkdir()
    monkeypatch.setattr(repair, "is_authoritative_nas_write_path", lambda _path: True)
    manifest = repair.build_tree_manifest(source)
    original_copy = repair._copy_tree_exact

    def _copy_then_mutate(source_arg, stage_arg, manifest_arg):
        original_copy(source_arg, stage_arg, manifest_arg)
        with (source / "01_法扶資料" / "document-a.pdf").open("ab") as handle:
            handle.write(b"changed")

    monkeypatch.setattr(repair, "_copy_tree_exact", _copy_then_mutate)

    with pytest.raises(repair.RepairError, match="source_changed_during_repair"):
        repair.repair_case_tree(
            source=source,
            destination=destination,
            expected_source_manifest_sha256=manifest["manifest_sha256"],
            case_identity_sha256=_identity(),
            receipt=None,
            apply=True,
        )

    assert destination.exists() is False
    assert list(destination.parent.glob(".magi-case-storage-repair-*")) == []


def test_manifest_rejects_symlink_and_hardlink(tmp_path):
    source = _fixture_tree(tmp_path)
    symlink = source / "01_法扶資料" / "link.pdf"
    symlink.symlink_to(source / "01_法扶資料" / "document-a.pdf")
    with pytest.raises(repair.RepairError, match="source_symlink_rejected"):
        repair.build_tree_manifest(source)
    symlink.unlink()

    hardlink = source / "01_法扶資料" / "hardlink.pdf"
    os.link(source / "01_法扶資料" / "document-a.pdf", hardlink)
    with pytest.raises(repair.RepairError, match="source_file_contract_failed"):
        repair.build_tree_manifest(source)


def test_smb_promotion_fallback_is_serialized_and_no_replace(tmp_path):
    parent = tmp_path / "nas"
    parent.mkdir()
    stage = parent / ".magi-case-storage-repair-fixture"
    stage.mkdir()
    (stage / "payload.bin").write_bytes(b"payload")
    destination = parent / "case"

    repair._rename_exclusive_smb_fallback(stage, destination)

    assert stage.exists() is False
    assert (destination / "payload.bin").read_bytes() == b"payload"
    assert (parent / ".magi-case-storage-repair-promotion.lock").exists() is False

    second_stage = parent / ".magi-case-storage-repair-fixture-2"
    second_stage.mkdir()
    with pytest.raises(repair.RepairError, match="destination_already_exists"):
        repair._rename_exclusive_smb_fallback(second_stage, destination)
    assert second_stage.is_dir()
    assert (parent / ".magi-case-storage-repair-promotion.lock").exists() is False
