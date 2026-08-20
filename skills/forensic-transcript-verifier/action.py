#!/usr/bin/env python3
"""MAGI V2/V3 entry point for forensic transcript verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent
MAGI_ROOT = SKILL_DIR.parents[1]
sys.path.insert(0, str(MAGI_ROOT))
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from audit_engine import audit_transcript, inspect_evidence, validate_docx  # noqa: E402


HELP = {
    "skill": "forensic-transcript-verifier",
    "operations": {
        "inspect": ["video", "transcript", "baseline", "asr_json", "output_dir"],
        "audit": [
            "transcript",
            "baseline",
            "asr_json",
            "baseline_video_start",
            "baseline_video_end",
            "output_dir",
        ],
        "validate-docx": ["transcript", "output_dir", "fontconfig"],
        "full-check": [
            "video",
            "transcript",
            "baseline",
            "asr_json",
            "baseline_video_start",
            "baseline_video_end",
            "output_dir",
        ],
        "autonomous-plan": [
            "video",
            "transcript",
            "baseline",
            "asr_json",
            "baseline_video_start",
            "baseline_video_end",
            "max_visual_reviews",
            "output_dir",
        ],
        "autonomous": [
            "video",
            "transcript",
            "baseline",
            "asr_json",
            "secondary_asr_json",
            "baseline_video_start",
            "baseline_video_end",
            "max_visual_reviews",
            "require_secondary_asr",
            "secondary_asr_independent_verified",
            "semantic_merge_gap",
            "output_docx",
            "output_dir",
        ],
        "live-start": ["manifest", "state"],
        "live-status": ["state"],
        "live-cancel": ["state"],
    },
    "example": {
        "operation": "full-check",
        "video": "/abs/hearing.mp4",
        "transcript": "/abs/candidate.docx",
        "baseline": "/abs/manually-corrected-excerpt.docx",
        "asr_json": "/abs/asr.json",
        "baseline_video_start": "01:23:49",
        "baseline_video_end": "01:29:20",
        "output_dir": "/abs/audit",
    },
    "autonomous_example": {
        "operation": "autonomous",
        "video": "/abs/hearing.mp4",
        "transcript": "/abs/full-draft.docx",
        "baseline": "/abs/manually-corrected-excerpt.docx",
        "asr_json": "/abs/primary-asr.json",
        "baseline_video_start": "01:23:49",
        "baseline_video_end": "01:29:20",
        "max_visual_reviews": 2000,
        "output_docx": "完整譯文_MAGI自主雙重勘驗版.docx",
        "output_dir": "/abs/audit",
    },
    "note": "autonomous performs two local visual/audio/model passes and creates a new DOCX; a qualified human must still make the final filing decision.",
}


def _audit_projection(result: dict[str, Any]) -> dict[str, Any]:
    baseline = result.get("baseline") or {}
    coverage = result.get("coverage") or {}
    return {
        "turns": result.get("turns"),
        "monotonic_blocks": result.get("monotonic_blocks"),
        "overlaps": result.get("overlaps"),
        "uncertainty_markers": result.get("uncertainty_markers"),
        "baseline": {
            "baseline_entries": baseline.get("baseline_entries"),
            "matched_entries": baseline.get("matched_entries"),
            "exact": baseline.get("exact"),
            "mismatches": baseline.get("mismatches"),
        },
        "coverage": {
            "selected_segments": coverage.get("selected_segments"),
            "uncovered_segments": coverage.get("uncovered_segments"),
            "uncovered_groups": coverage.get("uncovered_groups"),
        },
        "speaker_review": result.get("speaker_review"),
        "gates": result.get("gates"),
        "passed_deterministic_gates": result.get("passed_deterministic_gates"),
    }


def _parse_task(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {"operation": "help"}
    if text.lower() in {"help", "summary", "說明", "幫助"}:
        return {"operation": "help"}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"operation": text}
    if not isinstance(payload, dict):
        raise ValueError("task JSON must be an object")
    return payload


def _merge_cli(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    merged = dict(payload)
    for key in (
        "operation",
        "video",
        "transcript",
        "baseline",
        "asr_json",
        "baseline_video_start",
        "baseline_video_end",
        "output_dir",
        "fontconfig",
    ):
        value = getattr(args, key, None)
        if value not in (None, ""):
            merged[key] = value
    return merged


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_v3_completion_binding(
    payload: dict[str, Any], result: dict[str, Any]
) -> None:
    contract = payload.get("_v3_contract")
    if not isinstance(contract, dict):
        return
    output_dir = Path(str(contract.get("output_dir") or "")).expanduser().resolve()
    if output_dir != Path(str(payload.get("output_dir") or "")).expanduser().resolve():
        raise ValueError("V3 completion contract output directory mismatch")
    operation = str(payload.get("operation") or "").strip().lower().replace("_", "-")
    report_names = {
        "inspect": ("inspection.json",),
        "audit": ("audit.json",),
        "validate-docx": ("docx-validation.json",),
        "full-check": ("inspection.json", "audit.json", "docx-validation.json", "full-check.json"),
        "autonomous-plan": ("autonomous-plan.json",),
        "autonomous": ("autonomous.json", "audit.json", "docx-validation.json"),
    }.get(operation)
    if not report_names:
        raise ValueError(f"V3 binding does not support operation: {operation}")
    report_sha256: dict[str, str] = {}
    for name in report_names:
        path = output_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"V3 completion report is missing: {path}")
        report_sha256[name] = _sha256_file(path)
    artifact_sha256: dict[str, str] = {}
    output_docx = Path(str(result.get("output_docx") or "")).expanduser().resolve()
    if str(result.get("output_docx") or "") and output_docx.is_file():
        if output_dir not in output_docx.parents:
            raise ValueError("V3 output artifact escaped the lease workspace")
        artifact_sha256[output_docx.name] = _sha256_file(output_docx)
    binding = {
        "schema_version": 1,
        "contract": contract,
        "completed_at_ns": time.time_ns(),
        "report_sha256": report_sha256,
        "artifact_sha256": artifact_sha256,
    }
    path = output_dir / "v3-completion-binding.json"
    path.write_text(json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8")


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    operation = str(payload.get("operation") or "help").strip().lower().replace("_", "-")
    if operation == "help":
        return {"success": True, **HELP}
    if operation == "inspect":
        return inspect_evidence(payload)
    if operation == "audit":
        return audit_transcript(payload)
    if operation == "validate-docx":
        return validate_docx(payload, magi_root=MAGI_ROOT)
    if operation == "autonomous-plan":
        from video_agent import prepare_autonomous_video_review

        return prepare_autonomous_video_review(payload)
    if operation == "autonomous":
        from video_agent import run_autonomous_video_review

        return run_autonomous_video_review(payload)
    if operation in {"live-start", "live-status", "live-cancel"}:
        from live_runtime import cancel_live_job, get_live_status, start_live_job

        if operation == "live-status":
            return get_live_status(payload.get("state") or None)
        if operation == "live-cancel":
            return cancel_live_job(payload.get("state") or None)
        return start_live_job(
            payload.get("manifest") or None,
            state_path=payload.get("state") or None,
        )
    if operation == "full-check":
        output_dir = Path(payload.get("output_dir") or ".forensic-transcript-audit").expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        inspected = inspect_evidence(payload)
        pass_1_payload = {**payload, "output_dir": str(output_dir / "pass-1")}
        audit_pass_1 = audit_transcript(pass_1_payload)
        audit_pass_2 = audit_transcript(payload)
        audit_consistent = _audit_projection(audit_pass_1) == _audit_projection(audit_pass_2)
        validated = validate_docx(payload, magi_root=MAGI_ROOT)
        inspected_files = inspected.get("files") or {}
        inspection_ok = all(
            (inspected_files.get(field) or {}).get("exists")
            for field in ("video", "transcript", "baseline", "asr_json")
        ) and bool((inspected_files.get("video") or {}).get("ffprobe", {}).get("format", {}).get("duration"))
        result = {
            "success": True,
            "operation": operation,
            "inspection": inspected,
            "automated_audit_passes": 2,
            "audit_pass_1": audit_pass_1,
            "audit_pass_2": audit_pass_2,
            "audit_consistent": audit_consistent,
            "audit": audit_pass_2,
            "docx_validation": validated,
            "passed": (
                inspection_ok
                and bool(audit_pass_1.get("passed_deterministic_gates"))
                and bool(audit_pass_2.get("passed_deterministic_gates"))
                and audit_consistent
                and bool(validated.get("passed"))
            ),
            "inspection_gate_passed": inspection_ok,
            "manual_second_pass_required": True,
            "artifacts": [
                str(payload.get("transcript") or ""),
                (audit_pass_1.get("reports") or {}).get("json", ""),
                (audit_pass_2.get("reports") or {}).get("json", ""),
                validated.get("pdf", ""),
            ],
        }
        json_path = output_dir / "full-check.json"
        markdown_path = output_dir / "full-check.md"
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_path.write_text(
            "\n".join(
                (
                    "# full-check",
                    "",
                    f"- passed: `{result['passed']}`",
                    f"- inspection_gate_passed: `{result['inspection_gate_passed']}`",
                    f"- automated_audit_passes: `{result['automated_audit_passes']}`",
                    f"- audit_consistent: `{result['audit_consistent']}`",
                    f"- manual_second_pass_required: `{result['manual_second_pass_required']}`",
                    "",
                )
            ),
            encoding="utf-8",
        )
        result["reports"] = {"json": str(json_path), "markdown": str(markdown_path)}
        return result
    raise ValueError(f"unknown operation: {operation}")


def main() -> int:
    parser = argparse.ArgumentParser(description=HELP["skill"])
    parser.add_argument("task_positional", nargs="?", default="")
    parser.add_argument("--task", default="")
    parser.add_argument("--operation", default="")
    parser.add_argument("--video", default="")
    parser.add_argument("--transcript", default="")
    parser.add_argument("--baseline", default="")
    parser.add_argument("--asr-json", dest="asr_json", default="")
    parser.add_argument("--baseline-video-start", dest="baseline_video_start", default="")
    parser.add_argument("--baseline-video-end", dest="baseline_video_end", default="")
    parser.add_argument("--output-dir", dest="output_dir", default="")
    parser.add_argument("--fontconfig", default="")
    args = parser.parse_args()
    try:
        payload = _parse_task(args.task or args.task_positional)
        payload = _merge_cli(payload, args)
        result = execute(payload)
        _write_v3_completion_binding(payload, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
