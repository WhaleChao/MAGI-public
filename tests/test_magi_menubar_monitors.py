from __future__ import annotations

from gui.magi_menubar import _service_alive


def test_service_alive_accepts_display_name_aliases():
    assert _service_alive({"主伺服器": True}, "主伺服器", "Server") is True
    assert _service_alive({"Server": True}, "主伺服器", "Server") is True
    assert _service_alive({"主伺服器": False}, "主伺服器", "Server") is False
