from __future__ import annotations

import importlib
from pathlib import Path

import tomllib


def test_console_script_modules_are_importable():
    with open("pyproject.toml", "rb") as f:
        scripts = tomllib.load(f)["project"]["scripts"]

    for module_path in scripts.values():
        module_name, function_name = module_path.split(":", 1)
        module = importlib.import_module(module_name)
        assert callable(getattr(module, function_name))


def test_legacy_magi_entrypoints_remain_declared():
    with open("pyproject.toml", "rb") as f:
        scripts = tomllib.load(f)["project"]["scripts"]

    assert scripts["magi"] == "bin.cli:main"
    assert scripts["magi-start"] == "bin.start:main"
    assert scripts["magi-check"] == "bin.check:main"


def test_runtime_resolves_project_root(monkeypatch):
    from bin._runtime import resolve_release_root

    project_root = Path(__file__).parent.parent.resolve()
    monkeypatch.delenv("MAGI_ROOT", raising=False)
    monkeypatch.delenv("MAGI_ROOT_DIR", raising=False)
    monkeypatch.chdir(project_root)

    assert resolve_release_root() == project_root


def _fake_release_root(tmp_path: Path, launcher_name: str) -> Path:
    root = tmp_path / "MAGI-release"
    (root / "api").mkdir(parents=True)
    (root / "skills").mkdir()
    (root / "bin").mkdir()
    (root / "daemon.py").write_text("print('daemon')\n", encoding="utf-8")
    launcher = root / "bin" / launcher_name
    launcher.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    return root


def test_start_entrypoint_delegates_to_release_launcher(monkeypatch, tmp_path):
    from bin import start as start_cli

    project_root = _fake_release_root(tmp_path, "start")
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


def test_check_entrypoint_delegates_to_release_launcher(monkeypatch, tmp_path):
    from bin import check as check_cli

    project_root = _fake_release_root(tmp_path, "check")
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
