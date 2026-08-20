from __future__ import annotations

import json

import pytest

from scripts.v3_schedule_baseline_capture import BaselineCaptureError, capture_baseline
from scripts.v3_campaign.schedule_realism import (
    SOURCE_EVIDENCE_RECEIPT_FIELD,
    _source_evidence_receipt_sha256,
)
from skills.ops.cron_command_identity import command_definition_sha256


def test_capture_baseline_redacts_state_and_keeps_only_successful_duration(tmp_path):
    cron = tmp_path / "cron_jobs.json"
    state = tmp_path / "cron_state.json"
    previous = tmp_path / "baseline.json"
    cron.write_text(
        json.dumps(
            [
                {"id": "safe", "enabled": True, "command": "scripts/safe.py"},
                {"id": "failed", "enabled": True, "command": "scripts/failed.py"},
                {"id": "disabled", "enabled": False, "command": "scripts/disabled.py"},
            ]
        ),
        encoding="utf-8",
    )
    state.write_text(
        json.dumps(
            {
                "safe": {
                    "last_success": True,
                    "last_returncode": 0,
                    "last_timed_out": False,
                    "last_duration_sec": 1.25,
                    "command_sha256": command_definition_sha256("scripts/safe.py"),
                    "last_success_at": "2026-07-14T18:00:00+08:00",
                    "last_stdout_tail": "private output",
                },
                "failed": {
                    "last_success": False,
                    "last_returncode": 1,
                    "last_duration_sec": 9.0,
                    "last_error": "private failure",
                },
            }
        ),
        encoding="utf-8",
    )
    previous.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "duration_policy": {"single_observation_is_not_a_percentile": True},
                "representative_body_allowlist": [{"job_id": "safe"}],
            }
        ),
        encoding="utf-8",
    )

    payload = capture_baseline(
        cron_jobs_path=cron,
        runtime_state_path=state,
        previous_baseline_path=previous,
        captured_at="2026-07-14T10:00:00+00:00",
    )

    assert payload["coverage"] == {
        "job_definitions": 3,
        "enabled_job_definitions": 2,
        "enabled_jobs_with_successful_duration": 1,
        "enabled_jobs_without_successful_duration": 1,
        "minimum_samples_per_job_for_percentile": 3,
        "jobs_meeting_minimum_samples": 0,
        "global_duration_percentile_available": False,
    }
    assert len(payload["observations"]) == 1
    observation = payload["observations"][0]
    assert observation["job_id"] == "safe"
    assert observation["duration_seconds"] == 1.25
    assert observation["sample_count"] == 1
    assert observation["successful"] is True
    assert observation["observed_at"] == "2026-07-14T10:00:00+00:00"
    assert observation["samples"] == [
        {"duration_seconds": 1.25, "observed_at": "2026-07-14T10:00:00+00:00"}
    ]
    serialized = json.dumps(payload)
    assert "private output" not in serialized
    assert "private failure" not in serialized
    assert payload["coverage"]["global_duration_percentile_available"] is False
    assert payload["source_evidence"]["legacy_naive_timestamp_timezone"] == "Asia/Taipei"
    assert payload["source_evidence"]["observation_timestamps_normalized_to_utc"] is True
    assert payload["source_evidence"]["observation_command_binding"] == "canonical_argv_sha256_v1"
    assert payload["source_evidence"][SOURCE_EVIDENCE_RECEIPT_FIELD] == (
        _source_evidence_receipt_sha256(payload["source_evidence"])
    )


