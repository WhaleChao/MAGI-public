"""Authoritative worker-reap soak metric derivation shared by compiler and gate."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


class WorkerSoakEvidenceError(ValueError):
    """Raised when worker-soak measurements are structurally untrustworthy."""


_INTEGER_FIELDS = (
    "cycles_requested",
    "cycles_completed",
    "process_groups_gone",
    "active_workers_after",
    "governor_slots_after",
    "fd_drift",
)


def summarize_worker_soak_measurements(
    measurements: Sequence[Mapping[str, Any]],
    *,
    cycles_per_pass: int = 100,
) -> dict[str, Any]:
    """Derive the release-gate metrics without assuming a legacy pass count."""

    if (
        type(cycles_per_pass) is not int
        or cycles_per_pass <= 0
        or not isinstance(measurements, Sequence)
        or isinstance(measurements, (str, bytes, bytearray))
        or not measurements
    ):
        raise WorkerSoakEvidenceError("worker soak measurement set is invalid")

    detached: list[dict[str, int]] = []
    for index, row in enumerate(measurements):
        if not isinstance(row, Mapping):
            raise WorkerSoakEvidenceError(
                f"worker soak row {index} must be an object"
            )
        normalized: dict[str, int] = {}
        for field in _INTEGER_FIELDS:
            value = row.get(field)
            if type(value) is not int or value < 0:
                raise WorkerSoakEvidenceError(
                    f"worker soak row {index} {field} must be a non-negative integer"
                )
            normalized[field] = value
        detached.append(normalized)

    return {
        "cycles": sum(row["cycles_completed"] for row in detached),
        "unreaped_workers": sum(
            abs(row["cycles_completed"] - row["process_groups_gone"])
            for row in detached
        ),
        "resource_baseline_restored": all(
            row["cycles_requested"] == cycles_per_pass
            and row["cycles_completed"] == cycles_per_pass
            and row["process_groups_gone"] == cycles_per_pass
            and row["active_workers_after"] == 0
            and row["governor_slots_after"] == 0
            and row["fd_drift"] <= 2
            for row in detached
        ),
    }
