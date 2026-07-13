# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
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


def test_discord_preflight_noop_when_running(monkeypatch):
    monkeypatch.setattr(nightly_regression, "_discord_bot_process", lambda: (True, "12345"))
    result = nightly_regression.ensure_discord_bot_for_regression(wait_sec=1)
    assert result == {"ok": True, "action": "already_running", "pid": "12345"}


def test_discord_preflight_flags_duplicate_processes(monkeypatch):
    monkeypatch.setattr(nightly_regression, "_discord_bot_process", lambda: (True, "12345,67890"))
    result = nightly_regression.ensure_discord_bot_for_regression(wait_sec=1)
    assert result == {"ok": False, "action": "duplicate_running", "pid": "12345,67890", "count": 2}


def test_discord_process_scan_uses_ps_and_handles_space_paths(monkeypatch):
    class Proc:
        returncode = 0
        stdout = "\n".join(
            [
                "100 /bin/zsh -lc pgrep -f api/discord_bot.py",
                "101 /opt/homebrew/bin/python3 /Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2/api/discord_bot.py",
                "102 /opt/homebrew/bin/python3 api/discord_bot.py",
                "103 /opt/homebrew/bin/python3 api/server.py",
            ]
        )
        stderr = ""

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Proc()

    monkeypatch.setattr(nightly_regression.subprocess, "run", fake_run)

    running, pid_text = nightly_regression._discord_bot_process()

    assert running is True
    assert pid_text == "101,102"
    assert calls[0][0][:2] == ["ps", "axww"]
    assert calls[0][1]["env"]["COLUMNS"] == "4096"


def test_main_counts_discord_preflight_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        nightly_regression,
        "ensure_discord_bot_for_regression",
        lambda: {"ok": False, "action": "duplicate_running", "pid": "12345,67890", "count": 2},
    )
    monkeypatch.setitem(
        nightly_regression.SUITE_FUNCS,
        "system",
        lambda: {"suite": "system", "label": "System Health", "ok": True, "passed": 1, "failed": 0, "total": 1, "failures": []},
    )
    out = tmp_path / "nightly.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["nightly_regression.py", "--suites", "system", "--no-notify", "--json-out", str(out)],
    )

    rc = nightly_regression.main()
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 1
    assert payload["ok"] is False
    assert payload["suites"][0]["suite"] == "preflight"
    assert "duplicate_running" in payload["suites"][0]["failures"][0]


def test_discord_preflight_starts_missing_bot(monkeypatch, tmp_path):
    root = tmp_path
    (root / "api").mkdir()
    (root / "api" / "discord_bot.py").write_text("print('bot')\n", encoding="utf-8")
    monkeypatch.setattr(nightly_regression, "MAGI_DIR", root)
    monkeypatch.setattr(nightly_regression, "PYTHON", "python-test")

    calls = iter([(False, ""), (True, "24680")])
    monkeypatch.setattr(nightly_regression, "_discord_bot_process", lambda: next(calls))

    popen_calls = []

    class FakePopen:
        pid = 13579
        returncode = None

        def __init__(self, cmd, **kwargs):
            popen_calls.append((cmd, kwargs))

        def poll(self):
            return None

    monkeypatch.setattr(nightly_regression.subprocess, "Popen", FakePopen)

    result = nightly_regression.ensure_discord_bot_for_regression(wait_sec=1)
    assert result["ok"] is True
    assert result["action"] == "started"
    assert result["pid"] == "24680"
    assert result["launcher_pid"] == 13579
    assert popen_calls[0][0] == ["python-test", str(root / "api" / "discord_bot.py")]
    assert popen_calls[0][1]["cwd"] == str(root)
