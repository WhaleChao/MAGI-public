"""Read-only, standard-library macOS resource sampling.

This module deliberately does not construct :class:`ResourceSnapshot`.  It
keeps authoritative counters, derived values, and unavailable governor inputs
separate so RSS can never silently stand in for physical footprint or Metal
allocation.  Callers must reject snapshots whose ``complete`` flag is false.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

MEMORY_PRESSURE_COMMAND = ("/usr/bin/memory_pressure", "-Q")
VM_STAT_COMMAND = ("/usr/bin/vm_stat",)
SWAP_USAGE_COMMAND = ("/usr/sbin/sysctl", "-n", "vm.swapusage")
THERMAL_COMMAND = ("/usr/bin/pmset", "-g", "therm")
PROCESS_COMMAND = ("/bin/ps", "-axo", "pid=,ppid=,rss=,%cpu=,command=")
FOOTPRINT_COMMAND = ("/usr/bin/footprint", "--noCategories", "-f", "bytes")
MAX_FOOTPRINT_PIDS = 128


@dataclass(frozen=True, slots=True)
class MemoryPressureMetrics:
    total_memory_bytes: int
    page_size_bytes: int
    free_percent: float
    pressure_level: str
    pressure_level_source: str = "derived_from_memory_free_percent"


@dataclass(frozen=True, slots=True)
class VMStatMetrics:
    page_size_bytes: int
    pages: Mapping[str, int]
    system_available_mb: float
    pageouts_total_mb: float
    swapouts_total_mb: float
    system_available_source: str = "derived_free_inactive_speculative_pages"


@dataclass(frozen=True, slots=True)
class SwapUsageMetrics:
    total_mb: float
    used_mb: float
    free_mb: float
    encrypted: bool


@dataclass(frozen=True, slots=True)
class ProcessMetrics:
    pid: int
    ppid: int
    rss_mb: float
    cpu_percent: float
    command: str
    rss_is_physical_footprint: bool = False


@dataclass(frozen=True, slots=True)
class ProcessFootprintMetrics:
    pid: int
    footprint_mb: float
    physical_footprint_mb: float
    physical_footprint_peak_mb: float


@dataclass(frozen=True, slots=True)
class FootprintMetrics:
    target_pids: tuple[int, ...]
    processes: tuple[ProcessFootprintMetrics, ...]
    aggregate_physical_footprint_mb: float
    aggregate_method: str = "sum_of_pid_bound_phys_footprint_ledgers"
    source: str = "/usr/bin/footprint --noCategories -f bytes"


@dataclass(frozen=True, slots=True)
class MacOSResourceSample:
    observed_at: str
    complete: bool
    confidence: str
    missing_metrics: tuple[str, ...]
    errors: tuple[str, ...]
    memory_pressure: MemoryPressureMetrics | None
    vm_stat: VMStatMetrics | None
    swap_usage: SwapUsageMetrics | None
    thermal_state: str | None
    processes: tuple[ProcessMetrics, ...]
    footprint: FootprintMetrics | None
    missing_metric_reasons: Mapping[str, str]
    magi_physical_footprint_mb: float | None = None
    magi_metal_mb: None = None

    @property
    def governor_ready(self) -> bool:
        """Only complete samples may be adapted into governor inputs."""

        return self.complete


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _pressure_level(free_percent: float) -> str:
    # This is a conservative derived classification, not a kernel pressure
    # notification.  The source is explicit on MemoryPressureMetrics.
    if free_percent < 10:
        return "critical"
    if free_percent < 20:
        return "guarded"
    return "green"


def parse_memory_pressure(text: str) -> MemoryPressureMetrics:
    total = re.search(
        r"The system has\s+(\d+)\s+\((\d+) pages with a page size of (\d+)\)", text
    )
    free = re.search(r"System-wide memory free percentage:\s*([0-9]+(?:\.[0-9]+)?)%", text)
    if not total or not free:
        raise ValueError("required total/page-size/free-percent fields are missing")
    total_bytes = int(total.group(1))
    page_count = int(total.group(2))
    page_size = int(total.group(3))
    free_percent = float(free.group(1))
    if total_bytes <= 0 or page_count <= 0 or page_size <= 0:
        raise ValueError("memory size fields must be positive")
    if not 0 <= free_percent <= 100:
        raise ValueError("memory free percentage is outside 0..100")
    if page_count * page_size != total_bytes:
        raise ValueError("memory total does not match page count and page size")
    return MemoryPressureMetrics(
        total_memory_bytes=total_bytes,
        page_size_bytes=page_size,
        free_percent=free_percent,
        pressure_level=_pressure_level(free_percent),
    )


def parse_vm_stat(text: str) -> VMStatMetrics:
    header = re.search(r"page size of\s+(\d+) bytes", text)
    if not header:
        raise ValueError("vm_stat page size is missing")
    page_size = int(header.group(1))
    pages: dict[str, int] = {}
    for line in text.splitlines()[1:]:
        match = re.match(r'\s*"?([^":]+)"?:\s*([0-9]+)\.\s*$', line)
        if match:
            key = re.sub(r"\s+", "_", match.group(1).strip().lower())
            pages[key] = int(match.group(2))
    required = ("pages_free", "pages_inactive", "pages_speculative", "pageouts", "swapouts")
    missing = [name for name in required if name not in pages]
    if page_size <= 0 or missing:
        detail = ", ".join(missing) if missing else "invalid page size"
        raise ValueError(f"vm_stat required counters missing: {detail}")
    bytes_per_mb = 1024 * 1024
    available_pages = pages["pages_free"] + pages["pages_inactive"] + pages["pages_speculative"]
    return VMStatMetrics(
        page_size_bytes=page_size,
        pages=pages,
        system_available_mb=available_pages * page_size / bytes_per_mb,
        pageouts_total_mb=pages["pageouts"] * page_size / bytes_per_mb,
        swapouts_total_mb=pages["swapouts"] * page_size / bytes_per_mb,
    )


def _size_to_mb(value: str, unit: str) -> float:
    factors = {"K": 1 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024.0}
    try:
        factor = factors[unit.upper()]
    except KeyError as exc:
        raise ValueError(f"unsupported swap size unit: {unit}") from exc
    return float(value) * factor


def parse_swapusage(text: str) -> SwapUsageMetrics:
    values: dict[str, float] = {}
    for name, value, unit in re.findall(
        r"\b(total|used|free)\s*=\s*([0-9]+(?:\.[0-9]+)?)([KMGT])\b", text, re.IGNORECASE
    ):
        values[name.lower()] = _size_to_mb(value, unit)
    if set(values) != {"total", "used", "free"}:
        raise ValueError("swap total/used/free fields are missing")
    tolerance_mb = max(1.0, values["total"] * 0.001)
    if abs(values["used"] + values["free"] - values["total"]) > tolerance_mb:
        raise ValueError("swap used plus free does not match total")
    return SwapUsageMetrics(
        total_mb=values["total"],
        used_mb=values["used"],
        free_mb=values["free"],
        encrypted="(encrypted)" in text.lower(),
    )


def parse_pmset_thermal(text: str) -> str:
    lowered = text.lower()
    levels = [
        int(value)
        for value in re.findall(
            r"(?:thermal[_ ]level|(?:thermal|performance) warning level)\s*(?:=|:)\s*(\d+)",
            lowered,
        )
    ]
    limits = [
        int(value)
        for value in re.findall(r"(?:cpu_speed_limit|scheduler_limit)\s*=\s*(\d+)", lowered)
    ]
    if limits and min(limits) < 100:
        levels.append(2)
    no_warnings = (
        "no thermal warning level has been recorded" in lowered
        and "no performance warning level has been recorded" in lowered
    )
    if not levels and no_warnings:
        return "nominal"
    if not levels:
        raise ValueError("pmset thermal level is missing")
    level = max(levels)
    if level <= 0:
        return "nominal"
    if level == 1:
        return "fair"
    if level == 2:
        return "serious"
    return "critical"


def parse_ps(text: str) -> tuple[ProcessMetrics, ...]:
    processes: list[ProcessMetrics] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+([0-9]+(?:\.[0-9]+)?)\s+(.+)$", line)
        if not match:
            raise ValueError(f"ps row {line_number} is malformed")
        processes.append(
            ProcessMetrics(
                pid=int(match.group(1)),
                ppid=int(match.group(2)),
                rss_mb=int(match.group(3)) / 1024.0,
                cpu_percent=float(match.group(4)),
                command=match.group(5),
            )
        )
    if not processes:
        raise ValueError("ps process inventory is empty")
    return tuple(processes)


def parse_footprint(text: str, *, expected_pids: Sequence[int]) -> FootprintMetrics:
    """Parse PID-bound kernel physical-footprint ledger values.

    The auxiliary ``phys_footprint`` field is used instead of the summary's
    generic dirty-memory Footprint field.  It is never derived from ps RSS.
    """

    expected = tuple(dict.fromkeys(int(pid) for pid in expected_pids))
    if not expected or any(pid <= 0 for pid in expected):
        raise ValueError("expected footprint PIDs must be positive")
    header_pattern = re.compile(
        r"^.+?\[(\d+)\]:.*?Footprint:\s*(\d+)\s+B\b", re.MULTILINE
    )
    headers = list(header_pattern.finditer(text))
    if not headers:
        raise ValueError("footprint PID headers are missing")
    parsed: list[ProcessFootprintMetrics] = []
    for index, header in enumerate(headers):
        section_end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        section = text[header.end() : section_end]
        physical = re.search(r"^\s*phys_footprint:\s*(\d+)\s+B\s*$", section, re.MULTILINE)
        peak = re.search(r"^\s*phys_footprint_peak:\s*(\d+)\s+B\s*$", section, re.MULTILINE)
        if not physical or not peak:
            raise ValueError(f"footprint auxiliary physical fields missing for pid {header.group(1)}")
        parsed.append(
            ProcessFootprintMetrics(
                pid=int(header.group(1)),
                footprint_mb=int(header.group(2)) / 1024**2,
                physical_footprint_mb=int(physical.group(1)) / 1024**2,
                physical_footprint_peak_mb=int(peak.group(1)) / 1024**2,
            )
        )
    observed = tuple(item.pid for item in parsed)
    if len(observed) != len(set(observed)) or set(observed) != set(expected):
        raise ValueError(f"footprint PID identity mismatch: expected={sorted(expected)} observed={sorted(observed)}")
    ordered = tuple(sorted(parsed, key=lambda item: expected.index(item.pid)))
    return FootprintMetrics(
        target_pids=expected,
        processes=ordered,
        aggregate_physical_footprint_mb=sum(item.physical_footprint_mb for item in ordered),
    )


class MacOSResourceSampler:
    """Collect one bounded, read-only host sample using injectable commands."""

    def __init__(
        self,
        *,
        runner: CommandRunner = _default_runner,
        clock: Callable[[], datetime] | None = None,
        footprint_pids: Sequence[int] = (),
    ) -> None:
        self._runner = runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        pids = tuple(dict.fromkeys(int(pid) for pid in footprint_pids))
        if any(pid <= 0 for pid in pids):
            raise ValueError("footprint_pids must contain only positive PIDs")
        if len(pids) > MAX_FOOTPRINT_PIDS:
            raise ValueError(f"footprint_pids cannot exceed {MAX_FOOTPRINT_PIDS}")
        self._footprint_pids = pids

    def _collect(self, name: str, argv: Sequence[str]) -> tuple[str | None, str | None]:
        try:
            result = self._runner(argv)
        except Exception as exc:
            return None, f"{name} command failed: {type(exc).__name__}: {exc}"
        try:
            returncode = int(result.returncode)
            stdout = result.stdout or ""
            stderr = result.stderr or ""
        except (AttributeError, TypeError, ValueError) as exc:
            return None, f"{name} command returned an invalid result: {type(exc).__name__}"
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            return None, f"{name} command returned non-text output"
        if returncode != 0:
            detail = stderr.strip().replace("\n", " ")[:200]
            return None, f"{name} command failed rc={returncode}: {detail}"
        return stdout, None

    def sample(self) -> MacOSResourceSample:
        outputs: dict[str, str] = {}
        errors: list[str] = []
        commands = {
            "memory_pressure": MEMORY_PRESSURE_COMMAND,
            "vm_stat": VM_STAT_COMMAND,
            "swap_usage": SWAP_USAGE_COMMAND,
            "thermal_state": THERMAL_COMMAND,
            "process_inventory": PROCESS_COMMAND,
        }
        for name, argv in commands.items():
            output, error = self._collect(name, argv)
            if error:
                errors.append(error)
            elif output is not None:
                outputs[name] = output

        parsed: dict[str, object] = {}
        parsers: dict[str, Callable[[str], object]] = {
            "memory_pressure": parse_memory_pressure,
            "vm_stat": parse_vm_stat,
            "swap_usage": parse_swapusage,
            "thermal_state": parse_pmset_thermal,
            "process_inventory": parse_ps,
        }
        for name, parser in parsers.items():
            if name not in outputs:
                continue
            try:
                parsed[name] = parser(outputs[name])
            except (TypeError, ValueError) as exc:
                errors.append(f"{name} parse failed: {exc}")

        footprint: FootprintMetrics | None = None
        if self._footprint_pids:
            inventory = parsed.get("process_inventory", ())
            inventory_pids = {
                process.pid for process in inventory if isinstance(process, ProcessMetrics)
            }
            unknown_pids = sorted(set(self._footprint_pids) - inventory_pids)
            if unknown_pids:
                errors.append(f"footprint target PIDs absent from ps inventory: {unknown_pids}")
            else:
                argv = FOOTPRINT_COMMAND + tuple(
                    argument for pid in self._footprint_pids for argument in ("-p", str(pid))
                )
                output, error = self._collect("physical_footprint", argv)
                if error:
                    errors.append(error)
                elif output is not None:
                    try:
                        footprint = parse_footprint(output, expected_pids=self._footprint_pids)
                    except (TypeError, ValueError) as exc:
                        errors.append(f"physical_footprint parse failed: {exc}")

        missing = [name for name in commands if name not in parsed]
        missing_reasons: dict[str, str] = {}
        if footprint is None:
            missing.append("magi_physical_footprint_mb")
            missing_reasons["magi_physical_footprint_mb"] = (
                "explicit PID-bound /usr/bin/footprint sample was not available"
            )
        # powermetrics provides GPU time, not allocation bytes, and requires
        # superuser privileges. ioreg's AGX allocation is system-wide and its
        # per-user-client AppUsage data is undocumented GPU time. Neither is a
        # validated per-process Metal-memory source.
        missing.append("magi_metal_mb")
        missing_reasons["magi_metal_mb"] = (
            "no validated non-privileged per-process Metal allocation-byte source"
        )
        present_count = len(parsed)
        confidence = "none" if present_count == 0 else "partial" if missing else "high"
        observed = self._clock()
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return MacOSResourceSample(
            observed_at=observed.astimezone(timezone.utc).isoformat(),
            complete=not missing and not errors,
            confidence=confidence,
            missing_metrics=tuple(dict.fromkeys(missing)),
            errors=tuple(errors),
            memory_pressure=parsed.get("memory_pressure"),  # type: ignore[arg-type]
            vm_stat=parsed.get("vm_stat"),  # type: ignore[arg-type]
            swap_usage=parsed.get("swap_usage"),  # type: ignore[arg-type]
            thermal_state=parsed.get("thermal_state"),  # type: ignore[arg-type]
            processes=parsed.get("process_inventory", ()),  # type: ignore[arg-type]
            footprint=footprint,
            missing_metric_reasons=missing_reasons,
            magi_physical_footprint_mb=(
                footprint.aggregate_physical_footprint_mb if footprint is not None else None
            ),
        )


__all__ = [
    "MacOSResourceSample",
    "MacOSResourceSampler",
    "MemoryPressureMetrics",
    "FootprintMetrics",
    "ProcessFootprintMetrics",
    "ProcessMetrics",
    "SwapUsageMetrics",
    "VMStatMetrics",
    "parse_memory_pressure",
    "parse_footprint",
    "parse_pmset_thermal",
    "parse_ps",
    "parse_swapusage",
    "parse_vm_stat",
]
