from __future__ import annotations

from pathlib import Path

from scripts.ops.rename_judgment_case_folders import run


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
