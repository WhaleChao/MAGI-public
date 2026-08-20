"""Inert, signal-aware child used only by isolated LIVE validation."""

from __future__ import annotations

import os
import signal
import sys
import threading

from .service_manifest import assert_deployment_safety
from .service_runtime import ServiceRuntimeError


def main() -> int:
    try:
        assert_deployment_safety("isolated_live_validation", os.environ)
    except ServiceRuntimeError as exc:
        print(f"live validation probe blocked: {exc}", file=sys.stderr)
        return 2
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    try:
        while not stopped.wait(0.25):
            pass
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
