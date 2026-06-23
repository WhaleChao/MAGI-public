from __future__ import annotations

import importlib.util
from pathlib import Path

import fitz


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "skills" / "pdf-bookmarker" / "action.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("pdf_bookmarker_action_live_test", ACTION)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_scan_and_bookmark_writes_page1_bookmark_for_single_doc_filename(tmp_path: Path):
    pdf = tmp_path / "20250718 告知上訴權益同意書(余秋菊)_已簽名.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(pdf)
    doc.close()

    mod = _load_module()
    result = mod.scan_and_bookmark(str(pdf))

    assert result["success"] is True
    assert result["bookmarks"] == 1
    assert result["classification"] == "legitimate_single_doc"
    reopened = fitz.open(pdf)
    try:
        toc = reopened.get_toc()
    finally:
        reopened.close()
    assert toc == [[1, "20250718 告知上訴權益同意書(余秋菊)_已簽名", 1]]
