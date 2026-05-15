import json
from pathlib import Path


def test_duplicate_payment_slip_cleanup_quarantines_suffixes_and_seeds_registry(tmp_path, monkeypatch):
    from scripts.ops import disk_cleanup_healthcheck as mod

    case_root = tmp_path / "cases"
    review_dir = case_root / "法扶案件" / "刑事" / "2025-0133-吳志炳" / "02_閱卷資料" / "20260515"
    review_dir.mkdir(parents=True)
    keep = review_dir / "繳費單_吳志炳_114.原交易.000049.pdf"
    dup1 = review_dir / "繳費單_吳志炳_114.原交易.000049_1.pdf"
    dup2 = review_dir / "繳費單_吳志炳_114.原交易.000049_2.pdf"
    keep.write_bytes(b"keep")
    dup1.write_bytes(b"dup1")
    dup2.write_bytes(b"dup2")

    registry = tmp_path / "payment_registry.json"
    monkeypatch.setenv("MAGI_DISK_PAYMENT_DUPLICATE_ROOTS", str(case_root))
    monkeypatch.setenv("MAGI_PAYMENT_REGISTRY_PATH", str(registry))

    result = mod.cleanup_duplicate_payment_slips(dry_run=False)[0]

    assert result["duplicate_files"] == 2
    assert result["quarantined_files"] == 2
    assert keep.exists()
    assert not dup1.exists()
    assert not dup2.exists()

    data = json.loads(registry.read_text(encoding="utf-8"))
    entry = data["case:114原交易49:吳志炳"]
    assert entry["party"] == "吳志炳"
    assert entry["case_number"] == "114.原交易.000049"
    assert entry["file_paths"] == [str(keep)]


def test_duplicate_payment_slip_cleanup_renames_lone_suffix_to_canonical(tmp_path, monkeypatch):
    from scripts.ops import disk_cleanup_healthcheck as mod

    case_root = tmp_path / "cases"
    review_dir = case_root / "2025-0128-黃珊珊" / "06_閱卷資料" / "20260514"
    review_dir.mkdir(parents=True)
    suffixed = review_dir / "繳費單_黃珊珊_114.原訴.000084_11.pdf"
    canonical = review_dir / "繳費單_黃珊珊_114.原訴.000084.pdf"
    suffixed.write_bytes(b"only copy")

    registry = tmp_path / "payment_registry.json"
    monkeypatch.setenv("MAGI_DISK_PAYMENT_DUPLICATE_ROOTS", str(case_root))
    monkeypatch.setenv("MAGI_PAYMENT_REGISTRY_PATH", str(registry))

    result = mod.cleanup_duplicate_payment_slips(dry_run=False)[0]

    assert result["canonical_renamed_files"] == 1
    assert result["errors"] == []
    assert canonical.exists()
    assert not suffixed.exists()
    data = json.loads(registry.read_text(encoding="utf-8"))
    assert "case:114原訴84:黃珊珊" in data
    assert data["case:114原訴84:黃珊珊"]["file_paths"] == [str(canonical)]
