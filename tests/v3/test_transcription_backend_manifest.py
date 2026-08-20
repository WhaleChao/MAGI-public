from __future__ import annotations

import json
from pathlib import Path

from scripts.v3_validation.schema import validate_json_file


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "config" / "v3_transcription_backends.json"
SCHEMA = ROOT / "docs" / "architecture" / "v3" / "contracts" / "transcription-backends.schema.json"
EVALUATION = ROOT / "docs" / "architecture" / "v3" / "OWNSCRIBE_EVALUATION.json"
EXPECTED_OWNSCRIBE_COMMIT = "d9dcecde896ab0e5b15f475b6940669a611ccea1"


def _backend_map(manifest: dict) -> dict[str, dict]:
    return {backend["id"]: backend for backend in manifest["backends"]}


def test_transcription_manifest_matches_schema_and_has_unique_backends() -> None:
    manifest = validate_json_file(MANIFEST, SCHEMA)
    ids = [backend["id"] for backend in manifest["backends"]]

    assert len(ids) == len(set(ids))
    assert set(ids) == {"mlx_whisper", "whisper_cli", "ownscribe"}


def test_mlx_whisper_and_whisper_cli_form_the_only_enabled_dual_pair() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    backends = _backend_map(manifest)
    enabled = [backend for backend in manifest["backends"] if backend["enabled"]]

    assert manifest["policy"]["primary_backend_id"] == "mlx_whisper"
    assert manifest["policy"]["secondary_backend_id"] == "whisper_cli"
    assert {backend["id"] for backend in enabled} == {"mlx_whisper", "whisper_cli"}
    assert backends["mlx_whisper"]["role"] == "primary"
    assert backends["whisper_cli"]["role"] == "secondary"
    assert backends["mlx_whisper"]["implementation"]["source_path"] == (
        "skills/hearing/balthasar_local.py"
    )


def test_forensic_dual_pair_requires_absolute_offline_content_bound_artifacts() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    backends = _backend_map(manifest)
    primary = backends["mlx_whisper"]
    secondary = backends["whisper_cli"]

    for backend in (primary, secondary):
        contract = backend["implementation"]["runtime_contract"]
        assert contract["absolute_paths_required"] is True
        assert contract["artifact_content_sha256_required"] is True
        assert contract["license_id_required"] is True
        assert backend["isolation"]["network_access"] is False
        assert backend["isolation"]["maximum_concurrent_heavy_workers"] == 1
    assert primary["id"] != secondary["id"]
    assert secondary["selectable"] is True
    assert secondary["fallback_allowed"] is False
    assert secondary["implementation"]["runtime_contract"]["model_config"] == (
        "embedded_in_checkpoint"
    )


def test_ownscribe_is_fail_closed_until_legal_chinese_benchmark_passes() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ownscribe = _backend_map(manifest)["ownscribe"]
    gate = ownscribe["promotion_gate"]

    assert ownscribe["enabled"] is False
    assert ownscribe["selectable"] is False
    assert ownscribe["fallback_allowed"] is False
    assert ownscribe["execution_mode"] == "isolated_offline_only"
    assert ownscribe["isolation"] == {
        "required": True,
        "network_access": False,
        "production_writes": False,
        "maximum_concurrent_heavy_workers": 1,
    }
    assert ownscribe["model_acquisition"]["auto_download_allowed"] is False
    assert ownscribe["model_acquisition"]["missing_model_action"] == "reject"
    assert ownscribe["implementation"]["source_commit"] == EXPECTED_OWNSCRIBE_COMMIT
    assert gate["required"] is True
    assert gate["gate_id"] == "legal_zh_tw_audio_benchmark"
    assert gate["status"] != gate["promotion_requires_status"]
    assert gate["baseline_backend_id"] == "mlx_whisper"
    assert gate["thresholds_must_be_approved_before_execution"] is True
    assert gate["minimum_corpus_items"] == 10
    assert gate["minimum_authorized_duration_seconds"] == 600
    assert gate["minimum_human_gold_legal_terms"] == 100
    assert gate["minimum_multi_speaker_items"] == 3
    assert set(gate["required_scenarios"]) == {
        "court_hearing",
        "lawyer_client",
        "legal_dictation",
    }
    assert gate["portable_evidence_raw_content_forbidden"] is True
    assert gate["external_upload_allowed"] is False
    assert gate["corpus_and_result_verifier"] == (
        "scripts/v3_validation/transcription_quality_benchmark.py"
    )


def test_manifest_requires_a_versioned_provenance_preserving_adapter() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    contract = manifest["output_contract"]
    ownscribe = _backend_map(manifest)["ownscribe"]

    assert contract["adapter_required"] is True
    assert {
        "schema_version",
        "backend_id",
        "model_id",
        "source_commit",
        "language",
        "duration_seconds",
        "segments",
        "forensic_provenance",
    }.issubset(contract["required_top_level_fields"])
    assert ownscribe["current_output_contract"]["compatible"] is False


def test_evaluation_is_source_based_and_makes_no_verified_quality_claim() -> None:
    evaluation = json.loads(EVALUATION.read_text(encoding="utf-8"))
    scope = evaluation["scope"]

    assert evaluation["source"]["commit"] == EXPECTED_OWNSCRIBE_COMMIT
    assert evaluation["source"]["worktree_was_clean"] is True
    assert scope["basis"] == "source_and_tests"
    assert scope["readme_claims_treated_as_evidence"] is False
    assert scope["models_downloaded"] is False
    assert scope["dependencies_installed"] is False
    assert scope["live_services_touched"] is False
    assert scope["real_audio_inference_run"] is False
    assert scope["quality_metrics_verified"] is False
    assert evaluation["decision"]["status"] == "blocked_candidate"
    assert evaluation["decision"]["primary_backend_remains"] == "mlx_whisper"


def test_candidate_suite_result_is_explicitly_partial() -> None:
    result = json.loads(EVALUATION.read_text(encoding="utf-8"))["test_execution"]

    assert result["result"] == "partial_not_release_qualifying"
    assert result["passed"] == 207
    assert result["failed"] == 5
    assert result["errors"] == 31
    assert "intentionally not installed" in result["environment"]
