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
from datetime import datetime
from pathlib import Path
from typing import Any

MAGI_ROOT = Path(__file__).resolve().parents[2]


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


def expected_profile_now() -> str:
    now = datetime.now()
    minutes = now.hour * 60 + now.minute
    return "day" if 415 <= minutes < 1310 else "night"


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


def build_report(expect: str = "auto", *, require_aux: bool = True) -> ModelGateReport:
    expected = expected_profile_now() if expect == "auto" else expect
    probes = [probe_port(port) for port in (8080, 8081, 8082, 8083)]
    by_port = {p.port: p for p in probes}
    failures: list[str] = []
    warnings: list[str] = []

    if expected == "day":
        if not _has_keyword(by_port[8080], "e4b"):
            failures.append(f"8080 expected E4B, got {by_port[8080].model_id or by_port[8080].error or 'down'}")
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
        if not _has_keyword(by_port[8080], "26b"):
            failures.append(f"8080 expected 26B, got {by_port[8080].model_id or by_port[8080].error or 'down'}")
        if by_port[8082].ok or by_port[8083].ok:
            warnings.append("night profile has auxiliary models still online")

    active = active_profile()
    if active and active != expected and not (expected == "night" and active == "night-e4b-degraded"):
        failures.append(f"active_profile expected {expected}, got {active}")

    degraded = False
    degraded_reason = ""
    if expected == "day" and not failures and (not by_port[8082].ok or not by_port[8083].ok):
        degraded = True
        degraded_reason = "day_auxiliary_missing"
    if expected == "night" and _has_keyword(by_port[8080], "e4b"):
        degraded = True
        degraded_reason = "night_fell_back_to_e4b"

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
