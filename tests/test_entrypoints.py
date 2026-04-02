from __future__ import annotations

from pathlib import Path


def test_runtime_resolves_project_root(monkeypatch):
    from bin._runtime import resolve_release_root

    project_root = Path(__file__).parent.parent.resolve()
    monkeypatch.delenv("MAGI_ROOT", raising=False)
    monkeypatch.delenv("MAGI_ROOT_DIR", raising=False)
    monkeypatch.chdir(project_root)

    assert resolve_release_root() == project_root


def test_start_entrypoint_delegates_to_release_launcher(monkeypatch):
    from bin import start as start_cli

    project_root = Path(__file__).parent.parent.resolve()
    calls = {}

    def fake_call(cmd, cwd, env):
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        calls["env"] = env
        return 0

    monkeypatch.setenv("MAGI_ROOT", str(project_root))
    monkeypatch.setattr(start_cli.subprocess, "call", fake_call)

    result = start_cli.main(["--check-only"])

    assert result == 0
    assert calls["cmd"][:2] == ["bash", str(project_root / "bin" / "start")]
    assert calls["cmd"][-1] == "--check-only"
    assert calls["cwd"] == project_root
    assert calls["env"]["MAGI_ROOT"] == str(project_root)


def test_check_entrypoint_delegates_to_release_launcher(monkeypatch):
    from bin import check as check_cli

    project_root = Path(__file__).parent.parent.resolve()
    calls = {}

    def fake_call(cmd, cwd, env):
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        calls["env"] = env
        return 0

    monkeypatch.setenv("MAGI_ROOT", str(project_root))
    monkeypatch.setattr(check_cli.subprocess, "call", fake_call)

    result = check_cli.main()

    assert result == 0
    assert calls["cmd"] == ["bash", str(project_root / "bin" / "check")]
    assert calls["cwd"] == project_root
    assert calls["env"]["MAGI_ROOT_DIR"] == str(project_root)
