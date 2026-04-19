# -*- coding: utf-8 -*-
"""Tests for api.platforms.runtime_dir (R3)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pytest

from api.platforms import runtime_dir as rd


@pytest.fixture
def tmp_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")
    yield tmp_path


# --- path APIs ----------------------------------------------------------

def test_root_creates_dir(tmp_runtime):
    p = rd.root()
    assert p.exists() and p.is_dir()


def test_pending_path(tmp_runtime):
    p = rd.pending("laf_progress_submit")
    assert p.parent.name == "pending"
    assert p.name == "laf_progress_submit.json"


def test_metrics_path(tmp_runtime):
    p = rd.metrics("nvidia_nim_usage")
    assert p.parent.name == "metrics"
    assert p.suffix == ".jsonl"


def test_cron_state_path(tmp_runtime):
    p = rd.cron_state()
    assert p.name == "cron_state.json"


def test_name_rejects_path_separator(tmp_runtime):
    with pytest.raises(ValueError):
        rd.pending("../escape")
    with pytest.raises(ValueError):
        rd.pending("a/b")


def test_name_rejects_empty(tmp_runtime):
    with pytest.raises(ValueError):
        rd.pending("")


# --- atomic_write_json --------------------------------------------------

def test_atomic_write_json_ok(tmp_runtime):
    p = rd.cron_state()
    rd.atomic_write_json(p, {"k": "v"})
    assert json.loads(p.read_text()) == {"k": "v"}


def test_atomic_write_json_leaves_no_tmp(tmp_runtime):
    p = rd.cron_state()
    rd.atomic_write_json(p, {"k": 1})
    leftover = list(p.parent.glob(p.name + ".*"))
    assert leftover == []


def test_atomic_write_json_survives_bad_data(tmp_runtime):
    p = rd.cron_state()
    class Bad:
        pass
    with pytest.raises(TypeError):
        rd.atomic_write_json(p, {"x": Bad()})
    # 原檔不存在則仍不存在
    assert not p.exists()


# --- atomic_append_jsonl + rotation ------------------------------------

def test_jsonl_append(tmp_runtime):
    p = rd.metrics("usage")
    rd.atomic_append_jsonl(p, {"i": 1})
    rd.atomic_append_jsonl(p, {"i": 2})
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 2 and json.loads(lines[1]) == {"i": 2}


def test_jsonl_rotates_to_tail(tmp_runtime):
    p = rd.metrics("usage")
    # 501 appends: 第 501 筆觸發 rotate（501 > 500），file trim 到 300 筆後無後續 append
    for i in range(501):
        rd.atomic_append_jsonl(p, {"i": i}, rotate_at=500, keep_tail=300)
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 300
    # tail 是最後 300 筆（i=201..500）
    assert json.loads(lines[-1]) == {"i": 500}


# --- legacy_fallback ----------------------------------------------------

def test_legacy_fallback_prefers_new(tmp_runtime, tmp_path):
    new = rd.cron_state()
    new.write_text("{}")
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{}")
    assert rd.legacy_fallback(new, [legacy]) == new


def test_legacy_fallback_falls_back(tmp_runtime, tmp_path):
    new = rd.cron_state()
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{}")
    assert rd.legacy_fallback(new, [legacy]) == legacy


def test_legacy_fallback_none_exists_returns_new(tmp_runtime, tmp_path):
    new = rd.cron_state()
    legacy = tmp_path / "legacy.json"
    assert rd.legacy_fallback(new, [legacy]) == new
