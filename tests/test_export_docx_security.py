from __future__ import annotations

import importlib


def _reload_export_docx(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGI_EXPORTS_DIR", str(tmp_path / "exports"))
    import skills.ops.export_docx as export_docx

    return importlib.reload(export_docx)


def test_export_bilingual_docx_rejects_path_traversal_filename(monkeypatch, tmp_path):
    export_docx = _reload_export_docx(monkeypatch, tmp_path)

    result = export_docx.export_bilingual_docx(
        [{"page": 1, "source": "A", "target": "B"}],
        filename="../escape.docx",
    )

    assert result["success"] is False
    assert "basename" in result["error"]
    assert not (tmp_path / "escape.docx").exists()


def test_export_summary_docx_rejects_absolute_filename(monkeypatch, tmp_path):
    export_docx = _reload_export_docx(monkeypatch, tmp_path)

    result = export_docx.export_summary_docx(
        [{"heading": "H", "summary": "S", "excerpt": "E"}],
        filename=str(tmp_path / "escape.docx"),
    )

    assert result["success"] is False
    assert "basename" in result["error"]


def test_export_transcript_docx_rejects_non_docx_filename(monkeypatch, tmp_path):
    export_docx = _reload_export_docx(monkeypatch, tmp_path)

    result = export_docx.export_transcript_docx(
        [{"speaker": "A", "time": "00:00", "content": "hello"}],
        filename="transcript.txt",
    )

    assert result["success"] is False
    assert "must end with .docx" in result["error"]
