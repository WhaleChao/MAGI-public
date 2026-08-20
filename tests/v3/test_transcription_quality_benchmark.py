from __future__ import annotations

import copy

import pytest

from scripts.v3_validation.transcription_quality_benchmark import (
    BASELINE_BACKEND,
    CANDIDATE_BACKEND,
    CORPUS_SCHEMA,
    RESULT_SCHEMA,
    TranscriptionQualityEvidenceError,
    assess_corpus_manifest,
    evaluate_benchmark,
    sha256_json,
)


def _seal(value: dict, field: str) -> dict:
    value[field] = sha256_json(value)
    return value


def _manifest() -> dict:
    scenarios = ("court_hearing", "lawyer_client", "legal_dictation")
    items = []
    for index in range(10):
        items.append(
            {
                "item_id": f"legal-{index:02d}",
                "audio_sha256": f"{index + 1:064x}",
                "reference_sha256": f"{index + 101:064x}",
                "authorization_record_sha256": f"{index + 201:064x}",
                "license_basis": "organization_owned_synthetic",
                "lawfully_usable": True,
                "deidentified": True,
                "human_gold_verified": True,
                "human_gold_reviewer_id": "reviewer-01",
                "human_gold_verified_at": "2026-07-16T09:00:00Z",
                "duration_seconds": 60.0,
                "reference_character_count": 1000,
                "speaker_count": 2 if index < 3 else 1,
                "speaker_reference_units": 20,
                "legal_term_count": 10,
                "timestamp_boundary_count": 10,
                "scenario": scenarios[index % len(scenarios)],
                "acoustic_profile": "courtroom" if index % 2 else "clean",
            }
        )
    return _seal(
        {
            "schema": CORPUS_SCHEMA,
            "corpus_id": "legal-zh-tw-v1",
            "language": "zh-TW",
            "legal_domain": True,
            "contains_client_confidential_data": False,
            "raw_audio_embedded": False,
            "external_upload_allowed": False,
            "threshold_approval": {
                "approved": True,
                "approved_by_id": "release-owner-01",
                "approved_at": "2026-07-16T09:00:00Z",
                "approval_record_sha256": "a" * 64,
                "thresholds": {
                    "max_cer": 0.05,
                    "max_legal_term_error_rate": 0.1,
                    "max_mean_timestamp_drift_ms": 300.0,
                    "max_speaker_diarization_error_rate": 0.1,
                    "max_realtime_factor": 1.0,
                    "max_peak_rss_mb": 4096.0,
                    "max_peak_accelerator_mb": 3072.0,
                    "max_cer_ratio_to_baseline": 1.0,
                    "max_legal_term_error_ratio_to_baseline": 1.0,
                },
            },
            "items": items,
        },
        "manifest_sha256",
    )


def _backend(manifest: dict, backend_id: str, *, candidate: bool) -> dict:
    rows = []
    for source in manifest["items"]:
        rows.append(
            {
                "item_id": source["item_id"],
                "audio_sha256": source["audio_sha256"],
                "output_sha256": sha256_json([backend_id, source["item_id"]]),
                "output_contract_passed": True,
                "resource_policy_passed": True,
                "network_access_performed": False,
                "production_state_accessed": False,
                "process_group_drained": True,
                "char_errors": 8 if candidate else 10,
                "char_reference_units": 1000,
                "legal_term_errors": 0 if candidate else 1,
                "legal_term_reference_units": 10,
                "timestamp_abs_error_ms": 1000.0 if candidate else 1500.0,
                "timestamp_boundary_count": 10,
                "speaker_assignment_errors": 0 if candidate else 1,
                "speaker_reference_units": 20,
                "inference_seconds": 20.0 if candidate else 30.0,
                "audio_duration_seconds": source["duration_seconds"],
                "peak_rss_mb": 2000.0 if candidate else 1800.0,
                "peak_accelerator_mb": 1500.0 if candidate else 1200.0,
            }
        )
    return {
        "backend_id": backend_id,
        "model_artifact_sha256": "b" * 64 if candidate else "c" * 64,
        "backend_binary_sha256": "d" * 64 if candidate else "e" * 64,
        "items": rows,
    }


def _report(manifest: dict) -> dict:
    return _seal(
        {
            "schema": RESULT_SCHEMA,
            "corpus_id": manifest["corpus_id"],
            "corpus_manifest_sha256": manifest["manifest_sha256"],
            "baseline_backend_id": BASELINE_BACKEND,
            "candidate_backend_id": CANDIDATE_BACKEND,
            "candidate_source_commit": "f" * 40,
            "candidate_source_tree_sha256": "1" * 64,
            "runtime_manifest_sha256": "2" * 64,
            "matched_machine_state_sha256": "3" * 64,
            "execution_mode": "isolated_offline_only",
            "network_access_performed": False,
            "production_state_accessed": False,
            "raw_audio_or_gold_text_embedded": False,
            "matched_machine_state": True,
            "serialized_heavy_workers": True,
            "backend_results": [
                _backend(manifest, BASELINE_BACKEND, candidate=False),
                _backend(manifest, CANDIDATE_BACKEND, candidate=True),
            ],
        },
        "report_sha256",
    )


