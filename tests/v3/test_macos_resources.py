from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from magi_v3.macos_resources import (
    MEMORY_PRESSURE_COMMAND,
    FOOTPRINT_COMMAND,
    PROCESS_COMMAND,
    SWAP_USAGE_COMMAND,
    THERMAL_COMMAND,
    VM_STAT_COMMAND,
    MacOSResourceSampler,
    parse_memory_pressure,
    parse_footprint,
    parse_pmset_thermal,
    parse_ps,
    parse_swapusage,
    parse_vm_stat,
)

MEMORY_PRESSURE = """\
The system has 25769803776 (1572864 pages with a page size of 16384).
System-wide memory free percentage: 17%
"""

VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              100000.
Pages active:                            200000.
Pages inactive:                          300000.
Pages speculative:                        1000.
Pages purgeable:                          5000.
Pageouts:                                  2000.
Swapouts:                                  3000.
"""

SWAP_USAGE = "vm.swapusage: total = 2048.00M  used = 512.00M  free = 1536.00M  (encrypted)\n"

THERMAL = """\
Note: No thermal warning level has been recorded
Note: No performance warning level has been recorded
"""

PS = """\
  101     1  204800  12.5 /opt/magi/python worker.py --namespace magi-v3
  202   101   10240   0.0 /Applications/Browser Helper
"""

FOOTPRINT = """\
======================================================================
worker [101]: 64-bit    Footprint: 209715200 B (16384 bytes per page)
======================================================================

Auxiliary data:
    phys_footprint: 211812352 B
    phys_footprint_peak: 230686720 B

======================================================================
helper [202]: 64-bit    Footprint: 10485760 B (16384 bytes per page)
======================================================================

Auxiliary data:
    phys_footprint: 12582912 B
    phys_footprint_peak: 14680064 B

