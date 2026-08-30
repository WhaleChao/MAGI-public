"""Pure handoff workflows, fault simulation, and mutation authorization."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Literal

from .core import Assessment, CutoverError, Owner, Snapshot, assess_snapshot

Workflow = Literal["live-validation", "cutover", "rollback"]


@dataclass(frozen=True)
class Step:
    action: Literal["stop", "start", "verify"]
    release: str | None = None
    expected: str | None = None
    mode: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


WORKFLOWS: dict[str, tuple[Step, ...]] = {
    "live-validation": (
        Step("stop", "v2"),
        Step("verify", expected="zero"),
        Step("start", "v3", mode="isolated-validation"),
        Step("verify", expected="v3"),
        Step("stop", "v3"),
        Step("verify", expected="zero"),
        Step("start", "v2", mode="restore"),
        Step("verify", expected="v2"),
    ),
    "cutover": (
        Step("stop", "v2"),
        Step("verify", expected="zero"),
        Step("start", "v3", mode="primary"),
        Step("verify", expected="v3"),
    ),
    "rollback": (
        Step("stop", "v3"),
        Step("verify", expected="zero"),
        Step("start", "v2", mode="rollback"),
        Step("verify", expected="v2"),
    ),
}


def build_workflow(name: str) -> tuple[Step, ...]:
    try:
        steps = WORKFLOWS[name]
    except KeyError as exc:
        raise CutoverError(f"unknown workflow: {name}") from exc
    _validate_workflow(steps)
    return steps


def _validate_workflow(steps: Iterable[Step]) -> None:
    previous: Step | None = None
    for step in steps:
        if step.action == "start":
            if previous is None or previous.action != "verify" or previous.expected != "zero":
                raise CutoverError(f"unsafe workflow: start {step.release} is not preceded by verify zero")
        previous = step


def authorize_mutation(provided_token: str | None, environment_token: str | None = None) -> None:
    """Refuse every mutation while the phase-one cutover tool is read-only.

    The arguments remain only for source compatibility with future callers.
    In particular, a caller-supplied ``environment_token`` can never arm this
    implementation.
    """

    del provided_token, environment_token
    raise CutoverError("live mutation disabled: phase-one supports only plan, preflight, and simulate")


def _owner_templates(release: str) -> tuple[Owner, ...]:
    return (
        Owner(release, "scheduler", f"{release}:scheduler", "simulation", pid=1001),
        Owner(release, "writer", f"{release}:writer", "simulation", pid=1002),
        Owner(release, "browser", f"{release}:browser", "simulation", pid=1003),
        Owner(release, "model", f"{release}:model", "simulation", pid=1004),
        Owner(release, "port", f"{release}:5002", "simulation", pid=1005),
    )


def simulate_workflow(
    name: str,
    *,
    initial_release: str | None = None,
    residual_after_stop: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Execute a workflow against an in-memory ownership model only."""

    steps = build_workflow(name)
    if initial_release is None:
        initial_release = "v3" if name == "rollback" else "v2"
    owners = list(_owner_templates(initial_release))
    residuals = residual_after_stop or {}
    events: list[dict[str, Any]] = []
    failed = False

    for index, step in enumerate(steps, start=1):
        if step.action == "stop":
            keep_domains = set(residuals.get(str(step.release), ()))
            owners = [
                owner
                for owner in owners
                if owner.release != step.release or owner.domain in keep_domains
            ]
            events.append({"step": index, **step.to_dict(), "ok": True, "owners": len(owners)})
            continue
        if step.action == "start":
            zero = assess_snapshot(
                Snapshot(tuple(owners), coverage=frozenset({"process", "pidfile", "port", "launchd", "ownership"})),
                expected="zero",
            )
            if not zero.go:
                events.append({"step": index, **step.to_dict(), "ok": False, "reasons": list(zero.reasons)})
                failed = True
                break
            owners.extend(_owner_templates(str(step.release)))
            events.append({"step": index, **step.to_dict(), "ok": True, "owners": len(owners)})
            continue
        assessment = assess_snapshot(
            Snapshot(tuple(owners), coverage=frozenset({"process", "pidfile", "port", "launchd", "ownership"})),
            expected=step.expected,  # type: ignore[arg-type]
        )
        events.append(
            {"step": index, **step.to_dict(), "ok": assessment.go, "assessment": assessment.to_dict()}
        )
        if not assessment.go:
            failed = True
            break

    final_assessment: Assessment = assess_snapshot(
        Snapshot(tuple(owners), coverage=frozenset({"process", "pidfile", "port", "launchd", "ownership"}))
    )
    return {
        "workflow": name,
        "simulation_only": True,
        "ok": not failed,
        "events": events,
        "final": final_assessment.to_dict(),
    }
