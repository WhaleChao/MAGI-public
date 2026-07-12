# -*- coding: utf-8 -*-
"""
SafeProcess — 以 argv + whitelist 取代 shell=True。

Feature flag：
  MAGI_USE_SAFE_PROCESS=0/1  (預設 0，opt-in)

對外 API（只有這些，不要新增）:
  - run(argv, timeout_sec=120, env_whitelist_prefixes=None, cwd=None) -> SafeRunResult
  - parse_cron_command(cmdline: str) -> List[str]
  - launchctl_op(op: str, label: str) -> SafeRunResult
  - reset_for_test()   # 測試用，清 BoundedSemaphore

不准新增：
  - 非同步版本（asyncio）
  - popen_streaming
  - run_shell（就是要幹掉它）
"""

from __future__ import annotations

import os
import re
import signal
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

# --- 常數 ---------------------------------------------------------------

_MAX_CONCURRENT = 8
_STDOUT_CAP_BYTES = 1_048_576          # 1 MB
_STDERR_CAP_BYTES = 1_048_576
_SIGTERM_GRACE_SEC = 3.0
_SIGKILL_GRACE_SEC = 2.0
_LAUNCHCTL_LABEL_RE = re.compile(r"^com\.magi\.[a-z0-9\-]+$")
_PYTHON_EXECUTABLE_RE = re.compile(r"^python3(?:\.\d+)?$")

# argv[0] 白名單（basename 比對）
_ARGV0_WHITELIST = frozenset({
    "python3",
    "launchctl",
    "git",
    "curl",
    "mount_smbfs",
    "osascript",
    "tesseract",   # OCR runtime (Phase A)
    "pdftoppm",    # PDF → image conversion for OCR (Phase C)
    "/Users/ai/Desktop/MAGI_v2/venv/bin/python3",
})

# 允許帶入 subprocess 的 env 前綴（白名單）
_DEFAULT_ENV_PREFIXES: Tuple[str, ...] = (
    "MAGI_",
    "JUDICIAL_",
    "PATH",
    "HOME",
    "USER",
    "PYTHONPATH",
    "LANG",
    "LC_",
    "TZ",
)

# shell 禁字（即使走 argv 也拒絕這些 token 出現在任一 arg 內）
_SHELL_METACHARS = frozenset({";", "|", "&", "`", "$", "<", ">", "\n"})

# --- dataclass ----------------------------------------------------------

@dataclass
class SafeRunResult:
    returncode: int
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool
    killed: bool


class _SafeProcessCleanupError(RuntimeError):
    """Raised only when a timed-out owned process cannot be reaped safely."""

    safe_process_cleanup_failed = True


# --- 內部鎖（進程內並發上限）-------------------------------------------

_sem = threading.BoundedSemaphore(_MAX_CONCURRENT)
_sem_lock = threading.Lock()    # 保護 reset


def reset_for_test() -> None:
    """測試專用：重建 BoundedSemaphore。禁止在 production 呼叫。"""
    global _sem
    with _sem_lock:
        _sem = threading.BoundedSemaphore(_MAX_CONCURRENT)


# --- 驗證輔助函式 -------------------------------------------------------

def _validate_argv(argv: Sequence[str]) -> None:
    if not argv or not isinstance(argv, (list, tuple)):
        raise ValueError("argv must be a non-empty list/tuple")
    head = os.path.basename(argv[0])
    if (
        head not in _ARGV0_WHITELIST
        and argv[0] not in _ARGV0_WHITELIST
        and not _PYTHON_EXECUTABLE_RE.fullmatch(head)
    ):
        raise PermissionError(f"argv[0] not whitelisted: {argv[0]!r}")
    # python3 -c <code> 的 code 引數本就含 ; 是合法 Python，shell=False 下無注入風險
    _is_python_code_arg = (
        len(argv) >= 3
        and bool(_PYTHON_EXECUTABLE_RE.fullmatch(os.path.basename(argv[0])))
        and argv[1] == "-c"
    )
    for i, a in enumerate(argv):
        if not isinstance(a, str):
            raise TypeError(f"argv[{i}] must be str, got {type(a).__name__}")
        if _is_python_code_arg and i == 2:
            # code arg：只檢查真正危險的 backtick 和 $( )
            for meta in ("`", "$("):
                if meta in a:
                    raise PermissionError(
                        f"argv[{i}] contains shell metachar {meta!r}: {a!r}"
                    )
            continue
        for meta in _SHELL_METACHARS:
            if meta in a:
                raise PermissionError(
                    f"argv[{i}] contains shell metachar {meta!r}: {a!r}"
                )


