"""Stable exceptions exposed by the V3 core."""


class CoreError(RuntimeError):
    """Base class for expected V3 core failures."""


class ConfigurationError(CoreError, ValueError):
    """Configuration is missing or invalid."""


class LedgerError(CoreError):
    """Persistent job ledger operation failed."""


class JobNotFound(LedgerError, KeyError):
    """Requested job does not exist."""


class InvalidTransition(LedgerError, ValueError):
    """Requested job state transition is not allowed."""


class LeaseConflict(LedgerError):
    """Lease is missing, expired, or owned by another caller."""


class AdmissionDenied(CoreError):
    """Resource governor rejected a worker admission request."""


class SupervisorError(CoreError):
    """Worker supervisor could not safely manage an owned process."""


class WorkerAlreadyRunning(SupervisorError):
    """A job already has an active worker in this supervisor."""
