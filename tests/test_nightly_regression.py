# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

import scripts.ops.nightly_regression as nightly_regression


def test_run_mock_skills_missing_fixture_is_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(nightly_regression, "MAGI_DIR", tmp_path)
    result = nightly_regression.run_mock_skills(skills="all")
    assert result["ok"] is True
    assert result["failed"] == 0
    assert result["skipped"] == 1
    assert result["warned"] == 1
    assert "deprecated_or_missing_fixture" in result["warnings"][0]


def test_run_core_routes_warn_is_not_counted_as_failure(monkeypatch):
    payload = {
        "summary": {"pass": 6, "warn": 1, "fail": 0, "total": 7},
        "cases": [
            {"name": "translate_guide", "status": "PASS", "pass": True},
            {"name": "judgment_guide", "status": "WARN", "pass": True},
        ],
    }
    report_path = Path("/tmp/magi_smoke_core_routes.json")
    report_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(nightly_regression, "_run", lambda *args, **kwargs: (0, "", ""))
    result = nightly_regression.run_core_routes()
    assert result["ok"] is True
    assert result["failed"] == 0
    assert result["warned"] == 1
    assert result["failures"] == []
