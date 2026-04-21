"""
Shared fixtures for MAGI test suite.
"""

import os
import json
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    """Ensure required env vars are set for all tests."""
    defaults = {
        "MAGI_LINE_CHANNEL_ACCESS_TOKEN": "test_token",
        "MAGI_LINE_CHANNEL_SECRET": "test_secret",
        "MAGI_DISABLE_SERVER_STARTUP_HOOKS": "1",
        "DB_HOST": "127.0.0.1",
        "DB_USER": "test_user",
        "DB_PASSWORD": "test_pass",
        "FLASK_SECRET_KEY": "test_flask_secret",
        # Disable remote health gate in all tests; gate opt-in tests override this
        # with their own monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1").
        "MAGI_USE_REMOTE_HEALTH_GATE": "0",
    }
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def mock_omlx_response():
    """Mock a successful oMLX HTTP response."""
    def _make(text="mock response", model="TAIDE-12b-Chat-mlx-4bit"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": text}}],
            "model": model,
        }
        return resp
    return _make


@pytest.fixture
def mock_ollama_response():
    """Mock a successful Ollama HTTP response."""
    def _make(text="mock response", model="taide-12b"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "response": text,
            "model": model,
            "done": True,
        }
        return resp
    return _make
