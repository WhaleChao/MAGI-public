from __future__ import annotations

import pytest

from magi_v3.config import ResourcePolicy
from magi_v3.errors import AdmissionDenied
from magi_v3.resource import (
    AdmissionRequest,
    GlobalResourceGovernor,
    PressureLevel,
    ResourceSnapshot,
)


def request(
    worker_class: str,
    *,
    interactive: bool = False,
    priority_class: str = "P3",
) -> AdmissionRequest:
    return AdmissionRequest(
        worker_class=worker_class,
        estimated_footprint_mb=100,
        estimated_metal_mb=0,
        interactive=interactive,
        priority_class=priority_class,
    )


def test_global_light_limit_is_two() -> None:
    governor = GlobalResourceGovernor()
    snapshot = ResourceSnapshot()
    first = governor.acquire(request("light"), snapshot)
    with pytest.raises(AdmissionDenied, match="p0_light_reserve"):
        governor.acquire(request("light"), snapshot)
    second = governor.acquire(request("light", priority_class="P0"), snapshot)
    with pytest.raises(AdmissionDenied, match="light_limit"):
        governor.acquire(request("light", priority_class="P0"), snapshot)
    first.release()
    third = governor.acquire(request("light"), snapshot)
    assert governor.active_counts()["light"] == 2
    second.release()
    third.release()


def test_all_heavy_classes_share_two_tokens() -> None:
    governor = GlobalResourceGovernor()
    snapshot = ResourceSnapshot()
    browser = governor.acquire(request("browser"), snapshot)
    document = governor.acquire(request("document"), snapshot)
    with pytest.raises(AdmissionDenied, match="heavy_limit"):
        governor.acquire(request("transcription"), snapshot)
    document.release()
    browser.release()
    document = governor.acquire(request("document"), snapshot)
    document.release()


def test_guarded_pressure_and_interactive_reserve_block_background() -> None:
    governor = GlobalResourceGovernor()
    guarded = ResourceSnapshot(pressure=PressureLevel.GUARDED)
    with pytest.raises(AdmissionDenied, match="memory_pressure_warning"):
        governor.acquire(request("light"), guarded)

    low_available = ResourceSnapshot(system_available_mb=8200)
    large = AdmissionRequest(worker_class="light", estimated_footprint_mb=1000)
    with pytest.raises(AdmissionDenied, match="interactive_reserve"):
        governor.acquire(large, low_available)


def test_interactive_bypasses_soft_guards_but_not_hard_limits() -> None:
    governor = GlobalResourceGovernor()
    guarded = ResourceSnapshot(
        pressure=PressureLevel.GUARDED,
        memory_free_percent=20,
        system_available_mb=1000,
        swapout_delta_mb=1000,
    )
    lease = governor.acquire(request("light", interactive=True), guarded)
    lease.release()

    hard = ResourceSnapshot(magi_footprint_mb=12250)
    with pytest.raises(AdmissionDenied, match="footprint_hard_limit"):
        governor.acquire(request("light", interactive=True), hard)


def test_critical_pressure_allows_only_interactive_light() -> None:
    governor = GlobalResourceGovernor()
    critical = ResourceSnapshot(pressure=PressureLevel.CRITICAL)
    lease = governor.acquire(request("light", interactive=True), critical)
    with pytest.raises(AdmissionDenied, match="critical_memory_pressure"):
        governor.acquire(request("light"), critical)
    lease.release()


def test_interactive_activity_prevents_new_background_heavy() -> None:
    governor = GlobalResourceGovernor()
    snapshot = ResourceSnapshot(interactive_active=True)
    with pytest.raises(AdmissionDenied, match="interactive_activity"):
        governor.acquire(request("maintenance"), snapshot)


def test_active_interactive_lease_prevents_new_background_heavy() -> None:
    governor = GlobalResourceGovernor()
    snapshot = ResourceSnapshot()
    interactive = governor.acquire(
        request("light", interactive=True, priority_class="P0"),
        snapshot,
    )
    with pytest.raises(AdmissionDenied, match="interactive_activity"):
        governor.acquire(request("maintenance"), snapshot)
    interactive.release()


def test_policy_rejects_more_than_two_heavy() -> None:
    with pytest.raises(Exception, match="max_heavy"):
        ResourcePolicy(max_heavy=3).validate()


def test_active_reservations_count_toward_global_footprint() -> None:
    policy = ResourcePolicy(
        total_footprint_soft_mb=500,
        total_footprint_hard_mb=1000,
    )
    governor = GlobalResourceGovernor(policy)
    snapshot = ResourceSnapshot(system_available_mb=20000)
    first = governor.acquire(
        AdmissionRequest(worker_class="light", estimated_footprint_mb=300), snapshot
    )
    with pytest.raises(AdmissionDenied, match="footprint_soft_limit"):
        governor.acquire(
            AdmissionRequest(worker_class="light", estimated_footprint_mb=300), snapshot
        )
    first.release()
