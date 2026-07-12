from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PDF_NAMER_DIR = ROOT / "skills" / "pdf-namer"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PDF_NAMER_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _touch_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n% synthetic test fixture\n")


def test_pdf_namer_learning_rules_skip_synthetic_case_paths(tmp_path):
    action = _load_module("pdf_namer_action_synthetic_guard", "action.py")
    action.CORRECTIONS_PATH = str(tmp_path / "_no_corrections.json")
    action.LEARNED_RULES_PATH = str(tmp_path / "_learned_filename_rules.json")
    _touch_pdf(
        tmp_path
        / "法扶案件"
        / "消費者債務清理"
        / "2099-9999-dummy-case"
        / "07_證據資料"
        / "20260101 測試證據.pdf"
    )
    _touch_pdf(
        tmp_path
        / "法扶案件"
        / "民事"
        / "2026-0002-林小華-一審-返還借款"
        / "07_證據資料"
        / "20260101 借據.pdf"
    )

    result = action.build_filename_learning_rules(case_root=str(tmp_path), min_token_count=1)

    assert result["sample_count"] == 1
    assert "測試證據" not in str(result)
    assert "借據" in str(result)


def test_pdf_namer_nightly_samples_skip_synthetic_case_paths(tmp_path):
    nightly = _load_module("pdf_namer_nightly_synthetic_guard", "nightly_train.py")
    _touch_pdf(
        tmp_path
        / "法扶案件"
        / "消費者債務清理"
        / "2099-9999-dummy-case"
        / "07_證據資料"
        / "20260101 測試證據.pdf"
    )
    _touch_pdf(
        tmp_path
        / "法扶案件"
        / "民事"
        / "2026-0002-林小華-一審-返還借款"
        / "07_證據資料"
        / "20260101 借據.pdf"
    )

    samples = nightly.collect_samples(case_root=str(tmp_path), max_files=10, shuffle=False)

    assert len(samples) == 1
    assert samples[0]["filename"] == "20260101 借據.pdf"
