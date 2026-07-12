from __future__ import annotations

import importlib
import subprocess


def _write_blank_pdf(path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with open(path, "wb") as f:
        writer.write(f)


def _reload_pdf_action(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    exports = allowed / "exports"
    allowed.mkdir()
    exports.mkdir()
    monkeypatch.setenv("MAGI_EXPORTS_DIR", str(exports))
    monkeypatch.setenv("MAGI_PDF_TOOL_ALLOWED_ROOTS", str(allowed))
    import skills.pdf.action as pdf_action

    return importlib.reload(pdf_action), allowed, exports


def test_pdf_tool_rejects_output_outside_allowed_roots(monkeypatch, tmp_path):
    pdf_action, allowed, _exports = _reload_pdf_action(monkeypatch, tmp_path)
    source = allowed / "input.pdf"
    _write_blank_pdf(source)
    outside = pdf_action._MAGI_ROOT / "outside_pdf_contract_escape.pdf"

    result = pdf_action.dispatch(f"merge --files {source} --output {outside}")

    assert result["ok"] is False
    assert "outside allowed roots" in result["error"]
    assert not outside.exists()


def test_pdf_tool_allows_output_inside_managed_exports(monkeypatch, tmp_path):
    pdf_action, allowed, exports = _reload_pdf_action(monkeypatch, tmp_path)
    source = allowed / "input.pdf"
    _write_blank_pdf(source)
    output = exports / "merged.pdf"

    result = pdf_action.dispatch(f"merge --files {source} --output {output}")

    assert result["ok"] is True
    assert result["output"] == str(output.resolve())
    assert output.exists()


def test_pdfimages_branch_uses_timeout_and_cleans_stage(monkeypatch, tmp_path):
    pdf_action, allowed, _exports = _reload_pdf_action(monkeypatch, tmp_path)
    source = allowed / "input.pdf"
    _write_blank_pdf(source)
    out_dir = allowed / "images"
    seen = {}

    monkeypatch.setattr(pdf_action.shutil, "which", lambda name: "/usr/bin/pdfimages" if name == "pdfimages" else None)

    def _timeout_run(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(pdf_action.subprocess, "run", _timeout_run)

    result = pdf_action.dispatch(f"images --file {source} --output-dir {out_dir}")

    assert result["ok"] is False
    assert seen["timeout"] is not None
    assert not list(allowed.glob(".images*.tmp"))
