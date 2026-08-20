"""Read-only network surfaces for an isolated V3 lifecycle validation.

These factories intentionally do not import legacy applications.  They prove
launchd ownership, port binding, health composition, and a fixed sandbox file
read while making every unreviewed route unavailable.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from .service_manifest import assert_deployment_safety
from .service_runtime import ServiceRuntimeError

_MODE = "isolated_live_validation"
_PREVIEW_ROUTES = frozenset(
    {
        "/validation/osc/document-preview",
        "/validation/osc/document-download",
    }
)


class HealthApplication(Protocol):
    def response(self, path: str) -> tuple[int, dict[str, object]]: ...


def _environment(environ: Mapping[str, str] | None = None) -> Mapping[str, str]:
    env = os.environ if environ is None else environ
    assert_deployment_safety(_MODE, env)
    return env


def _fixture_path(environ: Mapping[str, str] | None = None) -> Path:
    env = _environment(environ)
    raw = env.get("MAGI_WEBSITE_ROOT", "").strip()
    if not raw:
        raise ServiceRuntimeError("validation website root is required")
    root = Path(raw).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise ServiceRuntimeError("validation website root must be absolute and non-symlink")
    root = root.resolve(strict=True)
    fixture = root / "data" / "live-validation-document.txt"
    if fixture.is_symlink() or not fixture.is_file():
        raise ServiceRuntimeError("validation document fixture is missing or unsafe")
    resolved = fixture.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ServiceRuntimeError("validation document fixture escapes its sandbox") from exc
    if resolved.stat().st_size > 1024 * 1024:
        raise ServiceRuntimeError("validation document fixture exceeds 1 MiB")
    return resolved


def _response(
    start_response: Callable[..., Any],
    status: str,
    body: bytes,
    *,
    content_type: str,
    headers: Iterable[tuple[str, str]] = (),
    head: bool = False,
) -> list[bytes]:
    response_headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-MAGI-Validation-Mode", _MODE),
        *headers,
    ]
    start_response(status, response_headers)
    return [b"" if head else body]


class ValidationWSGIApp:
    def __init__(self, *, surface: str) -> None:
        _environment()
        self.surface = surface

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "")).partition("?")[0]
        if method not in {"GET", "HEAD"}:
            return _response(
                start_response,
                "405 Method Not Allowed",
                b'{"status":"method_not_allowed"}',
                content_type="application/json",
                headers=(("Allow", "GET, HEAD"),),
                head=method == "HEAD",
            )
        if path == "/validation/ping":
            body = json.dumps(
                {"status": "ok", "mode": _MODE, "surface": self.surface},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            return _response(
                start_response,
                "200 OK",
                body,
                content_type="application/json",
                head=method == "HEAD",
            )
        if path in _PREVIEW_ROUTES:
            body = _fixture_path().read_bytes()
            disposition = (
                "attachment; filename=live-validation-document.txt"
                if path.endswith("download")
                else "inline; filename=live-validation-document.txt"
            )
            return _response(
                start_response,
                "200 OK",
                body,
                content_type="text/plain; charset=utf-8",
                headers=(("Content-Disposition", disposition),),
                head=method == "HEAD",
            )
        return _response(
            start_response,
            "404 Not Found",
            b'{"status":"not_found"}',
            content_type="application/json",
            head=method == "HEAD",
        )


def create_main_app() -> ValidationWSGIApp:
    return ValidationWSGIApp(surface="main_http")


def create_tools_app() -> ValidationWSGIApp:
    return ValidationWSGIApp(surface="tools_http")


def create_admin_server(
    *,
    server_address: tuple[str, int],
    health_application: HealthApplication,
    website_root: str | Path,
    website_admin_sha256: str | None = None,
    server_factory: Callable[[tuple[str, int], type[BaseHTTPRequestHandler]], Any] = ThreadingHTTPServer,
) -> Any:
    """Build a health-only 8088 server; all admin mutations stay absent."""

    del website_admin_sha256
    env = _environment()
    expected_root = Path(env["MAGI_WEBSITE_ROOT"]).expanduser().resolve(strict=True)
    supplied_root = Path(website_root).expanduser().resolve(strict=True)
    if supplied_root != expected_root:
        raise ServiceRuntimeError("validation admin website root binding mismatch")
    if server_address[0] not in {"127.0.0.1", "::1", "localhost"} or server_address[1] != 8088:
        raise ServiceRuntimeError("validation admin must bind loopback port 8088")

    class Handler(BaseHTTPRequestHandler):
        def _respond(self, *, write_body: bool) -> None:
            route = self.path.partition("?")[0]
            if route == "/validation/ping":
                status, payload = 200, {"status": "ok", "mode": _MODE, "surface": "control"}
            elif route in {"/livez", "/readyz", "/health"}:
                try:
                    status, payload = health_application.response(route)
                except Exception:
                    status, payload = 503, {"status": "not_ready", "ready": False}
            else:
                status, payload = 404, {"status": "not_found"}
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-MAGI-Validation-Mode", _MODE)
            self.end_headers()
            if write_body:
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            self._respond(write_body=True)

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
            self._respond(write_body=False)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            self.send_error(405)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return server_factory(server_address, Handler)
