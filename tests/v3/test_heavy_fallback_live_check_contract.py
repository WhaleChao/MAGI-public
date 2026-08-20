from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from scripts.ops import heavy_fallback_live_check as live_check


def test_load_env_uses_hash_bound_external_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    payload = b"NVIDIA_NIM_ENABLE=1\n"
    env_file.write_bytes(payload)
    monkeypatch.setenv("MAGI_ENV_FILE", str(env_file))
    monkeypatch.setenv(
        "MAGI_ENV_FILE_SHA256", hashlib.sha256(payload).hexdigest()
    )
    monkeypatch.delenv("NVIDIA_NIM_ENABLE", raising=False)

    live_check._load_env()

    assert os.environ["NVIDIA_NIM_ENABLE"] == "1"


def test_load_env_rejects_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NVIDIA_NIM_ENABLE=1\n", encoding="utf-8")
    monkeypatch.setenv("MAGI_ENV_FILE", str(env_file))
    monkeypatch.setenv("MAGI_ENV_FILE_SHA256", "0" * 64)

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        live_check._load_env()
