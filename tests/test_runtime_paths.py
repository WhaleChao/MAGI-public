"""Tests for api.runtime_paths — path resolution functions."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import pytest


def test_get_magi_root_returns_path_object(monkeypatch):
    """get_magi_root_dir() should return a Path object."""
    from api.runtime_paths import get_magi_root_dir

    result = get_magi_root_dir()
    assert isinstance(result, Path)


def test_get_magi_root_default_when_no_env(monkeypatch):
    """get_magi_root_dir() should return default when env vars not set."""
    monkeypatch.delenv("MAGI_ROOT_DIR", raising=False)
    monkeypatch.delenv("MAGI_ROOT", raising=False)

    from api.runtime_paths import get_magi_root_dir

    result = get_magi_root_dir()
    assert isinstance(result, Path)
    assert result.is_absolute()


def test_get_magi_root_respects_env_override(monkeypatch, tmp_path):
    """get_magi_root_dir() should respect MAGI_ROOT_DIR env var."""
    monkeypatch.setenv("MAGI_ROOT_DIR", str(tmp_path))

    from api.runtime_paths import get_magi_root_dir

    result = get_magi_root_dir()
    assert result == tmp_path.resolve()


def test_get_orch_dir_returns_path(monkeypatch):
    """get_orch_dir() should return a Path object."""
    from api.runtime_paths import get_orch_dir

    result = get_orch_dir()
    assert isinstance(result, Path)


def test_get_json_dir_returns_path(monkeypatch):
    """get_json_dir() should return a Path object."""
    from api.runtime_paths import get_json_dir

    result = get_json_dir()
    assert isinstance(result, Path)


def test_get_metrics_dir_returns_path(monkeypatch):
    """get_metrics_dir() should return a Path object."""
    from api.runtime_paths import get_metrics_dir

    result = get_metrics_dir()
    assert isinstance(result, Path)


def test_get_autopilot_runs_dir_returns_path(monkeypatch):
    """get_autopilot_runs_dir() should return a Path object."""
    from api.runtime_paths import get_autopilot_runs_dir

    result = get_autopilot_runs_dir()
    assert isinstance(result, Path)


def test_get_config_path_returns_path(monkeypatch):
    """get_config_path() should return a Path object."""
    from api.runtime_paths import get_config_path

    result = get_config_path()
    assert isinstance(result, Path)


def test_all_get_functions_return_absolute_paths(monkeypatch):
    """All get_*_dir() functions should return absolute paths."""
    from api.runtime_paths import (
        get_magi_root_dir,
        get_orch_dir,
        get_json_dir,
        get_metrics_dir,
        get_autopilot_runs_dir,
        get_config_path,
        get_legacy_code_root,
    )

    functions = [
        get_magi_root_dir,
        get_orch_dir,
        get_json_dir,
        get_metrics_dir,
        get_autopilot_runs_dir,
        get_config_path,
        get_legacy_code_root,
    ]

    for fn in functions:
        result = fn()
        assert isinstance(result, Path)
        assert result.is_absolute(), f"{fn.__name__} returned non-absolute path: {result}"


def test_no_hardcoded_users_ai_path(monkeypatch):
    """Module should not contain hardcoded /Users/ai paths."""
    import api.runtime_paths as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "/Users/ai" not in source, "Module contains hardcoded /Users/ai path"


def test_get_skill_python_returns_path(monkeypatch):
    """get_skill_python() should return a Path object."""
    from api.runtime_paths import get_skill_python

    result = get_skill_python()
    assert isinstance(result, Path)


def test_config_candidates_returns_path_list(monkeypatch):
    """config_candidates() should return a list of Path objects."""
    from api.runtime_paths import config_candidates

    result = config_candidates("config.json")
    assert isinstance(result, list)
    assert all(isinstance(p, Path) for p in result)


def test_ensure_path_on_sys_path_adds_to_sys_path(monkeypatch, tmp_path):
    """ensure_path_on_sys_path() should add path to sys.path."""
    from api.runtime_paths import ensure_path_on_sys_path

    initial_len = len(sys.path)
    result = ensure_path_on_sys_path(tmp_path)

    assert str(result.resolve()) in sys.path or str(tmp_path) in sys.path
    assert isinstance(result, Path)

    # Cleanup: remove the path we added
    sys.path = [p for p in sys.path if p != str(result.resolve())]


def test_ensure_magi_root_on_sys_path_returns_path(monkeypatch):
    """ensure_magi_root_on_sys_path() should return a Path object."""
    from api.runtime_paths import ensure_magi_root_on_sys_path

    result = ensure_magi_root_on_sys_path()
    assert isinstance(result, Path)
    assert result.is_absolute()
