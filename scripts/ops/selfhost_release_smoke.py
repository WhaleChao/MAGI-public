#!/usr/bin/env python3
"""Native, secret-free release lifecycle smoke test for MAGI self-host.

The check is safe to run in CI and on a publisher workstation.  It works only
inside a temporary directory and never installs a service, connects to a
customer database, or reads customer credentials.  Its purpose is to prove
that the exact distributable archive can be extracted, staged, hash-certified,
upgraded, rolled back, and can detect payload tampering on the current OS.
"""

from __future__ import annotations

import argparse
import json
import platform
import tempfile
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]

if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from magi_v3.selfhost import (  # noqa: E402
    activate_release,
    active_release,
    build_distribution_archive,
    build_service_plan,
    certify_active_release,
    default_config,
    default_layout,
    initialise_instance,
    rollback_release,
    stage_release,
    venv_python,
    write_launcher,
)


def _check(key: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"key": key, "ok": bool(ok), "detail": detail}


def run_smoke(source: Path = ROOT) -> dict[str, Any]:
    source = source.expanduser().resolve()
    system = platform.system()
    findings: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="magi-native-release-smoke-") as raw_tmp:
        tmp = Path(raw_tmp)
        archive_path = tmp / "MAGI-V3-selfhost.zip"
        archive = build_distribution_archive(source, archive_path)
        findings.append(_check(
            "secret_free_archive",
            archive.get("contains_secrets") is False
            and len(str(archive.get("archive_sha256") or "")) == 64,
            f"files={archive.get('file_count')} archive_sha256={archive.get('archive_sha256')}",
        ))

        extracted = tmp / "extracted"
        with zipfile.ZipFile(archive_path) as bundle:
            names = bundle.namelist()
            bundle.extractall(extracted)
            distribution = json.loads(bundle.read("MAGI-DISTRIBUTION.json"))
        forbidden = [
            name for name in names
            if name in {".env", "magi.env", "credentials.json", "token.json"}
            or name.startswith("tests/")
        ]
        findings.append(_check(
            "clean_extract",
            not forbidden
            and distribution.get("contains_secrets") is False
            and (extracted / "magi_v3" / "selfhost.py").is_file(),
            f"forbidden={len(forbidden)} content_sha256={distribution.get('content_sha256')}",
        ))

        env = {
            "HOME": str(tmp / "home"),
            "USERPROFILE": str(tmp / "home"),
            "LOCALAPPDATA": str(tmp / "local-app-data"),
            "MAGI_SELFHOST_HOME": str(tmp / "instance"),
        }
        layout = default_layout(system=system, home=tmp / "home", environ=env)
        config = default_config(layout=layout, source_root=extracted)
        initialise_instance(config)
        write_launcher(config)

        first = stage_release(extracted, config, release_id="smoke-r1")
        activate_release(config, "smoke-r1")
        first_certification = certify_active_release(config)
        findings.append(_check(
            "stage_and_certify",
            bool(first_certification.get("ok")),
            f"release=smoke-r1 tree_sha256={first.get('tree_sha256')}",
        ))

        second = stage_release(extracted, config, release_id="smoke-r2")
        activate_release(config, "smoke-r2")
        second_certification = certify_active_release(config)
        rollback = rollback_release(config)
        rolled_back = active_release(config) or {}
        findings.append(_check(
            "upgrade_and_rollback",
            bool(second_certification.get("ok"))
            and rollback.get("release_id") == "smoke-r1"
            and rolled_back.get("release_id") == "smoke-r1"
            and len(str(second.get("tree_sha256") or "")) == 64,
            "smoke-r2 was certified and the active marker returned to smoke-r1",
        ))

        active_root = Path(str(rolled_back["release_root"]))
        probe = active_root / "daemon.py"
        original = probe.read_bytes()
        probe.write_bytes(original + b"\n# tamper probe\n")
        tampered = certify_active_release(config)
        probe.write_bytes(original)
        restored = certify_active_release(config)
        findings.append(_check(
            "tamper_detection",
            not tampered.get("ok") and bool(restored.get("ok")),
            "payload mutation was rejected and original payload re-certified",
        ))

        service = build_service_plan(
            config,
            python_executable=venv_python(layout),
            launcher_path=layout.launcher_path,
        )
        native_service_ok = (
            (system == "Windows" and service.install and service.install[0][0].lower() == "schtasks")
            or (system == "Darwin" and service.artifact_bytes is not None and service.install and service.install[0][0] == "launchctl")
            or (system == "Linux" and service.install)
        )
        findings.append(_check(
            "native_service_plan",
            bool(native_service_ok),
            f"platform={system} service_id={service.service_id}",
        ))

    failed = [item for item in findings if not item["ok"]]
    return {
        "schema": "magi.selfhost.native-release-smoke/v1",
        "ok": not failed,
        "platform": system,
        "native_execution": True,
        "summary": {"pass": len(findings) - len(failed), "fail": len(failed)},
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    payload = run_smoke(args.source)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
