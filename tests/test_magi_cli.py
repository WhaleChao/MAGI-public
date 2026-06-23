from __future__ import annotations

import json
import tomllib
from pathlib import Path

from bin import cli


class FakeManager:
    def __init__(self, *, installed: bool = False, running: bool = False):
        self.installed = installed
        self.running = running
        self.installs = []
        self.starts = []
        self.stops = []
        self.root = Path("/")

    def _service_path(self, name: str) -> Path:
        marker = self.root / f"{name}.service"
        if self.installed:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("service", encoding="utf-8")
        return marker

    def install(self, name: str, command: str, description: str = "") -> bool:
        self.installs.append((name, command, description))
        return True

    def start(self, name: str) -> bool:
        self.starts.append(name)
        self.running = True
        return True

    def stop(self, name: str) -> bool:
        self.stops.append(name)
        self.running = False
        return True

    def is_running(self, name: str) -> bool:
        return self.running


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "MAGI_public"
    (root / "api").mkdir(parents=True)
    (root / "skills").mkdir()
    (root / "scripts").mkdir()
    (root / "daemon.py").write_text("print('daemon')\n", encoding="utf-8")
    (root / "scripts" / "magi_doctor.py").write_text("print('doctor')\n", encoding="utf-8")
    return root


def patch_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(cli, "resolve_release_root", lambda: root)
    monkeypatch.setattr(cli, "resolve_python", lambda _root: Path("/usr/bin/python3"))


def patch_manager(monkeypatch, manager: FakeManager, root: Path) -> None:
    manager.root = root / ".service-markers"
    monkeypatch.setattr(cli, "_get_service_manager", lambda: manager)


def test_service_install_dry_run_prints_plan_without_installing(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    manager = FakeManager(installed=False)
    patch_root(monkeypatch, root)
    patch_manager(monkeypatch, manager, root)

    assert cli.main(["service", "install", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "MAGI service install dry-run" in out
    assert str(root) in out
    assert str(root / "daemon.py") in out
    assert manager.installs == []
    assert manager.starts == []


def test_service_install_dry_run_json_is_machine_readable(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    patch_root(monkeypatch, root)
    patch_manager(monkeypatch, FakeManager(), root)

    assert cli.main(["service", "install", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["service"] == cli.SERVICE_NAME
    assert payload["root"] == str(root)
    assert payload["command"][-1] == str(root / "daemon.py")


def test_doctor_wraps_existing_script_with_runtime_env(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    calls = []
    patch_root(monkeypatch, root)

    def fake_call(command, **kwargs):
        calls.append((command, kwargs))
        return 0

    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    assert cli.main(["doctor", "--json", "--no-live"]) == 0

    command, kwargs = calls[0]
    assert command == ["/usr/bin/python3", str(root / "scripts" / "magi_doctor.py"), "--json", "--no-live"]
    assert kwargs["cwd"] == root
    assert kwargs["env"]["MAGI_ROOT_DIR"] == str(root)


def test_start_background_uses_resolved_root_without_service(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    popen_calls = []
    patch_root(monkeypatch, root)
    patch_manager(monkeypatch, FakeManager(installed=False), root)
    monkeypatch.setattr(cli, "_daemon_running", lambda: False)

    class FakePopen:
        pid = 4242

        def __init__(self, command, **kwargs):
            popen_calls.append((command, kwargs))

    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)

    assert cli.main(["start", "--check-only"]) == 0

    command, kwargs = popen_calls[0]
    assert command == ["/usr/bin/python3", str(root / "daemon.py"), "--check-only"]
    assert kwargs["cwd"] == root
    assert kwargs["env"]["MAGI_ROOT"] == str(root)
    assert "4242" in capsys.readouterr().out


def test_status_json_reports_resolved_root(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    patch_root(monkeypatch, root)
    patch_manager(monkeypatch, FakeManager(installed=True, running=True), root)
    monkeypatch.setattr(cli, "_daemon_running", lambda: True)
    monkeypatch.setattr(cli, "_health_probe", lambda url, timeout=1.5: {"ok": False, "error": "offline"})

    assert cli.main(["status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["root"] == str(root)
    assert payload["service"]["running"] is True
    assert payload["daemon"]["running"] is True


def test_pyproject_declares_formal_magi_console_script():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    scripts = data["project"]["scripts"]
    assert scripts["magi"] == "bin.cli:main"
    assert "magi-start" in scripts
    assert "magi-check" in scripts
