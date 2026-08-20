from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from magi_v3.live_validation import create_main_app, create_tools_app
from magi_v3.service_runtime import ServiceRuntimeError


def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "website"
    (root / "data").mkdir(parents=True)
    fixture = root / "data" / "live-validation-document.txt"
    fixture.write_text("preview fixture\n", encoding="utf-8")
    values = {
        "MAGI_V3_DEPLOYMENT_MODE": "isolated_live_validation",
        "MAGI_V3_LIVE_VALIDATION": "1",
        "MAGI_V3_EXTERNAL_WRITES_ENABLED": "0",
        "MAGI_V3_NOTIFICATIONS_ENABLED": "0",
        "MAGI_V3_SCHEDULER_ENABLED": "0",
        "MAGI_WEBSITE_ROOT": str(root),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return fixture


def _request(app: Any, path: str, method: str = "GET") -> tuple[str, dict[str, str], bytes]:
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(app({"PATH_INFO": path, "REQUEST_METHOD": method}, start_response))
    return captured["status"], captured["headers"], body


def test_validation_wsgi_exposes_only_read_only_fixed_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _environment(tmp_path, monkeypatch)
    original = fixture.read_bytes()

    for app in (create_main_app(), create_tools_app()):
        status, headers, body = _request(app, "/validation/ping")
        assert status == "200 OK"
        assert json.loads(body)["mode"] == "isolated_live_validation"
        assert headers["X-MAGI-Validation-Mode"] == "isolated_live_validation"

        status, headers, body = _request(app, "/validation/osc/document-preview")
        assert status == "200 OK" and body == original
        assert headers["Content-Disposition"].startswith("inline")

        status, headers, body = _request(app, "/validation/osc/document-download")
        assert status == "200 OK" and body == original
        assert headers["Content-Disposition"].startswith("attachment")

        assert _request(app, "/api/production-write", "POST")[0] == "405 Method Not Allowed"
        assert _request(app, "/unreviewed")[0] == "404 Not Found"
    assert fixture.read_bytes() == original


@pytest.mark.parametrize(
    ("name", "unsafe"),
    [
        ("MAGI_V3_EXTERNAL_WRITES_ENABLED", "1"),
        ("MAGI_V3_NOTIFICATIONS_ENABLED", "1"),
        ("MAGI_V3_SCHEDULER_ENABLED", "1"),
        ("MAGI_V3_LIVE_VALIDATION", "0"),
    ],
)
def test_validation_factory_fails_closed_if_any_safety_switch_drifts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    unsafe: str,
) -> None:
    _environment(tmp_path, monkeypatch)
    monkeypatch.setenv(name, unsafe)

    with pytest.raises(ServiceRuntimeError, match="safety binding mismatch"):
        create_main_app()