def _filter_env(prefixes: Optional[Sequence[str]]) -> dict:
    allow = tuple(prefixes) if prefixes else _DEFAULT_ENV_PREFIXES
    out = {}
    for k, v in os.environ.items():
        if any(k == p or k.startswith(p) for p in allow):
            out[k] = v
    return out


def _cap(s: bytes, max_bytes: int) -> str:
    if len(s) <= max_bytes:
        return s.decode("utf-8", errors="replace")
    truncated = s[:max_bytes]
    return truncated.decode("utf-8", errors="replace") + f"\n[...truncated {len(s) - max_bytes} bytes]"


def _owned_process_snapshot(root_pid: int) -> tuple[set[int], dict[int, int]]:
    """Return the runner-owned descendant PIDs and wholly-owned process groups.

    ``resource_guarded_run`` deliberately creates a new session for its child.
    A parent-only ``killpg`` cannot reach that nested group.  We therefore take
    a PPID snapshot while the wrapper is still alive and only signal groups
    whose every current member belongs to this runner's descendant tree.
    """
    try:
        table_proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return {int(root_pid)}, {}

    table: dict[int, tuple[int, int]] = {}
    for raw_line in (table_proc.stdout or "").splitlines():
        parts = raw_line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid, pgid = (int(part) for part in parts)
        except ValueError:
            continue
        if pid > 0 and ppid >= 0 and pgid > 0:
            table[pid] = (ppid, pgid)

    owned = {int(root_pid)}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _pgid) in table.items():
            if ppid in owned and pid not in owned:
                owned.add(pid)
                changed = True

    members_by_group: dict[int, set[int]] = {}
    for pid, (_ppid, pgid) in table.items():
        members_by_group.setdefault(pgid, set()).add(pid)
    groups = {
        pgid
        for pid in owned
        for _ppid, pgid in (table.get(pid, (0, 0)),)
        if pgid and members_by_group.get(pgid, set()) <= owned
    }
    return owned, {pid: table[pid][1] for pid in owned if pid in table and table[pid][1] in groups}


def _signal_owned_processes(
    proc: subprocess.Popen,
    *,
    signal_number: int,
    owned_pids: set[int],
    owned_groups: dict[int, int],
) -> tuple[set[int], dict[int, int]]:
    """Signal only the process groups and PIDs proven to be runner-owned."""
    current_pids, current_groups = _owned_process_snapshot(proc.pid)
    owned_pids.update(current_pids)
    owned_groups.update(current_groups)

    signaled: set[int] = set()
    for pgid in sorted(set(owned_groups.values())):
        try:
            os.killpg(pgid, signal_number)
            signaled.update(pid for pid, group in owned_groups.items() if group == pgid)
        except ProcessLookupError:
            continue
        except Exception:
            # Fall through to the individually-owned PID below.
            continue

    for pid in sorted(owned_pids - signaled):
        try:
            os.kill(pid, signal_number)
        except ProcessLookupError:
            continue
        except Exception:
            continue
    return owned_pids, owned_groups


# --- 主函式 -------------------------------------------------------------

