from __future__ import annotations

import sys

from scripts.ops import nightly_regression


def test_production_default_omits_retired_mock_suite(monkeypatch):
    calls: list[str] = []

    def _suite(name):
        def _run():
            calls.append(name)
            return {"suite": name, "label": name, "ok": True, "passed": 1, "failed": 0, "total": 1}

        return _run

    monkeypatch.setattr(
        nightly_regression,
        "SUITE_FUNCS",
        {
            "system": _suite("system"),
            "channels": _suite("channels"),
            "mock": _suite("mock"),
            "coreroutes": _suite("coreroutes"),
        },
    )
    monkeypatch.setattr(
        nightly_regression,
        "ensure_discord_bot_for_regression",
        lambda: {"ok": True, "action": "already_running", "pid": "1"},
    )
    monkeypatch.setattr(sys, "argv", ["nightly_regression.py", "--no-notify"])

    assert nightly_regression.main() == 0
    assert calls == ["system", "channels", "coreroutes"]


def test_missing_retired_mock_fixture_is_neutral(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_regression, "MAGI_DIR", tmp_path)

    result = nightly_regression.run_mock_skills()

    assert result["ok"] is True
    assert result["status"] == "retired"
    assert result["warned"] == 0
    assert result["warnings"] == []

