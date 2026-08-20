"""MAGI V3 lightweight control-core primitives.

Importing this package is deliberately side-effect free: no runtime directory is
created, no database is opened, and no socket or worker process is started.
"""

from .config import CoreSettings, ResourcePolicy, load_settings
from .ledger import JobLedger
from .quality_ledger import QualityOutcomeLedger, attest_release
from .resource import GlobalResourceGovernor
from .runtime import CoreRuntime

__all__ = [
    "CoreRuntime",
    "CoreSettings",
    "GlobalResourceGovernor",
    "JobLedger",
    "QualityOutcomeLedger",
    "ResourcePolicy",
    "load_settings",
    "attest_release",
]

__version__ = "0.1.0"
