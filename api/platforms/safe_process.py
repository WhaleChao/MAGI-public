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
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

# --- 常數 ---------------------------------------------------------------

_MAX_CONCURRENT = 8
_STDOUT_CAP_BYTES = 1_048_576          # 1 MB
_STDERR_CAP_BYTES = 1_048_576
_SIGTERM_GRACE_SEC = 3.0
# macOS may keep a SIGKILLed Python process in the kernel's exiting state for
# several seconds while its inherited stdout/stderr pipe is still open.  Two
# 2-second drains were too short for real OCR/indexing trees and converted a
# correctly-contained timeout into a misleading cleanup failure.  Keep the
# cleanup fail-closed, but give each of the two verified post-SIGKILL drains a
# realistic bound.
_SIGKILL_GRACE_SEC = 8.0
_LAUNCHCTL_LABEL_RE = re.compile(r"^com\.magi\.[a-z0-9\-]+$")
_PYTHON_EXECUTABLE_RE = re.compile(
    r"^(?:python3(?:\.\d+)?|python(?:3(?:\.\d+)?)?\.exe)$",
    re.IGNORECASE,
)

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
    "open",        # Existing local cron action: reveal a prepared folder in Finder
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

# These variables can redirect Python's prefix, venv discovery, import roots,
# or post-script control flow.  An accepted macOS venv alias gets a trusted
# PYTHONEXECUTABLE below; all competing caller/ambient values stay excluded.
_PYTHON_VENV_ENV_DENYLIST = frozenset({
    "PYTHONEXECUTABLE",
    "PYTHONHOME",
    "PYTHONINSPECT",
    "PYTHONPLATLIBDIR",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "__PYVENV_LAUNCHER__",
})

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


class _SafeProcessCancelledError(RuntimeError):
    """The verified owner requested a controlled child-tree shutdown."""

    safe_process_cancelled = True


# --- 內部鎖（進程內並發上限）-------------------------------------------

_sem = threading.BoundedSemaphore(_MAX_CONCURRENT)
_sem_lock = threading.Lock()    # 保護 reset


def reset_for_test() -> None:
    """測試專用：重建 BoundedSemaphore。禁止在 production 呼叫。"""
    global _sem
    with _sem_lock:
        _sem = threading.BoundedSemaphore(_MAX_CONCURRENT)


# --- 驗證輔助函式 -------------------------------------------------------

def _current_python_alias_target(executable: str) -> Optional[Tuple[str, str]]:
    """Return ``(canonical binary, trusted venv alias)`` when accepted.

    V3's sealed runtime may expose its venv interpreter as ``bin/python``.
    The basename alone is never sufficient: a relative/PATH lookup, missing
    file, another venv/runtime, or a non-macOS host remains denied.  The alias
    must be the exact executable of the current venv so macOS can use it as a
    trusted ``PYTHONEXECUTABLE`` without letting a caller choose that value.
    """

    if (
        sys.platform != "darwin"
        or os.path.basename(executable) != "python"
        or not os.path.isabs(executable)
        or not sys.executable
        or not os.path.isabs(sys.executable)
        or not getattr(sys, "_base_executable", "")
        or not os.path.isabs(sys._base_executable)
        or sys.prefix == sys.base_prefix
    ):
        return None
    try:
        alias = Path(os.path.abspath(executable))
        current_alias = Path(os.path.abspath(sys.executable))
        current_prefix = Path(os.path.abspath(sys.prefix))
        candidate = Path(executable).resolve(strict=True)
        # Unlike sys.executable, _base_executable is not redirected by
        # macOS PYTHONEXECUTABLE and remains bound to the binary that started
        # this process.  A later alias exchange therefore cannot become the
        # trusted target of a nested SafeProcess call.
        current = Path(sys._base_executable).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if (
        alias != current_alias
        or alias.parent.parent != current_prefix
        or not (current_prefix / "pyvenv.cfg").is_file()
        or candidate != current
        or not candidate.is_file()
    ):
        return None
    return str(current), str(alias)


def _is_current_python_alias(executable: str) -> bool:
    """Whether *executable* is an absolute alias for this interpreter."""

    return _current_python_alias_target(executable) is not None


