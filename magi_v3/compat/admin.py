"""Lazy Website Admin compatibility server for the V3 control role."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Protocol

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AdminCompatibilityError(RuntimeError):
    pass


class HealthApplication(Protocol):
    def response(self, path: str) -> tuple[int, dict[str, object]]: ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_admin_module(
    website_root: Path,
    website_admin_sha256: str | None = None,
) -> ModuleType:
    source = website_root / "admin" / "admin_server.py"
    if source.is_symlink() or not source.is_file():
        raise AdminCompatibilityError(f"Website Admin source is missing or unsafe: {source}")
    expected = (
        website_admin_sha256
        if website_admin_sha256 is not None
        else os.environ.get("MAGI_WEBSITE_ADMIN_SHA256", "")
    ).strip()
    if expected:
        if not _SHA256.fullmatch(expected) or _sha256(source) != expected:
            raise AdminCompatibilityError("Website Admin source SHA-256 mismatch")
    name = f"magi_v3_website_admin_{_sha256(source)[:16]}"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise AdminCompatibilityError("Website Admin module loader is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = getattr(module, "AdminHandler", None)
    if not isinstance(handler, type) or not issubclass(handler, BaseHTTPRequestHandler):
        raise AdminCompatibilityError("Website Admin module does not expose AdminHandler")

    # The code is release-checked, while content and Git writes intentionally
    # target the separately backed-up mutable website checkout.
    module.REPO_ROOT = website_root
    module.DATA_FILE = website_root / "data" / "site-data.json"
    module.CONTENT_FILE = website_root / "data" / "content.json"
    module.ASSETS_DIR = website_root / "assets"
    module.ADMIN_CONFIG_FILE = website_root / "admin" / ".admin_config.json"
    default_password = getattr(module, "default_password", None)
    if callable(default_password):
        handler.password = default_password()
    return module


def _overlay_handler(
    legacy_handler: type[BaseHTTPRequestHandler],
    health_application: HealthApplication,
) -> type[BaseHTTPRequestHandler]:
    class Handler(legacy_handler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            route = self.path.partition("?")[0]
            if route not in {"/livez", "/readyz", "/health"}:
                return super().do_GET()
            try:
                status, payload = health_application.response(route)
            except Exception:
                status, payload = 503, {"status": "not_ready", "ready": False}
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    Handler.__name__ = "V3AdminCompatibilityHandler"
    return Handler


def create_admin_server(
    *,
    server_address: tuple[str, int],
    health_application: HealthApplication,
    website_root: str | Path,
    website_admin_sha256: str | None = None,
    server_factory: Callable[[tuple[str, int], type[BaseHTTPRequestHandler]], Any] = ThreadingHTTPServer,
) -> Any:
    """Create the existing admin server with V3 health routes overlaid."""

    raw_root = Path(website_root).expanduser()
    if not raw_root.is_absolute() or raw_root.is_symlink():
        raise AdminCompatibilityError("MAGI_WEBSITE_ROOT must be an absolute non-symlink path")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise AdminCompatibilityError(f"Website root is unavailable: {exc}") from exc
    if not root.is_dir():
        raise AdminCompatibilityError("Website root must be a directory")
    if server_address[0] not in {"127.0.0.1", "::1", "localhost"} or server_address[1] != 8088:
        raise AdminCompatibilityError("Website Admin must bind loopback port 8088")
    module = _load_admin_module(root, website_admin_sha256)
    return server_factory(server_address, _overlay_handler(module.AdminHandler, health_application))
