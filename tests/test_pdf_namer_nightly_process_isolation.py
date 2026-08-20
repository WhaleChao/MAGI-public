from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import fitz


def _module():
    path = Path(__file__).resolve().parents[1] / "skills" / "pdf-namer" / "nightly_train.py"
    spec = importlib.util.spec_from_file_location("pdf_namer_nightly_process_isolation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nightly_analyze_runs_each_sample_in_bounded_child(monkeypatch):
    nightly = _module()
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"date": "20260715"}))

    monkeypatch.setattr(nightly.subprocess, "run", fake_run)

    result = nightly.analyze_one("/private/input.pdf")

    assert result == {"date": "20260715"}
    assert observed["command"][1].endswith("/action.py")
    assert observed["command"][-4:] == ["--task", "analyze", "--path", "/private/input.pdf"]
    assert observed["timeout"] == 180
    assert observed["check"] is False
    assert observed["env"]["MAGI_PDF_NAMER_TRUST_PREFIX_FIRST"] == "0"


def test_nightly_analyze_fails_closed_without_leaking_worker_output(monkeypatch):
    nightly = _module()
    monkeypatch.setattr(
        nightly.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="private payload"),
    )

    assert nightly.analyze_one("/private/input.pdf") == {"error": "sample_worker_exit:1"}


def test_nightly_hashes_pdf_in_bounded_chunks(tmp_path):
    nightly = _module()
    pdf = tmp_path / "sample.pdf"
    content = b"sample-pdf" * 1000
    pdf.write_bytes(content)

    assert nightly._sha256_file(str(pdf), chunk_size=17) == __import__("hashlib").sha256(content).hexdigest()


def test_nightly_does_not_reopen_nas_pdf_when_analyzer_omits_hash(tmp_path, monkeypatch):
    nightly = _module()
    filename = "20260716 臺灣花蓮地方法院115年度訴字第1號民事判決（測試當事人）.pdf"
    monkeypatch.setattr(
        nightly,
        "collect_samples",
        lambda **_kwargs: [{
            "path": "/remote/nas/sample.pdf",
            "filename": filename,
            "subfolder": "08_判決書",
            "label": "判決",
            "ground_truth": nightly._parse_existing_filename(filename),
        }],
    )
    monkeypatch.setattr(
        nightly,
        "analyze_one",
        lambda _path: {
            "suggested_filename": filename,
            "doc_type": "民事判決",
            "parties": ["測試當事人"],
            "date": "20260716",
            "date_method": "bounded_analyzer",
            "confidence": 0.9,
        },
    )
    monkeypatch.setattr(
        nightly,
        "_sha256_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("NAS PDF reopened")),
    )
    nightly.REPORT_PATH = str(tmp_path / "nightly-report.json")

    report = nightly.run_training(max_files=1, dry_run=True, report_only=True)

    assert report["status"] == "completed"
    assert report["analyzed"] == 1
    assert report["sample_manifest"][0]["pdf_sha256"] is None
    assert report["sample_manifest"][0]["pdf_sha256_available"] is False


def test_nightly_defers_transient_nas_scan_error(monkeypatch):
    nightly = _module()
    monkeypatch.setattr(
        nightly,
        "collect_samples",
        lambda **_kwargs: (_ for _ in ()).throw(OSError(6, "Device not configured")),
    )

    report = nightly.run_training(max_files=1, dry_run=True, report_only=True)

    assert report["status"] == "deferred"
    assert report["reason"] == "storage_device_temporarily_unavailable"
    assert report["errors"] == 0


def test_nightly_fixture_runs_real_pdf_child_and_writes_bound_manifest(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    state = fixture / "state"
    case_root = fixture / "cases"
    sample_dir = case_root / "2026-0001-測試當事人-民事-一審-損害賠償" / "08_判決書"
    sample_dir.mkdir(parents=True)
    state.mkdir()
    (fixture / ".magi-v3-schedule-fixture").write_text("owned\n", encoding="utf-8")
    filename = "20260716 臺灣花蓮地方法院115年度訴字第1號民事判決（測試當事人；原告之訴駁回）.pdf"
    pdf = sample_dir / filename
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "CASE 2026 fixture judgment")
    document.save(pdf)
    document.close()
    provider = fixture / "analysis-provider.json"
    provider.write_text(
        json.dumps(
            {
                "schema": "magi.v3.pdf-namer-analysis-fixture/v1",
                "proposals": {
                    filename: {
                        "expected_text": "CASE 2026 fixture judgment",
                        "suggested_filename": filename,
                        "doc_type": "民事判決",
                        "party": "測試當事人",
                        "date": "20260716",
                        "confidence": 0.95,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_V3_SCHEDULE_ADAPTER", "real_entrypoint_fixture_v1")
    monkeypatch.setenv("MAGI_V3_SCHEDULE_DRY_RUN", "1")
    monkeypatch.setenv("MAGI_V3_SCHEDULE_FIXTURE_ROOT", str(fixture))
    monkeypatch.setenv("MAGI_PDF_NAMER_ANALYSIS_FIXTURE_PATH", str(provider))
    monkeypatch.setenv("MAGI_PDF_NAMER_STATE_DIR", str(state))
    monkeypatch.setenv("MAGI_CASE_ROOT", str(case_root))
    # Loading PyMuPDF and the sealed child runtime can exceed 30 seconds on
    # the supported 16 GB Mac while LIVE cron work is active.  Keep a hard
    # bound, but align the fixture with the production child budget so the
    # test measures the output contract instead of transient startup load.
    monkeypatch.setenv("MAGI_PDF_NAMER_TRAIN_SAMPLE_TIMEOUT_SEC", "90")
    nightly = _module()

    report = nightly.run_training(max_files=1, dry_run=True, report_only=True)

    assert report["analyzed"] == 1
    assert report["errors"] == 0
    assert report["metrics"]["date_accuracy_pct"] == 100.0
    assert report["metrics"]["party_accuracy_pct"] == 100.0
    assert report["metrics"]["format_valid_pct"] == 100.0
    assert report["provider_quality_certified"] is False
    item = report["sample_manifest"][0]
    assert item["predicted_filename"] == filename
    assert item["parsed_page_count"] == 1
    assert len(item["pdf_sha256"]) == 64
    assert len(item["parsed_text_sha256"]) == 64
    assert pdf.is_file()
    assert not any(path.is_symlink() for path in fixture.rglob("*"))
