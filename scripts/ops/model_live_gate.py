#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live model topology gate for MAGI day/night operation."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from scripts.ops.omlx_profile_policy import (  # noqa: E402
    DAY_FALLBACK_MODEL_KEYWORD,
    DAY_MODEL_KEYWORD,
    NIGHT_FALLBACK_MODEL_KEYWORD,
    NIGHT_MODEL_KEYWORD,
    expected_profile_now as expected_omlx_profile_now,
)


@dataclass
class EndpointProbe:
    port: int
    ok: bool
    model_id: str = ""
    error: str = ""


@dataclass
class ModelGateReport:
    ok: bool
    expected_profile: str
    active_profile: str
    generated_at: str
    endpoints: list[EndpointProbe] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str = ""
    next_actions: list[str] = field(default_factory=list)
    restart_hint: str = ""
    profile_hint: str = ""


def expected_profile_now() -> str:
    profile, _keyword = expected_omlx_profile_now()
    return profile


def active_profile() -> str:
    try:
        return (Path.home() / ".omlx" / "active_profile").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def probe_port(port: int, timeout: float = 3.0) -> EndpointProbe:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        models = payload.get("data") if isinstance(payload, dict) else []
        model_id = ""
        if isinstance(models, list) and models:
            first = models[0]
            if isinstance(first, dict):
                model_id = str(first.get("id") or "")
        return EndpointProbe(port=port, ok=bool(model_id), model_id=model_id)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return EndpointProbe(port=port, ok=False, error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        return EndpointProbe(port=port, ok=False, error=f"{type(exc).__name__}: {exc}")


def _has_keyword(probe: EndpointProbe, keyword: str) -> bool:
    return probe.ok and keyword.lower() in probe.model_id.lower()


def _append_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


def _expected_keyword_for_profile(profile: str) -> str:
    return DAY_MODEL_KEYWORD if profile == "day" else NIGHT_MODEL_KEYWORD


def _failure_guidance(
    *,
    expected: str,
    active: str,
    by_port: dict[int, EndpointProbe],
    failures: list[str],
    require_aux: bool,
) -> tuple[list[str], str, str]:
    if not failures:
        return [], "", ""

    switch_script = "config/bin/omlx_switch_model.sh"
    expected_keyword = _expected_keyword_for_profile(expected)
    actions: list[str] = []
    main = by_port[8080]

    if not main.ok:
        _append_unique(
            actions,
            f"Restore primary oMLX 8080 with `{switch_script} auto`; expected {expected} / {expected_keyword.upper()}.",
        )
    elif not _has_keyword(main, expected_keyword):
        _append_unique(
            actions,
            f"Realign port 8080 with `{switch_script} {expected}`; it is serving {main.model_id or main.error or 'unknown'}.",
        )

    if any("active_profile" in item for item in failures):
        _append_unique(
            actions,
            f"Rewrite ~/.omlx/active_profile via `{switch_script} auto` or `{switch_script} {expected}`.",
        )

    if expected == "day" and require_aux:
        missing_aux = []
        if not _has_keyword(by_port[8082], "phi"):
            missing_aux.append("8082/Phi-4")
        if not _has_keyword(by_port[8083], "smol"):
            missing_aux.append("8083/SmolLM")
        if missing_aux:
            _append_unique(
                actions,
                f"Repair day auxiliary runtime(s) {', '.join(missing_aux)} with `{switch_script} day`.",
            )

    _append_unique(actions, "Rerun `scripts/ops/model_live_gate.py --expect auto --json` after repair.")

    profile_hint = (
        f"Expected profile={expected} ({expected_keyword.upper()} on 8080); "
        f"active_profile={active or 'missing'}."
    )
    if not main.ok:
        restart_hint = (
            "8080 is unreachable. Restart the oMLX runtime with "
            f"`{switch_script} auto`; if MAGI callers still show stale model state after 8080 is healthy, restart MAGI."
        )
    else:
        restart_hint = (
            "Model runtime is reachable but mismatched. Switch the oMLX profile first; restart MAGI only if callers keep "
            "cached/stale routing state after the gate passes."
        )
    return actions, restart_hint, profile_hint


def build_report(expect: str = "auto", *, require_aux: bool = True) -> ModelGateReport:
    expected = expected_profile_now() if expect == "auto" else expect
    probes = [probe_port(port) for port in (8080, 8081, 8082, 8083)]
    by_port = {p.port: p for p in probes}
    failures: list[str] = []
    warnings: list[str] = []

    if expected == "day":
        if _has_keyword(by_port[8080], DAY_MODEL_KEYWORD):
            pass
        elif _has_keyword(by_port[8080], DAY_FALLBACK_MODEL_KEYWORD):
            warnings.append(
                f"8080 is day fallback {DAY_FALLBACK_MODEL_KEYWORD.upper()}, "
                f"expected {DAY_MODEL_KEYWORD.upper()}"
            )
        else:
            failures.append(
                f"8080 expected {DAY_MODEL_KEYWORD.upper()}, "
                f"got {by_port[8080].model_id or by_port[8080].error or 'down'}"
            )
        if not _has_keyword(by_port[8081], "embed"):
            warnings.append(f"8081 embed not ready: {by_port[8081].model_id or by_port[8081].error or 'down'}")
        if require_aux:
            if not _has_keyword(by_port[8082], "phi"):
                failures.append(f"8082 expected Phi-4, got {by_port[8082].model_id or by_port[8082].error or 'down'}")
            if not _has_keyword(by_port[8083], "smol"):
                failures.append(f"8083 expected SmolLM, got {by_port[8083].model_id or by_port[8083].error or 'down'}")
        else:
            for port, keyword in ((8082, "phi"), (8083, "smol")):
                if not _has_keyword(by_port[port], keyword):
                    warnings.append(f"{port} auxiliary not ready")
    else:
        if _has_keyword(by_port[8080], NIGHT_MODEL_KEYWORD):
            pass
        elif _has_keyword(by_port[8080], NIGHT_FALLBACK_MODEL_KEYWORD):
            warnings.append(
                f"8080 is night fallback {NIGHT_FALLBACK_MODEL_KEYWORD.upper()}, "
                f"expected {NIGHT_MODEL_KEYWORD.upper()}"
            )
        else:
            failures.append(
                f"8080 expected {NIGHT_MODEL_KEYWORD.upper()}, "
                f"got {by_port[8080].model_id or by_port[8080].error or 'down'}"
            )
        if by_port[8082].ok or by_port[8083].ok:
            warnings.append("night profile has auxiliary models still online")

    active = active_profile()
    allowed_active = {expected}
    if expected == "day":
        allowed_active.add("day-e4b-degraded")
    if expected == "night":
        allowed_active.add("night-12b-degraded")
        allowed_active.add("night-e4b-degraded")
    if active and active not in allowed_active:
        failures.append(f"active_profile expected {expected}, got {active}")

    degraded = False
    degraded_reason = ""
    if (
        expected == "day"
        and DAY_FALLBACK_MODEL_KEYWORD.lower() != DAY_MODEL_KEYWORD.lower()
        and _has_keyword(by_port[8080], DAY_FALLBACK_MODEL_KEYWORD)
    ):
        degraded = True
        degraded_reason = "day_fell_back_to_e4b"
    if expected == "day" and not failures and (not by_port[8082].ok or not by_port[8083].ok):
        degraded = True
        degraded_reason = degraded_reason or "day_auxiliary_missing"
    if expected == "night" and _has_keyword(by_port[8080], NIGHT_FALLBACK_MODEL_KEYWORD):
        degraded = True
        degraded_reason = "night_fell_back_to_12b"
    elif expected == "night" and _has_keyword(by_port[8080], DAY_FALLBACK_MODEL_KEYWORD):
        degraded = True
        degraded_reason = "night_fell_back_to_e4b"

    next_actions, restart_hint, profile_hint = _failure_guidance(
        expected=expected,
        active=active,
        by_port=by_port,
        failures=failures,
        require_aux=require_aux,
    )

    return ModelGateReport(
        ok=not failures,
        expected_profile=expected,
        active_profile=active,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        endpoints=probes,
        failures=failures,
        warnings=warnings,
        degraded=degraded,
        degraded_reason=degraded_reason,
        next_actions=next_actions,
        restart_hint=restart_hint,
        profile_hint=profile_hint,
    )


def write_report(report: ModelGateReport, json_out: Path) -> None:
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    txt = json_out.with_suffix(".txt")
    lines = [
        f"MAGI model live gate: {'PASS' if report.ok else 'FAIL'}",
        f"expected={report.expected_profile} active={report.active_profile or '-'} generated={report.generated_at}",
    ]
    for probe in report.endpoints:
        lines.append(f"- {probe.port}: {'OK' if probe.ok else 'DOWN'} {probe.model_id or probe.error}")
    if report.warnings:
        lines.append("warnings: " + "; ".join(report.warnings))
    if report.failures:
        lines.append("failures: " + "; ".join(report.failures))
    if report.profile_hint:
        lines.append("profile_hint: " + report.profile_hint)
    if report.restart_hint:
        lines.append("restart_hint: " + report.restart_hint)
    if report.next_actions:
        lines.append("next_actions: " + "; ".join(report.next_actions))
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check live MAGI day/night model topology.")
    parser.add_argument("--expect", choices=["auto", "day", "night"], default="auto")
    parser.add_argument("--json-out", default=str(MAGI_ROOT / ".runtime" / "model_live_gate_latest.json"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-missing-aux", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(args.expect, require_aux=not args.allow_missing_aux)
    write_report(report, Path(args.json_out))
    payload = json.dumps(asdict(report), ensure_ascii=False, indent=2)
    if args.json:
        print(payload)
    else:
        print(payload)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
