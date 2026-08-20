import json
import os
from pathlib import Path
import subprocess


def test_watchdog_respects_switch_lock_and_profile_mtime():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "omlx_watchdog.sh").read_text(encoding="utf-8")
    block = source[source.index("is_in_switch_grace()") : source.index("log()")]
    assert 'if [ -d "${SWITCH_LOCKDIR}" ]' in block
    assert 'stat -f %m "${PROFILE_FILE}"' in block
    assert "_switch_ts" not in source


def _run_watchdog_once(tmp_path: Path, *, profile: str, configured_model: str, live_model: str):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "omlx_watchdog.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' '{json.dumps({'data': [{'id': live_model}]})}'\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    model_dir = tmp_path / "models-text"
    model_dir.mkdir()
    (model_dir / configured_model).mkdir()
    profile_file = tmp_path / "active_profile"
    profile_file.write_text(profile + "\n", encoding="utf-8")
    state_file = tmp_path / "state.json"
    log_file = tmp_path / "watchdog.log"
    launchctl_calls = tmp_path / "launchctl.calls"
    fake_launchctl = fake_bin / "launchctl"
    fake_launchctl.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> '{launchctl_calls}'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_launchctl.chmod(0o755)
    fake_pgrep = fake_bin / "pgrep"
    fake_pgrep.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    fake_pgrep.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "MAGI_OMLX_ACTIVE_PROFILE_FILE": str(profile_file),
            "MAGI_OMLX_CONFIGURED_MODEL_DIR": str(model_dir),
            "MAGI_OMLX_WATCHDOG_STATE_PATH": str(state_file),
            "MAGI_OMLX_WATCHDOG_LOG": str(log_file),
            "MAGI_OMLX_WATCHDOG_LOCK": str(tmp_path / "watchdog.pid"),
            "MAGI_OMLX_WATCHDOG_STATE_DIR": str(tmp_path),
            "MAGI_OMLX_WATCHDOG_LOG_DIR": str(tmp_path),
            "MAGI_OMLX_SWITCH_LOCKDIR": str(tmp_path / "no-switch-lock"),
        }
    )
    result = subprocess.run(
        ["/bin/bash", str(script), "--once"],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    state = json.loads(state_file.read_text(encoding="utf-8"))
    calls = launchctl_calls.read_text(encoding="utf-8") if launchctl_calls.exists() else ""
    return result, state, calls


def test_watchdog_accepts_night_12b_fallback_without_kickstart(tmp_path):
    model = "gemma-4-12B-it-4bit"
    result, state, launchctl_calls = _run_watchdog_once(
        tmp_path,
        profile="night-12b-degraded",
        configured_model=model,
        live_model=model,
    )

    assert result.returncode == 0, result.stderr
    assert state["status"] == "healthy"
    assert state["active_profile"] == "night-12b-degraded"
    assert state["active_model_id"] == model
    assert state["probe_model"] == model
    assert state["configured_model_ids"] == [model]
    assert state["model_compatible"] is True
    assert "kickstart" not in launchctl_calls


def test_watchdog_reports_profile_mismatch_without_kickstart(tmp_path):
    result, state, launchctl_calls = _run_watchdog_once(
        tmp_path,
        profile="night-12b-degraded",
        configured_model="gemma-4-e4b-it-4bit",
        live_model="gemma-4-e4b-it-4bit",
    )

    assert result.returncode == 1
    assert state["status"] == "degraded"
    assert state["reason"] == "model_context_mismatch"
    assert state["model_compatible"] is False
    assert state["model_context_reason"] == "model_profile_mismatch"
    assert "kickstart" not in launchctl_calls


def test_watchdog_reports_unconfigured_live_model_without_kickstart(tmp_path):
    result, state, launchctl_calls = _run_watchdog_once(
        tmp_path,
        profile="night-12b-degraded",
        configured_model="gemma-4-12B-it-4bit",
        live_model="gemma-4-e4b-it-4bit",
    )

    assert result.returncode == 1
    assert state["status"] == "degraded"
    assert state["model_context_reason"] == "model_not_in_enabled_plist_directory"
    assert "kickstart" not in launchctl_calls
