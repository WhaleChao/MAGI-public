# -*- coding: utf-8 -*-
"""Tests for api.platform.safe_process (R2)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from api.platforms import safe_process as sp


def _trusted_test_venv(tmp_path, monkeypatch):
    """Build the minimum macOS venv layout needed by PYTHONEXECUTABLE."""

    # A copied venv executable does not resolve to the framework binary that
    # actually started Python.  Production validation deliberately binds the
    # alias to ``_base_executable`` so an exchanged venv executable cannot be
    # trusted after validation; mirror that real interpreter contract here.
    canonical = Path(sys._base_executable).resolve(strict=True)
    base_prefix = sys.base_prefix
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    root = tmp_path / "trusted-venv"
    bin_dir = root / "bin"
    site_packages = root / "lib" / f"python{version}" / "site-packages"
    bin_dir.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    alias = bin_dir / "python"
    alias.symlink_to(canonical)
    (root / "pyvenv.cfg").write_text(
        "\n".join(
            (
                f"home = {canonical.parent}",
                "include-system-site-packages = false",
                f"version = {sys.version.split()[0]}",
                f"executable = {canonical}",
                "",
            )
        ),
        encoding="utf-8",
    )
    (site_packages / "magi_venv_only_probe.py").write_text(
        'VALUE = "venv-only"\n', encoding="utf-8"
    )
    monkeypatch.setattr(sp.sys, "platform", "darwin")
    monkeypatch.setattr(sp.sys, "executable", str(alias))
    monkeypatch.setattr(sp.sys, "_base_executable", str(canonical))
    monkeypatch.setattr(sp.sys, "prefix", str(root))
    monkeypatch.setattr(sp.sys, "base_prefix", str(base_prefix))
    return root, alias, canonical


@pytest.fixture(autouse=True)
def _reset_sem():
    sp.reset_for_test()
    yield
    sp.reset_for_test()


# --- argv 白名單 --------------------------------------------------------

def test_argv_head_whitelisted_python3():
    r = sp.run(["python3", "-c", "print('ok')"], timeout_sec=10)
    assert r.returncode == 0 and "ok" in r.stdout


def test_versioned_python3_executable_is_whitelisted():
    assert sp._PYTHON_EXECUTABLE_RE.fullmatch("python3.14")


def test_windows_python_executable_is_whitelisted():
    assert sp._PYTHON_EXECUTABLE_RE.fullmatch("python.exe")
    windows_python = "\\".join(
        ("C:", "Applications", "Agent", "runtime", "venv", "Scripts", "python.exe")
    )
    sp._validate_argv([windows_python, "fixture.py"])


def test_absolute_python_alias_for_current_interpreter_is_whitelisted(
    tmp_path, monkeypatch
):
    _root, alias, _canonical = _trusted_test_venv(tmp_path, monkeypatch)

    sp._validate_argv([str(alias), "fixture.py"])


def test_relative_or_foreign_python_alias_is_rejected(tmp_path):
    foreign = tmp_path / "python"
    foreign.symlink_to("/bin/echo")

    with pytest.raises(PermissionError):
        sp._validate_argv(["python", "fixture.py"])
    with pytest.raises(PermissionError):
        sp._validate_argv([str(foreign), "fixture.py"])


def test_current_python_alias_keeps_shell_metachar_guards(tmp_path, monkeypatch):
    _root, alias, _canonical = _trusted_test_venv(tmp_path, monkeypatch)

    with pytest.raises(PermissionError):
        sp._validate_argv([str(alias), "fixture.py;rm"])


def test_current_python_alias_does_not_inherit_python3_dash_c_exception(
    tmp_path, monkeypatch
):
    _root, alias, _canonical = _trusted_test_venv(tmp_path, monkeypatch)

    # The V3 business probes execute a script path, not ``python -c``.  Keep
    # the new compatibility allowance narrower than the existing python3
    # exception so semicolon-bearing inline programs remain fail-closed.
    with pytest.raises(PermissionError):
        sp._validate_argv([str(alias), "-c", "value = 1; print(value)"])


def test_run_executes_canonical_interpreter_after_python_alias_exchange(
    tmp_path, monkeypatch
):
    _root, alias, canonical = _trusted_test_venv(tmp_path, monkeypatch)
    original_validate = sp._validate_argv
    captured = {}

    def validate_then_exchange(argv):
        target = original_validate(argv)
        alias.unlink()
        alias.symlink_to("/bin/echo")
        return target

    class FakeProcess:
        pid = 4321
        returncode = 0

        def communicate(self, timeout):
            return b"", b""

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = dict(kwargs["env"])
        return FakeProcess()

    monkeypatch.setattr(sp, "_validate_argv", validate_then_exchange)
    monkeypatch.setattr(sp.subprocess, "Popen", fake_popen)

    result = sp.run(
        [str(alias), "fixture.py"],
        env_whitelist_prefixes=("PYTHON",),
        env_extra={"PYTHONEXECUTABLE": "/bin/echo"},
    )

    assert result.returncode == 0
    assert alias.resolve(strict=True) == Path("/bin/echo").resolve(strict=True)
    assert captured["argv"][0] == str(canonical)
    assert captured["argv"][0] != str(alias)
    assert captured["env"]["PYTHONEXECUTABLE"] == str(alias)


@pytest.mark.skipif(sys.platform != "darwin", reason="PYTHONEXECUTABLE is macOS-only")
def test_alias_exchange_still_runs_canonical_child_with_venv_site(
    tmp_path, monkeypatch
):
    root, alias, canonical = _trusted_test_venv(tmp_path, monkeypatch)
    probe = tmp_path / "probe_after_exchange.py"
    probe.write_text(
        "import json, sys\n"
        "import magi_venv_only_probe\n"
        "print(json.dumps({"
        "'prefix': sys.prefix, "
        "'base_executable': sys._base_executable, "
        "'value': magi_venv_only_probe.VALUE"
        "}))\n",
        encoding="utf-8",
    )
    original_validate = sp._validate_argv

    def validate_then_exchange(argv):
        binding = original_validate(argv)
        alias.unlink()
        alias.symlink_to("/bin/echo")
        return binding

    monkeypatch.setattr(sp, "_validate_argv", validate_then_exchange)

    result = sp.run([str(alias), str(probe)])

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "prefix": str(root),
        "base_executable": str(canonical),
        "value": "venv-only",
    }
    assert alias.resolve(strict=True) == Path("/bin/echo").resolve(strict=True)
    assert canonical != alias.resolve(strict=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="PYTHONEXECUTABLE is macOS-only")
def test_canonical_python_preserves_venv_prefix_and_venv_only_import(
    tmp_path, monkeypatch
):
    root, alias, canonical = _trusted_test_venv(tmp_path, monkeypatch)
    poison = tmp_path / "poison"
    release_root = tmp_path / "release"
    poison.mkdir()
    release_root.mkdir()
    (poison / "magi_venv_only_probe.py").write_text(
        'VALUE = "poisoned"\n', encoding="utf-8"
    )
    (release_root / "magi_release_probe.py").write_text(
        'VALUE = "release-root"\n', encoding="utf-8"
    )
    poisoned_environment = {
        "PYTHONEXECUTABLE": "/bin/echo",
        "PYTHONHOME": str(poison),
        "PYTHONINSPECT": "1",
        "PYTHONPLATLIBDIR": "poison-lib",
        "PYTHONSTARTUP": str(poison / "startup.py"),
        "PYTHONUSERBASE": str(poison),
        "__PYVENV_LAUNCHER__": "/bin/echo",
    }
    for key, value in poisoned_environment.items():
        monkeypatch.setenv(key, value)
    probe = tmp_path / "probe_venv.py"
    probe.write_text(
        "import json, os, sys\n"
        "import magi_release_probe, magi_venv_only_probe\n"
        "print(json.dumps({"
        "'executable': sys.executable, "
        "'prefix': sys.prefix, "
        "'base_prefix': sys.base_prefix, "
        "'value': magi_venv_only_probe.VALUE, "
        "'release_value': magi_release_probe.VALUE, "
        "'pythonpath': os.environ.get('PYTHONPATH'), "
        "'python_env': {k: os.environ.get(k) for k in "
        f"{sorted(poisoned_environment)!r}"
        "}}))\n",
        encoding="utf-8",
    )

    result = sp.run(
        [str(alias), str(probe)],
        env_whitelist_prefixes=("PATH", "PYTHON", "__PYVENV_"),
        env_extra={**poisoned_environment, "PYTHONPATH": str(release_root)},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["executable"] == str(alias)
    assert payload["prefix"] == str(root)
    assert payload["base_prefix"] != str(root)
    assert payload["value"] == "venv-only"
    assert payload["release_value"] == "release-root"
    assert payload["pythonpath"] == str(release_root)
    assert str(canonical) != str(alias)
    assert payload["python_env"]["PYTHONEXECUTABLE"] == str(alias)
    for key in poisoned_environment.keys() - {"PYTHONEXECUTABLE"}:
        assert payload["python_env"][key] is None


def test_current_python_alias_is_fail_closed_off_macos(tmp_path, monkeypatch):
    _root, alias, _canonical = _trusted_test_venv(tmp_path, monkeypatch)
    monkeypatch.setattr(sp.sys, "platform", "linux")

    with pytest.raises(PermissionError):
        sp._validate_argv([str(alias), "fixture.py"])


def test_argv_head_rejected_bash():
    with pytest.raises(PermissionError):
        sp.run(["bash", "-c", "echo x"])


def test_argv_head_rejected_sh():
    with pytest.raises(PermissionError):
        sp.run(["/bin/sh", "-c", "echo x"])


def test_argv_empty_rejected():
    with pytest.raises(ValueError):
        sp.run([])


def test_argv_non_string_rejected():
    with pytest.raises(TypeError):
        sp.run(["python3", 123])


# --- shell metachar denylist -------------------------------------------

def test_argv_semicolon_rejected():
    # 測試非 code 引數中的 shell injection（git msg 含 ;）
    with pytest.raises(PermissionError):
        sp.run(["git", "commit", "-m", "msg; rm -rf /"])


def test_argv_pipe_rejected():
    with pytest.raises(PermissionError):
        sp.run(["git", "commit", "-m", "msg | cat /etc/passwd"])


def test_argv_backtick_rejected():
    with pytest.raises(PermissionError):
        sp.run(["git", "commit", "-m", "`whoami`"])


# --- env 白名單 ---------------------------------------------------------

def test_env_default_prefix_filters_secrets(monkeypatch):
    monkeypatch.setenv("MAGI_X", "visible")
    monkeypatch.setenv("SECRET_TOKEN", "HIDDEN")
    r = sp.run(
        ["python3", "-c", "import os; print(os.environ.get('MAGI_X','')); print(os.environ.get('SECRET_TOKEN','NOPE'))"],
        timeout_sec=10,
    )
    assert "visible" in r.stdout
    assert "HIDDEN" not in r.stdout


def test_env_custom_prefix_extends(monkeypatch):
    monkeypatch.setenv("CUSTOM_OK", "yes")
    r = sp.run(
        ["python3", "-c", "import os; print(os.environ.get('CUSTOM_OK',''))"],
        env_whitelist_prefixes=("MAGI_", "CUSTOM_"),
        timeout_sec=10,
    )
    assert "yes" in r.stdout


def test_env_extra_respects_default_whitelist():
    r = sp.run(
        [
            "python3",
            "-c",
            "import os; print(os.environ.get('MAGI_EXTRA','')); print(os.environ.get('SECRET_EXTRA','NOPE'))",
        ],
        env_extra={"MAGI_EXTRA": "visible", "SECRET_EXTRA": "hidden"},
        timeout_sec=10,
    )
    assert "visible" in r.stdout
    assert "hidden" not in r.stdout


# --- timeout / kill -----------------------------------------------------

def test_timeout_triggers_sigterm():
    r = sp.run(["python3", "-c", "import time; time.sleep(30)"], timeout_sec=2.0)
    assert r.timed_out is True
    assert r.returncode != 0


def test_controlled_cancel_reaps_owned_process_without_timeout_failure():
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(sp._SafeProcessCancelledError):
            sp.run(
                ["python3", "-c", "import time; time.sleep(30)"],
                timeout_sec=60.0,
                _cancel_event=cancel,
            )
    finally:
        timer.cancel()

    assert time.monotonic() - started < 5.0


def test_sigkill_after_grace():
    # 子進程 ignore SIGTERM → 必須被 SIGKILL
    code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)"
    )
    r = sp.run(["python3", "-c", code], timeout_sec=1.0)
    assert r.timed_out is True and r.killed is True


def test_timeout_kills_child_process_group(tmp_path):
    marker = tmp_path / "child-survived.txt"
    code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',"
        f"\"import pathlib,time; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('alive')\"]); "
        "time.sleep(30)"
    )

    r = sp.run(["python3", "-c", code], timeout_sec=0.5)
    time.sleep(3.5)

    assert r.timed_out is True
    assert not marker.exists()


def test_timeout_kills_nested_new_session_descendant(tmp_path):
    """resource_guarded_run-style descendants must not escape the outer timeout."""
    marker = tmp_path / "nested-session-child-survived.txt"
    nested_code = (
        "import pathlib,time; "
        "time.sleep(3); "
        f"pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',"
        f"{nested_code!r}], start_new_session=True); "
        "time.sleep(30)"
    )

    r = sp.run(["python3", "-c", code], timeout_sec=0.5)
    time.sleep(3.5)

    assert r.timed_out is True
    assert not marker.exists()


def test_normal_parent_exit_reaps_tracked_descendant_without_false_timeout(
    tmp_path, monkeypatch
):
    """A completed launcher must not wait for a reparented pipe holder."""

    release_parent = tmp_path / "release-parent"
    child_pid_path = tmp_path / "child.pid"
    marker = tmp_path / "reparented-child-survived.txt"
    root_pid = {"value": 0}
    descendant_tracked = threading.Event()
    original_observation = sp._darwin_process_observation

    def observed(pid):
        result = original_observation(pid)
        if (
            result is not None
            and threading.current_thread().name.startswith("safe-process-tracker-")
            and root_pid["value"] > 0
            and result.ppid == root_pid["value"]
        ):
            descendant_tracked.set()
        return result

    monkeypatch.setattr(sp, "_darwin_process_observation", observed)
    nested_code = (
        "import os,pathlib,time; "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(30); "
        f"pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',"
        f"{nested_code!r}], start_new_session=True); "
        f"release=pathlib.Path({str(release_parent)!r}); "
        "\nwhile not release.exists(): time.sleep(0.001)"
    )

    def release_after_tracking(pid):
        root_pid["value"] = pid
        assert descendant_tracked.wait(2.0)
        release_parent.write_text("release", encoding="utf-8")

    result = sp.run(
        ["python3", "-c", parent_code],
        timeout_sec=0.2,
        _on_started=release_after_tracking,
    )

    assert result.returncode == 0
    assert result.timed_out is False
    assert child_pid_path.is_file()
    assert original_observation(int(child_pid_path.read_text())) is None
    assert not marker.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_normal_parent_exit_recovers_immediate_pipe_holder_20_times(
    tmp_path, monkeypatch
):
    """An unsynchronised setsid pipe holder cannot create false timeouts."""

    # Make this specifically exercise pipe-holder recovery rather than winning
    # a race in the normal child tracker.
    monkeypatch.setattr(sp, "_darwin_child_pids", lambda _pid: ())
    markers = []
    for attempt in range(20):
        marker = tmp_path / f"orphan-leak-{attempt}.txt"
        markers.append(marker)
        nested_code = (
            "import pathlib,time; "
            "time.sleep(0.35); "
            f"pathlib.Path({str(marker)!r}).write_text('leak')"
        )
        parent_code = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c',"
            f"{nested_code!r}], start_new_session=True)"
        )

        result = sp.run(["python3", "-c", parent_code], timeout_sec=0.03)
        # A 30 ms deadline deliberately races process startup: a parent that
        # has already exited stays a success, while one still alive at the
        # deadline is a genuine timeout.  Neither outcome may leak the holder.
        assert result.timed_out is (result.returncode != 0)

    time.sleep(0.45)
    assert [path for path in markers if path.exists()] == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt/libproc is macOS-only")
def test_pipe_holder_libproc_probe_works_inside_write_network_seatbelt():
    """Formal Seatbelt still permits cross-process LISTFDS/FDPIPEINFO proof."""

    source_root = Path(sp.__file__).resolve().parents[2]
    code = f"""
