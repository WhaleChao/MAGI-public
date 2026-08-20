from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from magi_v3.compat.admin import AdminCompatibilityError, create_admin_server


ADMIN_SOURCE = '''
from http.server import BaseHTTPRequestHandler
class AdminHandler(BaseHTTPRequestHandler):
    password = ""
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"legacy-admin")
    def do_POST(self):
        self.send_response(201); self.end_headers()
def default_password():
    return "configured-password"
'''


class Health:
    def response(self, path: str):
        return (200, {"status": "ready", "ready": True, "path": path})


class FakeServer:
    def __init__(self, address, handler):
        self.address = address
        self.handler = handler


def _website(tmp_path: Path) -> Path:
    root = tmp_path / "website"
    source = root / "admin" / "admin_server.py"
    source.parent.mkdir(parents=True)
    source.write_text(ADMIN_SOURCE, encoding="utf-8")
    (root / "data").mkdir()
    (root / "assets").mkdir()
    return root


def test_admin_factory_is_hash_bound_and_preserves_legacy_handler(tmp_path: Path, monkeypatch) -> None:
    root = _website(tmp_path)
    source = root / "admin" / "admin_server.py"
    monkeypatch.setenv("MAGI_WEBSITE_ADMIN_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())

    server = create_admin_server(
        server_address=("127.0.0.1", 8088),
        health_application=Health(),
        website_root=root,
        server_factory=FakeServer,
    )

    assert server.address == ("127.0.0.1", 8088)
    assert server.handler.__mro__[1].password == "configured-password"
    assert "do_POST" in server.handler.__mro__[1].__dict__


def test_admin_factory_rejects_hash_drift_external_bind_and_missing_source(
    tmp_path: Path, monkeypatch
) -> None:
    root = _website(tmp_path)
    monkeypatch.setenv("MAGI_WEBSITE_ADMIN_SHA256", "0" * 64)
    with pytest.raises(AdminCompatibilityError, match="SHA-256"):
        create_admin_server(
            server_address=("127.0.0.1", 8088),
            health_application=Health(),
            website_root=root,
            server_factory=FakeServer,
        )
    monkeypatch.delenv("MAGI_WEBSITE_ADMIN_SHA256")
    with pytest.raises(AdminCompatibilityError, match="loopback"):
        create_admin_server(
            server_address=("0.0.0.0", 8088),
            health_application=Health(),
            website_root=root,
            server_factory=FakeServer,
        )
    with pytest.raises(AdminCompatibilityError, match="missing"):
        create_admin_server(
            server_address=("127.0.0.1", 8088),
            health_application=Health(),
            website_root=tmp_path,
            server_factory=FakeServer,
        )
