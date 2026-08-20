from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from scripts.v3_validation.isolated_resource_window_plan_builder import (
    PROVISIONAL_GATE_IDS,
    ResourceWindowPlanError,
    build_plan,
)
from scripts.v3_validation.isolated_resource_window import sha256_json
from scripts.v3_validation.provisional_resource_window_execute import verify_plan


ROOT = Path(__file__).resolve().parents[2]
ADMIN_FIXTURE_BYTES = (
    b"from __future__ import annotations\n\n"
    b"class AdminHandler:\n"
    b"    synthetic_resource_window_fixture = True\n"
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path: Path, *, bundle_website: bool = False) -> dict:
    release = (tmp_path / "release").resolve()
    (release / "config").mkdir(parents=True)
    (release / "scripts/v3_validation").mkdir(parents=True)
    (release / "scripts/ops").mkdir(parents=True)
    (release / "magi_v3").mkdir(parents=True)
    (release / "bin").mkdir(parents=True)
    policy = release / "config/v3_resource_policy.json"
    shutil.copy2(ROOT / "config/v3_resource_policy.json", policy)
    members = [policy]
    for name in (
        "resource_window_core_adapter.py",
        "resource_window_model_adapter.py",
        "isolated_resource_window_collector.py",
    ):
        target = release / "scripts/v3_validation" / name
        shutil.copy2(ROOT / "scripts/v3_validation" / name, target)
        members.append(target)
    for relative in (
        "config/v3_resource_window.sb",
        "config/v3_service_manifest.json",
        "config/v3_launchagent_roles.json",
        "scripts/ops/run_daemon_no_site.py",
        "daemon.py",
        "magi_v3/control.py",
        "magi_v3/gateway.py",
        "magi_v3/supervisor_service.py",
    ):
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
        members.append(target)
    if bundle_website:
        bundled_admin = release / "whalechao.github.io/admin/admin_server.py"
        bundled_admin.parent.mkdir(parents=True)
        bundled_admin.write_bytes(ADMIN_FIXTURE_BYTES)
        members.append(bundled_admin)
    runtime = release / "bin/magi-v3-python"
    shutil.copy2(Path(sys.executable).resolve(), runtime)
    runtime.chmod(0o555)
    members.append(runtime)
    snapshot = "2" * 64
    manifest = {
        "release_id": "v3-test",
        "schema_version": 1,
        "immutable": True,
        "release_sha256": snapshot,
        "source_snapshot_sha256": snapshot,
        "files": [
            {"path": path.relative_to(release).as_posix(), "sha256": sha(path)}
            for path in members
        ],
    }
    manifest_path = release / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    for path in [*members, manifest_path]:
        path.chmod(0o555 if path == runtime else 0o444)
    for path in sorted((item for item in release.rglob("*") if item.is_dir()), reverse=True):
        path.chmod(0o555)
    release.chmod(0o555)
    model = (tmp_path / "model").resolve()
    model.mkdir()
    (model / "weights.bin").write_bytes(b"weights")
    prompt = (tmp_path / "prompt.txt").resolve()
    prompt.write_text("請摘要這份離線測試資料。")
    website = (tmp_path / "website").resolve()
    (website / "admin").mkdir(parents=True)
    (website / "admin/admin_server.py").write_bytes(ADMIN_FIXTURE_BYTES)
    external = (tmp_path / "external").resolve()
    external.mkdir()
    config = external / "config.json"
    credentials = external / "credentials.json"
    config.write_text("{}\n")
    credentials.write_text("{}\n")
    config.chmod(0o600)
    credentials.chmod(0o600)
    calendar_token = external / "google-calendar-token.json"
    laf_token = external / "laf-gmail-token.pickle"
    file_review_token = external / "filereview-token.pickle"
    for token in (calendar_token, laf_token, file_review_token):
        token.write_bytes(b"inert-token\n")
    outer = (tmp_path / "outer.json").resolve()
    context = {"campaign_id": "campaign", "release_sha": "1" * 64, "hardware_id": "mac", "gate_config_sha256": "2" * 64}
    gate = (tmp_path / "gate.json").resolve()
    gate.write_text(json.dumps({
        "schema": "magi.v3.provisional-resource-window-gate/v1",
        "status": "provisional_16_of_19_passed",
        "formal_live_eligible": False,
        **context,
        "release_manifest_sha256": sha(manifest_path),
        "required_evidence": list(PROVISIONAL_GATE_IDS),
        "excluded_resource_evidence": sorted([
            "matched_v2_warm_cold_performance_baseline_complete",
            "resource_policy_all_budgets_passed",
            "worker_process_group_footprint_and_metal_return_to_baseline",
        ]),
        "counts": {"required": 16, "passed": 16, "failed": 0, "missing": 0, "invalid": 0},
    }, sort_keys=True))
    gate.chmod(0o400)
    unsigned_outer = {
        "schema": "magi.v3.provisional-resource-window-plan/v1",
        "operation": "isolated_resource_window_validation",
        "context": context,
        "release_manifest": {"path": str(manifest_path), "sha256": sha(manifest_path)},
        "provisional_gate_report": {"path": str(gate), "sha256": sha(gate)},
        "token_sha256": "3" * 64,
    }
    unsigned_outer["plan_sha256"] = hashlib.sha256(
        (json.dumps(unsigned_outer, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    outer.write_text(json.dumps(unsigned_outer, sort_keys=True))
    outer.chmod(0o400)
    return {
        "release": release,
        "manifest": manifest_path,
        "model": model,
        "prompt": prompt,
        "website": website,
        "outer": outer,
        "runtime": runtime,
        "plan": (tmp_path / "prepared/resource-plan.json").resolve(),
        "token": (tmp_path / "prepared/resource-token.json").resolve(),
        "workdir": (tmp_path / "run").resolve(),
        "config": config,
        "credentials": credentials,
        "calendar_token": calendar_token,
        "laf_token": laf_token,
        "file_review_token": file_review_token,
    }


def invoke(paths: dict) -> dict[str, str]:
    return build_plan(
        release_root=paths["release"],
        release_manifest_sha256=sha(paths["manifest"]),
        python_runtime=paths["runtime"],
        model_root=paths["model"],
        prompt_path=paths["prompt"],
        outer_plan=paths["outer"],
        outer_plan_sha256=sha(paths["outer"]),
        output_plan=paths["plan"],
        output_token=paths["token"],
        workdir=paths["workdir"],
        model_backend_kind="mlx_vlm",
        website_root=paths["website"],
        website_admin_sha256=sha(paths["website"] / "admin/admin_server.py"),
        config_file=paths["config"],
        config_sha256=sha(paths["config"]),
        google_credentials_file=paths["credentials"],
        google_credentials_sha256=sha(paths["credentials"]),
        google_calendar_token_file=paths["calendar_token"],
        google_calendar_token_sha256=sha(paths["calendar_token"]),
        laf_gmail_token_file=paths["laf_token"],
        laf_gmail_token_sha256=sha(paths["laf_token"]),
        file_review_token_file=paths["file_review_token"],
        file_review_token_sha256=sha(paths["file_review_token"]),
    )


def external_kwargs(paths: dict) -> dict[str, object]:
    return {
        "config_file": paths["config"],
        "config_sha256": sha(paths["config"]),
        "google_credentials_file": paths["credentials"],
        "google_credentials_sha256": sha(paths["credentials"]),
        "google_calendar_token_file": paths["calendar_token"],
        "google_calendar_token_sha256": sha(paths["calendar_token"]),
        "laf_gmail_token_file": paths["laf_token"],
        "laf_gmail_token_sha256": sha(paths["laf_token"]),
        "file_review_token_file": paths["file_review_token"],
        "file_review_token_sha256": sha(paths["file_review_token"]),
    }


def test_builder_deep_verifies_and_writes_0400_plan_0600_secret_without_executing(
    tmp_path: Path,
) -> None:
    paths = fixture(tmp_path)
    result = invoke(paths)
    plan = json.loads(paths["plan"].read_text())
    token = json.loads(paths["token"].read_text())

    assert result["status"] == "prepared_not_executed"
    assert paths["plan"].stat().st_mode & 0o777 == 0o400
    assert paths["token"].stat().st_mode & 0o777 == 0o600
    assert token["approval_token"] not in json.dumps(result)
    assert token["zero_owner_phase_token"] not in json.dumps(result)
    assert plan["durations"]["v3_deep_idle_seconds"] == 1800
    assert plan["commands"]["model_repeats"] == 3
    assert plan["orchestration_binding"]["v2_restore_owner"] == (
        "outer_isolated_live_executor_finally"
    )
    assert plan["plan_sha256"] == sha256_json(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )
    assert plan["outer_owner_contract"]["outer_restore_readiness"]["v2"] == [
        "http://127.0.0.1:5002/health",
        "http://127.0.0.1:5003/health",
        "http://127.0.0.1:5014/health",
        "http://127.0.0.1:8088/health",
    ]
    assert plan["external_inputs"]["website_root"] == str(paths["website"])
    assert plan["external_inputs"]["laf_config_file"] == str(paths["config"])
    assert plan["external_inputs"]["google_credentials_file"] == str(paths["credentials"])
    assert plan["external_inputs"]["google_calendar_token_source_sha256"] == sha(
        paths["calendar_token"]
    )
    assert not paths["workdir"].exists()
    outer, inner = verify_plan(
        paths["outer"],
        sha(paths["outer"]),
        paths["plan"],
        sha(paths["plan"]),
    )
    assert outer["operation"] == "isolated_resource_window_validation"
    assert inner["plan_sha256"] == plan["plan_sha256"]


def test_builder_binds_verified_external_python_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = fixture(tmp_path)
    runtime = Path(sys.executable).resolve(strict=True)
    runtime_manifest = (tmp_path / "external/python-runtime-manifest.json").resolve()
    runtime_manifest.parent.mkdir(exist_ok=True)
    runtime_manifest.write_text('{"schema_version":1}\n', encoding="utf-8")
    paths["runtime"] = runtime
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME", str(runtime))
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_REALPATH", str(runtime))
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_SHA256", sha(runtime))
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_MANIFEST", str(runtime_manifest))
    monkeypatch.setenv(
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256", sha(runtime_manifest)
    )
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_TREE_SHA256", "a" * 64)

    result = invoke(paths)
    plan = json.loads(paths["plan"].read_text(encoding="utf-8"))
    binding = plan["release_binding"]["python_runtime_binding"]

    assert result["status"] == "prepared_not_executed"
    assert binding == {
        "kind": "manifest_bound_external",
        "path": str(runtime),
        "launcher_path": str(runtime),
        "sha256": sha(runtime),
        "realpath": str(runtime),
        "manifest": str(runtime_manifest),
        "manifest_sha256": sha(runtime_manifest),
        "tree_sha256": "a" * 64,
    }
    assert plan["commands"]["v2_core"][0][0] == str(runtime)


def test_builder_accepts_sealed_candidate_without_bundled_website(tmp_path: Path) -> None:
    paths = fixture(tmp_path)

    assert not (paths["release"] / "whalechao.github.io").exists()
    manifest = json.loads(paths["manifest"].read_text())
    assert not any(
        str(row["path"]).startswith("whalechao.github.io/")
        for row in manifest["files"]
    )

    invoke(paths)
    plan = json.loads(paths["plan"].read_text())
    assert plan["workload_binding"]["composition"]["external_inputs"] == plan[
        "external_inputs"
    ]


def test_builder_rejects_sealed_candidate_that_bundles_external_website(
    tmp_path: Path,
) -> None:
    paths = fixture(tmp_path, bundle_website=True)

    with pytest.raises(ResourceWindowPlanError, match="must not be bundled"):
        invoke(paths)

    assert not paths["plan"].exists()
    assert not paths["token"].exists()


def test_builder_rejects_release_member_tamper_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    paths = fixture(tmp_path)
    target = paths["release"] / "scripts/v3_validation/resource_window_core_adapter.py"
    target.chmod(0o644)
    target.write_text("tampered")
    paths["release"].chmod(0o555)

    with pytest.raises(ResourceWindowPlanError, match="hash mismatch"):
        invoke(paths)

    assert not paths["plan"].exists()
    assert not paths["token"].exists()


def test_builder_rejects_external_website_hash_mismatch(tmp_path: Path) -> None:
    paths = fixture(tmp_path)
    admin = paths["website"] / "admin/admin_server.py"
    expected = sha(admin)
    admin.write_text("tampered", encoding="utf-8")

    with pytest.raises(ResourceWindowPlanError, match="SHA-256 mismatch"):
        build_plan(
            release_root=paths["release"],
            release_manifest_sha256=sha(paths["manifest"]),
            python_runtime=paths["runtime"],
            model_root=paths["model"],
            prompt_path=paths["prompt"],
            outer_plan=paths["outer"],
            outer_plan_sha256=sha(paths["outer"]),
            output_plan=paths["plan"],
            output_token=paths["token"],
            workdir=paths["workdir"],
            model_backend_kind="mlx_vlm",
            website_root=paths["website"],
            website_admin_sha256=expected,
            **external_kwargs(paths),
        )

    assert not paths["plan"].exists()
    assert not paths["token"].exists()


def test_builder_rejects_external_website_admin_symlink(tmp_path: Path) -> None:
    paths = fixture(tmp_path)
    admin = paths["website"] / "admin/admin_server.py"
    expected = sha(admin)
    admin.unlink()
    symlink_target = tmp_path / "synthetic-admin-target.py"
    symlink_target.write_bytes(ADMIN_FIXTURE_BYTES)
    admin.symlink_to(symlink_target)

    with pytest.raises(ResourceWindowPlanError, match="must not traverse symlinks"):
        build_plan(
            release_root=paths["release"],
            release_manifest_sha256=sha(paths["manifest"]),
            python_runtime=paths["runtime"],
            model_root=paths["model"],
            prompt_path=paths["prompt"],
            outer_plan=paths["outer"],
            outer_plan_sha256=sha(paths["outer"]),
            output_plan=paths["plan"],
            output_token=paths["token"],
            workdir=paths["workdir"],
            model_backend_kind="mlx_vlm",
            website_root=paths["website"],
            website_admin_sha256=expected,
            **external_kwargs(paths),
        )

    assert not paths["plan"].exists()
    assert not paths["token"].exists()
