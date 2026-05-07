from scripts import magi_doctor


def test_package_available_falls_back_to_project_venv(monkeypatch):
    calls = []

    monkeypatch.setattr(magi_doctor.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(magi_doctor, "_project_python", lambda: magi_doctor.Path("/tmp/project-python"))
    monkeypatch.setattr(magi_doctor.sys, "executable", "/tmp/current-python")

    class Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(magi_doctor.subprocess, "run", fake_run)

    assert magi_doctor._package_available("fastapi") is True
    assert calls and calls[0][0] == "/tmp/project-python"
