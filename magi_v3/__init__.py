"""MAGI V3 lightweight control-core primitives.

Importing this package is deliberately side-effect free: no runtime directory is
created, no database is opened, and no socket or worker process is started.
Public control-core symbols are resolved lazily so importing a narrow submodule
does not pull telemetry, networking, databases, or workers into the process.
"""

from importlib import import_module

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

_LAZY_EXPORTS = {
    "CoreSettings": (".config", "CoreSettings"),
    "ResourcePolicy": (".config", "ResourcePolicy"),
    "load_settings": (".config", "load_settings"),
    "JobLedger": (".ledger", "JobLedger"),
    "QualityOutcomeLedger": (".quality_ledger", "QualityOutcomeLedger"),
    "attest_release": (".quality_ledger", "attest_release"),
    "GlobalResourceGovernor": (".resource", "GlobalResourceGovernor"),
    "CoreRuntime": (".runtime", "CoreRuntime"),
}


def __getattr__(name: str):
    binding = _LAZY_EXPORTS.get(name)
    if binding is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = binding
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_EXPORTS})
