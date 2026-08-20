"""Explicit composition root for the V3 lightweight core."""

from __future__ import annotations

from dataclasses import dataclass

from .config import CoreSettings, load_settings
from .health import HealthService
from .instance import SingleActiveGuard
from .ledger import JobLedger
from .resource import GlobalResourceGovernor
from .supervisor import WorkerSupervisor


@dataclass(slots=True)
class CoreRuntime:
    settings: CoreSettings
    ledger: JobLedger
    governor: GlobalResourceGovernor
    supervisor: WorkerSupervisor
    health: HealthService
    active_guard: SingleActiveGuard

    @classmethod
    def build(cls, settings: CoreSettings | None = None) -> "CoreRuntime":
        """Compose objects without opening the database or creating paths."""

        configured = settings or load_settings()
        configured.validate()
        ledger = JobLedger(
            configured.resolved_ledger_path,
            busy_timeout_ms=configured.sqlite_busy_timeout_ms,
        )
        governor = GlobalResourceGovernor(configured.resource)
        guard = SingleActiveGuard(
            configured.host_active_lock_path,
            instance_id=configured.instance_id,
        )
        supervisor = WorkerSupervisor(governor)
        health = HealthService(
            ledger=ledger,
            governor=governor,
            state_dir=configured.state_dir,
            active_guard=guard,
        )
        return cls(configured, ledger, governor, supervisor, health, guard)

    def initialize(self) -> None:
        """Explicitly initialize durable state without becoming active."""

        self.ledger.initialize()

    def activate(self) -> None:
        """Initialize state and become the one active V3 core instance."""

        self.initialize()
        self.active_guard.acquire()

    def close(self) -> None:
        self.supervisor.shutdown(grace_sec=2.0)
        self.active_guard.release()

    def __enter__(self) -> "CoreRuntime":
        self.activate()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
