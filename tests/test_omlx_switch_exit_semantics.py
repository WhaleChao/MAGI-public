from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWITCH = ROOT / "config" / "bin" / "omlx_switch_model.sh"


def _run_gatekeeper_result(tmp_path: Path, returncode: int) -> subprocess.CompletedProcess[str]:
    magi_root = tmp_path / "release"
    gatekeeper = magi_root / "scripts" / "ops" / "omlx_switch_gatekeeper.py"
    gatekeeper.parent.mkdir(parents=True)
    gatekeeper.write_text("# source-presence sentinel\n", encoding="utf-8")
    gatekeeper.chmod(0o755)

    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "echo gatekeeper-sentinel >&2\n"
        f"exit {returncode}\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    return subprocess.run(
        ["/bin/bash", str(SWITCH), "night"],
        cwd=ROOT,
        env={
            **os.environ,
            "MAGI_ROOT_DIR": str(magi_root),
            "MAGI_V3_EXECUTABLE_PATH": str(fake_python),
            "MAGI_OMLX_SWITCH_LOCKDIR": str(tmp_path / "switch.lock"),
            "MAGI_OMLX_SWITCH_LOG": str(tmp_path / "switch.log"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_gatekeeper_pause_is_deferred_not_success(tmp_path: Path) -> None:
    result = _run_gatekeeper_result(tmp_path, 1)

    assert result.returncode == 75
    assert "gatekeeper-sentinel" in result.stdout
    assert "延後本次 night" in result.stdout


def test_gatekeeper_launcher_failure_is_not_relabelled_as_pause(tmp_path: Path) -> None:
    result = _run_gatekeeper_result(tmp_path, 126)

    assert result.returncode == 126
    assert "gatekeeper-sentinel" in result.stdout
    assert "無法可信執行（rc=126）" in result.stdout
    assert "處於 pause 狀態" not in result.stdout
