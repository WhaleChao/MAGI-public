# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path


def test_ingest_source_skips_extractor_exception(tmp_path, monkeypatch):
    import skills.obsidian.action as action

    source_root = tmp_path / "source"
    high_value = source_root / "04_我方歷次書狀"
    high_value.mkdir(parents=True)
    bad_pdf = high_value / "bad.pdf"
    bad_pdf.write_bytes(b"%PDF-1.4 broken")
    good_txt = high_value / "good.txt"
    good_txt.write_text("這是一份足夠長的測試文字，用來確認單一壞檔不會中斷整批 ingest。", encoding="utf-8")

    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setitem(action.SOURCE_ROOTS, "測試", source_root)
    monkeypatch.setattr(action, "_get_vault_path", lambda: vault)
    monkeypatch.setattr(action, "_load_ingest_state", lambda: {"files": {}})
    saved = {}
    monkeypatch.setattr(action, "_save_ingest_state", lambda state: saved.update(state))
    monkeypatch.setattr(action, "_load_index", lambda: {"notes": {}})
    monkeypatch.setattr(action, "_save_index", lambda idx: None)

    fake_vector = types.ModuleType("skills.documents.vector_pipeline")
    fake_vector.ingest_text_to_vector_memory = lambda **kw: {
        "success": True,
        "doc_key": "doc-1",
        "chunks_written": 1,
    }
    monkeypatch.setitem(sys.modules, "skills.documents.vector_pipeline", fake_vector)

    import skills.obsidian.extractors as extractors

    def fake_extract_text(path: Path):
        if path.name == "bad.pdf":
            raise RuntimeError("broken pdf")
        return {"success": True, "text": "可索引文字" * 20, "pages": 1, "method": "fake"}

    monkeypatch.setattr(extractors, "extract_text", fake_extract_text)
    monkeypatch.setattr(extractors, "file_hash", lambda path: path.stem)
    importlib.reload(action)
    monkeypatch.setitem(action.SOURCE_ROOTS, "測試", source_root)
    monkeypatch.setattr(action, "_get_vault_path", lambda: vault)
    monkeypatch.setattr(action, "_load_ingest_state", lambda: {"files": {}})
    monkeypatch.setattr(action, "_save_ingest_state", lambda state: saved.update(state))
    monkeypatch.setattr(action, "_load_index", lambda: {"notes": {}})
    monkeypatch.setattr(action, "_save_index", lambda idx: None)
    monkeypatch.setattr(extractors, "extract_text", fake_extract_text)
    monkeypatch.setattr(extractors, "file_hash", lambda path: path.stem)

    result = action.task_ingest_source(source="測試", include_folders="high-value", limit=10)

    assert result["success"] is True
    assert result["processed"] == 1
    assert result["errors"] == 1
    assert result["error_details"][0]["path"].endswith("bad.pdf")
    assert "good.txt" in json.dumps(saved, ensure_ascii=False)


def test_ingest_source_malformed_pdf_becomes_warning(tmp_path, monkeypatch):
    import skills.obsidian.action as action

    source_root = tmp_path / "source"
    high_value = source_root / "04_我方歷次書狀"
    high_value.mkdir(parents=True)
    bad_pdf = high_value / "bad.pdf"
    bad_pdf.write_bytes(b"%PDF-1.4 malformed")
    good_txt = high_value / "good.txt"
    good_txt.write_text("這是一份可匯入的正常文本。" * 10, encoding="utf-8")

    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setitem(action.SOURCE_ROOTS, "測試", source_root)
    monkeypatch.setattr(action, "_get_vault_path", lambda: vault)
    monkeypatch.setattr(action, "_load_ingest_state", lambda: {"files": {}})
    monkeypatch.setattr(action, "_save_ingest_state", lambda state: None)
    monkeypatch.setattr(action, "_load_index", lambda: {"notes": {}})
    monkeypatch.setattr(action, "_save_index", lambda idx: None)

    fake_vector = types.ModuleType("skills.documents.vector_pipeline")
    fake_vector.ingest_text_to_vector_memory = lambda **kw: {
        "success": True,
        "doc_key": "doc-1",
        "chunks_written": 1,
    }
    monkeypatch.setitem(sys.modules, "skills.documents.vector_pipeline", fake_vector)

    import skills.obsidian.extractors as extractors

    def fake_extract_text(path: Path):
        if path.name == "bad.pdf":
            return {"success": False, "error": "Syntax Warning: May not be a PDF file"}
        return {"success": True, "text": "可索引文字" * 20, "pages": 1, "method": "fake"}

    monkeypatch.setattr(extractors, "extract_text", fake_extract_text)
    monkeypatch.setattr(extractors, "file_hash", lambda path: path.stem)
    importlib.reload(action)
    monkeypatch.setitem(action.SOURCE_ROOTS, "測試", source_root)
    monkeypatch.setattr(action, "_get_vault_path", lambda: vault)
    monkeypatch.setattr(action, "_load_ingest_state", lambda: {"files": {}})
    monkeypatch.setattr(action, "_save_ingest_state", lambda state: None)
    monkeypatch.setattr(action, "_load_index", lambda: {"notes": {}})
    monkeypatch.setattr(action, "_save_index", lambda idx: None)
    monkeypatch.setattr(extractors, "extract_text", fake_extract_text)
    monkeypatch.setattr(extractors, "file_hash", lambda path: path.stem)

    result = action.task_ingest_source(source="測試", include_folders="high-value", limit=10)

    assert result["success"] is True
    assert result["processed"] == 1
    assert result["errors"] == 0
    assert result["warnings"] == 1
    assert result["malformed_pdf_skipped"] == 1
    assert result["warning_details"][0]["kind"] == "malformed_pdf"
