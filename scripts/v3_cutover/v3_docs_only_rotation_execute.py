#!/usr/bin/env python3
"""Execute a V3 rotation whose immutable delta is documentation-only.

The normal V3 rotation intentionally binds every release to a complete formal
campaign.  Re-running that campaign after changing only authenticated manual
assets adds risk without exercising new production code.  This wrapper permits
one narrow carry-forward: it proves every non-documentation release member is
byte-identical to the active predecessor, runs the sealed manual regression
set, re-evaluates the predecessor's complete 14/14 campaign, and then delegates
all mutation, ownership, readiness, snapshot, and rollback work to the normal
rotation executor.

Any unexpected path, manifest drift, failed focused test, privacy violation,
or stale/invalid predecessor evidence fails before the first service stop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v3_cutover import v3_rotation_execute as rotation
from scripts.v3_cutover.activation import active_release_marker
from scripts.v3_cutover.core import CutoverError
from scripts.v3_release_gate import evaluate_evidence


SCHEMA = "magi.v3.docs-only-promotion/v1"
FOCUSED_SCHEMA = "magi.v3.docs-only-focused-validation/v1"
ALLOWED_EXACT = frozenset(
    {
        "docs/architecture/v3/V3_IMPLEMENTATION_STATUS.md",
        "scripts/docs/build_magi_encyclopedia.py",
        "tests/test_web_information_architecture.py",
    }
)
MANUAL_PREFIX = "magi_v3/manual_assets/"
MANUAL_NAMES = frozenset(
    {
        "MAGI_V3_維修百科全書_rc643.html",
        "MAGI_V3_維修百科全書_rc643.pdf",
        "MAGI_V3_維修百科全書_rc643.md",
        "MAGI_V3_原始碼索引_rc643.json",
    }
)
FOCUSED_TESTS = (
    "tests/test_dashboard_pages_blueprint.py",
    "tests/test_web_information_architecture.py",
    "tests/v3/test_manual_skill_mutable_state_isolation.py",
    "tests/v3/test_generated_implementation_status.py",
)
PASS_RE = re.compile(r"(?P<passed>[0-9]+) passed(?:, (?P<skipped>[0-9]+) skipped)?")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _manifest(deployment: rotation.BoundDeployment) -> dict[str, Any]:
    raw = rotation._safe_file_bytes(  # noqa: SLF001 - same trusted cutover package
        deployment.release_manifest.path,
        "release manifest",
        maximum=16 * 1024 * 1024,
    )
    return rotation._load_json_bytes(raw, "release manifest")  # noqa: SLF001


def _inventory(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = document.get("files")
    if not isinstance(rows, list) or not rows:
        raise CutoverError("docs-only release inventory is missing")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "sha256", "size", "mode"}
            or not isinstance(row.get("path"), str)
            or row["path"] in result
        ):
            raise CutoverError("docs-only release inventory row is invalid")
        result[row["path"]] = dict(row)
    return result


def _allowed(path: str) -> bool:
    if path in ALLOWED_EXACT:
        return True
    if not path.startswith(MANUAL_PREFIX):
        return False
    name = path.removeprefix(MANUAL_PREFIX)
    return "/" not in name and bool(
        re.fullmatch(r"MAGI_V3_(?:維修百科全書|原始碼索引)_rc[0-9]+\.(?:html|pdf|md|json)", name)
    )


def build_docs_only_impact(
    previous: rotation.BoundDeployment,
    candidate: rotation.BoundDeployment,
) -> dict[str, Any]:
    """Prove that candidate runtime code/config is byte-identical."""

    before = _manifest(previous)
    after = _manifest(candidate)
    before_rows = _inventory(before)
    after_rows = _inventory(after)
    changed: list[dict[str, Any]] = []
    unchanged = 0
    for path in sorted(set(before_rows) | set(after_rows)):
        old = before_rows.get(path)
        new = after_rows.get(path)
        if old == new:
            unchanged += 1
            continue
        if not _allowed(path):
            raise CutoverError(f"docs-only candidate changed operational member: {path}")
        changed.append(
            {
                "path": path,
                "change": "added" if old is None else "removed" if new is None else "modified",
                "before_sha256": None if old is None else old["sha256"],
                "after_sha256": None if new is None else new["sha256"],
            }
        )
    if not changed:
        raise CutoverError("docs-only candidate has no documentation delta")
    candidate_manual = {
        path.removeprefix(MANUAL_PREFIX)
        for path in after_rows
        if path.startswith(MANUAL_PREFIX)
    }
    if candidate_manual != MANUAL_NAMES:
        raise CutoverError("docs-only candidate manual allowlist is not exact RC643")
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "previous_release_id": previous.release_id,
        "previous_release_sha256": previous.release_sha,
        "previous_release_manifest_sha256": previous.release_manifest.sha256,
        "candidate_release_id": candidate.release_id,
        "candidate_release_sha256": candidate.release_sha,
        "candidate_release_manifest_sha256": candidate.release_manifest.sha256,
        "unchanged_member_count": unchanged,
        "changed_member_count": len(changed),
        "changed_members": changed,
        "operational_members_byte_identical": True,
        "manual_asset_names": sorted(candidate_manual),
    }
    payload["impact_sha256"] = _digest(_canonical(payload))
    return payload


def _privacy(document: Mapping[str, Any]) -> dict[str, Any]:
    value = document.get("privacy_audit")
    if (
        not isinstance(value, dict)
        or value.get("status") != "passed"
        or value.get("violations") != 0
        or value.get("content_in_evidence") is not False
    ):
        raise CutoverError("docs-only candidate privacy audit is not exact pass")
    return dict(value)


def _manual_hashes(candidate: rotation.BoundDeployment) -> dict[str, str]:
    rows = _inventory(_manifest(candidate))
    result: dict[str, str] = {}
    for name in sorted(MANUAL_NAMES):
        path = MANUAL_PREFIX + name
        row = rows.get(path)
        if row is None:
            raise CutoverError(f"docs-only candidate manual asset is missing: {name}")
        result[name] = str(row["sha256"])
    markdown = candidate.release_root / MANUAL_PREFIX / "MAGI_V3_維修百科全書_rc643.md"
    text = markdown.read_text(encoding="utf-8")
    for required in (
        "v3-20260831-rc643-r75-hotfix7-r1",
        "14,235,019,434 bytes",
        "153 個 superseded workspace 項目",
    ):
        if required not in text:
            raise CutoverError(f"docs-only manual metadata is incomplete: {required}")
    if "v3-20260830-rc643-r75-hotfix3" in text:
        raise CutoverError("docs-only manual retained the stale hotfix3 identity")
    return result


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    target = path.expanduser()
    if not target.is_absolute() or target.resolve(strict=False) != target or target.exists():
        raise CutoverError("focused validation receipt output must be a new canonical absolute path")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_canonical(payload))
        handle.flush()
        os.fsync(handle.fileno())


def generate_focused_receipt(
    *,
    previous: rotation.BoundDeployment,
    candidate: rotation.BoundDeployment,
    python_runtime: Path,
    output: Path,
) -> dict[str, Any]:
    impact = build_docs_only_impact(previous, candidate)
    runtime = python_runtime.expanduser()
    deployment_document = rotation._load_json_bytes(  # noqa: SLF001
        rotation._safe_file_bytes(candidate.manifest.path, "candidate deployment manifest"),  # noqa: SLF001
        "candidate deployment manifest",
    )
    external_inputs = deployment_document.get("external_inputs")
    if (
        not runtime.is_absolute()
        or not runtime.is_file()
        or not os.access(runtime, os.X_OK)
        or not isinstance(external_inputs, dict)
        or external_inputs.get("python_runtime") != str(runtime)
        or external_inputs.get("python_runtime_sha256") != rotation._sha256(runtime)  # noqa: SLF001
    ):
        raise CutoverError("focused validation Python runtime is not deployment-bound")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("MAGI_V3_")
    }
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": "/dev/null",
            "MAGI_ROOT": str(candidate.release_root),
        }
    )
    command = [
        str(runtime),
        "-B",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *FOCUSED_TESTS,
    ]
    completed = subprocess.run(
        command,
        cwd=candidate.release_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    combined = (completed.stdout or "") + "\n" + (completed.stderr or "")
    match = PASS_RE.search(combined)
    passed = int(match.group("passed")) if match else -1
    skipped = int(match.group("skipped") or 0) if match else -1
    if completed.returncode != 0 or passed != 46 or skipped != 0:
        raise CutoverError("sealed docs-only focused regression is not exact 46/46")
    candidate_manifest = _manifest(candidate)
    payload: dict[str, Any] = {
        "schema": FOCUSED_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "previous_release_id": previous.release_id,
        "candidate_release_id": candidate.release_id,
        "candidate_release_sha256": candidate.release_sha,
        "candidate_release_manifest_sha256": candidate.release_manifest.sha256,
        "impact_sha256": impact["impact_sha256"],
        "selected_tests": list(FOCUSED_TESTS),
        "passed": passed,
        "skipped": skipped,
        "returncode": completed.returncode,
        "test_output_sha256": _digest(combined.encode("utf-8")),
        "privacy_audit": _privacy(candidate_manifest),
        "manual_asset_sha256": _manual_hashes(candidate),
    }
    payload["receipt_sha256"] = _digest(_canonical(payload))
    _write_exclusive(output, payload)
    return payload


def validate_focused_receipt(
    path: Path,
    *,
    previous: rotation.BoundDeployment,
    candidate: rotation.BoundDeployment,
    impact: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    raw = rotation._safe_file_bytes(path, "focused validation receipt", maximum=2 * 1024 * 1024)  # noqa: SLF001
    document = rotation._load_json_bytes(raw, "focused validation receipt")  # noqa: SLF001
    claimed = document.pop("receipt_sha256", None)
    expected = _digest(_canonical(document))
    document["receipt_sha256"] = claimed
    if (
        claimed != expected
        or document.get("schema") != FOCUSED_SCHEMA
        or document.get("previous_release_id") != previous.release_id
        or document.get("candidate_release_id") != candidate.release_id
        or document.get("candidate_release_sha256") != candidate.release_sha
        or document.get("candidate_release_manifest_sha256") != candidate.release_manifest.sha256
        or document.get("impact_sha256") != impact.get("impact_sha256")
        or document.get("selected_tests") != list(FOCUSED_TESTS)
        or document.get("passed") != 46
        or document.get("skipped") != 0
        or document.get("returncode") != 0
        or document.get("manual_asset_sha256") != _manual_hashes(candidate)
        or document.get("privacy_audit") != _privacy(_manifest(candidate))
    ):
        raise CutoverError("focused validation receipt is invalid or drifted")
    return document, _digest(raw)


class DocsOnlyRotationExecutor(rotation.V3RotationExecutor):
    def __init__(self, *, focused_receipt: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.focused_receipt = focused_receipt

    def _verify_gate(self, candidate: rotation.BoundDeployment) -> Mapping[str, Any]:
        previous = rotation.load_bound_deployment(self.previous_root)
        impact = build_docs_only_impact(previous, candidate)
        focused, focused_sha = validate_focused_receipt(
            self.focused_receipt,
            previous=previous,
            candidate=candidate,
            impact=impact,
        )
        config_raw = rotation._safe_file_bytes(self.gate_config, "release gate configuration")  # noqa: SLF001
        if _digest(config_raw) != self.gate_config_sha256:
            raise CutoverError("release gate configuration SHA-256 drifted")
        config = rotation._load_json_bytes(config_raw, "release gate configuration")  # noqa: SLF001
        report = evaluate_evidence(
            config,
            self.evidence_dir,
            expected_context={
                "campaign_id": self.campaign_id,
                "release_sha": previous.release_sha,
                "hardware_id": self.hardware_id,
                "gate_config_sha256": self.gate_config_sha256,
            },
            now=self.clock(),
        )
        required = config.get("required_evidence")
        if (
            report.get("decision") != "GO"
            or not isinstance(required, list)
            or report.get("required_count") != len(required)
            or report.get("passed_count") != len(required)
            or report.get("missing") != []
            or report.get("failed") != []
            or report.get("invalid") != {}
            or "human_go_approval_recorded" not in report.get("passed", [])
        ):
            raise CutoverError("predecessor full release gate is not exact GO")
        self._event(
            "release_gate_carried_forward",
            decision="GO",
            passed_count=len(required),
            predecessor_release_id=previous.release_id,
            predecessor_release_sha256=previous.release_sha,
            candidate_release_id=candidate.release_id,
            candidate_release_sha256=candidate.release_sha,
            docs_only_impact_sha256=impact["impact_sha256"],
            focused_receipt_sha256=focused_sha,
            focused_passed=focused["passed"],
        )
        return {
            **report,
            "carried_forward_from_release_id": previous.release_id,
            "candidate_release_id": candidate.release_id,
            "docs_only_impact": impact,
            "focused_receipt_sha256": focused_sha,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute a fail-closed docs-only V3 production rotation."
    )
    parser.add_argument("--previous-deploy", type=Path, required=True)
    parser.add_argument("--candidate-deploy", type=Path, required=True)
    parser.add_argument("--rollback-deploy", type=Path, required=True)
    parser.add_argument("--state-parent", type=Path, required=True)
    parser.add_argument("--launchagents-directory", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--gate-config", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config-sha256", required=True)
    parser.add_argument("--rollback-snapshot-directory", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--python-runtime", type=Path, required=True)
    parser.add_argument("--focused-receipt", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        previous = rotation.load_bound_deployment(args.previous_deploy)
        candidate = rotation.load_bound_deployment(args.candidate_deploy)
        if not args.focused_receipt.exists():
            generate_focused_receipt(
                previous=previous,
                candidate=candidate,
                python_runtime=args.python_runtime,
                output=args.focused_receipt,
            )
        executor = DocsOnlyRotationExecutor(
            previous_deploy_root=args.previous_deploy,
            candidate_deploy_root=args.candidate_deploy,
            rollback_deploy_root=args.rollback_deploy,
            state_parent=args.state_parent,
            launchagents_directory=args.launchagents_directory,
            evidence_dir=args.evidence_dir,
            gate_config=args.gate_config,
            campaign_id=args.campaign_id,
            hardware_id=args.hardware_id,
            gate_config_sha256=args.gate_config_sha256,
            rollback_snapshot_directory=args.rollback_snapshot_directory,
            report_output=args.report_output,
            focused_receipt=args.focused_receipt,
        )
        if args.verify_only:
            rollback_deployment = rotation.load_bound_deployment(args.rollback_deploy)
            executor._verify_deployment_relationships(previous, candidate, rollback_deployment)  # noqa: SLF001
            marker = active_release_marker(
                args.state_parent / "active-release.json",
                expected_release="v3",
                expected_release_id=previous.release_id,
                expected_release_root=previous.release_root,
                expected_manifest_sha256=previous.release_manifest.sha256,
            )
            del marker
            gate = executor._verify_gate(candidate)  # noqa: SLF001
            executor._verify_current_install(previous)  # noqa: SLF001
            observation = executor._require_observation(previous)  # noqa: SLF001
            report = {
                "schema": "magi.v3.docs-only-rotation-preflight/v1",
                "ok": True,
                "mutation_performed": False,
                "previous_release_id": previous.release_id,
                "candidate_release_id": candidate.release_id,
                "gate_decision": gate["decision"],
                "observation": observation,
                "events": executor.events,
            }
        else:
            report = executor.execute()
    except (CutoverError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "error": str(exc)}))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
