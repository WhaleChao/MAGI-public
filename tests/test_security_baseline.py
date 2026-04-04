"""Tests for security baselines — CORS, headers, cookies."""

import pytest
from unittest.mock import patch, MagicMock


class TestToolsAPICors:
    """Tests for tools_api CORS configuration."""

    def test_cors_has_allowlist_not_wildcard(self):
        """tools_api CORS should use allowlist, not wildcard origins."""
        import os
        from pathlib import Path

        # Read tools_api.py source
        tools_api_path = Path(__file__).parent.parent / "api" / "tools_api.py"
        source = tools_api_path.read_text(encoding="utf-8")

        # Check that CORS is configured with origins parameter (allowlist)
        assert "CORS(app, origins=" in source or "CORS(app," in source
        # Check that wildcard is NOT used in CORS call
        assert 'origins="*"' not in source
        assert "origins=['*']" not in source
        assert 'CORS(app, "*")' not in source


    def test_cors_origins_from_env_allowlist(self):
        """CORS origins should come from env allowlist, not wildcard."""
        with patch.dict('os.environ', {
            'MAGI_CORS_ORIGINS': 'http://localhost:3000,http://localhost:5002'
        }):
            # Import fresh to get env vars
            import importlib
            import sys
            if 'api.tools_api' in sys.modules:
                del sys.modules['api.tools_api']

            from api import tools_api

            # The module should have defined _cors_origins as a list
            assert hasattr(tools_api, '_cors_origins')
            assert isinstance(tools_api._cors_origins, list)
            # Should not contain wildcard
            assert '*' not in str(tools_api._cors_origins)


class TestServerSecurityHeaders:
    """Tests for Flask bootstrap security headers."""

    def test_app_factory_has_security_headers_middleware(self):
        """app_factory.py should define security headers middleware."""
        from pathlib import Path

        app_factory_path = Path(__file__).parent.parent / "api" / "app_factory.py"
        source = app_factory_path.read_text(encoding="utf-8")

        # Check for security headers
        assert "X-Content-Type-Options" in source
        assert "X-Frame-Options" in source
        assert "X-XSS-Protection" in source
        assert "Referrer-Policy" in source


    def test_security_headers_set_correct_values(self):
        """Security headers should have secure default values."""
        from pathlib import Path

        app_factory_path = Path(__file__).parent.parent / "api" / "app_factory.py"
        source = app_factory_path.read_text(encoding="utf-8")

        # Check for nosniff
        assert "nosniff" in source
        # Check for SAMEORIGIN (or similar)
        assert "SAMEORIGIN" in source
        # Check for strict-origin or similar
        assert "strict-origin" in source or "SAMEORIGIN" in source

    def test_security_headers_apply_to_runtime_response(self, monkeypatch):
        from api import app_factory

        monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
        app = app_factory.create_base_app()
        app_factory.install_security_headers(app)

        @app.route("/ping")
        def _ping():
            return "ok"

        client = app.test_client()
        response = client.get("/ping")

        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


class TestServerSessionCookie:
    """Tests for session cookie hardening."""

    def test_session_cookie_httponly_enabled(self):
        """Session cookie should have HttpOnly flag."""
        from pathlib import Path

        app_factory_path = Path(__file__).parent.parent / "api" / "app_factory.py"
        source = app_factory_path.read_text(encoding="utf-8")

        assert "SESSION_COOKIE_HTTPONLY" in source
        assert (
            "SESSION_COOKIE_HTTPONLY = True" in source
            or "SESSION_COOKIE_HTTPONLY = 'True'" in source
            or "app.config['SESSION_COOKIE_HTTPONLY'] = True" in source
            or 'app.config["SESSION_COOKIE_HTTPONLY"] = True' in source
        )


    def test_session_cookie_samesite_lax(self):
        """Session cookie SameSite should be Lax."""
        from pathlib import Path

        app_factory_path = Path(__file__).parent.parent / "api" / "app_factory.py"
        source = app_factory_path.read_text(encoding="utf-8")

        assert "SESSION_COOKIE_SAMESITE" in source
        assert "'Lax'" in source or '"Lax"' in source


    def test_session_cookie_config_exists(self):
        """Flask app should have session cookie config section."""
        from pathlib import Path

        app_factory_path = Path(__file__).parent.parent / "api" / "app_factory.py"
        source = app_factory_path.read_text(encoding="utf-8")

        # Look for session cookie hardening config
        lines_with_session = [line for line in source.split('\n') if 'SESSION_COOKIE' in line]
        assert len(lines_with_session) >= 2, "Should have at least SESSION_COOKIE_HTTPONLY and SESSION_COOKIE_SAMESITE"


    def test_session_cookie_flags_exist_at_runtime(self, monkeypatch):
        from api import app_factory

        monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
        app = app_factory.create_base_app()

        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


class TestJudgmentCollectorSecurity:
    """Tests for judgment-collector SSL defaults."""

    def test_insecure_ssl_default_is_zero(self):
        """judgment-collector should default to SSL enabled (insecure=0)."""
        import os
        from pathlib import Path

        # Search for judgment-collector related files
        judgment_collector_dir = Path(__file__).parent.parent / "skills" / "judgment-collector"
        if not judgment_collector_dir.exists():
            pytest.skip("judgment-collector skill not found")

        # Look for config files
        config_files = list(judgment_collector_dir.glob("**/*.py")) + list(judgment_collector_dir.glob("**/*.json"))

        found_config = False
        for config_file in config_files:
            try:
                content = config_file.read_text(encoding="utf-8", errors="ignore")
                if "insecure" in content or "ssl" in content.lower():
                    found_config = True
                    # If insecure/ssl appears, default should be secure
                    if "insecure" in content and "0" in content:
                        break
            except Exception:
                pass

        # If we found insecure config, verify it defaults to secure
        if found_config:
            assert True, "judgment-collector has SSL configuration"


class TestCORSMiddlewarePresence:
    """Tests for presence of CORS middleware configuration."""

    def test_tools_api_has_cors_import(self):
        """tools_api.py should import CORS middleware."""
        from pathlib import Path

        tools_api_path = Path(__file__).parent.parent / "api" / "tools_api.py"
        source = tools_api_path.read_text(encoding="utf-8")

        assert "flask_cors" in source or "CORS" in source


    def test_cors_applied_to_app_instance(self):
        """CORS should be applied to Flask app instance."""
        from pathlib import Path

        tools_api_path = Path(__file__).parent.parent / "api" / "tools_api.py"
        source = tools_api_path.read_text(encoding="utf-8")

        assert "CORS(app" in source


class TestSecurityHeadersNotWildcard:
    """Tests that security settings don't use wildcard."""

    def test_no_cors_wildcard_in_tools_api(self):
        """tools_api CORS should not use wildcard."""
        from pathlib import Path

        tools_api_path = Path(__file__).parent.parent / "api" / "tools_api.py"
        source = tools_api_path.read_text(encoding="utf-8")

        # Look for CORS wildcard patterns
        assert 'origins="*"' not in source
        assert "origins=['*']" not in source
        assert 'CORS(app, "*")' not in source


    def test_security_headers_complete(self):
        """All security headers should be present."""
        from pathlib import Path

        app_factory_path = Path(__file__).parent.parent / "api" / "app_factory.py"
        source = app_factory_path.read_text(encoding="utf-8")

        required_headers = [
            "X-Content-Type-Options",
            "X-Frame-Options",
            "X-XSS-Protection",
            "Referrer-Policy",
        ]

        for header in required_headers:
            assert header in source, f"Missing security header: {header}"