import os,signal,sys,time
sys.path.insert(0, {str(source_root)!r})
from api.platforms import safe_process as sp
r,w=os.pipe()
pid=os.fork()
if pid == 0:
    os.close(r)
    # The parent proves the pipe identity and always terminates this child in
    # ``finally``.  Keep the holder alive well beyond a heavily loaded formal
    # suite so scheduler delay cannot turn the capability probe into a race.
    time.sleep(30)
    os._exit(0)
os.close(w)
try:
    target=sp._darwin_pipe_info(os.getpid(),r)
    held=sp._darwin_process_pipe_identities(pid)
    assert target is not None and tuple(sorted(target)) in held
finally:
    try: os.kill(pid,signal.SIGKILL)
    except ProcessLookupError: pass
    os.waitpid(pid,0)
    os.close(r)
"""
    profile = (
        "(version 1)(allow default)(deny network*)(deny file-write*)"
        '(allow file-write* (literal "/dev/null"))'
    )
    command = [sys.executable, "-I", "-S", "-c", code]
    if os.environ.get("MAGI_V3_RELEASE_QUALITY_SEATBELT_CHILD") != "1":
        command = ["/usr/bin/sandbox-exec", "-p", profile, "--", *command]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=False,
    )

    if (
        completed.returncode == 71
        and "sandbox_apply: Operation not permitted" in completed.stderr
        and os.environ.get("MAGI_V3_RELEASE_QUALITY_SEATBELT_CHILD") != "1"
    ):
        # Managed hosts may forbid creating a nested Seatbelt even though this
        # source-level CI process is not running under MAGI's formal profile.
        # Keep the result fail-closed and separately prove the libproc behavior;
        # the formal campaign sets the marker and proves the combined property.
        direct = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        assert direct.returncode == 0, direct.stderr
        return
    assert completed.returncode == 0, completed.stderr


def test_pipe_holder_recovery_fails_closed_when_process_scan_is_unavailable(
    monkeypatch,
):
    tracker = object.__new__(sp._OwnedProcessTracker)
    tracker.pipe_endpoints = (
        sp._PipeEndpoint(fd=10, handle=100, peer_handle=101),
        sp._PipeEndpoint(fd=11, handle=200, peer_handle=201),
    )
    tracker.launched_at = time.time()
    tracker.owner_uid = os.getuid()
    monkeypatch.setattr(
        sp,
        "_darwin_live_pipe_identities",
        lambda _endpoints: frozenset({(100, 101), (200, 201)}),
    )
    monkeypatch.setattr(sp, "_darwin_process_table", lambda: None)

    with pytest.raises(sp._SafeProcessCleanupError, match="cannot enumerate"):
        tracker.recover_pipe_holders()


def test_timeout_retries_reap_after_sigkill_communicate_timeout(monkeypatch):
    class FakeProcess:
        pid = 4321
        returncode = -9

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout):
            self.calls += 1
            if self.calls <= 3:
                if self.calls == 1:
                    # Model the initial communicate call consuming its full
                    # deadline; later calls exercise the bounded reap retries.
                    time.sleep(timeout)
                raise sp.subprocess.TimeoutExpired(["python3"], timeout, output=b"out", stderr=b"err")
            return b"out", b"err"

    proc = FakeProcess()
    monkeypatch.setattr(sp.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(
        sp, "_signal_owned_processes", lambda *_args, **_kwargs: {4321}
    )

    result = sp.run(["python3", "-c", "pass"], timeout_sec=0.01)

    assert result.timed_out is True
    assert result.killed is True
    assert proc.calls == 4


def test_cleanup_error_reports_only_current_owned_pids(monkeypatch):
    class FakeProcess:
        pid = 4321
        returncode = None

        def communicate(self, timeout):
            raise sp.subprocess.TimeoutExpired(["python3"], timeout)

    monkeypatch.setattr(sp.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        sp, "_signal_owned_processes", lambda *_args, **_kwargs: {4321}
    )
    monkeypatch.setattr(
        sp._OwnedProcessTracker, "current_pids", lambda _self: {4321, 4322}
    )

    with pytest.raises(
        sp._SafeProcessCleanupError,
        match=r"live_owned_pids=\[4321, 4322\]",
    ):
        sp.run(["python3", "-c", "pass"], timeout_sec=0.01)


def test_timeout_unreapable_after_sigkill_raises_cleanup_failure(monkeypatch):
    class FakeProcess:
        pid = 4321
        returncode = None

        def communicate(self, timeout):
            raise sp.subprocess.TimeoutExpired(["python3"], timeout)

    monkeypatch.setattr(sp.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        sp, "_signal_owned_processes", lambda *_args, **_kwargs: {4321}
    )

    with pytest.raises(sp._SafeProcessCleanupError, match="could not be reaped"):
        sp.run(["python3", "-c", "pass"], timeout_sec=0.01)


def test_start_callback_failure_reaps_owned_child(monkeypatch):
    class FakeProcess:
        pid = 4321
        returncode = -9

        def communicate(self, timeout):
            return b"", b""

    signals = []
    monkeypatch.setattr(sp.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())

    def record_signal(_proc, *, signal_number, tracker):
        signals.append(signal_number)
        return {tracker.root_pid}

    monkeypatch.setattr(sp, "_signal_owned_processes", record_signal)

    with pytest.raises(sp._SafeProcessCleanupError, match="start callback failed; child was reaped"):
        sp.run(["python3", "-c", "pass"], _on_started=lambda _pid: (_ for _ in ()).throw(RuntimeError("state failed")))

    assert signals == [sp.signal.SIGKILL]


def test_signal_rejects_reused_pid_start_identity(monkeypatch):
    expected = sp._ProcessObservation(
        identity=sp._ProcessIdentity(
            pid=4321,
            start_sec=10,
            start_usec=20,
            uid=os.getuid(),
            start_abstime=100,
        ),
        ppid=1,
        pgid=4321,
    )
    reused = sp._ProcessObservation(
        identity=sp._ProcessIdentity(
            pid=4321,
            start_sec=11,
            start_usec=20,
            uid=os.getuid(),
            start_abstime=101,
        ),
        ppid=1,
        pgid=4321,
    )

    class Tracker:
        owner_uid = os.getuid()

        def current(self):
            return {4321: expected}

    signals = []
    monkeypatch.setattr(sp.sys, "platform", "darwin")
    monkeypatch.setattr(sp, "_darwin_process_table", lambda: {4321: reused})
    monkeypatch.setattr(sp.os, "kill", lambda *args: signals.append(args))
    monkeypatch.setattr(sp.os, "killpg", lambda *args: signals.append(args))

    signaled = sp._signal_owned_processes(
        type("Proc", (), {"pid": 4321})(),
        signal_number=sp.signal.SIGKILL,
        tracker=Tracker(),
    )

    assert signaled == set()
    assert signals == []


def test_signal_does_not_kill_group_with_unknown_member(monkeypatch):
    expected = sp._ProcessObservation(
        identity=sp._ProcessIdentity(
            pid=4321,
            start_sec=10,
            start_usec=20,
            uid=os.getuid(),
            start_abstime=100,
        ),
        ppid=1,
        pgid=5000,
    )
    unknown = sp._ProcessObservation(
        identity=sp._ProcessIdentity(
            pid=4322,
            start_sec=10,
            start_usec=30,
            uid=os.getuid(),
            start_abstime=101,
        ),
        ppid=1,
        pgid=5000,
    )

    class Tracker:
        owner_uid = os.getuid()

        def current(self):
            return {4321: expected}

    pid_signals = []
    group_signals = []
    monkeypatch.setattr(sp.sys, "platform", "darwin")
    monkeypatch.setattr(
        sp, "_darwin_process_table", lambda: {4321: expected, 4322: unknown}
    )
    monkeypatch.setattr(sp, "_darwin_process_observation", lambda _pid: expected)
    monkeypatch.setattr(sp.os, "kill", lambda *args: pid_signals.append(args))
    monkeypatch.setattr(sp.os, "killpg", lambda *args: group_signals.append(args))

    signaled = sp._signal_owned_processes(
        type("Proc", (), {"pid": 4321})(),
        signal_number=sp.signal.SIGKILL,
        tracker=Tracker(),
    )

    assert signaled == set()
    assert group_signals == []
    assert pid_signals == [(4321, sp.signal.SIGKILL)]


# --- stdout / stderr cap -----------------------------------------------

def test_stdout_truncated_at_1mb():
    code = "print('x' * (2 * 1024 * 1024))"
    r = sp.run(["python3", "-c", code], timeout_sec=15)
    assert "truncated" in r.stdout
    assert len(r.stdout.encode("utf-8")) <= 1_048_576 + 200


# --- launchctl ----------------------------------------------------------

def test_launchctl_label_regex_accepts_valid():
    # 只驗證 regex，不真的跑 launchctl
    assert sp._LAUNCHCTL_LABEL_RE.match("com.magi.daemon")
    assert sp._LAUNCHCTL_LABEL_RE.match("com.magi.omlx-phi4")


def test_launchctl_label_regex_rejects_invalid():
    with pytest.raises(PermissionError):
        sp.launchctl_op("bootout", "com.other.service")
    with pytest.raises(PermissionError):
        sp.launchctl_op("bootout", "com.magi.DAEMON")  # 大寫不准
    with pytest.raises(PermissionError):
        sp.launchctl_op("bootout", "com.magi.;rm")


def test_launchctl_op_whitelist():
    with pytest.raises(PermissionError):
        sp.launchctl_op("unload", "com.magi.daemon")   # 舊動詞不准


# --- parse_cron_command -------------------------------------------------

def test_parse_cron_simple():
    assert sp.parse_cron_command("python3 script.py --flag x") == [
        "python3", "script.py", "--flag", "x"
    ]


def test_parse_cron_repairs_unquoted_application_support_runtime_path():
    runtime_root = Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v2"
    command = (
        f"{runtime_root}/venv/bin/python3 "
        f"{runtime_root}/scripts/ops/token_health_check.py "
        "--refresh"
    )

    assert sp.parse_cron_command(command) == [
        str(runtime_root / "venv/bin/python3"),
        str(runtime_root / "scripts/ops/token_health_check.py"),
        "--refresh",
    ]


def test_parse_cron_accepts_quoted_application_support_runtime_path():
    runtime_root = Path("/").joinpath(
        "Users", "example", "Library", "Application Support", "MAGI", "runtime", "MAGI_v2"
    )
    command = (
        f"'{runtime_root}/venv/bin/python3' "
        f"'{runtime_root}/scripts/ops/resource_guarded_run.py' "
        "--job-id job_drive_case_sync_all_files -- python3 scripts/drive_case_sync_worker.py"
    )

    argv = sp.parse_cron_command(command)

    assert argv[:4] == [
        str(runtime_root / "venv/bin/python3"),
        str(runtime_root / "scripts/ops/resource_guarded_run.py"),
        "--job-id",
        "job_drive_case_sync_all_files",
    ]


def test_parse_cron_rejects_pipe():
    with pytest.raises(PermissionError):
        sp.parse_cron_command("python3 a.py | cat")


def test_parse_cron_rejects_dollar_paren():
    with pytest.raises(PermissionError):
        sp.parse_cron_command("python3 a.py $(whoami)")


# --- OCR runtime whitelist (Phase A) ------------------------------------

def test_tesseract_in_whitelist():
    """tesseract must be whitelisted so tesseract_provider can use SafeProcess."""
    assert "tesseract" in sp._ARGV0_WHITELIST


def test_pdftoppm_in_whitelist():
    """pdftoppm must be whitelisted for PDF → image conversion (Phase C)."""
    assert "pdftoppm" in sp._ARGV0_WHITELIST


def test_tesseract_argv_accepted_without_run():
    """_validate_argv should accept tesseract without raising PermissionError."""
    # Does not actually run tesseract — just validates argv construction
    sp._validate_argv(["tesseract", "/tmp/test.png", "stdout", "-l", "chi_tra+eng", "--psm", "3"])


def test_pdftoppm_argv_accepted_without_run():
    """_validate_argv should accept pdftoppm without raising PermissionError."""
    sp._validate_argv(["pdftoppm", "-r", "300", "/tmp/test.pdf", "/tmp/test_out"])
