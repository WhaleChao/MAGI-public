"""
Shared fixtures for MAGI test suite.
"""

import os
import json
import sys
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) in sys.path:
    sys.path.remove(str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT))
for _module_name, _module in list(sys.modules.items()):
    if _module_name == "api" or _module_name.startswith("api."):
        _module_file = str(getattr(_module, "__file__", "") or "")
        if _module_file and not _module_file.startswith(str(_REPO_ROOT)):
            sys.modules.pop(_module_name, None)

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
        # Disable NVIDIA NIM by default; tests that explicitly test NIM behaviour
        # (e.g. test_inference_gateway_heavy_fast_path.py) set NVIDIA_NIM_ENABLE=1
        # in their own setup_method / monkeypatch.setenv, overriding this default.
        "NVIDIA_NIM_ENABLE": "0",
        # Disable strict-NIM retry loop by default so unit tests that patch
        # run_nim_chat see exactly 1 call, regardless of whether .env has been
        # loaded by a previous test importing api.handlers.summary_handler or
        # api.handlers.translation_handler (both call load_dotenv() on import).
        "MAGI_HEAVY_STRICT_NIM": "0",
        "MAGI_HEAVY_STRICT_NIM_RETRIES": "0",
        # Unit tests must never call the public TLR endpoint unless they opt in
        # and mock the adapter explicitly.
        "MAGI_TWLEGALRAG_ENABLE": "0",
    }
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def mock_omlx_response():
    """Mock a successful oMLX HTTP response."""
    def _make(text="mock response", model="gemma-4-e4b-it-4bit"):
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
    def _make(text="mock response", model="gemma-4-e4b"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "response": text,
            "model": model,
            "done": True,
        }
        return resp
    return _make