def test_authorized_hash_only_corpus_is_ready_without_opening_media() -> None:
    result = assess_corpus_manifest(_manifest())

    assert result["ready"] is True
    assert result["item_count"] == 10
    assert result["total_duration_seconds"] == 600.0
    assert result["raw_audio_or_gold_text_exposed"] is False


def test_existing_one_second_generic_fixture_cannot_satisfy_corpus_gate() -> None:
    manifest = _manifest()
    manifest["items"] = manifest["items"][:1]
    manifest["items"][0]["duration_seconds"] = 1.0
    manifest.pop("manifest_sha256")
    _seal(manifest, "manifest_sha256")

    with pytest.raises(TranscriptionQualityEvidenceError, match="at least 10"):
        assess_corpus_manifest(manifest)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("contains_client_confidential_data", True, "non-confidential"),
        ("external_upload_allowed", True, "offline"),
    ],
)
def test_corpus_rejects_confidential_or_uploadable_material(field, value, message) -> None:
    manifest = _manifest()
    manifest[field] = value
    manifest.pop("manifest_sha256")
    _seal(manifest, "manifest_sha256")

    with pytest.raises(TranscriptionQualityEvidenceError, match=message):
        assess_corpus_manifest(manifest)


def test_portable_evidence_rejects_raw_transcript_or_local_path() -> None:
    for key, value in (("reference_text", "機密文字"), ("audio_path", "/private/a.wav")):
        manifest = _manifest()
        manifest["items"][0][key] = value
        manifest.pop("manifest_sha256")
        _seal(manifest, "manifest_sha256")

        with pytest.raises(TranscriptionQualityEvidenceError, match="forbidden sensitive field"):
            assess_corpus_manifest(manifest)


def test_corpus_rejects_duplicate_audio_disguised_as_ten_items() -> None:
    manifest = _manifest()
    manifest["items"][1]["audio_sha256"] = manifest["items"][0]["audio_sha256"]
    manifest.pop("manifest_sha256")
    _seal(manifest, "manifest_sha256")

    with pytest.raises(TranscriptionQualityEvidenceError, match="unique audio"):
        assess_corpus_manifest(manifest)


def test_benchmark_recomputes_metrics_and_passes_only_approved_thresholds() -> None:
    manifest = _manifest()
    result = evaluate_benchmark(manifest, _report(manifest))

    assert result["decision"] == "GO"
    assert result["passed"] is True
    assert result["candidate"]["cer"] == 0.008
    assert result["candidate"]["legal_term_error_rate"] == 0.0
    assert result["raw_audio_or_gold_text_exposed"] is False


def test_report_hash_tamper_fails_closed() -> None:
    manifest = _manifest()
    report = _report(manifest)
    report["backend_results"][1]["items"][0]["char_errors"] = 0

    with pytest.raises(TranscriptionQualityEvidenceError, match="report_sha256"):
        evaluate_benchmark(manifest, report)


def test_candidate_regression_is_no_go_even_if_execution_contract_passes() -> None:
    manifest = _manifest()
    report = _report(manifest)
    for row in report["backend_results"][1]["items"]:
        row["char_errors"] = 20
    report.pop("report_sha256")
    _seal(report, "report_sha256")

    result = evaluate_benchmark(manifest, report)

    assert result["decision"] == "NO_GO"
    assert "CER_BASELINE_REGRESSION" in result["blockers"]


def test_item_metric_denominators_must_match_human_gold_manifest() -> None:
    manifest = _manifest()
    report = _report(manifest)
    report["backend_results"][1]["items"][0]["char_reference_units"] = 999
    report.pop("report_sha256")
    _seal(report, "report_sha256")

    with pytest.raises(TranscriptionQualityEvidenceError, match="character count binding"):
        evaluate_benchmark(manifest, report)


def test_missing_human_threshold_approval_fails_closed() -> None:
    manifest = _manifest()
    manifest["threshold_approval"]["approved"] = False
    manifest.pop("manifest_sha256")
    _seal(manifest, "manifest_sha256")

    with pytest.raises(TranscriptionQualityEvidenceError, match="human approval"):
        assess_corpus_manifest(manifest)


def test_backend_output_contract_or_network_use_fails_closed() -> None:
    manifest = _manifest()
    for field, value in (
        ("output_contract_passed", False),
        ("network_access_performed", True),
    ):
        report = _report(manifest)
        report["backend_results"][1]["items"][0][field] = value
        report.pop("report_sha256")
        _seal(report, "report_sha256")

        with pytest.raises(TranscriptionQualityEvidenceError, match="execution contract"):
            evaluate_benchmark(manifest, report)


def test_manifest_hash_tamper_fails_closed() -> None:
    manifest = _manifest()
    tampered = copy.deepcopy(manifest)
    tampered["items"][0]["legal_term_count"] = 999

    with pytest.raises(TranscriptionQualityEvidenceError, match="manifest_sha256"):
        assess_corpus_manifest(tampered)
