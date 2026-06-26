from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_insights_to_vectors.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sync_insights_to_vectors_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_embedding_validation_rejects_zero_vectors():
    mod = _load_module()

    assert mod._embedding_is_valid([0.1, 0.0])
    assert not mod._embedding_is_valid([0.0, 0.0, 0.0])
    assert not mod._embedding_is_valid(None)


def test_get_embedding_returns_none_on_provider_failure(monkeypatch):
    mod = _load_module()

    class _Requests:
        @staticmethod
        def post(*_args, **_kwargs):
            raise RuntimeError("provider down")

    monkeypatch.setitem(__import__("sys").modules, "requests", _Requests)

    assert mod._get_embedding("hello") is None
