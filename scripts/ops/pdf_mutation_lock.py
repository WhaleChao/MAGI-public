"""Shared lock for in-place PDF mutations.

PDF bookmark writers use a temp-save + replace or incremental save path.  The
operations are safe individually, but two background jobs targeting the same
batch can still interleave and overwrite each other's TOC updates.  This module
provides one common flock-backed domain lock for those in-place mutations.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from scripts.ops.background_task_locks import BackgroundLock, acquire_lock, lock_path

PDF_IN_PLACE_MUTATION_LOCK_NAME = "pdf_in_place_mutation"


class PdfMutationLockBusy(RuntimeError):
    """Raised when a non-blocking PDF mutation lock cannot be acquired."""

    def __init__(self, lock: BackgroundLock) -> None:
        self.lock = lock
        active = lock.active_owner or {}
        super().__init__(
            "pdf_in_place_mutation lock is already held"
            + (f" by pid {active.get('pid')}" if active.get("pid") else "")
        )


def pdf_in_place_mutation_lock_path() -> Path:
    return lock_path(PDF_IN_PLACE_MUTATION_LOCK_NAME)


def _owner_label(owner: str, pdf_path: str | Path | None) -> str:
    base = str(owner or "pdf-mutator").strip() or "pdf-mutator"
    if pdf_path is None:
        return base
    return f"{base}:{Path(pdf_path).name}"


@contextmanager
def pdf_in_place_mutation_lock(
    *,
    owner: str,
    pdf_path: str | Path | None = None,
    blocking: bool = True,
) -> Iterator[BackgroundLock]:
    """Hold the shared lock while mutating a PDF in place.

    ``blocking=True`` is the default for repair/batch jobs: they should wait
    rather than fail halfway through a large maintenance run.  Tests and probes
    can pass ``blocking=False`` to observe the active owner.
    """

    lock = acquire_lock(
        PDF_IN_PLACE_MUTATION_LOCK_NAME,
        owner=_owner_label(owner, pdf_path),
        kind="pdf_in_place_mutation",
        blocking=blocking,
    )
    if not lock.acquired:
        raise PdfMutationLockBusy(lock)
    try:
        yield lock
    finally:
        lock.release()
