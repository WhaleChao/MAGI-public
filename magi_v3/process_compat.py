"""Portable process-group helpers used by the V3 ownership fences."""

from __future__ import annotations

import os
import signal


def process_group(pid: int) -> int:
    getter = getattr(os, "getpgid", None)
    return int(getter(pid)) if getter is not None else int(pid)


def signal_group(group: int, signum: int | signal.Signals) -> None:
    sender = getattr(os, "killpg", None)
    if sender is not None:
        sender(group, signum)
    else:
        os.kill(group, signum)


def group_exists(group: int) -> bool:
    try:
        signal_group(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = ("group_exists", "process_group", "signal_group")
