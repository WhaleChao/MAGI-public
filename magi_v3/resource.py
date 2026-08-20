"""Global in-process resource admission for a single V3 supervisor."""

from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .config import ResourcePolicy
from .errors import AdmissionDenied
from .ledger import WORKER_CLASSES


class PressureLevel(StrEnum):
    GREEN = "green"
    GUARDED = "guarded"
    CRITICAL = "critical"


HEAVY_CLASSES = frozenset(
    {"browser", "document", "transcription", "integration", "model", "maintenance"}
)


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """Host metrics supplied by a cheap platform-specific sampler."""

    pressure: PressureLevel = PressureLevel.GREEN
    memory_free_percent: float = 100.0
    system_available_mb: float = 24576.0
    swapout_delta_mb: float = 0.0
    magi_footprint_mb: float = 0.0
    magi_metal_mb: float = 0.0
    background_cpu_percent: float = 0.0
    thermal_state: str = "nominal"
    interactive_active: bool = False


class ResourceSnapshotProvider(Protocol):
    def snapshot(self) -> ResourceSnapshot:
        """Return a current, side-effect-free host resource snapshot."""


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    worker_class: str
    estimated_footprint_mb: float
    estimated_metal_mb: float = 0.0
    cpu_percent: int = 0
    disk_io: str = "none"
    nas_io: str = "none"
    network: str = "none"
    browser_tokens: int = 0
    interactive: bool = False
    priority_class: str = "P3"
    job_id: str | None = None

    def validate(self) -> None:
        if self.worker_class not in WORKER_CLASSES:
            raise ValueError(f"unsupported worker class: {self.worker_class}")
        if (
            isinstance(self.estimated_footprint_mb, bool)
            or not isinstance(self.estimated_footprint_mb, (int, float))
            or not math.isfinite(self.estimated_footprint_mb)
            or isinstance(self.estimated_metal_mb, bool)
            or not isinstance(self.estimated_metal_mb, (int, float))
            or not math.isfinite(self.estimated_metal_mb)
            or self.estimated_footprint_mb < 0
            or self.estimated_metal_mb < 0
        ):
            raise ValueError("resource estimates cannot be negative")
        if type(self.cpu_percent) is not int or not 0 <= self.cpu_percent <= 1000:
            raise ValueError("cpu_percent must be an integer in [0, 1000]")
        if self.disk_io not in {"none", "light", "heavy"}:
            raise ValueError("disk_io has an unsupported class")
        if self.nas_io not in {"none", "light", "heavy"}:
            raise ValueError("nas_io has an unsupported class")
        if self.network not in {"none", "light", "heavy"}:
            raise ValueError("network has an unsupported class")
        if type(self.browser_tokens) is not int or not 0 <= self.browser_tokens <= 1:
            raise ValueError("browser_tokens must be an integer in [0, 1]")
        if self.priority_class not in {"P0", "P1", "P2", "P3", "P4"}:
            raise ValueError(f"unsupported priority class: {self.priority_class}")


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    allowed: bool
    reasons: tuple[str, ...]
    projected_footprint_mb: float
    projected_metal_mb: float


