from __future__ import annotations

from pathlib import Path

from api.osc import case_folder_schema as schema
from scripts.ops.rename_judgment_case_folders import canonical_name_for_legacy, run


def test_rename_legacy_judgment_folder(tmp_path: Path):
    case = tmp_path / "2026-0001-測試"
    old = case / "10_判決書"
    old.mkdir(parents=True)
    (old / "判決.pdf").write_bytes(b"pdf")

    report = run([tmp_path], apply=True)

    new = case / "10_判決書或終局裁定及處分"
    assert report["ok"] is True
    assert report["legacy_folder_count"] == 1
    assert not old.exists()
    assert (new / "判決.pdf").read_bytes() == b"pdf"


def test_rename_legacy_judgment_folder_names_follow_shared_schema():
    for prefix in schema.JUDGMENT_FOLDER_REPAIR_PREFIXES:
        assert canonical_name_for_legacy(schema.legacy_judgment_folder_name(prefix)) == schema.judgment_folder_name(prefix)
    assert canonical_name_for_legacy(schema.LEGACY_JUDGMENT_FOLDER_LABEL) == schema.JUDGMENT_FOLDER_LABEL
    assert canonical_name_for_legacy(schema.JUDGMENT_FOLDER_LABEL) == ""


def test_rename_legacy_judgment_folder_with_07_prefix(tmp_path: Path):
    case = tmp_path / "2026-0001-舊無償案件"
    old = case / "07_判決書"
    old.mkdir(parents=True)
    (old / "判決.pdf").write_bytes(b"pdf")

    report = run([tmp_path], apply=True)

    new = case / "07_判決書或終局裁定及處分"
    assert report["ok"] is True
    assert report["legacy_folder_count"] == 1
    assert not old.exists()
    assert (new / "判決.pdf").read_bytes() == b"pdf"


def test_rename_scans_every_schema_legacy_repair_prefix(tmp_path: Path):
    case = tmp_path / "2026-0003-歷史前綴"
    for prefix in schema.JUDGMENT_FOLDER_REPAIR_PREFIXES:
        old = case / schema.legacy_judgment_folder_name(prefix)
        old.mkdir(parents=True)
        (old / f"{prefix}.pdf").write_bytes(str(prefix).encode())

    report = run([tmp_path], apply=True)

    assert report["ok"] is True
    assert report["legacy_folder_count"] == len(schema.JUDGMENT_FOLDER_REPAIR_PREFIXES)
    for prefix in schema.JUDGMENT_FOLDER_REPAIR_PREFIXES:
        assert not (case / schema.legacy_judgment_folder_name(prefix)).exists()
        assert (case / schema.judgment_folder_name(prefix) / f"{prefix}.pdf").read_bytes() == str(prefix).encode()


def test_merge_existing_canonical_folder_without_overwrite(tmp_path: Path):
    case = tmp_path / "2026-0002-測試"
    old = case / "10_判決書"
    new = case / "10_判決書或終局裁定及處分"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "same.pdf").write_bytes(b"same")
    (new / "same.pdf").write_bytes(b"same")
    (old / "different.pdf").write_bytes(b"old")
    (new / "different.pdf").write_bytes(b"new")
    (old / "only-old.pdf").write_bytes(b"old-only")

    report = run([tmp_path], apply=True)

    item = report["items"][0]
    assert item["merged"] is True
    assert not old.exists()
    assert (new / "same.pdf").read_bytes() == b"same"
    assert (new / "different.pdf").read_bytes() == b"new"
    assert (new / "only-old.pdf").read_bytes() == b"old-only"
    conflicts = list((new / ".judgment_folder_rename_conflicts").rglob("different*.pdf"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == b"old"


def test_dry_run_does_not_touch_files(tmp_path: Path):
    old = tmp_path / "case" / "08_判決書"
    old.mkdir(parents=True)
    (old / "a.pdf").write_bytes(b"a")

    report = run([tmp_path], apply=False)

    assert report["legacy_folder_count"] == 1
    assert old.exists()
    assert not (tmp_path / "case" / "08_判決書或終局裁定及處分").exists()