def _validate_argv(argv: Sequence[str]) -> Optional[Tuple[str, str]]:
    """Validate argv and return the captured binding for an accepted alias.

    Returning the target captured during validation lets :func:`run` avoid
    executing the original symlink after it has been checked.
    """

    if not argv or not isinstance(argv, (list, tuple)):
        raise ValueError("argv must be a non-empty list/tuple")
    for i, argument in enumerate(argv):
        if not isinstance(argument, str):
            raise TypeError(f"argv[{i}] must be str, got {type(argument).__name__}")
    # A Windows command may be validated by a macOS/Linux release builder.
    # Normalise separators before taking the basename so the same allowlist is
    # enforced during cross-platform packaging and on the destination host.
    head = os.path.basename(argv[0].replace("\\", "/"))
    current_python_alias_binding = _current_python_alias_target(argv[0])
    if (
        head not in _ARGV0_WHITELIST
        and argv[0] not in _ARGV0_WHITELIST
        and not _PYTHON_EXECUTABLE_RE.fullmatch(head)
        and current_python_alias_binding is None
    ):
        raise PermissionError(f"argv[0] not whitelisted: {argv[0]!r}")
    # python3 -c <code> 的 code 引數本就含 ; 是合法 Python，shell=False 下無注入風險
    _is_python_code_arg = (
        len(argv) >= 3
        and bool(_PYTHON_EXECUTABLE_RE.fullmatch(os.path.basename(argv[0].replace("\\", "/"))))
        and argv[1] == "-c"
    )
    for i, a in enumerate(argv):
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
    return current_python_alias_binding


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


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    start_sec: int
    start_usec: int
    uid: int = -1
    start_abstime: int = 0


@dataclass(frozen=True)
class _ProcessObservation:
    identity: _ProcessIdentity
    ppid: int
    pgid: int


@dataclass(frozen=True)
class _PipeEndpoint:
    fd: int
    handle: int
    peer_handle: int

    @property
    def identity(self) -> tuple[int, int]:
        return tuple(sorted((self.handle, self.peer_handle)))


_darwin_libproc_handle = None
_darwin_libproc_types = None
_darwin_libproc_lock = threading.Lock()


def _darwin_libproc():
    """Return one configured libproc handle and proc_bsdinfo type."""

    global _darwin_libproc_handle, _darwin_libproc_types
    if _darwin_libproc_handle is not None:
        return _darwin_libproc_handle, _darwin_libproc_types
    with _darwin_libproc_lock:
        if _darwin_libproc_handle is not None:
            return _darwin_libproc_handle, _darwin_libproc_types
        import ctypes

        class _ProcBSDInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        class _ProcFDInfo(ctypes.Structure):
            _fields_ = [
                ("proc_fd", ctypes.c_int32),
                ("proc_fdtype", ctypes.c_uint32),
            ]

        class _ProcFileInfo(ctypes.Structure):
            _fields_ = [
                ("fi_openflags", ctypes.c_uint32),
                ("fi_status", ctypes.c_uint32),
                ("fi_offset", ctypes.c_int64),
                ("fi_type", ctypes.c_int32),
                ("fi_guardflags", ctypes.c_uint32),
            ]

        class _VInfoStat(ctypes.Structure):
            _fields_ = [
                ("vst_dev", ctypes.c_uint32),
                ("vst_mode", ctypes.c_uint16),
                ("vst_nlink", ctypes.c_uint16),
                ("vst_ino", ctypes.c_uint64),
                ("vst_uid", ctypes.c_uint32),
                ("vst_gid", ctypes.c_uint32),
                ("vst_atime", ctypes.c_int64),
                ("vst_atimensec", ctypes.c_int64),
                ("vst_mtime", ctypes.c_int64),
                ("vst_mtimensec", ctypes.c_int64),
                ("vst_ctime", ctypes.c_int64),
                ("vst_ctimensec", ctypes.c_int64),
                ("vst_birthtime", ctypes.c_int64),
                ("vst_birthtimensec", ctypes.c_int64),
                ("vst_size", ctypes.c_int64),
                ("vst_blocks", ctypes.c_int64),
                ("vst_blksize", ctypes.c_int32),
                ("vst_flags", ctypes.c_uint32),
                ("vst_gen", ctypes.c_uint32),
                ("vst_rdev", ctypes.c_uint32),
                ("vst_qspare", ctypes.c_int64 * 2),
            ]

        class _PipeInfo(ctypes.Structure):
            _fields_ = [
                ("pipe_stat", _VInfoStat),
                ("pipe_handle", ctypes.c_uint64),
                ("pipe_peerhandle", ctypes.c_uint64),
                ("pipe_status", ctypes.c_int32),
                ("rfu_1", ctypes.c_int32),
            ]

        class _PipeFDInfo(ctypes.Structure):
            _fields_ = [("pfi", _ProcFileInfo), ("pipeinfo", _PipeInfo)]

        class _RUsageInfoV4(ctypes.Structure):
            _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
                (name, ctypes.c_uint64)
                for name in (
                    "user", "system", "idle", "interrupt", "pageins",
                    "wired", "resident", "phys", "start", "exit",
                    "child_user", "child_system", "child_idle",
                    "child_interrupt", "child_pageins", "child_elapsed",
                    "disk_read", "disk_write", "qos_default",
                    "qos_maintenance", "qos_background", "qos_utility",
                    "qos_legacy", "qos_user_init", "qos_user_inter",
                    "billed", "serviced", "logical", "lifetime",
                    "instructions", "cycles", "billed_energy",
                    "serviced_energy", "interval", "runnable",
                )
            ]

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        libproc.proc_listallpids.restype = ctypes.c_int
        libproc.proc_listchildpids.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_listchildpids.restype = ctypes.c_int
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_pidinfo.restype = ctypes.c_int
        libproc.proc_pidfdinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_pidfdinfo.restype = ctypes.c_int
        libproc.proc_pid_rusage.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        libproc.proc_pid_rusage.restype = ctypes.c_int
        _darwin_libproc_handle = libproc
        _darwin_libproc_types = (
            _ProcBSDInfo,
            _ProcFDInfo,
            _PipeFDInfo,
            _RUsageInfoV4,
        )
        return libproc, _darwin_libproc_types


