from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager, UserMixin


def _client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.config["SECRET_KEY"] = "test"

    login = LoginManager()
    login.init_app(app)

    class TestUser(UserMixin):
        id = "test-user"

    @login.user_loader
    def _load_user(_user_id):
        return TestUser()

    from api.blueprints.osc_cases import osc_bp

    app.register_blueprint(osc_bp)
    return app.test_client()


def test_case_file_upload_rejects_blocked_extensions(tmp_path: Path):
    client = _client()
    case_dir = tmp_path / "cases"
    case_dir.mkdir()

    with patch(
        "api.blueprints.osc_cases._osc_resolve_existing_local_path", return_value=str(case_dir)
    ), patch("api.blueprints.osc_cases._osc_is_safe_local_path", return_value=True):
        response = client.post(
            "/api/osc/files/upload",
            data={
                "folder_path": str(case_dir),
                "file": (BytesIO(b"MZ fake exe content"), "bad.exe"),
            },
            content_type="multipart/form-data",
        )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"] == "blocked_extension:.exe"


def test_case_file_upload_rejects_executable_signature_even_with_pdf_ext(tmp_path: Path):
    client = _client()
    case_dir = tmp_path / "cases"
    case_dir.mkdir()

    with patch(
        "api.blueprints.osc_cases._osc_resolve_existing_local_path", return_value=str(case_dir)
    ), patch("api.blueprints.osc_cases._osc_is_safe_local_path", return_value=True):
        response = client.post(
            "/api/osc/files/upload",
            data={
                "folder_path": str(case_dir),
                "file": (BytesIO(b"MZThis is disguised executable"), "invoice.pdf"),
            },
            content_type="multipart/form-data",
        )

    assert response.status_code == 415
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"] == "blocked_content_signature"
    assert payload["detail"].startswith("executable_signature:")