def run(
    argv: Sequence[str],
    timeout_sec: float = 120.0,
    env_whitelist_prefixes: Optional[Sequence[str]] = None,
    cwd: Optional[str] = None,
    env_extra: Optional[dict] = None,
    _on_started: Optional[Callable[[int], None]] = None,
) -> SafeRunResult:
    """以 argv 啟動子進程，禁用 shell=True。超時走 SIGTERM→3s→SIGKILL。"""
    _validate_argv(argv)
    env = _filter_env(env_whitelist_prefixes)
    if env_extra:
        allow = tuple(env_whitelist_prefixes) if env_whitelist_prefixes else _DEFAULT_ENV_PREFIXES
        for k, v in env_extra.items():
            if any(k == p or str(k).startswith(p) for p in allow):
                env[str(k)] = str(v)
    t0 = time.time()
    killed = False
    timed_out = False

    acquired = _sem.acquire(timeout=30.0)
    if not acquired:
        raise RuntimeError("SafeProcess concurrency limit exceeded (>30s wait)")
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            shell=False,               # 絕不 shell=True
            close_fds=True,
            start_new_session=True,
        )
        owned_pids = {proc.pid}
        owned_groups: dict[int, int] = {}
        if _on_started is not None:
            try:
                _on_started(proc.pid)
            except Exception as exc:
                owned_pids, owned_groups = _signal_owned_processes(
                    proc,
                    signal_number=signal.SIGKILL,
                    owned_pids=owned_pids,
                    owned_groups=owned_groups,
                )
                try:
                    proc.communicate(timeout=_SIGKILL_GRACE_SEC)
                except subprocess.TimeoutExpired as reap_exc:
                    raise _SafeProcessCleanupError(
                        f"process start callback failed and child could not be reaped: pid={proc.pid} "
                        f"owned_pids={sorted(owned_pids)}"
                    ) from reap_exc
                raise _SafeProcessCleanupError(
                    f"process start callback failed; child was reaped: pid={proc.pid}"
                ) from exc
        try:
            out_b, err_b = proc.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            owned_pids, owned_groups = _signal_owned_processes(
                proc,
                signal_number=signal.SIGTERM,
                owned_pids=owned_pids,
                owned_groups=owned_groups,
            )
            try:
                out_b, err_b = proc.communicate(timeout=_SIGTERM_GRACE_SEC)
            except subprocess.TimeoutExpired:
                killed = True
                owned_pids, owned_groups = _signal_owned_processes(
                    proc,
                    signal_number=signal.SIGKILL,
                    owned_pids=owned_pids,
                    owned_groups=owned_groups,
                )
                try:
                    out_b, err_b = proc.communicate(timeout=_SIGKILL_GRACE_SEC)
                except subprocess.TimeoutExpired:
                    # A just-killed child may still be in the kernel's exit
                    # path. Re-signal only the same verified ownership set,
                    # then make one final bounded reap attempt.
                    owned_pids, owned_groups = _signal_owned_processes(
                        proc,
                        signal_number=signal.SIGKILL,
                        owned_pids=owned_pids,
                        owned_groups=owned_groups,
                    )
                    try:
                        out_b, err_b = proc.communicate(timeout=_SIGKILL_GRACE_SEC)
                    except subprocess.TimeoutExpired as exc:
                        raise _SafeProcessCleanupError(
                            f"timed-out process group could not be reaped: pid={proc.pid} "
                            f"owned_pids={sorted(owned_pids)}"
                        ) from exc
        rc = proc.returncode if proc.returncode is not None else -1
        return SafeRunResult(
            returncode=rc,
            stdout=_cap(out_b or b"", _STDOUT_CAP_BYTES),
            stderr=_cap(err_b or b"", _STDERR_CAP_BYTES),
            duration_sec=time.time() - t0,
            timed_out=timed_out,
            killed=killed,
        )
    finally:
        _sem.release()


# --- cron 指令解析 ------------------------------------------------------

def parse_cron_command(cmdline: str) -> List[str]:
    """把 cron 的 command 字串（可能含空白）切成 argv。禁止 |, &, ;, `, $。"""
    if not isinstance(cmdline, str):
        raise TypeError("cmdline must be str")
    cmdline = cmdline.strip()
    if not cmdline:
        raise ValueError("empty cmdline")
    for meta in (";", "|", "&", "`", "$(", ">", "<"):
        if meta in cmdline:
            raise PermissionError(f"cron cmdline contains shell metachar {meta!r}")
    tokens = shlex.split(cmdline, posix=True)
    tokens = _repair_known_unquoted_space_paths(tokens)
    if not tokens:
        raise ValueError("shlex.split produced empty argv")
    return tokens


def _repair_known_unquoted_space_paths(tokens: List[str]) -> List[str]:
    """Repair legacy cron commands that forgot to quote MAGI's runtime path.

    The installed runtime lives under "Application Support".  Older cron rows
    stored commands without quotes, so shlex splits the path into
    "/Users/ai/Library/Application" and "Support/...".  Joining only this
    exact known prefix keeps shell metachar protection intact while allowing
    the audit layer to validate the real argv.
    """
    repaired: List[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if (
            token == "/Users/ai/Library/Application"
            and i + 1 < len(tokens)
            and tokens[i + 1].startswith("Support/")
        ):
            repaired.append(f"{token} {tokens[i + 1]}")
            i += 2
            continue
        repaired.append(token)
        i += 1
    return repaired


# --- launchctl 操作 -----------------------------------------------------

def launchctl_op(op: str, label: str) -> SafeRunResult:
    """op in {'bootout','bootstrap','kickstart','print','list'}；label 必須符合 ^com\\.magi\\.[a-z0-9\\-]+$。"""
    if op not in {"bootout", "bootstrap", "kickstart", "print", "list"}:
        raise PermissionError(f"launchctl op not allowed: {op!r}")
    if not _LAUNCHCTL_LABEL_RE.match(label):
        raise PermissionError(f"launchctl label invalid: {label!r}")
    uid = os.getuid()
    target = f"gui/{uid}/{label}"
    if op == "bootstrap":
        plist = f"/Users/{os.environ.get('USER','ai')}/Library/LaunchAgents/{label}.plist"
        argv = ["launchctl", "bootstrap", f"gui/{uid}", plist]
    elif op == "kickstart":
        argv = ["launchctl", "kickstart", "-kp", target]
    elif op == "list":
        argv = ["launchctl", "list"]
    else:
        argv = ["launchctl", op, target]
    return run(argv, timeout_sec=30.0)
