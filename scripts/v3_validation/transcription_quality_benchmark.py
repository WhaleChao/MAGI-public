#!/usr/bin/env python3
"""Fail-closed, privacy-preserving legal zh-TW ASR benchmark verifier.

The verifier never opens audio, gold transcripts, databases, or production paths.  A
private benchmark runner may consume those inputs in an isolated workspace, but the
portable evidence accepted here contains only opaque identifiers, content hashes,
counts, timings, and resource measurements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


CORPUS_SCHEMA = "magi.v3.legal-zh-tw-asr-corpus/v1"
RESULT_SCHEMA = "magi.v3.legal-zh-tw-asr-benchmark/v1"
BASELINE_BACKEND = "mlx_whisper"
CANDIDATE_BACKEND = "ownscribe"
MINIMUM_ITEMS = 10
MINIMUM_DURATION_SECONDS = 600.0
MINIMUM_LEGAL_TERMS = 100
MINIMUM_MULTI_SPEAKER_ITEMS = 3
REQUIRED_SCENARIOS = {"court_hearing", "lawyer_client", "legal_dictation"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
ITEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
FORBIDDEN_PORTABLE_KEYS = {
    "audio",
    "audio_path",
    "file_path",
    "gold_text",
    "raw_audio",
    "raw_text",
    "reference_text",
    "transcript",
    "transcript_text",
}
METRIC_KEYS = (
    "cer",
    "legal_term_error_rate",
    "mean_timestamp_drift_ms",
    "speaker_diarization_error_rate",
    "realtime_factor",
    "peak_rss_mb",
    "peak_accelerator_mb",
)
THRESHOLD_KEYS = tuple(f"max_{name}" for name in METRIC_KEYS) + (
    "max_cer_ratio_to_baseline",
    "max_legal_term_error_ratio_to_baseline",
)


class TranscriptionQualityEvidenceError(ValueError):
    """Raised when corpus or benchmark evidence is incomplete or unsafe."""


def _exact_keys(value: Mapping[str, Any], expected: set[str], description: str) -> None:
    observed = set(value)
    if observed != expected:
        raise TranscriptionQualityEvidenceError(
            f"{description} fields are invalid; missing={sorted(expected - observed)}, "
            f"unexpected={sorted(observed - expected)}"
        )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _verify_self_hash(value: Mapping[str, Any], field: str) -> None:
    claimed = value.get(field)
    unhashed = dict(value)
    unhashed.pop(field, None)
    if not isinstance(claimed, str) or claimed != sha256_json(unhashed):
        raise TranscriptionQualityEvidenceError(f"{field} is missing or invalid")


def _finite_nonnegative(value: Any, description: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise TranscriptionQualityEvidenceError(
            f"{description} must be finite and non-negative"
        )
    return float(value)


def _positive_integer(value: Any, description: str) -> int:
    if type(value) is not int or value < 1:
        raise TranscriptionQualityEvidenceError(f"{description} must be a positive integer")
    return value


def _nonnegative_integer(value: Any, description: str) -> int:
    if type(value) is not int or value < 0:
        raise TranscriptionQualityEvidenceError(
            f"{description} must be a non-negative integer"
        )
    return value


def _sha(value: Any, description: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise TranscriptionQualityEvidenceError(f"{description} must be a SHA-256")
    return value


def _reject_sensitive_payload(value: Any, *, location: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_PORTABLE_KEYS:
                raise TranscriptionQualityEvidenceError(
                    f"portable benchmark evidence contains forbidden sensitive field: {location}.{key}"
                )
            _reject_sensitive_payload(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_payload(child, location=f"{location}[{index}]")


def _valid_timestamp(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TranscriptionQualityEvidenceError(f"{description} must be an RFC3339 UTC timestamp")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise TranscriptionQualityEvidenceError(
            f"{description} must be an RFC3339 UTC timestamp"
        ) from exc
    return value


def assess_corpus_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a sanitized corpus manifest and return non-sensitive coverage."""

    _reject_sensitive_payload(manifest)
    _verify_self_hash(manifest, "manifest_sha256")
    _exact_keys(
        manifest,
        {
            "schema",
            "corpus_id",
            "language",
            "legal_domain",
            "contains_client_confidential_data",
            "raw_audio_embedded",
            "external_upload_allowed",
            "threshold_approval",
            "items",
            "manifest_sha256",
        },
        "corpus manifest",
    )
    if (
        manifest.get("schema") != CORPUS_SCHEMA
        or manifest.get("language") != "zh-TW"
        or manifest.get("legal_domain") is not True
        or manifest.get("contains_client_confidential_data") is not False
        or manifest.get("raw_audio_embedded") is not False
        or manifest.get("external_upload_allowed") is not False
    ):
        raise TranscriptionQualityEvidenceError(
            "corpus must be legal zh-TW, non-confidential, hash-only, and offline"
        )
    corpus_id = manifest.get("corpus_id")
    if not isinstance(corpus_id, str) or not ITEM_ID_RE.fullmatch(corpus_id):
        raise TranscriptionQualityEvidenceError("corpus_id is invalid")

    approval = manifest.get("threshold_approval")
    if not isinstance(approval, Mapping) or approval.get("approved") is not True:
        raise TranscriptionQualityEvidenceError("benchmark thresholds lack explicit human approval")
    _exact_keys(
        approval,
        {
            "approved",
            "approved_by_id",
            "approved_at",
            "approval_record_sha256",
            "thresholds",
        },
        "threshold approval",
    )
    if (
        not isinstance(approval.get("approved_by_id"), str)
        or not ITEM_ID_RE.fullmatch(approval["approved_by_id"])
    ):
        raise TranscriptionQualityEvidenceError("threshold approver is missing")
    _valid_timestamp(approval.get("approved_at"), "threshold approval time")
    _sha(approval.get("approval_record_sha256"), "threshold approval record")
    thresholds = approval.get("thresholds")
    if not isinstance(thresholds, Mapping) or set(thresholds) != set(THRESHOLD_KEYS):
        raise TranscriptionQualityEvidenceError("approved benchmark threshold set is incomplete")
    checked_thresholds = {
        key: _finite_nonnegative(thresholds.get(key), f"threshold {key}")
        for key in THRESHOLD_KEYS
    }
    for key in (
        "max_cer",
        "max_legal_term_error_rate",
        "max_speaker_diarization_error_rate",
    ):
        if checked_thresholds[key] > 1:
            raise TranscriptionQualityEvidenceError(f"threshold {key} cannot exceed 1")

    items = manifest.get("items")
    if not isinstance(items, list) or len(items) < MINIMUM_ITEMS:
        raise TranscriptionQualityEvidenceError(
            f"at least {MINIMUM_ITEMS} authorized corpus items are required"
        )
    ids: set[str] = set()
    audio_hashes: set[str] = set()
    reference_hashes: set[str] = set()
    duration = 0.0
    legal_terms = 0
    multi_speaker = 0
    scenarios: set[str] = set()
    acoustic_profiles: set[str] = set()
    for row in items:
        if not isinstance(row, Mapping):
            raise TranscriptionQualityEvidenceError("corpus item must be an object")
        _exact_keys(
            row,
            {
                "item_id",
                "audio_sha256",
                "reference_sha256",
                "authorization_record_sha256",
                "license_basis",
                "lawfully_usable",
                "deidentified",
                "human_gold_verified",
                "human_gold_reviewer_id",
                "human_gold_verified_at",
                "duration_seconds",
                "reference_character_count",
                "speaker_count",
                "speaker_reference_units",
                "legal_term_count",
                "timestamp_boundary_count",
                "scenario",
                "acoustic_profile",
            },
            "corpus item",
        )
        item_id = row.get("item_id")
        if (
            not isinstance(item_id, str)
            or not ITEM_ID_RE.fullmatch(item_id)
            or item_id in ids
        ):
            raise TranscriptionQualityEvidenceError("corpus item id is invalid or duplicated")
        ids.add(item_id)
        audio_hash = _sha(row.get("audio_sha256"), f"{item_id} audio")
        reference_hash = _sha(row.get("reference_sha256"), f"{item_id} reference")
        if audio_hash in audio_hashes or reference_hash in reference_hashes:
            raise TranscriptionQualityEvidenceError(
                "corpus items must bind unique audio and human-gold content"
            )
        audio_hashes.add(audio_hash)
        reference_hashes.add(reference_hash)
        _sha(row.get("authorization_record_sha256"), f"{item_id} authorization record")
        if (
            row.get("lawfully_usable") is not True
            or row.get("deidentified") is not True
            or row.get("human_gold_verified") is not True
            or not isinstance(row.get("human_gold_reviewer_id"), str)
            or not ITEM_ID_RE.fullmatch(row["human_gold_reviewer_id"])
            or row.get("license_basis")
            not in {"speaker_written_consent", "public_domain", "organization_owned_synthetic"}
        ):
            raise TranscriptionQualityEvidenceError(
                f"{item_id} lacks lawful use, de-identification, or human-gold proof"
            )
        _valid_timestamp(row.get("human_gold_verified_at"), f"{item_id} gold verification time")
        item_duration = _finite_nonnegative(row.get("duration_seconds"), f"{item_id} duration")
        if item_duration <= 0:
            raise TranscriptionQualityEvidenceError(f"{item_id} duration must be positive")
        speakers = _positive_integer(row.get("speaker_count"), f"{item_id} speaker count")
        _positive_integer(
            row.get("reference_character_count"), f"{item_id} reference character count"
        )
        _positive_integer(
            row.get("speaker_reference_units"), f"{item_id} speaker reference units"
        )
        _positive_integer(
            row.get("timestamp_boundary_count"), f"{item_id} timestamp boundary count"
        )
        term_count = _positive_integer(row.get("legal_term_count"), f"{item_id} legal term count")
        scenario = row.get("scenario")
        acoustic = row.get("acoustic_profile")
        if scenario not in REQUIRED_SCENARIOS:
            raise TranscriptionQualityEvidenceError(f"{item_id} scenario is unsupported")
        if acoustic not in {"clean", "office", "courtroom", "remote_call"}:
            raise TranscriptionQualityEvidenceError(f"{item_id} acoustic profile is unsupported")
        duration += item_duration
        legal_terms += term_count
        multi_speaker += int(speakers >= 2)
        scenarios.add(str(scenario))
        acoustic_profiles.add(str(acoustic))

    blockers: list[str] = []
    if duration < MINIMUM_DURATION_SECONDS:
        blockers.append("TOTAL_AUTHORIZED_AUDIO_BELOW_600_SECONDS")
    if legal_terms < MINIMUM_LEGAL_TERMS:
        blockers.append("HUMAN_GOLD_LEGAL_TERM_COUNT_BELOW_100")
    if multi_speaker < MINIMUM_MULTI_SPEAKER_ITEMS:
        blockers.append("MULTI_SPEAKER_ITEMS_BELOW_3")
    if scenarios != REQUIRED_SCENARIOS:
        blockers.append("REQUIRED_LEGAL_SCENARIOS_INCOMPLETE")
    scenario_counts = {
        scenario: sum(row.get("scenario") == scenario for row in items)
        for scenario in REQUIRED_SCENARIOS
    }
    if any(count < 2 for count in scenario_counts.values()):
        blockers.append("LEGAL_SCENARIO_ITEM_COUNT_BELOW_2")
    if len(acoustic_profiles) < 2:
        blockers.append("ACOUSTIC_PROFILE_DIVERSITY_INCOMPLETE")
    return {
        "corpus_id": corpus_id,
        "corpus_manifest_sha256": manifest["manifest_sha256"],
        "item_count": len(items),
        "total_duration_seconds": round(duration, 6),
        "legal_term_count": legal_terms,
        "multi_speaker_item_count": multi_speaker,
        "scenarios": sorted(scenarios),
        "acoustic_profiles": sorted(acoustic_profiles),
        "thresholds": checked_thresholds,
        "ready": not blockers,
        "blockers": blockers,
        "raw_audio_or_gold_text_exposed": False,
    }


