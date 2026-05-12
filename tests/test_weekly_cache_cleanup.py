# -*- coding: utf-8 -*-
"""Tests for weekly retired-runtime/cache cleanup."""

from __future__ import annotations

import os
import time
from pathlib import Path

from scripts.ops import weekly_cache_cleanup as wc


def _touch_old(path: Path, age_days: int = 30) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)
    ts = time.time() - age_days * 86400
    os.utime(path, (ts, ts))


def test_retired_ollama_root_is_removed(tmp_path, monkeypatch):
    ollama = tmp_path / ".ollama"
    _touch_old(ollama / "models" / "blob")
    target = {"path": ollama, "label": "retired_ollama", "env_keep": "MAGI_KEEP_RETIRED_OLLAMA"}

    summary = wc.cleanup_retired_root(target, dry_run=False)

    assert summary["deleted_entries"] == 1
    assert summary["freed_bytes"] > 0
    assert not ollama.exists()


def test_retired_ollama_can_be_kept_by_env(tmp_path, monkeypatch):
    ollama = tmp_path / ".ollama"
    _touch_old(ollama / "models" / "blob")
    monkeypatch.setenv("MAGI_KEEP_RETIRED_OLLAMA", "1")
    target = {"path": ollama, "label": "retired_ollama", "env_keep": "MAGI_KEEP_RETIRED_OLLAMA"}

    summary = wc.cleanup_retired_root(target, dry_run=False)

    assert summary["skipped_by_env"] is True
    assert ollama.exists()


def test_cache_cleanup_preserves_judicial_collector_backlog(tmp_path, monkeypatch):
    cache_root = tmp_path / ".cache"
    backlog = cache_root / "judgment_collector" / "judicial_api" / "raw" / "case.json"
    stale = cache_root / "uv" / "archive-v0" / "pkg.whl"
    _touch_old(backlog)
    _touch_old(stale)
    monkeypatch.setattr(wc, "_PROTECTED_PATHS", {cache_root / "judgment_collector"}, raising=True)

    summary = wc.cleanup_target(
        {"path": cache_root, "atime_days": 14, "label": "user_cache"},
        dry_run=False,
    )

    assert summary["deleted_entries"] == 1
    assert summary["skipped_protected"] == 1
    assert backlog.exists()
    assert not stale.exists()


def test_omlx_model_and_training_roots_are_protected(tmp_path, monkeypatch):
    omlx = tmp_path / ".omlx"
    model = omlx / "models" / "gemma" / "model.safetensors"
    training = omlx / "training" / "distill" / "adapter.safetensors"
    cache = omlx / "cache-e4b" / "old.bin"
    for path in (model, training, cache):
        _touch_old(path)
    monkeypatch.setattr(
        wc,
        "_PROTECTED_PATHS",
        {omlx / "models", omlx / "models-vision", omlx / "training"},
        raising=True,
    )

    summary = wc.cleanup_target(
        {"path": omlx, "atime_days": 14, "label": "omlx_root"},
        dry_run=False,
    )

    assert summary["deleted_entries"] == 1
    assert summary["skipped_protected"] == 2
    assert model.exists()
    assert training.exists()
    assert not cache.exists()


def test_permission_denied_cache_entry_is_skipped_not_error(tmp_path, monkeypatch):
    cache_root = tmp_path / "Library" / "Caches"
    protected = cache_root / "com.apple.Safari"
    _touch_old(protected / "blob")

    def deny(*args, **kwargs):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(wc.shutil, "rmtree", deny)

    summary = wc.cleanup_target(
        {"path": cache_root, "atime_days": 14, "label": "user_library_caches"},
        dry_run=False,
    )

    assert summary["skipped_permission"] == 1
    assert summary["errors"] == []


def test_cache_cleanup_preserves_json_bundle_content(tmp_path):
    cache_root = tmp_path / "cache"
    standalone = cache_root / "Paperclip" / "_internal" / "holidays_config.json"
    disposable = cache_root / "tmp" / "blob.bin"
    _touch_old(standalone)
    _touch_old(disposable)

    summary = wc.cleanup_target(
        {"path": cache_root, "atime_days": 14, "label": "cache"},
        dry_run=False,
    )

    assert summary["skipped_preserved_content"] == 1
    assert summary["deleted_entries"] == 1
    assert standalone.exists()
    assert not disposable.exists()
