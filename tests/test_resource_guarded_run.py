from __future__ import annotations

from scripts.ops import resource_governor as rg
from scripts.ops import resource_guarded_run as guarded


def _decision(level: str, disk: float = 80, free_inactive: float = 10) -> rg.ResourceDecision:
    snap = rg.ResourceSnapshot(
        disk_free_gb=disk,
        disk_total_gb=460,
        swap_used_gb=1,
        free_gb=4,
        inactive_gb=max(0, free_inactive - 4),
        free_plus_inactive_gb=free_inactive,
    )
    return rg.ResourceDecision(
        ok=level != "critical",
        level=level,
        reasons=[],
        actions=[],
        snapshot=snap,
    )


def test_should_block_at_configured_level():
    blocked, reasons = guarded._should_block(
        _decision("core_only"),
        block_at="core_only",
        require_disk_free_gb=None,
        require_free_inactive_gb=None,
    )
    assert blocked is True
    assert reasons == ["resource_level>=core_only:core_only"]


def test_should_not_block_lower_level():
    blocked, reasons = guarded._should_block(
        _decision("throttle"),
        block_at="core_only",
        require_disk_free_gb=None,
        require_free_inactive_gb=None,
    )
    assert blocked is False
    assert reasons == []


def test_explicit_disk_requirement_blocks_even_when_level_is_normal():
    blocked, reasons = guarded._should_block(
        _decision("normal", disk=44),
        block_at="critical",
        require_disk_free_gb=60,
        require_free_inactive_gb=None,
    )
    assert blocked is True
    assert reasons == ["disk_free<60GB:44GB"]
