"""Pure single-active ownership rules and gate loading.

The cutover tooling deliberately treats missing or ambiguous evidence as an
unsafe state.  The live probe is kept in :mod:`probe`; the rules in this module
are deterministic and can be fault-injected without touching launchd.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ReleaseName = Literal["v2", "v3"]
ExpectedState = Literal["any", "zero", "v2", "v3"]
SINGLETON_DOMAINS = frozenset(
    {
        "scheduler",
        "writer",
        "browser",
        "model",
        "ingress",
        "gateway",
        "webhook",
        "discord_consumer",
        "file_watcher",
        "notification_sender",
    }
)


class CutoverError(RuntimeError):
    """Base exception for a fail-closed cutover refusal."""


class GateConfigError(CutoverError):
    """The gate file is missing or does not contain required safety policy."""


@dataclass(frozen=True)
class Owner:
    """One active or potentially active release-specific owner."""

    release: ReleaseName | None
    domain: str
    owner_id: str
    source: str
    pid: int | None = None
    root: str = ""
    namespace: str = ""
    active: bool = True
    ambiguous: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Snapshot:
    """Normalized result of all read-only ownership probes."""

    owners: tuple[Owner, ...] = ()
    probe_errors: tuple[str, ...] = ()
    coverage: frozenset[str] = frozenset()
    observed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "coverage": sorted(self.coverage),
            "probe_errors": list(self.probe_errors),
            "owners": [owner.to_dict() for owner in self.owners],
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class Assessment:
    go: bool
    state: str
    active_releases: tuple[str, ...]
    reasons: tuple[str, ...]
    domain_owners: dict[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess_cutover_window(
    window: dict[str, Any],
    *,
    timezone_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate the configured maintenance window using an explicit timezone.

    This is a hard cutover gate, not a scheduling hint.  End time is exclusive
    and overnight windows (for example 23:00-02:00) are supported.  A release
    may further bind the window to one or more explicit local calendar dates;
    this permits a narrowly-scoped, operator-authorized maintenance window
    without turning a one-off exception into a permanent all-day bypass.
    """

    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise GateConfigError(f"invalid cutover timezone: {timezone_name!r}") from exc

    def parse_clock(field: str) -> time:
        value = window.get(field)
        if not isinstance(value, str):
            raise GateConfigError(f"cutover window.{field} must be HH:MM")
        try:
            return datetime.strptime(value, "%H:%M").time()
        except ValueError as exc:
            raise GateConfigError(f"cutover window.{field} must be HH:MM") from exc

    start = parse_clock("start")
    end = parse_clock("end")
    if start == end:
        raise GateConfigError("cutover window start and end must differ")
    observed = now or datetime.now(zone)
    if observed.tzinfo is None:
        raise GateConfigError("cutover window clock must be timezone-aware")
    local = observed.astimezone(zone)
    current = local.time().replace(tzinfo=None)
    inside_clock = start <= current < end if start < end else current >= start or current < end
    allowed_dates_value = window.get("allowed_local_dates")
    allowed_dates: tuple[str, ...] = ()
    if allowed_dates_value is not None:
        if (
            not isinstance(allowed_dates_value, list)
            or not allowed_dates_value
            or any(not isinstance(value, str) for value in allowed_dates_value)
        ):
            raise GateConfigError(
                "cutover window.allowed_local_dates must be a non-empty list of YYYY-MM-DD"
            )
        parsed_dates: list[str] = []
        for value in allowed_dates_value:
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise GateConfigError(
                    "cutover window.allowed_local_dates must contain YYYY-MM-DD"
                ) from exc
            if parsed.isoformat() != value:
                raise GateConfigError(
                    "cutover window.allowed_local_dates must contain canonical YYYY-MM-DD"
                )
            parsed_dates.append(value)
        if len(set(parsed_dates)) != len(parsed_dates):
            raise GateConfigError("cutover window.allowed_local_dates contains duplicates")
        allowed_dates = tuple(parsed_dates)
    date_allowed = not allowed_dates or local.date().isoformat() in allowed_dates
    inside = inside_clock and date_allowed
    return {
        "timezone": timezone_name,
        "start": window["start"],
        "end": window["end"],
        "allowed_local_dates": list(allowed_dates),
        "observed_at": local.isoformat(timespec="seconds"),
        "within_window": inside,
        "reason": "" if inside else "outside_cutover_window",
    }


