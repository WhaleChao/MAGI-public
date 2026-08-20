"""Cheap control-core health checks with no model or browser imports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ledger import JobLedger
from .instance import SingleActiveGuard
from .resource import GlobalResourceGovernor
from .health_presentation import present_health


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: str
    ready: bool
    checked_at: str
    components: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["user_status"] = present_health(
            status=self.status,
            ready=self.ready,
            components=self.components,
        )
        return payload


class HealthService:
    """Reports process and ledger state without probing MLX/OCR/browser services."""

    def __init__(
        self,
        *,
        ledger: JobLedger,
        governor: GlobalResourceGovernor,
        state_dir: Path,
        active_guard: SingleActiveGuard,
    ) -> None:
        self.ledger = ledger
        self.governor = governor
        self.state_dir = state_dir
        self.active_guard = active_guard

    @staticmethod
    def liveness() -> HealthReport:
        return HealthReport(
            status="live",
            ready=True,
            checked_at=_now(),
            components={"process": "ok"},
        )

    def readiness(self) -> HealthReport:
        """Check only local control dependencies; never call model endpoints."""

        ledger_exists = self.ledger.path.is_file()
        ledger_ok = ledger_exists and self.ledger.ping()
        state_dir_ok = self.state_dir.is_dir()
        active_owner = self.active_guard.acquired
        ready = bool(ledger_ok and state_dir_ok and active_owner)
        return HealthReport(
            status="ready" if ready else "not_ready",
            ready=ready,
            checked_at=_now(),
            components={
                "ledger": "ok" if ledger_ok else "unavailable",
                "state_dir": "ok" if state_dir_ok else "unavailable",
                "active_release_lock": "owned" if active_owner else "not_owned",
                "workers": self.governor.active_counts(),
                "model_probe_performed": False,
            },
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
