from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from magi_v3.cron_policy import CronDispatchPolicyError, load_cron_dispatch_policy


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate_cron_snapshot_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAGI_CRON_JOBS_FILE", raising=False)
    monkeypatch.delenv("MAGI_CRON_JOBS_SHA256", raising=False)
    monkeypatch.delenv("MAGI_CRON_JOBS_SOURCE_SHA256", raising=False)


def test_policy_is_hash_bound_and_preserves_global_resource_caps() -> None:
    policy = load_cron_dispatch_policy(ROOT)

    assert policy.max_workers == 4
    assert policy.lane_caps == {"light": 2, "batch": 2, "maintenance": 2}
    assert policy.can_start_lane("light", []) is True
    assert policy.can_start_lane("light", ["light", "light"]) is False
    assert policy.can_start_lane("batch", ["maintenance"]) is True
    assert policy.can_start_lane("maintenance", ["batch"]) is True
    assert policy.can_start_lane("batch", ["maintenance", "batch"]) is False
    assert policy.can_start_lane("maintenance", ["light", "light"]) is True
    assert max(policy.phase_delay_seconds.values()) <= 6 * 3600
    assert policy.phase_delay_seconds["job_drive_case_sync_bidirectional"] == 3000


def test_policy_loads_hash_bound_external_snapshot_without_release_root_cron(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "immutable-release"
    (root / "config").mkdir(parents=True)
    (root / "config" / "v3_schedule_dispatch_policy.json").write_bytes(
        (ROOT / "config" / "v3_schedule_dispatch_policy.json").read_bytes()
    )
    snapshot = tmp_path / "runtime" / "cron_jobs.snapshot.json"
    snapshot.parent.mkdir()
    snapshot.write_bytes((ROOT / "cron_jobs.json").read_bytes())
    snapshot_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", str(snapshot))
    monkeypatch.setenv("MAGI_CRON_JOBS_SHA256", snapshot_sha)
    monkeypatch.setenv("MAGI_CRON_JOBS_SOURCE_SHA256", snapshot_sha)

    policy = load_cron_dispatch_policy(root)

    assert not (root / "cron_jobs.json").exists()
    assert policy.cron_jobs_sha256 == snapshot_sha


def test_policy_accepts_rebased_external_snapshot_with_distinct_trusted_source_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "immutable-release"
    (root / "config").mkdir(parents=True)
    policy_path = ROOT / "config" / "v3_schedule_dispatch_policy.json"
    (root / "config" / policy_path.name).write_bytes(policy_path.read_bytes())
    source_payload = (ROOT / "cron_jobs.json").read_bytes()
    source_sha = hashlib.sha256(source_payload).hexdigest()
    jobs = json.loads(source_payload.decode("utf-8"))
    source_root = str(ROOT)
    configured_source_root = str(ROOT.parent / "source-v3-current")
    rebased_job = next(
        job
        for job in jobs
        if source_root in str(job.get("command") or "")
        or configured_source_root in str(job.get("command") or "")
    )
    original_command = rebased_job["command"]
    rebased_job["command"] = original_command.replace(
        configured_source_root, "/immutable/releases/magi-v3"
    ).replace(source_root, "/immutable/releases/magi-v3")
    assert rebased_job["command"] != original_command
    snapshot = tmp_path / "runtime" / "cron_jobs.v3.json"
    snapshot.parent.mkdir()
    snapshot.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    snapshot_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    assert snapshot_sha != source_sha
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", str(snapshot))
    monkeypatch.setenv("MAGI_CRON_JOBS_SHA256", snapshot_sha)
    monkeypatch.setenv("MAGI_CRON_JOBS_SOURCE_SHA256", source_sha)

    policy = load_cron_dispatch_policy(root)

    assert policy.cron_jobs_sha256 == snapshot_sha


def test_external_snapshot_binding_cannot_fall_back_or_bypass_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "release"
    (root / "config").mkdir(parents=True)
    (root / "config" / "v3_schedule_dispatch_policy.json").write_bytes(
        (ROOT / "config" / "v3_schedule_dispatch_policy.json").read_bytes()
    )
    (root / "cron_jobs.json").write_bytes((ROOT / "cron_jobs.json").read_bytes())
    snapshot = tmp_path / "external.json"
    snapshot.write_bytes((ROOT / "cron_jobs.json").read_bytes() + b"\n")
    snapshot_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", str(snapshot))

    with pytest.raises(CronDispatchPolicyError, match="binding is incomplete"):
        load_cron_dispatch_policy(root)

    monkeypatch.setenv("MAGI_CRON_JOBS_SHA256", snapshot_sha)
    with pytest.raises(CronDispatchPolicyError, match="binding is incomplete"):
        load_cron_dispatch_policy(root)

    monkeypatch.setenv("MAGI_CRON_JOBS_SOURCE_SHA256", "0" * 64)
    with pytest.raises(CronDispatchPolicyError, match="policy binding drifted"):
        load_cron_dispatch_policy(root)


def test_external_snapshot_rejects_relative_path_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "release"
    (root / "config").mkdir(parents=True)
    (root / "config" / "v3_schedule_dispatch_policy.json").write_bytes(
        (ROOT / "config" / "v3_schedule_dispatch_policy.json").read_bytes()
    )
    source = tmp_path / "cron_jobs.json"
    source.write_bytes((ROOT / "cron_jobs.json").read_bytes())
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", "cron_jobs.json")
    monkeypatch.setenv("MAGI_CRON_JOBS_SHA256", source_sha)
    monkeypatch.setenv("MAGI_CRON_JOBS_SOURCE_SHA256", source_sha)

    with pytest.raises(CronDispatchPolicyError, match="path must be absolute"):
        load_cron_dispatch_policy(root)

    link = tmp_path / "cron_jobs.link.json"
    link.symlink_to(source)
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", str(link))
    with pytest.raises(CronDispatchPolicyError, match="non-symlink regular file"):
        load_cron_dispatch_policy(root)


def test_policy_binding_and_phase_delays_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "release"
    (root / "config").mkdir(parents=True)
    (root / "cron_jobs.json").write_bytes((ROOT / "cron_jobs.json").read_bytes())
    policy = json.loads(
        (ROOT / "config" / "v3_schedule_dispatch_policy.json").read_text(encoding="utf-8")
    )
    policy["cron_jobs_sha256"] = "0" * 64
    (root / "config" / "v3_schedule_dispatch_policy.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    with pytest.raises(CronDispatchPolicyError, match="binding drifted"):
        load_cron_dispatch_policy(root)

    policy["cron_jobs_sha256"] = json.loads(
        (ROOT / "config" / "v3_schedule_dispatch_policy.json").read_text(encoding="utf-8")
    )["cron_jobs_sha256"]
    policy["phase_delay_seconds"]["job_pdf_namer_nightly"] = 6 * 3600 + 1
    (root / "config" / "v3_schedule_dispatch_policy.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    with pytest.raises(CronDispatchPolicyError, match="out of bounds"):
        load_cron_dispatch_policy(root)
