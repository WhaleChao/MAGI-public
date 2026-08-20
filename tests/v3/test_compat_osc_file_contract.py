from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin, login_user
from werkzeug.test import Client
from werkzeug.wrappers import Response

from magi_v3.compat.gateway import LazyCompatibilityApp, RouteInventory

FIXTURE_PATH = Path(__file__).parent / "compat" / "behavior_fixtures" / "osc-file-content.json"
INVENTORY_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "architecture"
    / "v3"
    / "generated"
    / "v2_runtime_routes.json"
)


class ContractUser(UserMixin):
    def __init__(self, user_id: str, role: str) -> None:
        self.id = user_id
        self.role = role


@pytest.fixture
def contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload = base64.b64decode(fixture["content_base64"], validate=True)
    assert len(payload) == fixture["content_length"]
    assert hashlib.sha256(payload).hexdigest() == fixture["content_sha256"]

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    document = allowed / fixture["filename"]
    document.write_bytes(payload)
    monkeypatch.setenv("PAPERCLIP_FILEMANAGER_TEST_BASE", str(allowed))

    from api.blueprints import osc_cases, osc_files

    monkeypatch.setattr(osc_cases, "_osc_audit_file_event", lambda *_a, **_k: None)
    monkeypatch.setattr(osc_files, "_audit_file_event", lambda *_a, **_k: None)

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="v3-osc-contract-only")
    login = LoginManager(app)
    login.login_view = "contract_login_page"

    @login.user_loader
    def load_user(user_id: str):
        if user_id == "operator-contract":
            return ContractUser(user_id, "operator")
        if user_id == "viewer-contract":
            return ContractUser(user_id, "viewer")
        return None

    @app.get("/login")
    def contract_login_page():
        return "contract login", 200

    @app.post("/__contract/login/<role>")
    def contract_login(role: str):
        if role not in {"operator", "viewer"}:
            return {"ok": False}, 404
        login_user(ContractUser(f"{role}-contract", role))
        return {"ok": True, "role": role}

    app.register_blueprint(osc_cases.osc_bp)
    app.register_blueprint(osc_files.osc_files_bp)
    compat = LazyCompatibilityApp(
        "5002",
        inventory=RouteInventory.load(INVENTORY_PATH),
        loader=lambda _service: app,
        # The full 280-route surface is verified elsewhere. This contract loads
        # only the two real OSC blueprints to avoid importing/starting services.
        verifier=lambda _app, _service, _inventory: None,
    )
    return fixture, payload, allowed, document, compat


def _logged_in_client(compat: LazyCompatibilityApp, role: str = "operator") -> Client:
    client = Client(compat, Response, use_cookies=True)
    response = client.post(f"/__contract/login/{role}")
    assert response.status_code == 200
    return client


def test_contract_routes_are_bound_inside_the_verified_347_route_inventory() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    inventory = RouteInventory.load(INVENTORY_PATH)
    signatures = {
        (route.rule, route.endpoint, route.methods)
        for route in inventory.for_service(fixture["route"]["service"])
    }

    assert inventory.counts == {"5002": 280, "5003": 67, "total": 347}
    assert (
        fixture["route"]["preview"],
        fixture["route"]["preview_endpoint"],
        ("GET",),
    ) in signatures
    assert (
        fixture["route"]["content"],
        fixture["route"]["content_endpoint"],
        ("GET",),
    ) in signatures