class ResourceLease:
    """Idempotently releases one admitted worker slot."""

    __slots__ = ("token", "request", "_governor", "_released")

    def __init__(
        self,
        token: str,
        request: AdmissionRequest,
        governor: "GlobalResourceGovernor",
    ) -> None:
        self.token = token
        self.request = request
        self._governor = governor
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if not self._released:
            self._governor.release(self.token)
            self._released = True

    def __enter__(self) -> "ResourceLease":
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class GlobalResourceGovernor:
    """Enforce heavy<=2, browser=1 and light<=2 across the V3 core."""

    def __init__(self, policy: ResourcePolicy | None = None) -> None:
        self.policy = policy or ResourcePolicy()
        self.policy.validate()
        self._lock = threading.RLock()
        self._active: dict[str, AdmissionRequest] = {}

    def evaluate(
        self,
        request: AdmissionRequest,
        snapshot: ResourceSnapshot,
    ) -> AdmissionDecision:
        request.validate()
        with self._lock:
            active = tuple(self._active.values())
        reasons: list[str] = []
        reserved_footprint = sum(item.estimated_footprint_mb for item in active)
        reserved_metal = sum(item.estimated_metal_mb for item in active)
        reserved_cpu = sum(item.cpu_percent for item in active)
        reserved_browser_tokens = sum(item.browser_tokens for item in active)
        projected_footprint = (
            snapshot.magi_footprint_mb + reserved_footprint + request.estimated_footprint_mb
        )
        projected_metal = snapshot.magi_metal_mb + reserved_metal + request.estimated_metal_mb
        light_count = sum(item.worker_class == "light" for item in active)
        non_p0_light_count = sum(
            item.worker_class == "light" and item.priority_class != "P0" for item in active
        )
        heavy_count = sum(item.worker_class in HEAVY_CLASSES for item in active)
        browser_count = sum(item.worker_class == "browser" for item in active)
        interactive_active = snapshot.interactive_active or any(
            item.interactive for item in active
        )

        if request.worker_class == "light":
            if light_count >= self.policy.max_light:
                reasons.append("light_limit")
            elif request.priority_class != "P0" and non_p0_light_count >= (
                self.policy.max_light - self.policy.reserved_p0_light_slots
            ):
                reasons.append("p0_light_reserve")
        if request.worker_class in HEAVY_CLASSES and heavy_count >= self.policy.max_heavy:
            reasons.append("heavy_limit")
        if request.worker_class == "browser" and browser_count >= self.policy.max_browser:
            reasons.append("browser_limit")
        if reserved_browser_tokens + request.browser_tokens > self.policy.max_browser:
            reasons.append("browser_token_limit")

        critical_interactive_light = request.interactive and request.worker_class == "light"
        if snapshot.pressure is PressureLevel.CRITICAL and not critical_interactive_light:
            reasons.append("critical_memory_pressure")
        if snapshot.thermal_state in {"serious", "critical"} and not critical_interactive_light:
            reasons.append("critical_thermal_pressure")
        if projected_footprint > self.policy.total_footprint_hard_mb:
            reasons.append("footprint_hard_limit")
        if projected_metal > self.policy.metal_hard_mb:
            reasons.append("metal_hard_limit")

        if not request.interactive:
            if snapshot.pressure is PressureLevel.GUARDED:
                reasons.append("memory_pressure_warning")
            if snapshot.thermal_state in {"fair", "serious", "critical"}:
                reasons.append("thermal_guard")
            if (
                snapshot.background_cpu_percent + reserved_cpu + request.cpu_percent
                > self.policy.max_background_cpu_percent
            ):
                reasons.append("background_cpu_limit")
            if snapshot.memory_free_percent < self.policy.min_memory_free_percent:
                reasons.append("memory_free_reserve")
            if snapshot.swapout_delta_mb > self.policy.max_swapout_delta_mb:
                reasons.append("swapout_growth")
            if projected_footprint > self.policy.total_footprint_soft_mb:
                reasons.append("footprint_soft_limit")
            if projected_metal > self.policy.metal_soft_mb:
                reasons.append("metal_soft_limit")
            required_available = (
                reserved_footprint
                + request.estimated_footprint_mb
                + self.policy.interactive_reserve_mb
            )
            if snapshot.system_available_mb < required_available:
                reasons.append("interactive_reserve")
            if interactive_active and request.worker_class in HEAVY_CLASSES:
                reasons.append("interactive_activity")

        return AdmissionDecision(
            allowed=not reasons,
            reasons=tuple(reasons),
            projected_footprint_mb=projected_footprint,
            projected_metal_mb=projected_metal,
        )

    def acquire(
        self,
        request: AdmissionRequest,
        snapshot: ResourceSnapshot,
    ) -> ResourceLease:
        """Atomically evaluate and reserve one concurrency slot."""

        request.validate()
        with self._lock:
            decision = self.evaluate(request, snapshot)
            if not decision.allowed:
                raise AdmissionDenied(",".join(decision.reasons))
            token = uuid.uuid4().hex
            self._active[token] = request
            return ResourceLease(token, request, self)

    def release(self, token: str) -> None:
        with self._lock:
            self._active.pop(token, None)

    def active_counts(self) -> dict[str, int]:
        with self._lock:
            active = tuple(self._active.values())
        return {
            "total": len(active),
            "light": sum(item.worker_class == "light" for item in active),
            "heavy": sum(item.worker_class in HEAVY_CLASSES for item in active),
            "browser": sum(item.worker_class == "browser" for item in active),
            "interactive": sum(item.interactive for item in active),
        }
