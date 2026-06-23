from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from scripts.ops import public_push_guard


def test_public_push_guard_requires_public_remote_clean_tree_and_strict_audit(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["git", "remote", "get-url"]:
            return SimpleNamespace(stdout="git@github.com:WhaleChao/MAGI-public.git\n", stderr="", returncode=0)
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(stdout="codex/public-safe\n", stderr="", returncode=0)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd[1:4] == ["scripts/public_release_audit.py", "--public-isolation", "--strict"]:
            return SimpleNamespace(stdout=json.dumps({"ok": True, "errors": 0, "warnings": 0}), stderr="", returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = public_push_guard.check_public_push("public")

    assert result["ok"] is True
    assert result["checks"] == {
        "remote_matches_profile": True,
        "working_tree_clean": True,
        "release_audit_strict": True,
    }
    assert any(call[0:2] == ["git", "status"] for call in calls)


def test_public_push_guard_blocks_wrong_remote_and_dirty_tree(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return SimpleNamespace(stdout="git@github.com:WhaleChao/MAGI-v2.git\n", stderr="", returncode=0)
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(stdout="codex/private\n", stderr="", returncode=0)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(stdout=" M api/server.py\n?? private.pdf\n", stderr="", returncode=0)
        if cmd[1:4] == ["scripts/public_release_audit.py", "--public-isolation", "--strict"]:
            return SimpleNamespace(stdout=json.dumps({"ok": True, "errors": 0, "warnings": 0}), stderr="", returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = public_push_guard.check_public_push("origin")

    assert result["ok"] is False
    assert result["checks"]["remote_matches_profile"] is False
    assert result["checks"]["working_tree_clean"] is False
    assert result["dirty_count"] == 2


def test_private_push_guard_accepts_private_remote_and_strict_secret_audit(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return SimpleNamespace(stdout="git@github.com:WhaleChao/MAGI-v2.git\n", stderr="", returncode=0)
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(stdout="codex/private\n", stderr="", returncode=0)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd[1:] == ["scripts/public_release_audit.py", "--strict", "--json"]:
            return SimpleNamespace(stdout=json.dumps({"ok": True, "errors": 0, "warnings": 0}), stderr="", returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = public_push_guard.check_remote_push("origin", profile="private")

    assert result["ok"] is True
    assert result["profile"] == "private"
    assert result["checks"]["remote_matches_profile"] is True