======================================================================
Summary Footprint: 220200960 B
======================================================================
"""


def completed(argv, stdout: str, *, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def fixture_runner(overrides=None):
    outputs = {
        MEMORY_PRESSURE_COMMAND: MEMORY_PRESSURE,
        VM_STAT_COMMAND: VM_STAT,
        SWAP_USAGE_COMMAND: SWAP_USAGE,
        THERMAL_COMMAND: THERMAL,
        PROCESS_COMMAND: PS,
    }
    outputs.update(overrides or {})

    def run(argv):
        value = outputs[tuple(argv)]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, subprocess.CompletedProcess):
            return value
        return completed(argv, value)

    return run


def test_parsers_preserve_units_and_derived_provenance() -> None:
    pressure = parse_memory_pressure(MEMORY_PRESSURE)
    vm = parse_vm_stat(VM_STAT)
    swap = parse_swapusage(SWAP_USAGE)
    processes = parse_ps(PS)

    assert pressure.free_percent == 17
    assert pressure.pressure_level == "guarded"
    assert pressure.pressure_level_source == "derived_from_memory_free_percent"
    assert vm.system_available_mb == pytest.approx((100000 + 300000 + 1000) * 16384 / 1024**2)
    assert vm.system_available_source == "derived_free_inactive_speculative_pages"
    assert vm.pageouts_total_mb == pytest.approx(31.25)
    assert vm.swapouts_total_mb == pytest.approx(46.875)
    assert swap.used_mb == 512
    assert swap.encrypted is True
    assert processes[0].rss_mb == 200
    assert processes[0].cpu_percent == 12.5
    assert processes[0].rss_is_physical_footprint is False


def test_footprint_parser_uses_pid_bound_phys_footprint_not_rss_or_generic_summary() -> None:
    footprint = parse_footprint(FOOTPRINT, expected_pids=(101, 202))

    assert footprint.target_pids == (101, 202)
    assert footprint.processes[0].footprint_mb == 200
    assert footprint.processes[0].physical_footprint_mb == 202
    assert footprint.processes[1].physical_footprint_mb == 12
    assert footprint.aggregate_physical_footprint_mb == 214
    assert "phys_footprint" in footprint.aggregate_method


def test_sampler_is_read_only_bounded_and_never_promotes_rss_to_footprint_or_metal() -> None:
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        return fixture_runner()(argv)

    sample = MacOSResourceSampler(
        runner=runner,
        clock=lambda: datetime(2026, 7, 14, 5, 0, tzinfo=timezone.utc),
    ).sample()

    assert calls == [
        MEMORY_PRESSURE_COMMAND,
        VM_STAT_COMMAND,
        SWAP_USAGE_COMMAND,
        THERMAL_COMMAND,
        PROCESS_COMMAND,
    ]
    assert sample.observed_at == "2026-07-14T05:00:00+00:00"
    assert sample.memory_pressure is not None
    assert sample.vm_stat is not None
    assert sample.swap_usage is not None
    assert sample.thermal_state == "nominal"
    assert len(sample.processes) == 2
    assert sample.magi_physical_footprint_mb is None
    assert sample.magi_metal_mb is None
    assert "magi_physical_footprint_mb" in sample.missing_metrics
    assert "magi_metal_mb" in sample.missing_metrics
    assert sample.complete is False
    assert sample.governor_ready is False
    assert sample.confidence == "partial"
    assert "per-process Metal" in sample.missing_metric_reasons["magi_metal_mb"]


def test_explicit_ps_bound_pids_enable_authoritative_physical_footprint() -> None:
    base_runner = fixture_runner()

    def runner(argv):
        if tuple(argv[:4]) == FOOTPRINT_COMMAND:
            assert tuple(argv[4:]) == ("-p", "101", "-p", "202")
            return completed(argv, FOOTPRINT)
        return base_runner(argv)

    sample = MacOSResourceSampler(runner=runner, footprint_pids=(101, 202)).sample()

    assert sample.footprint is not None
    assert sample.magi_physical_footprint_mb == 214
    assert "magi_physical_footprint_mb" not in sample.missing_metrics
    assert sample.magi_metal_mb is None
    assert sample.governor_ready is False


def test_footprint_target_must_exist_in_same_ps_inventory() -> None:
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        return fixture_runner()(argv)

    sample = MacOSResourceSampler(runner=runner, footprint_pids=(999,)).sample()

    assert all(call[:4] != FOOTPRINT_COMMAND for call in calls)
    assert any("absent from ps inventory" in error for error in sample.errors)
    assert "magi_physical_footprint_mb" in sample.missing_metrics


def test_footprint_pid_identity_mismatch_is_incomplete() -> None:
    base_runner = fixture_runner()

    def runner(argv):
        if tuple(argv[:4]) == FOOTPRINT_COMMAND:
            return completed(argv, FOOTPRINT.replace("worker [101]", "worker [999]"))
        return base_runner(argv)

    sample = MacOSResourceSampler(runner=runner, footprint_pids=(101, 202)).sample()

    assert sample.footprint is None
    assert any("PID identity mismatch" in error for error in sample.errors)
    assert sample.governor_ready is False


def test_footprint_targets_are_positive_unique_and_bounded() -> None:
    with pytest.raises(ValueError, match="positive"):
        MacOSResourceSampler(footprint_pids=(0,))
    with pytest.raises(ValueError, match="cannot exceed"):
        MacOSResourceSampler(footprint_pids=tuple(range(1, 130)))


def test_command_failure_is_incomplete_without_aborting_other_metrics() -> None:
    failure = completed(MEMORY_PRESSURE_COMMAND, "", returncode=1, stderr="permission denied")
    sample = MacOSResourceSampler(runner=fixture_runner({MEMORY_PRESSURE_COMMAND: failure})).sample()

    assert sample.memory_pressure is None
    assert sample.vm_stat is not None
    assert "memory_pressure" in sample.missing_metrics
    assert any("rc=1" in error for error in sample.errors)
    assert sample.complete is False
    assert sample.confidence == "partial"


def test_runner_exception_is_incomplete_and_redacts_long_stderr() -> None:
    failure = completed(SWAP_USAGE_COMMAND, "", returncode=2, stderr="x" * 500)
    sample = MacOSResourceSampler(
        runner=fixture_runner({VM_STAT_COMMAND: OSError("not available"), SWAP_USAGE_COMMAND: failure})
    ).sample()

    assert "vm_stat" in sample.missing_metrics
    assert "swap_usage" in sample.missing_metrics
    assert any("OSError" in error for error in sample.errors)
    assert max(len(error) for error in sample.errors) < 260


def test_invalid_runner_result_is_incomplete_instead_of_raising() -> None:
    sample = MacOSResourceSampler(runner=fixture_runner({THERMAL_COMMAND: object()})).sample()

    assert sample.thermal_state is None
    assert "thermal_state" in sample.missing_metrics
    assert any("non-text output" in error for error in sample.errors)


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_memory_pressure, "System-wide memory free percentage: 10%"),
        (parse_vm_stat, "Mach Virtual Memory Statistics: (page size of 4096 bytes)"),
        (parse_swapusage, "total = 1.00G used = 0.50G"),
        (parse_pmset_thermal, "thermal information unavailable"),
        (parse_ps, "pid ppid rss cpu command"),
    ],
)
def test_missing_or_malformed_required_fields_are_rejected(parser, payload: str) -> None:
    with pytest.raises(ValueError):
        parser(payload)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("Thermal_Level = 0", "nominal"),
        ("Thermal_Level = 1", "fair"),
        ("Thermal_Level = 2", "serious"),
        ("Thermal_Level = 3", "critical"),
        ("Performance warning level: 1", "fair"),
        ("CPU_Speed_Limit = 80\nScheduler_Limit = 90", "serious"),
    ],
)
def test_thermal_levels_are_conservatively_mapped(payload: str, expected: str) -> None:
    assert parse_pmset_thermal(payload) == expected


def test_parse_failure_marks_metric_missing_and_sample_incomplete() -> None:
    sample = MacOSResourceSampler(runner=fixture_runner({PROCESS_COMMAND: "not ps output\n"})).sample()

    assert sample.processes == ()
    assert "process_inventory" in sample.missing_metrics
    assert any("process_inventory parse failed" in error for error in sample.errors)
    assert sample.complete is False
