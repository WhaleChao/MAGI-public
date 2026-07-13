# -*- coding: utf-8 -*-
from __future__ import annotations


def test_chandra_healthcheck_does_not_persist_raw_text(monkeypatch):
    from scripts.ops import chandra_ocr_healthcheck as mod

    class FakeProbe:
        available = True

        def to_dict(self):
            return {
                "available": True,
                "reason": "",
                "uses_qwen_backend": True,
                "private_only": True,
            }

    class FakeResult:
        success = True
        text = "臺灣花蓮地方法院\n114年度訴字第123號\n王小明"
        error = ""
        duration_sec = 0.5

    monkeypatch.setattr(mod.chandra_provider, "probe", lambda check_server=True: FakeProbe())
    monkeypatch.setattr(mod.chandra_provider, "run_pdf_page", lambda *a, **k: FakeResult())

    report = mod.run("/tmp/example.pdf", page=0, timeout_sec=1)

    assert report["ok"] is True
    assert report["live_pdf"]["text_len"] == len(FakeResult.text)
    assert report["live_pdf"]["entity_counts"]["case_numbers_found"] >= 1
    assert "臺灣花蓮地方法院" not in str(report)
    assert "王小明" not in str(report)
