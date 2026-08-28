from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from flask import Flask, Response, request, session
from werkzeug.test import Client

from magi_v3.compat import (
    CompatibilityLoadError,
    LazyCompatibilityApp,
    RouteInventory,
    create_app,
    create_main_app,
    create_tools_app,
    inventory_report,
)
from magi_v3.compat.gateway import RouteSpec, verify_loaded_surface

ROOT = Path(__file__).resolve().parents[2]


def test_import_is_lazy_and_cannot_open_socket_or_start_process() -> None:
    code = """
import socket
import subprocess
import sys

def blocked(*args, **kwargs):
    raise AssertionError("import attempted an external side effect")

socket.socket = blocked
subprocess.Popen = blocked
import magi_v3.compat
assert "api.server" not in sys.modules
assert "api.tools_api" not in sys.modules
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_pinned_inventory_covers_both_apps_and_sensitive_http_shapes() -> None:
    inventory = RouteInventory.load()
    report = inventory_report()

    assert inventory.counts == {"5002": 280, "5003": 67, "total": 347}
    assert report["route_methods"] == 431
    assert report["cross_service_conflicts"] == [
        {"rule": "/health", "method": "GET", "services": ["5002", "5003"]},
        {"rule": "/livez", "method": "GET", "services": ["5002", "5003"]},
    ]
    signatures = {(route.service, route.rule, route.endpoint) for route in inventory.routes}
    assert {
        ("5002", "/login", "login"),
        ("5002", "/logout", "logout"),
        ("5002", "/callback", "callback"),
        ("5002", "/line/webhook", "callback"),
        ("5002", "/telegram/webhook", "telegram.telegram_webhook"),
        ("5002", "/api/osc/files/upload-multi", "osc_files.osc_files_upload_multi_api"),
        ("5003", "/osc/external/chat", "external_osc_chat"),
        ("5003", "/shortcut/pdf_text", "api_shortcut_pdf_text"),
    } <= signatures


def test_factory_does_not_call_legacy_loader_until_first_request() -> None:
    calls: list[str] = []

    def loader(service: str):
        calls.append(service)
        raise AssertionError("not expected during factory creation")

    app = create_app("5002", loader=loader)

    assert calls == []
    assert app.status() == {
        "service": "5002",
        "loaded": False,
        "route_count": 280,
        "error": "",
        "startup_hooks_enabled": False,
    }


def test_fixed_production_factories_bind_their_declared_service() -> None:
    assert create_main_app().service == "5002"
    assert create_tools_app().service == "5003"


def _representative_app(seen: dict[str, object]) -> Flask:
    app = Flask(__name__)
    app.secret_key = "compat-test-secret"

    @app.post("/callback")
    def callback() -> Response:
        seen["callback"] = (request.get_data(), request.headers.get("X-Line-Signature"))
        response = Response(b"OK", status=202, content_type="text/plain; charset=utf-8")
        response.headers.add("X-Compat", "first")
        response.headers.add("X-Compat", "second")
        return response

    @app.get("/login")
    def login() -> Response:
        session["user_id"] = "u-7"
        return Response("logged-in")

    @app.get("/session")
    def session_state() -> dict[str, object]:
        return {"user_id": session.get("user_id")}

    @app.post("/osc/external/chat")
    def stream() -> Response:
        def events():
            yield b'data: {"delta":"one"}\n\n'
            yield b"data: [DONE]\n\n"

        return Response(events(), content_type="text/event-stream")

    @app.post("/shortcut/pdf_text")
    def upload() -> Response:
        uploaded = request.files["file"]
        seen["upload"] = (uploaded.filename, uploaded.content_type, uploaded.read())
        return Response("parsed", status=207, content_type="text/plain; charset=utf-8")

    return app


def test_wsgi_passthrough_preserves_body_duplicate_headers_stream_upload_and_session() -> None:
    seen: dict[str, object] = {}
    legacy = _representative_app(seen)
    loads: list[str] = []

    def loader(service: str):
        loads.append(service)
        return legacy

    adapter = LazyCompatibilityApp(
        "5002",
        inventory=RouteInventory(()),
        loader=loader,
        verifier=lambda app, service, inventory: None,
    )
    client = Client(adapter, Response, use_cookies=True)

    callback = client.post("/callback", data=b'{"event":1}', headers={"X-Line-Signature": "signed"})
    assert callback.status_code == 202
    assert callback.get_data() == b"OK"
    assert callback.headers.getlist("X-Compat") == ["first", "second"]
    assert seen["callback"] == (b'{"event":1}', "signed")

    login = client.get("/login")
    assert login.status_code == 200
    assert login.headers.getlist("Set-Cookie")
    assert client.get("/session").json == {"user_id": "u-7"}

    stream = client.post("/osc/external/chat", buffered=False)
    assert stream.content_type == "text/event-stream"
    assert list(stream.response) == [b'data: {"delta":"one"}\n\n', b"data: [DONE]\n\n"]

    upload = client.post(
        "/shortcut/pdf_text",
        data={"file": (io.BytesIO(b"%PDF-compatible\n"), "brief.pdf", "application/pdf")},
    )
    assert upload.status_code == 207
    assert upload.get_data() == b"parsed"
    assert seen["upload"] == ("brief.pdf", "application/pdf", b"%PDF-compatible\n")
    assert loads == ["5002"]


def test_loaded_url_map_must_exactly_match_service_inventory() -> None:
    app = Flask(__name__)
    app.add_url_rule("/health", endpoint="health", view_func=lambda: "ok", methods=["GET"])
    inventory = RouteInventory((RouteSpec("5003", "/health", ("GET",), "health"),))

    verify_loaded_surface(app, "5003", inventory)

    drift = RouteInventory((RouteSpec("5003", "/readyz", ("GET",), "readyz"),))
    with pytest.raises(CompatibilityLoadError, match="route surface mismatch"):
        LazyCompatibilityApp("5003", inventory=drift, loader=lambda service: app).load()


def test_declared_native_extensions_do_not_redefine_legacy_inventory() -> None:
    app = Flask(__name__)
    app.add_url_rule(
        "/tools",
        endpoint="video_studio.public_tools_page",
        view_func=lambda: "tools",
        methods=["GET"],
    )
    app.add_url_rule(
        "/video-studio",
        endpoint="video_studio.video_studio_page",
        view_func=lambda: "video",
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/video-studio/health",
        endpoint="video_studio.video_studio_health",
        view_func=lambda: "health",
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/video-studio/render",
        endpoint="video_studio.video_studio_render",
        view_func=lambda: "render",
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/video-studio/interpret",
        endpoint="video_studio.video_studio_interpret",
        view_func=lambda: "interpret",
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/video-studio/render-assets",
        endpoint="video_studio.video_studio_render_assets",
        view_func=lambda: "render assets",
        methods=["POST"],
    )
    app.add_url_rule(
        "/cookie-cutter",
        endpoint="cookie_cutter.cookie_cutter_page",
        view_func=lambda: "cookie cutter",
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/cookie-cutter/prepare",
        endpoint="cookie_cutter.cookie_cutter_prepare_api",
        view_func=lambda: "prepare",
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/cookie-cutter/generate",
        endpoint="cookie_cutter.cookie_cutter_generate_api",
        view_func=lambda: "generate",
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/cookie-cutter/health",
        endpoint="cookie_cutter.cookie_cutter_health_api",
        view_func=lambda: "health",
        methods=["GET"],
    )
    app.add_url_rule(
        "/exam-tutor",
        endpoint="exam_tutor.exam_tutor_page",
        view_func=lambda: "page",
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/exam-tutor/review",
        endpoint="exam_tutor.exam_tutor_review_api",
        view_func=lambda: "review",
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/exam-tutor/choice-bank",
        endpoint="exam_tutor.exam_tutor_choice_bank_api",
        view_func=lambda: "bank",
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/exam-tutor/choice-attempt",
        endpoint="exam_tutor.exam_tutor_choice_attempt_api",
        view_func=lambda: "attempt",
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/exam-tutor/choice-import",
        endpoint="exam_tutor.exam_tutor_choice_import_api",
        view_func=lambda: "import",
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/exam-tutor/essay-bank",
        endpoint="exam_tutor.exam_tutor_essay_bank_api",
        view_func=lambda: "essay bank",
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/exam-tutor/trends",
        endpoint="exam_tutor.exam_tutor_trends_api",
        view_func=lambda: "trends",
        methods=["GET"],
    )
    app.add_url_rule(
        "/exam-tutor/archive/<path:relative_path>",
        endpoint="exam_tutor.exam_tutor_archive_file",
        view_func=lambda relative_path: relative_path,
        methods=["GET"],
    )
    for rule, endpoint in (
        ("/manual", "dashboard_pages.maintenance_manual"),
        ("/manual/pdf", "dashboard_pages.maintenance_manual_pdf"),
        ("/manual/markdown", "dashboard_pages.maintenance_manual_markdown"),
        ("/manual/source-index.json", "dashboard_pages.maintenance_manual_source_index"),
    ):
        app.add_url_rule(rule, endpoint=endpoint, view_func=lambda: "manual", methods=["GET"])

    verify_loaded_surface(app, "5002", RouteInventory(()))

    app.add_url_rule(
        "/undeclared-extension",
        endpoint="undeclared_extension",
        view_func=lambda: "unexpected",
        methods=["GET"],
    )
    with pytest.raises(CompatibilityLoadError, match="route surface mismatch"):
        verify_loaded_surface(app, "5002", RouteInventory(()))


def test_load_failure_returns_stable_503_without_leaking_exception() -> None:
    def loader(service: str):
        raise RuntimeError("secret loader detail")

    adapter = LazyCompatibilityApp(
        "5003",
        inventory=RouteInventory(()),
        loader=loader,
        verifier=lambda app, service, inventory: None,
    )

    response = Client(adapter, Response).get("/health")

    assert response.status_code == 503
    assert response.json == {"ok": False, "error": "compatibility_surface_unavailable", "service": "5003"}
    assert "secret" not in response.get_data(as_text=True)
    assert "secret loader detail" in adapter.status()["error"]
