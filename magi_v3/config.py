"""Small, dependency-free configuration loader for the V3 core."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import ConfigurationError


_PREFIX = "MAGI_V3_"


def _default_state_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v3"


def _default_host_active_lock_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "active-release.lock"


def _as_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean, got {value!r}")


def _as_int(value: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _as_float(value: str, *, name: str, minimum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be numeric") from exc
    if parsed < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum:g}")
    return parsed


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    """Initial single-node resource limits, expressed in MiB."""

    max_light: int = 2
    max_heavy: int = 2
    max_browser: int = 1
    reserved_p0_light_slots: int = 1
    max_background_cpu_percent: float = 200.0
    interactive_reserve_mb: float = 8192.0
    total_footprint_soft_mb: float = 10240.0
    total_footprint_hard_mb: float = 12288.0
    metal_soft_mb: float = 6656.0
    metal_hard_mb: float = 8192.0
    min_memory_free_percent: float = 35.0
    max_swapout_delta_mb: float = 64.0

    def validate(self) -> None:
        if not 1 <= self.max_light <= 2:
            raise ConfigurationError("resource.max_light must be 1 or 2")
        if not 1 <= self.max_heavy <= 2:
            raise ConfigurationError("resource.max_heavy must be 1 or 2")
        if self.max_browser != 1:
            raise ConfigurationError("resource.max_browser must remain 1 in phase one")
        if not 0 <= self.reserved_p0_light_slots <= self.max_light:
            raise ConfigurationError("resource.reserved_p0_light_slots must fit inside max_light")
        if self.max_background_cpu_percent <= 0:
            raise ConfigurationError("resource.max_background_cpu_percent must be positive")
        if self.total_footprint_soft_mb > self.total_footprint_hard_mb:
            raise ConfigurationError("soft footprint limit cannot exceed hard limit")
        if self.metal_soft_mb > self.metal_hard_mb:
            raise ConfigurationError("soft Metal limit cannot exceed hard limit")
        if not 0.0 <= self.min_memory_free_percent <= 100.0:
            raise ConfigurationError("minimum memory-free percent must be in [0, 100]")


@dataclass(frozen=True, slots=True)
class CoreSettings:
    """Immutable settings with production binding disabled by default."""

    state_dir: Path = field(default_factory=_default_state_dir)
    ledger_path: Path | None = None
    host_active_lock_path: Path = field(default_factory=_default_host_active_lock_path)
    instance_id: str = "local-v3"
    bind_enabled: bool = False
    bind_host: str = "127.0.0.1"
    bind_port: int = 0
    sqlite_busy_timeout_ms: int = 5000
    default_lease_seconds: int = 60
    resource: ResourcePolicy = field(default_factory=ResourcePolicy)

    @property
    def resolved_ledger_path(self) -> Path:
        return self.ledger_path or self.state_dir / "ledger.sqlite3"

    def validate(self) -> None:
        if not self.instance_id.strip():
            raise ConfigurationError("instance_id cannot be empty")
        if not self.state_dir.is_absolute():
            raise ConfigurationError("state_dir must be absolute")
        if self.ledger_path is not None and not self.ledger_path.is_absolute():
            raise ConfigurationError("ledger_path must be absolute")
        if not self.host_active_lock_path.is_absolute():
            raise ConfigurationError("host_active_lock_path must be absolute")
        if self.host_active_lock_path.is_relative_to(self.state_dir):
            raise ConfigurationError("host_active_lock_path must be outside the version state directory")
        if self.bind_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ConfigurationError("phase-one core may bind only to loopback")
        if self.bind_enabled and self.bind_port == 0:
            raise ConfigurationError("an explicit non-zero port is required when binding is enabled")
        if not 0 <= self.bind_port <= 65535:
            raise ConfigurationError("bind_port must be in [0, 65535]")
        self.resource.validate()


def load_settings(environ: Mapping[str, str] | None = None) -> CoreSettings:
    """Load settings from a mapping without reading ``.env`` or touching disk."""

    env = os.environ if environ is None else environ
    state_dir = Path(env.get(f"{_PREFIX}STATE_DIR", str(_default_state_dir()))).expanduser().resolve()
    ledger_raw = env.get(f"{_PREFIX}LEDGER_PATH", "").strip()
    ledger_path = Path(ledger_raw).expanduser().resolve() if ledger_raw else None

    resource = ResourcePolicy(
        max_light=_as_int(env.get(f"{_PREFIX}MAX_LIGHT", "2"), name="MAX_LIGHT", minimum=1, maximum=2),
        max_heavy=_as_int(env.get(f"{_PREFIX}MAX_HEAVY", "2"), name="MAX_HEAVY", minimum=1, maximum=2),
        max_browser=_as_int(env.get(f"{_PREFIX}MAX_BROWSER", "1"), name="MAX_BROWSER", minimum=1, maximum=1),
        reserved_p0_light_slots=_as_int(
            env.get(f"{_PREFIX}RESERVED_P0_LIGHT_SLOTS", "1"),
            name="RESERVED_P0_LIGHT_SLOTS",
            minimum=0,
            maximum=2,
        ),
        max_background_cpu_percent=_as_float(
            env.get(f"{_PREFIX}MAX_BACKGROUND_CPU_PERCENT", "200"),
            name="MAX_BACKGROUND_CPU_PERCENT",
            minimum=1.0,
        ),
        interactive_reserve_mb=_as_float(
            env.get(f"{_PREFIX}INTERACTIVE_RESERVE_MB", "8192"),
            name="INTERACTIVE_RESERVE_MB",
            minimum=4096.0,
        ),
        total_footprint_soft_mb=_as_float(
            env.get(f"{_PREFIX}FOOTPRINT_SOFT_MB", "10240"),
            name="FOOTPRINT_SOFT_MB",
            minimum=512.0,
        ),
        total_footprint_hard_mb=_as_float(
            env.get(f"{_PREFIX}FOOTPRINT_HARD_MB", "12288"),
            name="FOOTPRINT_HARD_MB",
            minimum=512.0,
        ),
        metal_soft_mb=_as_float(
            env.get(f"{_PREFIX}METAL_SOFT_MB", "6656"),
            name="METAL_SOFT_MB",
            minimum=0.0,
        ),
        metal_hard_mb=_as_float(
            env.get(f"{_PREFIX}METAL_HARD_MB", "8192"),
            name="METAL_HARD_MB",
            minimum=0.0,
        ),
        min_memory_free_percent=_as_float(
            env.get(f"{_PREFIX}MIN_MEMORY_FREE_PERCENT", "35"),
            name="MIN_MEMORY_FREE_PERCENT",
            minimum=0.0,
        ),
        max_swapout_delta_mb=_as_float(
            env.get(f"{_PREFIX}MAX_SWAPOUT_DELTA_MB", "64"),
            name="MAX_SWAPOUT_DELTA_MB",
            minimum=0.0,
        ),
    )
    settings = CoreSettings(
        state_dir=state_dir,
        ledger_path=ledger_path,
        host_active_lock_path=Path(
            env.get(f"{_PREFIX}HOST_ACTIVE_LOCK_PATH", str(_default_host_active_lock_path()))
        ).expanduser().resolve(),
        instance_id=env.get(f"{_PREFIX}INSTANCE_ID", "local-v3").strip(),
        bind_enabled=_as_bool(env.get(f"{_PREFIX}BIND_ENABLED", "0"), name="BIND_ENABLED"),
        bind_host=env.get(f"{_PREFIX}BIND_HOST", "127.0.0.1").strip(),
        bind_port=_as_int(env.get(f"{_PREFIX}BIND_PORT", "0"), name="BIND_PORT", minimum=0, maximum=65535),
        sqlite_busy_timeout_ms=_as_int(
            env.get(f"{_PREFIX}SQLITE_BUSY_TIMEOUT_MS", "5000"),
            name="SQLITE_BUSY_TIMEOUT_MS",
            minimum=100,
            maximum=60000,
        ),
        default_lease_seconds=_as_int(
            env.get(f"{_PREFIX}DEFAULT_LEASE_SECONDS", "60"),
            name="DEFAULT_LEASE_SECONDS",
            minimum=5,
            maximum=86400,
        ),
        resource=resource,
    )
    settings.validate()
    return settings
