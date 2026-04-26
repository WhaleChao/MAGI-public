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
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# --- 常數 ---------------------------------------------------------------

_MAX_CONCURRENT = 8
_STDOUT_CAP_BYTES = 1_048_576          # 1 MB
_STDERR_CAP_BYTES = 1_048_576
_SIGTERM_GRACE_SEC = 3.0
_LAUNCHCTL_LABEL_RE = re.compile(r"^com\.magi\.[a-z0-9\-]+$")

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
    if head not in _ARGV0_WHITELIST and argv[0] not in _ARGV0_WHITELIST:
        raise PermissionError(f"argv[0] not whitelisted: {argv[0]!r}")
    # python3 -c <code> 的 code 引數本就含 ; 是合法 Python，shell=False 下無注入風險
    _is_python_code_arg = (
        len(argv) >= 3
        and os.path.basename(argv[0]) == "python3"
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


# --- 主函式 -------------------------------------------------------------

def run(
    argv: Sequence[str],
    timeout_sec: float = 120.0,
    env_whitelist_prefixes: Optional[Sequence[str]] = None,
    cwd: Optional[str] = None,
    env_extra: Optional[dict] = None,
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
        )
        try:
            out_b, err_b = proc.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                out_b, err_b = proc.communicate(timeout=_SIGTERM_GRACE_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()
                killed = True
                out_b, err_b = proc.communicate(timeout=2.0)
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
    if not tokens:
        raise ValueError("shlex.split produced empty argv")
    return tokens


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