def _darwin_process_observation(pid: int) -> Optional[_ProcessObservation]:
    if sys.platform != "darwin" or int(pid) <= 0:
        return None
    try:
        import ctypes

        libproc, types = _darwin_libproc()
        info_type, _fd_type, _pipe_type, rusage_type = types
        info = info_type()
        received = int(
            libproc.proc_pidinfo(
                int(pid),
                3,  # PROC_PIDTBSDINFO
                0,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        )
        if received != ctypes.sizeof(info) or int(info.pbi_pid) != int(pid):
            return None
        ppid = int(info.pbi_ppid)
        pgid = int(info.pbi_pgid)
        if ppid < 0 or pgid <= 0:
            return None
        usage = rusage_type()
        start_abstime = 0
        if int(
            libproc.proc_pid_rusage(
                int(pid), 4, ctypes.byref(usage)  # RUSAGE_INFO_V4
            )
        ) == 0:
            start_abstime = int(usage.start)
        return _ProcessObservation(
            identity=_ProcessIdentity(
                pid=int(pid),
                start_sec=int(info.pbi_start_tvsec),
                start_usec=int(info.pbi_start_tvusec),
                uid=int(info.pbi_uid),
                start_abstime=start_abstime,
            ),
            ppid=ppid,
            pgid=pgid,
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _darwin_child_pids(parent_pid: int) -> tuple[int, ...]:
    if sys.platform != "darwin" or int(parent_pid) <= 0:
        return ()
    try:
        import ctypes

        libproc, _types = _darwin_libproc()
        estimated = int(libproc.proc_listchildpids(int(parent_pid), None, 0))
        if estimated <= 0:
            return ()
        capacity = estimated + 16
        pids = (ctypes.c_int * capacity)()
        count = int(
            libproc.proc_listchildpids(
                int(parent_pid), pids, ctypes.sizeof(pids)
            )
        )
        if count <= 0:
            return ()
        return tuple(
            int(pid)
            for pid in pids[: min(count, capacity)]
            if int(pid) > 0
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return ()


def _darwin_pipe_info(pid: int, fd: int) -> Optional[tuple[int, int]]:
    """Return the oriented libproc pipe handles for one file descriptor."""

    if sys.platform != "darwin" or int(pid) <= 0 or int(fd) < 0:
        return None
    try:
        import ctypes

        libproc, types = _darwin_libproc()
        _bsd_type, _fd_type, pipe_type, _rusage_type = types
        info = pipe_type()
        received = int(
            libproc.proc_pidfdinfo(
                int(pid),
                int(fd),
                6,  # PROC_PIDFDPIPEINFO
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        )
        if received != ctypes.sizeof(info):
            return None
        handle = int(info.pipeinfo.pipe_handle)
        peer_handle = int(info.pipeinfo.pipe_peerhandle)
        if handle <= 0:
            return None
        return handle, peer_handle
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _darwin_process_pipe_identities(
    pid: int,
) -> Optional[frozenset[tuple[int, int]]]:
    """List private pipe identities held by *pid*, or ``None`` if unprovable."""

    if sys.platform != "darwin" or int(pid) <= 0:
        return None
    try:
        import ctypes

        libproc, types = _darwin_libproc()
        _bsd_type, fd_type, _pipe_type, _rusage_type = types
        proc_pidlistfds = 1
        prox_fdtype_pipe = 6
        estimated_bytes = int(
            libproc.proc_pidinfo(int(pid), proc_pidlistfds, 0, None, 0)
        )
        if estimated_bytes <= 0:
            return None
        capacity = max(16, estimated_bytes // ctypes.sizeof(fd_type) + 16)
        fds = (fd_type * capacity)()
        received_bytes = int(
            libproc.proc_pidinfo(
                int(pid),
                proc_pidlistfds,
                0,
                fds,
                ctypes.sizeof(fds),
            )
        )
        if received_bytes < 0 or received_bytes % ctypes.sizeof(fd_type):
            return None
        identities: set[tuple[int, int]] = set()
        count = min(received_bytes // ctypes.sizeof(fd_type), capacity)
        for row in fds[:count]:
            if int(row.proc_fdtype) != prox_fdtype_pipe:
                continue
            handles = _darwin_pipe_info(int(pid), int(row.proc_fd))
            if handles is None or handles[1] <= 0:
                continue
            identities.add(tuple(sorted(handles)))
        return frozenset(identities)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _darwin_parent_pipe_endpoints(proc: subprocess.Popen) -> tuple[_PipeEndpoint, ...]:
    """Capture the parent-side stdout/stderr pipe endpoints immediately."""

    if sys.platform != "darwin":
        return ()
    endpoints: list[_PipeEndpoint] = []
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            fd = int(stream.fileno())
        except (AttributeError, OSError, TypeError, ValueError):
            return ()
        handles = _darwin_pipe_info(os.getpid(), fd)
        if handles is None or handles[1] <= 0:
            return ()
        endpoints.append(
            _PipeEndpoint(fd=fd, handle=handles[0], peer_handle=handles[1])
        )
    return tuple(endpoints)


def _darwin_live_pipe_identities(
    endpoints: Sequence[_PipeEndpoint],
) -> frozenset[tuple[int, int]]:
    """Revalidate which captured child-facing pipe peers remain open."""

    live: set[tuple[int, int]] = set()
    for endpoint in endpoints:
        handles = _darwin_pipe_info(os.getpid(), endpoint.fd)
        if handles is None or handles[0] != endpoint.handle:
            raise _SafeProcessCleanupError(
                "SafeProcess stdout/stderr pipe identity could not be revalidated"
            )
        if handles[1] == endpoint.peer_handle:
            live.add(endpoint.identity)
        elif handles[1] != 0:
            raise _SafeProcessCleanupError(
                "SafeProcess stdout/stderr pipe peer identity changed"
            )
    return frozenset(live)


def _darwin_process_table() -> Optional[dict[int, _ProcessObservation]]:
    """Read process identity, parent, and group with macOS libproc.

    ``ps`` is a setuid executable on macOS and cannot be exec'd by the
    write/network Seatbelt used by V3 release certification.  libproc exposes
    the same kernel process metadata in-process, so timeout containment keeps
    working without weakening that sandbox or depending on shell commands.
    """

    if sys.platform != "darwin":
        return None
    try:
        import ctypes

        libproc, _types = _darwin_libproc()
        estimated = int(libproc.proc_listallpids(None, 0))
        if estimated <= 0:
            return None
        # Processes can appear between the sizing and filling calls.  Leave a
        # bounded margin so a short-lived descendant is not omitted solely by
        # that race.
        capacity = estimated + 128
        pids = (ctypes.c_int * capacity)()
        count = int(libproc.proc_listallpids(pids, ctypes.sizeof(pids)))
        if count <= 0:
            return None

        table: dict[int, _ProcessObservation] = {}
        for raw_pid in pids[: min(count, capacity)]:
            pid = int(raw_pid)
            observation = _darwin_process_observation(pid)
            if observation is not None:
                table[pid] = observation
        return table
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _ps_process_table() -> dict[int, _ProcessObservation]:
    """Portable fallback used where an in-process process API is unavailable."""

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
        return {}

    table: dict[int, _ProcessObservation] = {}
    for raw_line in (table_proc.stdout or "").splitlines():
        parts = raw_line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid, pgid = (int(part) for part in parts)
        except ValueError:
            continue
        if pid > 0 and ppid >= 0 and pgid > 0:
            table[pid] = _ProcessObservation(
                identity=_ProcessIdentity(pid=pid, start_sec=0, start_usec=0),
                ppid=ppid,
                pgid=pgid,
            )
    return table


class _OwnedProcessTracker:
    """Continuously retain immutable identities for observed descendants."""

    def __init__(
        self,
        root_pid: int,
        *,
        pipe_endpoints: Sequence[_PipeEndpoint] = (),
        launched_at: Optional[float] = None,
        poll_seconds: float = 0.005,
    ):
        self.root_pid = int(root_pid)
        self.pipe_endpoints = tuple(pipe_endpoints)
        self.launched_at = float(time.time() if launched_at is None else launched_at)
        self.owner_uid = os.getuid()
        self.poll_seconds = float(poll_seconds)
        self._owned: dict[int, _ProcessIdentity] = {}
        self._current: dict[int, _ProcessObservation] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.observe()
        self._thread = threading.Thread(
            target=self._monitor,
            name=f"safe-process-tracker-{self.root_pid}",
            daemon=True,
        )
        self._thread.start()

    def _monitor(self) -> None:
        while not self._stop.is_set():
            self.observe()
            self._stop.wait(self.poll_seconds)

    def observe(self) -> dict[int, _ProcessObservation]:
        if sys.platform == "darwin":
            with self._lock:
                known = dict(self._owned)
            current: dict[int, _ProcessObservation] = {}
            for pid, identity in known.items():
                observation = _darwin_process_observation(pid)
                if observation is not None and observation.identity == identity:
                    current[pid] = observation
            if self.root_pid not in known:
                root = _darwin_process_observation(self.root_pid)
                if root is not None:
                    known[self.root_pid] = root.identity
                    current[self.root_pid] = root
            queue = list(current)
            visited = set(queue)
            while queue:
                parent_pid = queue.pop()
                for child_pid in _darwin_child_pids(parent_pid):
                    child = _darwin_process_observation(child_pid)
                    if child is None or child.ppid != parent_pid:
                        continue
                    known[child_pid] = child.identity
                    current[child_pid] = child
                    if child_pid not in visited:
                        visited.add(child_pid)
                        queue.append(child_pid)
        else:
            table = _ps_process_table()
            with self._lock:
                known = dict(self._owned)
            owned = {
                pid
                for pid, identity in known.items()
                if pid in table and table[pid].identity == identity
            }
            if self.root_pid not in known and self.root_pid in table:
                known[self.root_pid] = table[self.root_pid].identity
                owned.add(self.root_pid)
            changed = True
            while changed:
                changed = False
                for pid, observation in table.items():
                    if observation.ppid in owned and pid not in owned:
                        known[pid] = observation.identity
                        owned.add(pid)
                        changed = True
            current = {pid: table[pid] for pid in owned}
        with self._lock:
            self._owned.update(known)
            self._current = current
            return dict(current)

    def current(self) -> dict[int, _ProcessObservation]:
        return self.observe()

    def recover_pipe_holders(self) -> dict[int, _ProcessObservation]:
        """Adopt reparented holders of this run's still-open private pipes.

        This is intentionally an exceptional all-PID scan.  Normal tracking
        uses only ``proc_listchildpids``; global scans are reserved for the
        bounded timeout-quiesce path.  A few millisecond retries cover the
        kernel interval where a signalled process is exiting but its pipe peer
        has not yet disappeared from the parent endpoint.
        """

        if sys.platform != "darwin":
            return self.current()
        if len(self.pipe_endpoints) != 2:
            raise _SafeProcessCleanupError(
                "timed-out orphan containment lacks verified stdout/stderr pipes"
            )
        not_before = (max(0, int(self.launched_at) - 1), 0)
        last_inaccessible: list[int] = []
        last_missing: set[tuple[int, int]] = set()
        for attempt in range(20):
            live_pipes = _darwin_live_pipe_identities(self.pipe_endpoints)
            if not live_pipes:
                return self.current()
            table = _darwin_process_table()
            if table is None:
                raise _SafeProcessCleanupError(
                    "timed-out orphan containment cannot enumerate Darwin processes"
                )

            recovered: dict[int, _ProcessObservation] = {}
            matched_pipes: set[tuple[int, int]] = set()
            inaccessible: list[int] = []
            for pid, observation in table.items():
                identity = observation.identity
                if (
                    pid == os.getpid()
                    or identity.uid != self.owner_uid
                    or (identity.start_sec, identity.start_usec) < not_before
                ):
                    continue
                pipe_identities = _darwin_process_pipe_identities(pid)
                if pipe_identities is None:
                    refreshed = _darwin_process_observation(pid)
                    if (
                        refreshed is not None
                        and refreshed.identity == observation.identity
                    ):
                        inaccessible.append(pid)
                    continue
                matches = set(pipe_identities) & set(live_pipes)
                if not matches:
                    continue
                refreshed = _darwin_process_observation(pid)
                if (
                    refreshed is None
                    or refreshed.identity != observation.identity
                    or refreshed.pgid != observation.pgid
                    or identity.start_abstime <= 0
                    or identity.uid != self.owner_uid
                ):
                    raise _SafeProcessCleanupError(
                        f"pipe-holder identity could not be proven: pid={pid}"
                    )
                recovered[pid] = observation
                matched_pipes.update(matches)

            missing = set(live_pipes) - matched_pipes
            if not inaccessible and not missing:
                with self._lock:
                    for pid, observation in recovered.items():
                        self._owned[pid] = observation.identity
                        self._current[pid] = observation
                return self.current()
            last_inaccessible = inaccessible
            last_missing = missing
            if attempt < 19:
                time.sleep(0.002)

        if last_inaccessible:
            raise _SafeProcessCleanupError(
                "timed-out orphan pipe-holder scan was incomplete: "
                f"pids={sorted(last_inaccessible)}"
            )
        raise _SafeProcessCleanupError(
            "live SafeProcess pipe peer has no identity-verified holder: "
            f"pipes={sorted(last_missing)}"
        )

    def owned_pids(self) -> set[int]:
        with self._lock:
            return set(self._owned)

    def current_pids(self) -> set[int]:
        """Return only identities that are still alive and still match."""

        return set(self.current())

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(0.1, self.poll_seconds * 4))


def _signal_owned_processes(
    proc: subprocess.Popen,
    *,
    signal_number: int,
    tracker: _OwnedProcessTracker,
    recover_orphans: bool = False,
) -> set[int]:
    """Signal only identities that still match the tracked start time."""
    if recover_orphans and sys.platform == "darwin":
        tracker.recover_pipe_holders()
    current = tracker.current()
    table = _darwin_process_table()
    if table is None and sys.platform == "darwin":
        raise _SafeProcessCleanupError(
            "owned process identities cannot be revalidated with Darwin libproc"
        )
    if table is None:
        table = _ps_process_table()
    if sys.platform == "darwin":
        unverifiable = sorted(
            pid
            for pid, observation in current.items()
            if observation.identity.uid != tracker.owner_uid
            or observation.identity.start_abstime <= 0
        )
        if unverifiable:
            raise _SafeProcessCleanupError(
                f"owned process start identity is unprovable: pids={unverifiable}"
            )
    verified = {
        pid: table[pid]
        for pid, observation in current.items()
        if pid in table
        and table[pid].identity == observation.identity
        and table[pid].pgid == observation.pgid
    }
    members_by_group: dict[int, set[int]] = {}
    for pid, observation in table.items():
        members_by_group.setdefault(observation.pgid, set()).add(pid)
    groups = {
        observation.pgid
        for observation in verified.values()
        if members_by_group.get(observation.pgid)
        and members_by_group[observation.pgid] <= set(verified)
    }

    signaled: set[int] = set()
    for pgid in sorted(groups):
        expected = {
            pid: observation
            for pid, observation in verified.items()
            if observation.pgid == pgid
        }
        refreshed = _darwin_process_table()
        if refreshed is None and sys.platform == "darwin":
            raise _SafeProcessCleanupError(
                "process group identity cannot be revalidated with Darwin libproc"
            )
        if refreshed is None:
            refreshed = _ps_process_table()
        refreshed_members = {
            pid for pid, observation in refreshed.items() if observation.pgid == pgid
        }
        if refreshed_members != set(expected) or any(
            pid not in refreshed
            or refreshed[pid].identity != observation.identity
            or refreshed[pid].pgid != observation.pgid
            for pid, observation in expected.items()
        ):
            continue
        try:
            os.killpg(pgid, signal_number)
            signaled.update(expected)
        except ProcessLookupError:
            continue
        except Exception:
            # Fall through to the individually-owned PID below.
            continue

    for pid, observation in sorted(verified.items()):
        if pid in signaled:
            continue
        refreshed = (
            _darwin_process_observation(pid)
            if sys.platform == "darwin"
            else _ps_process_table().get(pid)
        )
        if (
            refreshed is None
            or refreshed.identity != observation.identity
            or refreshed.pgid != observation.pgid
        ):
            continue
        try:
            os.kill(pid, signal_number)
        except ProcessLookupError:
            continue
        except Exception:
            continue
    return signaled


def _quiesce_owned_processes(
    proc: subprocess.Popen,
    *,
    tracker: _OwnedProcessTracker,
) -> None:
    """Freeze and converge the complete verified timeout process tree.

    Recovering before a signal alone leaves a fork race: a runnable holder can
    create another detached holder after the scan.  SIGSTOP first freezes the
    currently proven set; repeated pipe-holder recovery then freezes any child
    that was created just before its parent stopped.  Two identical ownership
    snapshots prove convergence.  Failure to converge is a cleanup failure,
    never permission to signal an unverified process.
    """

    previous: Optional[frozenset[int]] = None
    for _attempt in range(8):
        _signal_owned_processes(
            proc,
            signal_number=signal.SIGSTOP,
            tracker=tracker,
            recover_orphans=True,
        )
        current = frozenset(tracker.current())
        if previous == current:
            return
        previous = current
    raise _SafeProcessCleanupError(
        "timed-out process ownership did not converge while quiescing"
    )


def _terminate_owned_processes(
    proc: subprocess.Popen[bytes],
    *,
    tracker: _OwnedProcessTracker,
) -> tuple[bytes, bytes, bool, bool]:
    """Terminate and reap only the process tree proven to belong to *proc*."""

    _quiesce_owned_processes(proc, tracker=tracker)
    term_signaled = _signal_owned_processes(
        proc,
        signal_number=signal.SIGTERM,
        tracker=tracker,
        recover_orphans=False,
    )
    root_signaled = proc.pid in term_signaled
    _signal_owned_processes(
        proc,
        signal_number=signal.SIGCONT,
        tracker=tracker,
        recover_orphans=False,
    )
    try:
        out_b, err_b = proc.communicate(timeout=_SIGTERM_GRACE_SEC)
        return out_b or b"", err_b or b"", False, root_signaled
    except subprocess.TimeoutExpired:
        _quiesce_owned_processes(proc, tracker=tracker)
        kill_signaled = _signal_owned_processes(
            proc,
            signal_number=signal.SIGKILL,
            tracker=tracker,
            recover_orphans=False,
        )
        root_signaled = root_signaled or proc.pid in kill_signaled
        try:
            out_b, err_b = proc.communicate(timeout=_SIGKILL_GRACE_SEC)
        except subprocess.TimeoutExpired:
            retry_signaled = _signal_owned_processes(
                proc,
                signal_number=signal.SIGKILL,
                tracker=tracker,
                recover_orphans=False,
            )
            root_signaled = root_signaled or proc.pid in retry_signaled
            try:
                out_b, err_b = proc.communicate(timeout=_SIGKILL_GRACE_SEC)
            except subprocess.TimeoutExpired as exc:
                raise _SafeProcessCleanupError(
                    f"owned process group could not be reaped: pid={proc.pid} "
                    f"live_owned_pids={sorted(tracker.current_pids())}"
                ) from exc
        return out_b or b"", err_b or b"", True, root_signaled


def _communicate_owned_process(
    proc: subprocess.Popen[bytes],
    *,
    argv: Sequence[str],
    timeout_sec: float,
    tracker: _OwnedProcessTracker,
    cancel_event: Optional[threading.Event],
) -> tuple[bytes, bytes, bool]:
    """Drain one owned process without waiting on leaked descendant pipes.

    ``Popen.communicate`` does not finish when the direct child has exited but
    a detached descendant still holds the inherited stdout/stderr descriptors.
    Browser drivers can legitimately reach exactly that state after their
    Python launcher has already returned success.  Poll in bounded slices so
    the direct child's terminal state is observed; then contain only the
    identity-verified descendants that still own this invocation's pipes.

    The third return value records whether containment required SIGKILL.  A
    normally completed direct child is not mislabeled as timed out merely
    because its disposable driver needed cleanup.
    """

    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _terminate_owned_processes(proc, tracker=tracker)
            raise _SafeProcessCancelledError(
                f"owned process cancelled during controlled shutdown: pid={proc.pid}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            poll = getattr(proc, "poll", None)
            if callable(poll) and poll() is not None:
                out_b, err_b, killed, _root_signaled = _terminate_owned_processes(
                    proc, tracker=tracker
                )
                return out_b, err_b, killed
            raise subprocess.TimeoutExpired(argv, timeout_sec)
        try:
            out_b, err_b = proc.communicate(timeout=min(0.25, remaining))
            return out_b or b"", err_b or b"", False
        except subprocess.TimeoutExpired:
            poll = getattr(proc, "poll", None)
            if callable(poll) and poll() is not None:
                out_b, err_b, killed, _root_signaled = _terminate_owned_processes(
                    proc, tracker=tracker
                )
                return out_b, err_b, killed
            continue


# --- 主函式 -------------------------------------------------------------

def run(
    argv: Sequence[str],
    timeout_sec: float = 120.0,
    env_whitelist_prefixes: Optional[Sequence[str]] = None,
    cwd: Optional[str] = None,
    env_extra: Optional[dict] = None,
    _on_started: Optional[Callable[[int], None]] = None,
    _cancel_event: Optional[threading.Event] = None,
) -> SafeRunResult:
    """以 argv 啟動子進程，禁用 shell=True。超時走 SIGTERM→3s→SIGKILL。"""
    current_python_alias_binding = _validate_argv(argv)
    normalized_argv = list(argv)
    if current_python_alias_binding is not None:
        # Never pass the checked symlink to Popen: it could be exchanged after
        # validation.  Execute the canonical interpreter captured above.
        normalized_argv[0] = current_python_alias_binding[0]
    env = _filter_env(env_whitelist_prefixes)
    if env_extra:
        allow = tuple(env_whitelist_prefixes) if env_whitelist_prefixes else _DEFAULT_ENV_PREFIXES
        for k, v in env_extra.items():
            if any(k == p or str(k).startswith(p) for p in allow):
                env[str(k)] = str(v)
    if current_python_alias_binding is not None:
        # macOS uses this lexical venv path to locate pyvenv.cfg even though
        # Popen executes the race-free canonical binary.  Apply it last so a
        # custom environment whitelist or env_extra cannot replace it.
        for key in _PYTHON_VENV_ENV_DENYLIST:
            env.pop(key, None)
        env["PYTHONEXECUTABLE"] = current_python_alias_binding[1]
    t0 = time.time()
    killed = False
    timed_out = False

    acquired = _sem.acquire(timeout=30.0)
    if not acquired:
        raise RuntimeError("SafeProcess concurrency limit exceeded (>30s wait)")
    tracker: _OwnedProcessTracker | None = None
    try:
        proc = subprocess.Popen(
            normalized_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            shell=False,               # 絕不 shell=True
            close_fds=True,
            start_new_session=True,
        )
        tracker = _OwnedProcessTracker(
            proc.pid,
            pipe_endpoints=_darwin_parent_pipe_endpoints(proc),
            launched_at=t0,
        )
        if _on_started is not None:
            try:
                _on_started(proc.pid)
            except Exception as exc:
                _signal_owned_processes(
                    proc,
                    signal_number=signal.SIGKILL,
                    tracker=tracker,
                )
                try:
                    proc.communicate(timeout=_SIGKILL_GRACE_SEC)
                except subprocess.TimeoutExpired as reap_exc:
                    raise _SafeProcessCleanupError(
                        f"process start callback failed and child could not be reaped: pid={proc.pid} "
                        f"live_owned_pids={sorted(tracker.current_pids())}"
                    ) from reap_exc
                raise _SafeProcessCleanupError(
                    f"process start callback failed; child was reaped: pid={proc.pid}"
                ) from exc
        try:
            out_b, err_b, cleanup_killed = _communicate_owned_process(
                proc,
                argv=normalized_argv,
                timeout_sec=timeout_sec,
                tracker=tracker,
                cancel_event=_cancel_event,
            )
            killed = killed or cleanup_killed
        except subprocess.TimeoutExpired:
            out_b, err_b, killed, root_signaled = _terminate_owned_processes(
                proc, tracker=tracker
            )
            # A direct child can exit naturally in the tiny interval between
            # the deadline poll and identity-verified containment.  If no
            # termination signal reached that root and it returned success,
            # only detached pipe holders were cleaned up; reporting this as a
            # timeout would create a contradictory rc=0/timed_out=true result.
            timed_out = root_signaled or proc.returncode != 0
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
        if tracker is not None:
            tracker.stop()
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
    stored commands without quotes, so shlex splits the home-relative runtime
    before "Support/...".  Joining only this
    exact known prefix keeps shell metachar protection intact while allowing
    the audit layer to validate the real argv.
    """
    repaired: List[str] = []
    application_prefix = str(Path.home() / "Library" / "Application")
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if (
            token == application_prefix
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
        plist = str(Path.home() / "Library" / "LaunchAgents" / f"{label}.plist")
        argv = ["launchctl", "bootstrap", f"gui/{uid}", plist]
    elif op == "kickstart":
        argv = ["launchctl", "kickstart", "-kp", target]
    elif op == "list":
        argv = ["launchctl", "list"]
    else:
        argv = ["launchctl", op, target]
    return run(argv, timeout_sec=30.0)