def test_v3_compat_session_preview_and_full_download_contract(contract) -> None:
    fixture, payload, _allowed, document, compat = contract
    anonymous = Client(compat, Response, use_cookies=True)
    unauthorized = anonymous.get(f"/api/osc/files/preview?path={quote(str(document))}")
    assert unauthorized.status_code == 302
    assert unauthorized.headers["Location"].startswith("/login?next=")

    client = _logged_in_client(compat)
    preview = client.get(f"/api/osc/files/preview?path={quote(str(document))}")
    assert preview.status_code == 200
    preview_data = preview.get_json()
    assert preview_data == {
        "ok": True,
        "kind": "pdf",
        "content_url": f"/api/osc/files/content?path={quote(str(document), safe='')}&inline=1",
        "name": fixture["filename"],
    }

    inline = client.get(preview_data["content_url"])
    assert inline.status_code == 200
    assert inline.data == payload
    assert inline.content_type.startswith(fixture["expected_mime"])
    assert inline.headers["Content-Disposition"].startswith("inline;")
    assert inline.headers["Accept-Ranges"] == "bytes"
    assert inline.headers["Content-Length"] == str(len(payload))
    assert inline.headers["Cache-Control"].startswith("private")

    download = client.get(f"/api/osc/files/content?path={quote(str(document))}")
    assert download.status_code == 200
    assert download.data == payload
    assert download.headers["Content-Disposition"].startswith("attachment;")
    assert f'filename="{fixture["filename"]}"' in download.headers["Content-Disposition"]


def test_v3_compat_range_mime_and_unsatisfied_range_contract(contract) -> None:
    fixture, payload, _allowed, document, compat = contract
    client = _logged_in_client(compat)
    selected = fixture["range"]

    partial = client.get(
        f"/api/osc/files/content?path={quote(str(document))}&inline=1",
        headers={"Range": selected["header"]},
    )
    assert partial.status_code == 206
    assert partial.data == base64.b64decode(selected["body_base64"], validate=True)
    assert partial.data == payload[selected["start"] : selected["end"] + 1]
    assert partial.headers["Content-Range"] == (
        f"bytes {selected['start']}-{selected['end']}/{len(payload)}"
    )
    assert partial.headers["Content-Length"] == str(selected["end"] - selected["start"] + 1)
    assert partial.content_type.startswith(fixture["expected_mime"])

    invalid = client.get(
        f"/api/osc/files/content?path={quote(str(document))}",
        headers={"Range": "bytes=999-1000"},
    )
    assert invalid.status_code == 416
    assert invalid.headers["Content-Range"] == f"bytes */{len(payload)}"

    multipart = client.get(
        f"/api/osc/files/content?path={quote(str(document))}",
        headers={"Range": "bytes=0-1,4-5"},
    )
    assert multipart.status_code == 416


def test_v3_compat_safe_missing_is_404_but_escape_and_symlink_are_403(contract, tmp_path: Path) -> None:
    _fixture, _payload, allowed, _document, compat = contract
    client = _logged_in_client(compat)

    missing = client.get(f"/api/osc/files/preview?path={quote(str(allowed / 'missing.pdf'))}")
    assert missing.status_code == 404
    assert missing.get_json()["error"] == "file_not_found"
    missing_download = client.get(
        f"/api/osc/files/content?path={quote(str(allowed / 'missing.pdf'))}"
    )
    assert missing_download.status_code == 404

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-outside")
    forbidden_preview = client.get(f"/api/osc/files/preview?path={quote(str(outside))}")
    assert forbidden_preview.status_code == 403
    assert forbidden_preview.get_json()["error"] == "path_not_allowed"
    forbidden_download = client.get(f"/api/osc/files/content?path={quote(str(outside))}")
    assert forbidden_download.status_code == 403

    linked = allowed / "linked.pdf"
    linked.symlink_to(outside)
    linked_response = client.get(f"/api/osc/files/content?path={quote(str(linked))}")
    assert linked_response.status_code == 403

    traversal = allowed / "folder" / ".." / ".." / "outside.pdf"
    traversal_response = client.get(f"/api/osc/files/content?path={quote(str(traversal))}")
    assert traversal_response.status_code == 403


def test_v3_compat_session_cookie_is_not_shared_between_clients(contract) -> None:
    _fixture, _payload, _allowed, document, compat = contract
    authenticated = _logged_in_client(compat, "viewer")
    isolated = Client(compat, Response, use_cookies=True)

    assert authenticated.get(f"/api/osc/files/preview?path={quote(str(document))}").status_code == 200
    denied = isolated.get(f"/api/osc/files/content?path={quote(str(document))}")
    assert denied.status_code == 302
    assert denied.headers["Location"].startswith("/login?next=")