def test_capture_baseline_accumulates_unique_successes_and_computes_p95(tmp_path):
    cron = tmp_path / "cron_jobs.json"
    state = tmp_path / "cron_state.json"
    previous = tmp_path / "baseline.json"
    cron.write_text(
        json.dumps([{"id": "safe", "enabled": True, "command": "scripts/safe.py"}]),
        encoding="utf-8",
    )
    previous.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "duration_policy": {"single_observation_is_not_a_percentile": True},
                "representative_body_allowlist": [{"job_id": "safe"}],
                "observations": [
                    {
                        "job_id": "safe",
                        "command_sha256": command_definition_sha256("scripts/safe.py"),
                        "duration_seconds": 2.0,
                        "sample_count": 2,
                        "successful": True,
                        "observed_at": "2026-07-13T02:00:00+08:00",
                        "samples": [
                            {"duration_seconds": 1.0, "observed_at": "2026-07-12T02:00:00+08:00"},
                            {"duration_seconds": 2.0, "observed_at": "2026-07-13T02:00:00+08:00"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state.write_text(
        json.dumps(
            {
                "safe": {
                    "last_success": True,
                    "last_returncode": 0,
                    "last_timed_out": False,
                    "last_duration_sec": 3.0,
                    "command_sha256": command_definition_sha256("scripts/safe.py"),
                    "last_success_at": "2026-07-14T02:00:00+08:00",
                }
            }
        ),
        encoding="utf-8",
    )

    payload = capture_baseline(
        cron_jobs_path=cron,
        runtime_state_path=state,
        previous_baseline_path=previous,
    )

    observation = payload["observations"][0]
    assert observation["sample_count"] == 3
    assert observation["duration_p95_seconds"] == 3.0
    assert payload["coverage"]["jobs_meeting_minimum_samples"] == 1
    assert payload["coverage"]["global_duration_percentile_available"] is True


def test_capture_baseline_normalizes_offsets_before_deduplication(tmp_path):
    cron = tmp_path / "cron_jobs.json"
    state = tmp_path / "cron_state.json"
    previous = tmp_path / "baseline.json"
    cron.write_text(json.dumps([{"id": "safe", "enabled": True}]), encoding="utf-8")
    previous.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "duration_policy": {"single_observation_is_not_a_percentile": True},
                "representative_body_allowlist": [{"job_id": "safe"}],
                "observations": [
                    {
                        "job_id": "safe",
                        "command_sha256": command_definition_sha256(""),
                        "samples": [
                            {
                                "duration_seconds": 1.25,
                                "observed_at": "2026-07-14T18:00:00+08:00",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state.write_text(
        json.dumps(
            {
                "safe": {
                    "last_success": True,
                    "last_returncode": 0,
                    "last_timed_out": False,
                    "last_duration_sec": 1.25,
                    "command_sha256": command_definition_sha256(""),
                    "last_success_at": "2026-07-14T10:00:00Z",
                }
            }
        ),
        encoding="utf-8",
    )

    payload = capture_baseline(
        cron_jobs_path=cron,
        runtime_state_path=state,
        previous_baseline_path=previous,
    )

    assert payload["observations"][0]["sample_count"] == 1
    assert payload["observations"][0]["observed_at"] == "2026-07-14T10:00:00+00:00"


def test_capture_baseline_rejects_conflicting_duration_for_same_instant(tmp_path):
    cron = tmp_path / "cron_jobs.json"
    state = tmp_path / "cron_state.json"
    previous = tmp_path / "baseline.json"
    cron.write_text(json.dumps([{"id": "safe", "enabled": True}]), encoding="utf-8")
    previous.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "duration_policy": {"single_observation_is_not_a_percentile": True},
                "representative_body_allowlist": [{"job_id": "safe"}],
                "observations": [
                    {
                        "job_id": "safe",
                        "command_sha256": command_definition_sha256(""),
                        "samples": [
                            {
                                "duration_seconds": 1.25,
                                "observed_at": "2026-07-14T18:00:00+08:00",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state.write_text(
        json.dumps(
            {
                "safe": {
                    "last_success": True,
                    "last_returncode": 0,
                    "last_timed_out": False,
                    "last_duration_sec": 9.0,
                    "command_sha256": command_definition_sha256(""),
                    "last_success_at": "2026-07-14T10:00:00Z",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BaselineCaptureError, match="disagree"):
        capture_baseline(
            cron_jobs_path=cron,
            runtime_state_path=state,
            previous_baseline_path=previous,
        )


def test_capture_baseline_interprets_legacy_naive_scheduler_time_as_taipei(tmp_path):
    cron = tmp_path / "cron_jobs.json"
    state = tmp_path / "cron_state.json"
    previous = tmp_path / "baseline.json"
    cron.write_text(json.dumps([{"id": "safe", "enabled": True}]), encoding="utf-8")
    previous.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "duration_policy": {"single_observation_is_not_a_percentile": True},
                "representative_body_allowlist": [{"job_id": "safe"}],
            }
        ),
        encoding="utf-8",
    )
    state.write_text(
        json.dumps(
            {
                "safe": {
                    "last_success": True,
                    "last_returncode": 0,
                    "last_timed_out": False,
                    "last_duration_sec": 1.0,
                    "command_sha256": command_definition_sha256(""),
                    "last_success_at": "2026-07-14T18:00:00",
                }
            }
        ),
        encoding="utf-8",
    )

    payload = capture_baseline(
        cron_jobs_path=cron,
        runtime_state_path=state,
        previous_baseline_path=previous,
    )

    assert payload["observations"][0]["observed_at"] == "2026-07-14T10:00:00+00:00"


def test_capture_baseline_invalidates_observation_after_command_change(tmp_path):
    cron = tmp_path / "cron_jobs.json"
    state = tmp_path / "cron_state.json"
    previous = tmp_path / "baseline.json"
    old_command = "scripts/task.py --max-depth 5"
    new_command = "scripts/task.py --max-depth 20"
    cron.write_text(
        json.dumps([{"id": "safe", "enabled": True, "command": new_command}]),
        encoding="utf-8",
    )
    previous.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "duration_policy": {"single_observation_is_not_a_percentile": True},
                "representative_body_allowlist": [{"job_id": "safe"}],
                "observations": [
                    {
                        "job_id": "safe",
                        "command_sha256": command_definition_sha256(old_command),
                        "duration_seconds": 4.0,
                        "sample_count": 1,
                        "successful": True,
                        "observed_at": "2026-07-15T01:00:00+00:00",
                        "samples": [
                            {
                                "duration_seconds": 4.0,
                                "observed_at": "2026-07-15T01:00:00+00:00",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state.write_text(
        json.dumps(
            {
                "safe": {
                    "last_success": True,
                    "last_returncode": 0,
                    "last_timed_out": False,
                    "last_duration_sec": 5.0,
                    "last_success_at": "2026-07-15T02:00:00+00:00",
                    "command_sha256": command_definition_sha256(old_command),
                }
            }
        ),
        encoding="utf-8",
    )

    payload = capture_baseline(
        cron_jobs_path=cron,
        runtime_state_path=state,
        previous_baseline_path=previous,
    )

    assert payload["observations"] == []
    assert payload["coverage"]["enabled_jobs_without_successful_duration"] == 1
    invalidated = payload["invalidated_observations"]
    assert invalidated == [
        {
            "job_id": "safe",
            "reason": "COMMAND_DEFINITION_CHANGED_AFTER_OBSERVATION",
            "observed_command_sha256": command_definition_sha256(old_command),
            "current_command_sha256": command_definition_sha256(new_command),
            "invalidated_sample_count": 1,
            "last_observed_at": "2026-07-15T01:00:00+00:00",
        }
    ]


def test_capture_baseline_accepts_only_runtime_sample_bound_to_current_command(tmp_path):
    cron = tmp_path / "cron_jobs.json"
    state = tmp_path / "cron_state.json"
    previous = tmp_path / "baseline.json"
    command = "scripts/task.py --max-depth 20"
    command_sha256 = command_definition_sha256(command)
    cron.write_text(
        json.dumps([{"id": "safe", "enabled": True, "command": command}]),
        encoding="utf-8",
    )
    previous.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "duration_policy": {"single_observation_is_not_a_percentile": True},
                "representative_body_allowlist": [{"job_id": "safe"}],
                "invalidated_observations": [],
            }
        ),
        encoding="utf-8",
    )
    state.write_text(
        json.dumps(
            {
                "safe": {
                    "last_success": True,
                    "last_returncode": 0,
                    "last_timed_out": False,
                    "last_duration_sec": 5.0,
                    "last_success_at": "2026-07-15T02:00:00+00:00",
                    "command_sha256": command_sha256,
                }
            }
        ),
        encoding="utf-8",
    )

    payload = capture_baseline(
        cron_jobs_path=cron,
        runtime_state_path=state,
        previous_baseline_path=previous,
    )

    assert payload["observations"][0]["command_sha256"] == command_sha256
    assert payload["invalidated_observations"] == []
