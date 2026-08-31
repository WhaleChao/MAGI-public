#!/usr/bin/env python3
"""Generate or verify MAGI release supply-chain evidence."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

from magi_v3.supply_chain import (
    LOCK_SCHEMA,
    SupplyChainError,
    audit_runtime_install_policy,
    cyclonedx_sbom,
    installed_components,
    runtime_lock,
    scan_release_secrets,
    validate_vulnerability_receipt,
    validate_release_supply_chain_binding,
    verify_wheelhouse,
    wheelhouse_manifest,
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--wheelhouse-manifest", type=Path)
    parser.add_argument("--runtime-lock", type=Path)
    parser.add_argument("--vulnerability-receipt", type=Path)
    parser.add_argument("--base-runtime-manifest", type=Path)
    parser.add_argument("--strict-release", action="store_true")
    args = parser.parse_args()

    failures = []
    runtime_findings = audit_runtime_install_policy(args.source_root.resolve())
    if runtime_findings:
        failures.extend(runtime_findings)
    secret_findings = scan_release_secrets(args.release_root.resolve()) if args.release_root else []
    if secret_findings:
        failures.extend(secret_findings)

    components = installed_components()
    lock = runtime_lock(
        python_version=platform.python_version(),
        platform=platform.platform(),
        components=components,
    )
    sbom = cyclonedx_sbom(components=components, serial_seed=lock["packages_sha256"])
    if args.output_dir:
        _write(args.output_dir / "python-runtime-lock.json", lock)
        _write(args.output_dir / "sbom.cdx.json", sbom)
        if args.wheelhouse and not args.wheelhouse_manifest:
            _write(args.output_dir / "wheelhouse-manifest.json", wheelhouse_manifest(args.wheelhouse))
    if args.strict_release:
        required = {
            "release-root": args.release_root,
            "wheelhouse": args.wheelhouse,
            "wheelhouse-manifest": args.wheelhouse_manifest,
            "runtime-lock": args.runtime_lock,
            "vulnerability-receipt": args.vulnerability_receipt,
            "base-runtime-manifest": args.base_runtime_manifest,
        }
        failures.extend(f"missing_release_evidence:{name}" for name, value in required.items() if value is None)
    wheelhouse_verified = False
    runtime_lock_verified = False
    vulnerability_receipt_verified = False
    release_binding_verified = False
    if args.wheelhouse_manifest:
        try:
            if args.wheelhouse is None:
                raise SupplyChainError("wheelhouse path is required with its manifest")
            verify_wheelhouse(
                args.wheelhouse,
                json.loads(args.wheelhouse_manifest.read_text(encoding="utf-8")),
            )
            wheelhouse_verified = True
        except (OSError, json.JSONDecodeError, SupplyChainError) as exc:
            failures.append(f"release_evidence:wheelhouse:{exc}")
    if args.runtime_lock:
        try:
            expected_lock = json.loads(args.runtime_lock.read_text(encoding="utf-8"))
            if (
                not isinstance(expected_lock, dict)
                or expected_lock.get("schema") != LOCK_SCHEMA
                or expected_lock.get("packages_sha256") != lock["packages_sha256"]
            ):
                raise SupplyChainError("runtime lock does not match the bound interpreter")
            runtime_lock_verified = True
        except (OSError, json.JSONDecodeError, SupplyChainError) as exc:
            failures.append(f"release_evidence:runtime_lock:{exc}")
    if args.vulnerability_receipt:
        try:
            receipt = json.loads(args.vulnerability_receipt.read_text(encoding="utf-8"))
            validate_vulnerability_receipt(
                receipt,
                expected_packages_sha256=lock["packages_sha256"],
            )
            vulnerability_receipt_verified = True
        except (OSError, json.JSONDecodeError, SupplyChainError) as exc:
            failures.append(f"release_evidence:vulnerability_receipt:{exc}")
    if args.release_root:
        try:
            validate_release_supply_chain_binding(
                args.release_root,
                base_runtime_manifest=args.base_runtime_manifest,
            )
            release_binding_verified = True
        except (OSError, json.JSONDecodeError, SupplyChainError) as exc:
            failures.append(f"release_evidence:binding:{exc}")

    report = {
        "ok": not failures,
        "package_count": len(components),
        "packages_sha256": lock["packages_sha256"],
        "runtime_install_findings": runtime_findings,
        "secret_findings": secret_findings,
        "runtime_lock_verified": runtime_lock_verified,
        "vulnerability_receipt_verified": vulnerability_receipt_verified,
        "wheelhouse_verified": wheelhouse_verified,
        "release_binding_verified": release_binding_verified,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