def _aggregate_backend(
    backend: Mapping[str, Any], *, item_map: Mapping[str, Mapping[str, Any]]
) -> dict[str, float]:
    rows = backend.get("items")
    if not isinstance(rows, list) or len(rows) != len(item_map):
        raise TranscriptionQualityEvidenceError("backend item results do not cover the corpus")
    seen: set[str] = set()
    totals = {
        "char_errors": 0,
        "char_reference_units": 0,
        "legal_term_errors": 0,
        "legal_term_reference_units": 0,
        "timestamp_abs_error_ms": 0.0,
        "timestamp_boundary_count": 0,
        "speaker_assignment_errors": 0,
        "speaker_reference_units": 0,
        "inference_seconds": 0.0,
        "audio_duration_seconds": 0.0,
    }
    peak_rss = 0.0
    peak_accelerator = 0.0
    for row in rows:
        if not isinstance(row, Mapping):
            raise TranscriptionQualityEvidenceError("backend item result must be an object")
        _exact_keys(
            row,
            {
                "item_id",
                "audio_sha256",
                "output_sha256",
                "output_contract_passed",
                "resource_policy_passed",
                "network_access_performed",
                "production_state_accessed",
                "process_group_drained",
                "char_errors",
                "char_reference_units",
                "legal_term_errors",
                "legal_term_reference_units",
                "timestamp_abs_error_ms",
                "timestamp_boundary_count",
                "speaker_assignment_errors",
                "speaker_reference_units",
                "inference_seconds",
                "audio_duration_seconds",
                "peak_rss_mb",
                "peak_accelerator_mb",
            },
            "backend item result",
        )
        item_id = row.get("item_id")
        source = item_map.get(str(item_id))
        if source is None or item_id in seen:
            raise TranscriptionQualityEvidenceError("backend item result id is missing or duplicated")
        seen.add(str(item_id))
        if row.get("audio_sha256") != source.get("audio_sha256"):
            raise TranscriptionQualityEvidenceError(f"{item_id} audio binding mismatch")
        _sha(row.get("output_sha256"), f"{item_id} output")
        if (
            row.get("output_contract_passed") is not True
            or row.get("resource_policy_passed") is not True
            or row.get("network_access_performed") is not False
            or row.get("production_state_accessed") is not False
            or row.get("process_group_drained") is not True
        ):
            raise TranscriptionQualityEvidenceError(f"{item_id} execution contract failed")
        for key in (
            "char_errors",
            "legal_term_errors",
            "speaker_assignment_errors",
        ):
            totals[key] += _nonnegative_integer(row.get(key), f"{item_id} {key}")
        for key in (
            "char_reference_units",
            "legal_term_reference_units",
            "timestamp_boundary_count",
            "speaker_reference_units",
        ):
            totals[key] += _positive_integer(row.get(key), f"{item_id} {key}")
        for key in ("timestamp_abs_error_ms", "inference_seconds", "audio_duration_seconds"):
            totals[key] += _finite_nonnegative(row.get(key), f"{item_id} {key}")
        if row["inference_seconds"] <= 0:
            raise TranscriptionQualityEvidenceError(f"{item_id} inference time must be positive")
        if abs(float(row["audio_duration_seconds"]) - float(source["duration_seconds"])) > 0.001:
            raise TranscriptionQualityEvidenceError(f"{item_id} duration binding mismatch")
        if row["char_reference_units"] != source["reference_character_count"]:
            raise TranscriptionQualityEvidenceError(
                f"{item_id} reference character count binding mismatch"
            )
        if row["legal_term_reference_units"] != source["legal_term_count"]:
            raise TranscriptionQualityEvidenceError(
                f"{item_id} legal-term count binding mismatch"
            )
        if row["timestamp_boundary_count"] != source["timestamp_boundary_count"]:
            raise TranscriptionQualityEvidenceError(
                f"{item_id} timestamp boundary count binding mismatch"
            )
        if row["speaker_reference_units"] != source["speaker_reference_units"]:
            raise TranscriptionQualityEvidenceError(
                f"{item_id} speaker reference count binding mismatch"
            )
        if row["legal_term_errors"] > row["legal_term_reference_units"]:
            raise TranscriptionQualityEvidenceError(f"{item_id} legal-term counts are invalid")
        if row["speaker_assignment_errors"] > row["speaker_reference_units"]:
            raise TranscriptionQualityEvidenceError(f"{item_id} speaker counts are invalid")
        item_rss = _finite_nonnegative(row.get("peak_rss_mb"), f"{item_id} RSS")
        if item_rss <= 0:
            raise TranscriptionQualityEvidenceError(f"{item_id} RSS must be positive")
        peak_rss = max(peak_rss, item_rss)
        peak_accelerator = max(
            peak_accelerator,
            _finite_nonnegative(row.get("peak_accelerator_mb"), f"{item_id} accelerator"),
        )
    return {
        "cer": totals["char_errors"] / totals["char_reference_units"],
        "legal_term_error_rate": totals["legal_term_errors"]
        / totals["legal_term_reference_units"],
        "mean_timestamp_drift_ms": totals["timestamp_abs_error_ms"]
        / totals["timestamp_boundary_count"],
        "speaker_diarization_error_rate": totals["speaker_assignment_errors"]
        / totals["speaker_reference_units"],
        "realtime_factor": totals["inference_seconds"] / totals["audio_duration_seconds"],
        "peak_rss_mb": peak_rss,
        "peak_accelerator_mb": peak_accelerator,
    }


