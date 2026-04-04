from __future__ import annotations

from pathlib import Path

from scripts.install_omlx_text import build_launch_agent_plist


def test_build_launch_agent_plist_sets_text_memory_guardrails(monkeypatch, tmp_path):
    monkeypatch.setenv("OMLX_TEXT_MAX_MODEL_MEMORY", "10GB")
    monkeypatch.setenv("OMLX_TEXT_MODEL_DIR", "/tmp/models-text")

    plist = build_launch_agent_plist(tmp_path / "project", tmp_path / "runtime")
    env = plist["EnvironmentVariables"]

    assert plist["Label"] == "com.magi.omlx"
    assert plist["ProgramArguments"] == ["/bin/bash", "/opt/homebrew/bin/omlx-magi-start-text"]
    assert env["OMLX_TEXT_MAX_MODEL_MEMORY"] == "10GB"
    assert env["OMLX_TEXT_MODEL_DIR"] == "/tmp/models-text"
    assert env["OMLX_TEXT_PORT"] == "8080"
