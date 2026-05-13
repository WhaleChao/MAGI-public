# -*- coding: utf-8 -*-
"""Tests for the optional Chandra OCR provider.

These tests deliberately do not download model weights or start vLLM.
"""

from __future__ import annotations

import os
from pathlib import Path


def test_probe_disabled_by_default(monkeypatch):
    from skills.engine.ocr import chandra_provider

    monkeypatch.delenv("MAGI_CHANDRA_OCR_ENABLE", raising=False)
    result = chandra_provider.probe(check_server=False)

    assert result.available is False
    assert "MAGI_CHANDRA_OCR_ENABLE" in result.reason


def test_probe_requires_model_license_acceptance(monkeypatch, tmp_path):
    from skills.engine.ocr import chandra_provider

    fake_cli = tmp_path / "chandra"
    fake_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_cli.chmod(0o755)

    monkeypatch.setenv("MAGI_CHANDRA_OCR_ENABLE", "1")
    monkeypatch.setenv("MAGI_CHANDRA_CLI", str(fake_cli))
    monkeypatch.delenv("MAGI_CHANDRA_ACCEPT_MODEL_LICENSE", raising=False)

    result = chandra_provider.probe(check_server=False)

    assert result.available is False
    assert "MAGI_CHANDRA_ACCEPT_MODEL_LICENSE" in result.reason


def test_probe_vllm_server_checked(monkeypatch, tmp_path):
    from skills.engine.ocr import chandra_provider

    fake_cli = tmp_path / "chandra"
    fake_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_cli.chmod(0o755)

    monkeypatch.setenv("MAGI_CHANDRA_OCR_ENABLE", "1")
    monkeypatch.setenv("MAGI_CHANDRA_ACCEPT_MODEL_LICENSE", "1")
    monkeypatch.setenv("MAGI_CHANDRA_CLI", str(fake_cli))

    def fake_reachable(base_url, timeout_sec=2.0):
        return False, "server down"

    monkeypatch.setattr(chandra_provider, "_vllm_server_reachable", fake_reachable)
    result = chandra_provider.probe(check_server=True)

    assert result.available is False
    assert result.reason == "server down"


def test_run_pdf_page_reads_markdown(monkeypatch, tmp_path):
    from skills.engine.ocr import chandra_provider

    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    fake_cli = tmp_path / "chandra"
    fake_cli.write_text(
        "#!/bin/sh\n"
        "mkdir -p \"$2/result\"\n"
        "printf '臺灣花蓮地方法院\\n114年度訴字第123號\\n' > \"$2/result/input.md\"\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)

    monkeypatch.setenv("MAGI_CHANDRA_OCR_ENABLE", "1")
    monkeypatch.setenv("MAGI_CHANDRA_ACCEPT_MODEL_LICENSE", "1")
    monkeypatch.setenv("MAGI_CHANDRA_CLI", str(fake_cli))
    monkeypatch.setattr(chandra_provider, "_vllm_server_reachable", lambda base_url, timeout_sec=2.0: (True, ""))

    result = chandra_provider.run_pdf_page(str(pdf), page_num=0, timeout_sec=5)

    assert result.success is True
    assert "臺灣花蓮地方法院" in result.text
    assert "--page-range" in (result.command or [])
    assert "1" in (result.command or [])


def test_default_cli_candidate_points_to_isolated_install():
    from skills.engine.ocr import chandra_provider

    # The installation step uses this isolated venv so MAGI's main venv is not polluted.
    assert "/tmp/magi_chandra_venv/bin/chandra" in chandra_provider._DEFAULT_CLI_CANDIDATES
