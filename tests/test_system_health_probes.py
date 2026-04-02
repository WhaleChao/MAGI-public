from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock


MAGI_ROOT = Path(__file__).resolve().parent.parent
MAGI_DOCTOR_PATH = MAGI_ROOT / "skills" / "magi-doctor" / "action.py"


def _load_magi_doctor_module():
    spec = importlib.util.spec_from_file_location("magi_doctor_test", MAGI_DOCTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mock_models_response(status_code=200, models=None):
    """Create a mock requests.get response for /v1/models."""
    if models is None:
        models = [{"id": "TAIDE-12b-Chat-mlx-4bit"}]
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"data": models}
    return resp


def test_system_test_omlx_uses_models_endpoint(monkeypatch):
    from skills.ops import system_test

    mock_resp = _mock_models_response(200, [{"id": "TAIDE-12b-Chat-mlx-4bit"}, {"id": "Qwen2.5-Coder-14B"}])
    monkeypatch.setattr("skills.ops.system_test._requests.get", lambda url, timeout=5: mock_resp)

    result = system_test.test_casper_ollama()

    assert result["pass"] is True
    assert "2 models" in result["detail"]


def test_system_test_omlx_unreachable(monkeypatch):
    from skills.ops import system_test

    def raise_err(url, timeout=5):
        raise ConnectionError("refused")

    monkeypatch.setattr("skills.ops.system_test._requests.get", raise_err)

    result = system_test.test_casper_ollama()

    assert result["pass"] is False


def test_magi_doctor_probe_uses_models_endpoint(monkeypatch):
    module = _load_magi_doctor_module()

    mock_resp = _mock_models_response(200, [{"id": "TAIDE-12b-Chat-mlx-4bit"}])
    monkeypatch.setattr(module.requests, "get", lambda url, timeout=5: mock_resp)

    result = module._probe_omlx_chat(timeout_sec=8)

    assert result["pass"] is True


def test_magi_doctor_repair_ollama_uses_models_probe(monkeypatch):
    module = _load_magi_doctor_module()

    mock_resp = _mock_models_response(200, [])
    monkeypatch.setattr(module.requests, "get", lambda url, timeout=5: mock_resp)

    result = module._repair_ollama()

    assert result["repaired"] is False
    assert "oMLX" in result["detail"] or "空模型" in result["detail"]
