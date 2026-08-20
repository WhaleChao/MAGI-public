"""Fail-closed boundary for bounded production-entrypoint schedule fixtures."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "magi.schedule-product-fixture/v1"


class ScheduleFixtureError(RuntimeError):
    pass


class _SafetyObservation:
    """Process-local evidence collected from Python audit events.

    Seatbelt remains the outer enforcement boundary.  This observer is the
    independent, product-visible receipt: it derives claims from events that
    occurred after the fixture was bound instead of trusting booleans supplied
    by the fixture body.
    """

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.subprocesses: list[str] = []
        self.network_addresses: list[str] = []
        self.written_paths: list[str] = []
        self.accessed_paths: list[str] = []

    @staticmethod
    def _path(value: Any) -> Path | None:
        if isinstance(value, int) or value is None:
            return None
        try:
            return Path(os.fsdecode(value)).expanduser().resolve(strict=False)
        except (OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _open_is_write(args: tuple[Any, ...]) -> bool:
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        if isinstance(mode, str) and any(char in mode for char in "wax+"):
            return True
        if isinstance(flags, int):
            mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            return bool(flags & mask)
        return False

    def record(self, event: str, args: tuple[Any, ...]) -> None:
        writes: list[Path] = []
        reads: list[Path] = []
        subprocess_name = ""
        address = ""
        if event == "subprocess.Popen":
            subprocess_name = str(args[0] if args else "")[:500]
        elif event == "socket.connect":
            raw_address = args[1] if len(args) > 1 else None
            address = repr(raw_address)[:500]
        elif event == "open":
            path = self._path(args[0] if args else None)
            if path is not None:
                (writes if self._open_is_write(args) else reads).append(path)
        elif event in {"os.remove", "os.rmdir", "os.mkdir"}:
            path = self._path(args[0] if args else None)
            if path is not None:
                writes.append(path)
        elif event in {"os.rename", "os.replace"}:
            for raw in args[:2]:
                path = self._path(raw)
                if path is not None:
                    writes.append(path)
        elif event in {"os.listdir", "os.scandir"}:
            path = self._path(args[0] if args else None)
            if path is not None:
                reads.append(path)
        if not (writes or reads or subprocess_name or address):
            return
        with self.lock:
            if subprocess_name:
                self.subprocesses.append(subprocess_name)
            if address:
                self.network_addresses.append(address)
            self.written_paths.extend(str(path) for path in writes)
            self.accessed_paths.extend(str(path) for path in reads)

    @staticmethod
    def _is_loopback(raw: str) -> bool:
        lowered = raw.lower()
        return any(token in lowered for token in ("127.0.0.1", "::1", "localhost"))

    @staticmethod
    def _is_nas_path(raw: str) -> bool:
        path = Path(raw)
        candidates = (
            Path("/Volumes"),
            Path.home() / ".magi_mounts",
            Path.home() / "Library" / "CloudStorage",
        )
        return any(_inside(path, candidate) for candidate in candidates)

    def receipt(self, *, include_process: bool) -> dict[str, Any]:
        with self.lock:
            subprocesses = list(self.subprocesses)
            addresses = list(self.network_addresses)
            written = list(self.written_paths)
            accessed = list(self.accessed_paths)
        outside_writes = [
            raw
            for raw in written
            if raw != "/dev/null" and not _inside(Path(raw), self.root)
        ]
        external_addresses = [raw for raw in addresses if not self._is_loopback(raw)]
        database_addresses = [
            raw
            for raw in addresses
            if any(token in raw for token in ("3306", "3307", "mysql", "mariadb"))
        ]
        nas_paths = [raw for raw in (*accessed, *written) if self._is_nas_path(raw)]
        receipt: dict[str, Any] = {
            "fixture_root": str(self.root),
            "external_network_accessed": bool(external_addresses),
            "production_database_accessed": bool(database_addresses),
            "production_state_written": bool(outside_writes),
            "nas_accessed": bool(nas_paths),
            "writes_bounded_to_fixture": not outside_writes,
        }
        if include_process:
            receipt.update(
                {
                    "subprocess_spawned": bool(subprocesses),
                    "subprocess_spawn_count": len(subprocesses),
                    "observation_source": "python_audit_hook",
                }
            )
        return receipt


_OBSERVATIONS: dict[str, _SafetyObservation] = {}
_OBSERVATIONS_LOCK = threading.Lock()
_AUDIT_HOOK_INSTALLED = False


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    with _OBSERVATIONS_LOCK:
        observations = list(_OBSERVATIONS.values())
    for observation in observations:
        observation.record(event, args)


def _install_audit_hook() -> None:
    global _AUDIT_HOOK_INSTALLED
    if _AUDIT_HOOK_INSTALLED:
        return
    sys.addaudithook(_audit_hook)
    _AUDIT_HOOK_INSTALLED = True


def _begin_safety_observation(fixture: "ScheduleFixture") -> None:
    _install_audit_hook()
    key = str(fixture.root)
    with _OBSERVATIONS_LOCK:
        if key in _OBSERVATIONS:
            raise ScheduleFixtureError("fixture safety observation is already active")
        _OBSERVATIONS[key] = _SafetyObservation(fixture.root)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _owned_regular(path: Path, description: str) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_nlink != 1
    ):
        raise ScheduleFixtureError(f"{description} must be an owner-controlled regular file")


def _owned_directory(path: Path, description: str) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ScheduleFixtureError(f"{description} must be an owner-controlled directory")


@dataclass(frozen=True)
class ScheduleFixture:
    job_id: str
    root: Path
    inputs: Path
    workspace: Path
    outputs: Path
    manifest: Mapping[str, Any]

    @property
    def sample_id(self) -> int:
        value = self.manifest.get("product_input", {}).get("sample_id")
        if type(value) is not int or not 1 <= value <= 3:
            raise ScheduleFixtureError("fixture sample_id must be an integer in 1..3")
        return value

    def input_path(self, name: str) -> Path:
        if not name or Path(name).name != name or name.startswith("."):
            raise ScheduleFixtureError("fixture input must be a plain basename")
        path = (self.inputs / name).resolve(strict=False)
        if not _inside(path, self.inputs) or path.is_symlink() or not path.exists():
            raise ScheduleFixtureError("fixture input escapes the bounded input directory")
        _owned_regular(path, "fixture input")
        return path

    def output_path(self, raw: str | Path) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.outputs / path
        path = path.resolve(strict=False)
        if not _inside(path, self.outputs) or path.is_symlink():
            raise ScheduleFixtureError("fixture output escapes the bounded output directory")
        return path


def load_schedule_fixture(raw_root: str | Path, *, job_id: str) -> ScheduleFixture:
    if os.environ.get("MAGI_V3_SCHEDULE_FIXTURE") != "1":
        raise ScheduleFixtureError("bounded fixture mode requires MAGI_V3_SCHEDULE_FIXTURE=1")
    root = Path(raw_root).expanduser()
    if not root.is_absolute() or root.resolve(strict=False) != root or root.is_symlink():
        raise ScheduleFixtureError("fixture root must be a canonical absolute directory")
    expected_root = os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT", "").strip()
    if not expected_root or Path(expected_root).expanduser().resolve(strict=False) != root:
        raise ScheduleFixtureError("fixture root is not environment-bound")
    metadata = root.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ScheduleFixtureError("fixture root must be owner-controlled and non-writable by group/other")
    manifest_path = root / "fixture.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ScheduleFixtureError("fixture manifest is missing or unsafe")
    _owned_regular(manifest_path, "fixture manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != SCHEMA
        or manifest.get("job_id") != job_id
        or not isinstance(manifest.get("product_input"), dict)
    ):
        raise ScheduleFixtureError("fixture manifest identity/product input is invalid")
    inputs = root / "inputs"
    workspace = root / "workspace"
    outputs = root / "outputs"
    if not inputs.is_dir() or inputs.is_symlink():
        raise ScheduleFixtureError("fixture input directory is missing or unsafe")
    _owned_directory(inputs, "fixture input directory")
    for path in (workspace, outputs):
        if path.exists() and (not path.is_dir() or path.is_symlink()):
            raise ScheduleFixtureError("fixture writable directory is unsafe")
        path.mkdir(mode=0o700, exist_ok=True)
        _owned_directory(path, "fixture writable directory")
    fixture = ScheduleFixture(job_id, root, inputs, workspace, outputs, manifest)
    # Reject an unbound or replayed sample before any product entrypoint can do
    # work.  The property remains available to the report contract as evidence.
    fixture.sample_id
    _begin_safety_observation(fixture)
    return fixture


def write_fixture_report(
    fixture: ScheduleFixture,
    raw_output: str | Path,
    payload: Mapping[str, Any],
) -> Path:
    output = fixture.output_path(raw_output)
    relative_parent = output.parent.relative_to(fixture.outputs)
    current = fixture.outputs
    for part in relative_parent.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise ScheduleFixtureError("fixture output parent contains a symlink")
            _owned_directory(current, "fixture output directory")
        else:
            current.mkdir(mode=0o700)
    data = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        raw = data.encode("utf-8")
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return output


def safety_receipt(
    fixture: ScheduleFixture, *, include_process: bool = False
) -> dict[str, Any]:
    """Finalize a receipt derived from observed fixture activity.

    A fixture may describe expected results, but it cannot describe its own
    safety.  Missing observation state is therefore fail-closed rather than
    silently recreating the former constant receipt.
    """

    key = str(fixture.root)
    with _OBSERVATIONS_LOCK:
        observation = _OBSERVATIONS.pop(key, None)
    if observation is None:
        raise ScheduleFixtureError("fixture safety observation is missing or already finalized")
    return observation.receipt(include_process=include_process)
