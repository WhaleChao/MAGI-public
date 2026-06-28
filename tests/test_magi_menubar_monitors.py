from __future__ import annotations

import json

from gui.magi_menubar import (
    _check_omlx,
    _magi_process_memory_icon,
    _omlx_text_status,
    _service_alive,
    _system_memory_icon,
)


def test_service_alive_accepts_display_name_aliases():
    assert _service_alive({"主伺服器": True}, "主伺服器", "Server") is True
    assert _service_alive({"Server": True}, "主伺服器", "Server") is True
    assert _service_alive({"主伺服器": False}, "主伺服器", "Server") is False


def test_menubar_text_status_marks_day_e4b_primary():
    status = _omlx_text_status("gemma-4-e4b-it-4bit", "day", "e4b", "day")
    assert status["icon"] == "🟢"
    assert status["degraded"] is False
    assert "日間4B" in status["label"]


def test_menubar_text_status_marks_night_e4b_degraded_fallback():
    status = _omlx_text_status("gemma-4-e4b-it-4bit", "night", "26b", "night-e4b-degraded")
    assert status["icon"] == "🟡"
    assert status["degraded"] is True
    assert "夜間降級E4B" in status["label"]
    assert "預期26B" in status["label"]


def test_check_omlx_returns_actual_model_instead_of_legacy_main_model_filter(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "gemma-4-12B-it-4bit"}]}).encode("utf-8")

    monkeypatch.setenv("MAGI_MAIN_MODEL", "gemma-4-e4b-it-4bit")
    monkeypatch.setattr("gui.magi_menubar.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())

    assert _check_omlx(8080) == "gemma-4-12B-it-4bit"


def test_menubar_memory_icons_match_operational_health_thresholds():
    assert _system_memory_icon(69.9) == "🟢"
    assert _system_memory_icon(85.0) == "🟡"
    assert _system_memory_icon(92.0) == "🔴"

    assert _magi_process_memory_icon(2368) == "🟢"
    assert _magi_process_memory_icon(4096) == "🟡"
    assert _magi_process_memory_icon(8192) == "🔴"
