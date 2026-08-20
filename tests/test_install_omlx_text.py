from pathlib import Path

from scripts.install_omlx_text import build_launch_agent_plist


def test_normal_runtime_does_not_bind_release_python(monkeypatch) -> None:
    monkeypatch.delenv("OMLX_GEMMA4_UNIFIED_RUNTIME", raising=False)
    monkeypatch.setenv("MAGI_OMLX_GEMMA4_PYTHON", "/old/release/bin/python")

    environment = build_launch_agent_plist(
        Path("/opt/magi/releases/v3-test"), Path("/opt/magi/runtime")
    )["EnvironmentVariables"]

    assert environment["OMLX_GEMMA4_UNIFIED_RUNTIME"] == "0"
    assert "MAGI_ROOT_DIR" not in environment
    assert "MAGI_OMLX_GEMMA4_PYTHON" not in environment


def test_unified_runtime_binds_explicit_python(monkeypatch) -> None:
    monkeypatch.setenv("OMLX_GEMMA4_UNIFIED_RUNTIME", "true")
    monkeypatch.setenv("MAGI_OMLX_GEMMA4_PYTHON", "/opt/magi/runtime/bin/python")

    environment = build_launch_agent_plist(
        Path("/opt/magi/releases/v3-test"), Path("/opt/magi/runtime")
    )["EnvironmentVariables"]

    assert environment["MAGI_ROOT_DIR"] == "/opt/magi/releases/v3-test"
    assert environment["MAGI_OMLX_GEMMA4_PYTHON"] == "/opt/magi/runtime/bin/python"