def evaluate_benchmark(
    manifest: Mapping[str, Any], report: Mapping[str, Any]
) -> dict[str, Any]:
    """Recompute all promotion metrics and return a fail-closed decision."""

    corpus = assess_corpus_manifest(manifest)
    if not corpus["ready"]:
        raise TranscriptionQualityEvidenceError(
            f"corpus coverage is incomplete: {corpus['blockers']}"
        )
    _reject_sensitive_payload(report)
    _verify_self_hash(report, "report_sha256")
    _exact_keys(
        report,
        {
            "schema",
            "corpus_id",
            "corpus_manifest_sha256",
            "baseline_backend_id",
            "candidate_backend_id",
            "candidate_source_commit",
            "candidate_source_tree_sha256",
            "runtime_manifest_sha256",
            "matched_machine_state_sha256",
            "execution_mode",
            "network_access_performed",
            "production_state_accessed",
            "raw_audio_or_gold_text_embedded",
            "matched_machine_state",
            "serialized_heavy_workers",
            "backend_results",
            "report_sha256",
        },
        "benchmark report",
    )
    if (
        report.get("schema") != RESULT_SCHEMA
        or report.get("corpus_id") != corpus["corpus_id"]
        or report.get("corpus_manifest_sha256") != corpus["corpus_manifest_sha256"]
        or report.get("baseline_backend_id") != BASELINE_BACKEND
        or report.get("candidate_backend_id") != CANDIDATE_BACKEND
        or report.get("execution_mode") != "isolated_offline_only"
        or report.get("network_access_performed") is not False
        or report.get("production_state_accessed") is not False
        or report.get("raw_audio_or_gold_text_embedded") is not False
        or report.get("matched_machine_state") is not True
        or report.get("serialized_heavy_workers") is not True
    ):
        raise TranscriptionQualityEvidenceError("benchmark report binding or isolation is invalid")
    if not isinstance(report.get("candidate_source_commit"), str) or not GIT_COMMIT_RE.fullmatch(
        report["candidate_source_commit"]
    ):
        raise TranscriptionQualityEvidenceError("candidate_source_commit is invalid")
    for key in (
        "candidate_source_tree_sha256",
        "runtime_manifest_sha256",
        "matched_machine_state_sha256",
    ):
        _sha(report.get(key), key)
    backends = report.get("backend_results")
    if not isinstance(backends, list) or len(backends) != 2:
        raise TranscriptionQualityEvidenceError("exactly two matched backend results are required")
    by_id = {
        str(row.get("backend_id")): row
        for row in backends
        if isinstance(row, Mapping)
    }
    if set(by_id) != {BASELINE_BACKEND, CANDIDATE_BACKEND}:
        raise TranscriptionQualityEvidenceError("baseline and candidate backend results are required")
    for row in by_id.values():
        _exact_keys(
            row,
            {"backend_id", "model_artifact_sha256", "backend_binary_sha256", "items"},
            "backend result",
        )
    if any(
        not SHA256_RE.fullmatch(str(row.get("model_artifact_sha256", "")))
        or not SHA256_RE.fullmatch(str(row.get("backend_binary_sha256", "")))
        for row in by_id.values()
    ):
        raise TranscriptionQualityEvidenceError("backend model/binary provenance is incomplete")
    item_map = {str(row["item_id"]): row for row in manifest["items"]}
    baseline = _aggregate_backend(by_id[BASELINE_BACKEND], item_map=item_map)
    candidate = _aggregate_backend(by_id[CANDIDATE_BACKEND], item_map=item_map)
    thresholds = corpus["thresholds"]
    blockers = [
        f"{key.upper()}_THRESHOLD_FAILED"
        for key in METRIC_KEYS
        if candidate[key] > thresholds[f"max_{key}"]
    ]
    cer_ratio = (
        candidate["cer"] / baseline["cer"]
        if baseline["cer"]
        else (0.0 if not candidate["cer"] else None)
    )
    legal_ratio = (
        candidate["legal_term_error_rate"] / baseline["legal_term_error_rate"]
        if baseline["legal_term_error_rate"]
        else (0.0 if not candidate["legal_term_error_rate"] else None)
    )
    if cer_ratio is None or cer_ratio > thresholds["max_cer_ratio_to_baseline"]:
        blockers.append("CER_BASELINE_REGRESSION")
    if legal_ratio is None or legal_ratio > thresholds["max_legal_term_error_ratio_to_baseline"]:
        blockers.append("LEGAL_TERM_BASELINE_REGRESSION")
    decision = "GO" if not blockers else "NO_GO"
    return {
        "gate_id": "legal_zh_tw_audio_benchmark",
        "decision": decision,
        "passed": decision == "GO",
        "corpus_manifest_sha256": corpus["corpus_manifest_sha256"],
        "report_sha256": report["report_sha256"],
        "baseline": {key: round(value, 9) for key, value in baseline.items()},
        "candidate": {key: round(value, 9) for key, value in candidate.items()},
        "ratios_to_baseline": {
            "cer": cer_ratio,
            "legal_term_error_rate": legal_ratio,
        },
        "approved_thresholds": thresholds,
        "blockers": blockers,
        "raw_audio_or_gold_text_exposed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify sanitized legal zh-TW ASR corpus/benchmark evidence without opening media"
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise TranscriptionQualityEvidenceError("manifest must be an object")
        if args.report is None:
            result = assess_corpus_manifest(manifest)
        else:
            report = json.loads(args.report.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise TranscriptionQualityEvidenceError("report must be an object")
            result = evaluate_benchmark(manifest, report)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0 if result.get("ready") is True or result.get("passed") is True else 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "gate_id": "legal_zh_tw_audio_benchmark",
                    "decision": "NO_GO",
                    "passed": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