def assess_absolute_window(
    window: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate a one-day, daylight-only cutover authorization window.

    Unlike :func:`assess_cutover_window`, this is deliberately not a recurring
    maintenance window.  The local date is part of the signed release policy,
    so a later day cannot accidentally inherit an earlier authorization.  The
    window is start-inclusive/end-exclusive and may only be between 06:00 and
    22:00 local Asia/Taipei time.  Callers must pass the release-bound policy;
    command line or environment values are intentionally not accepted here.
    """

    if not isinstance(window, dict):
        raise GateConfigError("conditional daytime window must be an object")
    timezone_name = window.get("timezone")
    if timezone_name != "Asia/Taipei":
        raise GateConfigError("conditional daytime window requires Asia/Taipei timezone")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise GateConfigError(f"invalid cutover timezone: {timezone_name!r}") from exc
    def parse_instant(field: str) -> datetime:
        value = window.get(field)
        if not isinstance(value, str):
            raise GateConfigError(f"conditional daytime window.{field} must be ISO-8601")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise GateConfigError(f"conditional daytime window.{field} must be ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise GateConfigError(f"conditional daytime window.{field} must be timezone-aware")
        local = parsed.astimezone(zone)
        if local.isoformat(timespec="seconds") != value:
            raise GateConfigError(
                f"conditional daytime window.{field} must be canonical Asia/Taipei ISO-8601"
            )
        return local

    start_at = parse_instant("starts_at")
    end_at = parse_instant("ends_at")
    if start_at.date() != end_at.date():
        raise GateConfigError("conditional daytime window must be same-day")
    start = start_at.time().replace(tzinfo=None)
    end = end_at.time().replace(tzinfo=None)
    earliest, latest = time(6, 0), time(22, 0)
    if not earliest <= start < end <= latest:
        raise GateConfigError(
            "conditional daytime window must be same-day and within 06:00-22:00 Asia/Taipei"
        )

    observed = now or datetime.now(zone)
    if observed.tzinfo is None:
        raise GateConfigError("conditional daytime window clock must be timezone-aware")
    local = observed.astimezone(zone)
    inside = start_at <= local < end_at
    return {
        "kind": "conditional_daytime",
        "timezone": timezone_name,
        "starts_at": window["starts_at"],
        "ends_at": window["ends_at"],
        "observed_at": local.isoformat(timespec="seconds"),
        "within_window": inside,
        "reason": "" if inside else "outside_conditional_daytime_window",
    }


def load_gate_config(path: str | Path) -> dict[str, Any]:
    """Load and minimally validate the repository cutover gate file."""

    gate_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(gate_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GateConfigError(f"gate file not found: {gate_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GateConfigError(f"gate file unreadable: {gate_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GateConfigError("gate file root must be an object")
    if payload.get("schema_version") != 1:
        raise GateConfigError("unsupported gate schema_version")
    window = payload.get("window")
    if not isinstance(window, dict) or not window.get("start") or not window.get("end"):
        raise GateConfigError("gate file must define window.start and window.end")
    conditional_daytime_window = payload.get("conditional_daytime_window")
    if conditional_daytime_window is not None:
        # Validate the immutable policy at load time.  Its actual result is
        # intentionally ignored here because it is assessed again at cutover.
        assess_absolute_window(conditional_daytime_window)
    no_go = payload.get("automatic_no_go")
    required = payload.get("required_evidence")
    if not isinstance(no_go, list) or not isinstance(required, list):
        raise GateConfigError("gate file must define automatic_no_go and required_evidence lists")
    if not required or not all(isinstance(item, str) and item for item in required):
        raise GateConfigError("gate file required_evidence must be a non-empty list of names")
    source_contract = payload.get("source_contract")
    if not isinstance(source_contract, dict):
        raise GateConfigError("gate file must define a source_contract object")
    legacy_v2 = source_contract.get("legacy_v2_validation") != "disabled"
    mandatory_no_go = {"more_than_one_writer_or_scheduler_owner"}
    if legacy_v2:
        mandatory_no_go.update(
            {
                "v2_process_or_release_owner_still_active_before_v3_start",
                "v2_port_scheduler_writer_or_model_owner_not_released",
            }
        )
    else:
        mandatory_no_go.update(
            {
                "previous_v3_release_owner_not_released",
                "current_v3_or_previous_v3_rollback_artifact_not_ready",
            }
        )
    missing = sorted(mandatory_no_go - {str(item) for item in no_go})
    if missing:
        raise GateConfigError(f"gate file is missing single-active no-go rules: {', '.join(missing)}")
    return payload


def _dedupe_active(owners: Iterable[Owner]) -> tuple[Owner, ...]:
    unique: dict[tuple[str | None, str, str], Owner] = {}
    for owner in owners:
        if not owner.active:
            continue
        key = (owner.release, owner.domain, owner.owner_id)
        previous = unique.get(key)
        if previous is None or (previous.ambiguous and not owner.ambiguous):
            unique[key] = owner
    return tuple(unique.values())


def assess_snapshot(
    snapshot: Snapshot,
    *,
    expected: ExpectedState = "any",
    required_coverage: Iterable[str] = ("process", "pidfile", "port", "launchd", "ownership"),
) -> Assessment:
    """Return a fail-closed single-active assessment.

    A release is active if any normalized release-specific owner is active.
    Multiple process records belonging to one process group should share an
    ``owner_id`` so browser children do not look like multiple browser owners.
    """

    reasons: list[str] = []
    missing_coverage = sorted(set(required_coverage) - set(snapshot.coverage))
    if missing_coverage:
        reasons.append(f"probe coverage missing: {', '.join(missing_coverage)}")
    reasons.extend(f"probe error: {item}" for item in snapshot.probe_errors)

    owners = _dedupe_active(snapshot.owners)
    ambiguous = [owner for owner in owners if owner.ambiguous]
    for owner in ambiguous:
        reasons.append(
            f"unclassified active owner domain={owner.domain} owner={owner.owner_id} source={owner.source}"
        )

    active_releases = tuple(sorted({str(owner.release) for owner in owners if owner.release in {"v2", "v3"}}))
    if len(active_releases) > 1:
        reasons.append(f"simultaneous active releases: {', '.join(active_releases)}")

    domain_owners: dict[str, tuple[str, ...]] = {}
    singleton_domains = SINGLETON_DOMAINS | {
        owner.domain for owner in owners if owner.domain.startswith("model_host_")
    }
    for domain in sorted(singleton_domains):
        def logical_identity(owner: Owner) -> str:
            if owner.source == "process" and owner.owner_id.startswith("tree:"):
                return f"{owner.release or 'unknown'}:pid:{owner.owner_id.removeprefix('tree:')}"
            return f"{owner.release or 'unknown'}:{'pid:' + str(owner.pid) if owner.pid else owner.owner_id}"

        identities = sorted(
            {
                logical_identity(owner)
                for owner in owners
                if owner.domain == domain
            }
        )
        domain_owners[domain] = tuple(identities)
        if len(identities) > 1:
            reasons.append(f"multiple {domain} owners: {', '.join(identities)}")

    if not active_releases:
        state = "quiescent"
    elif active_releases == ("v2",):
        state = "v2_active"
    elif active_releases == ("v3",):
        state = "v3_active"
    else:
        state = "unsafe_mixed"

    expected_releases = {"zero": (), "v2": ("v2",), "v3": ("v3",)}
    if expected != "any" and active_releases != expected_releases[expected]:
        reasons.append(
            f"expected {expected} ownership, observed {','.join(active_releases) if active_releases else 'zero'}"
        )
    if expected == "zero":
        residual_owners = [
            owner for owner in owners if owner.release in {"v2", "v3"} or owner.ambiguous
        ]
        if residual_owners:
            reasons.append(f"residual owners remain after stop: {len(residual_owners)}")

    return Assessment(
        go=not reasons,
        state=state,
        active_releases=active_releases,
        reasons=tuple(dict.fromkeys(reasons)),
        domain_owners=domain_owners,
    )
