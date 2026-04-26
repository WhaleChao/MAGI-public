from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


MAGI_ROOT = Path(__file__).resolve().parent.parent
INDEXER_PATH = MAGI_ROOT / "skills" / "transcript-indexer" / "action.py"


def _load_indexer_module():
    spec = importlib.util.spec_from_file_location("transcript_indexer_test", INDEXER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_index_partial_semantics_returns_success_for_mixed_outcome(tmp_path, monkeypatch, capsys):
    mod = _load_indexer_module()

    pdf_ok = tmp_path / "ok.pdf"
    pdf_fail = tmp_path / "fail.pdf"
    pdf_ok.write_bytes(b"%PDF-1.4\n")
    pdf_fail.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(mod, "TRANSCRIPT_BUDGET_SEC", 9999)
    monkeypatch.setattr(mod, "TRANSCRIPT_MAX_PDFS_PER_RUN", 9999)
    monkeypatch.setattr(mod, "TRANSCRIPT_LISTING_BUDGET_SEC", 9999)
    monkeypatch.setattr(mod, "_load_index", lambda: {"indexed": {}, "stats": {"total_chunks": 0, "total_files": 0}})
    monkeypatch.setattr(mod, "_save_index", lambda idx: None)
    monkeypatch.setattr(
        mod,
        "_iter_transcript_pdfs",
        lambda: iter([(pdf_ok, "Case-A", "05_筆錄"), (pdf_fail, "Case-B", "05_筆錄")]),
    )
    monkeypatch.setattr(mod, "_extract_pages", lambda _p: [(1, "法官：這是測試內容。")])
    monkeypatch.setattr(
        mod,
        "_parse_chunks",
        lambda pages, pdf_path, case_name, date_str, transcript_type: [
            {
                "text": f"{case_name} chunk",
                "speaker": "法官",
                "page": 1,
                "line_start": 1,
                "line_end": 1,
                "date": "2026-04-26",
                "case_name": case_name,
                "transcript_type": transcript_type,
                "file_name": pdf_path.name,
            }
        ],
    )

    calls = {"n": 0}

    def _remember_batch(_batch):
        calls["n"] += 1
        return {"ok": calls["n"] == 1}

    monkeypatch.setattr(mod, "_get_mem_bridge", lambda: (_remember_batch, lambda *a, **k: []))

    summary = mod.cmd_index(force=True)
    assert summary["status"] == "partial"
    assert summary["success"] is True
    assert summary["fatal"] is False
    assert summary["errors_count"] == 1

    calls["n"] = 0
    monkeypatch.setattr(sys, "argv", ["action.py", "--task", "index", "--force", "1"])
    rc = mod.main()
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "partial"
