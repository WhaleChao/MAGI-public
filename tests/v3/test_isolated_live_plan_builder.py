from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.v3_validation.isolated_live_execute import (
    IsolatedLiveBlocked,
    load_isolated_live_plan,
    verify_static_plan,
)
from scripts.v3_validation.isolated_live_plan_builder import create_isolated_live_plan
from tests.v3.test_isolated_live_execute import _json, _prepared


def _build(tmp_path: Path) -> tuple[dict[str, str], object, Path]:
    prepared = _prepared(tmp_path)
    prepared.plan.unlink()
    output = (tmp_path / "plans" / "isolated-live-run-1.json").resolve()
    result = create_isolated_live_plan(
        plan_id="isolated-live-run-1",
        output=output,
        token_file=prepared.token,
        release_manifest=prepared.release_manifest,
        deploy_manifest=prepared.deploy_manifest,
        deploy_prepared_marker=prepared.marker,
        offline_gate_report=prepared.offline,
    )
    return result, prepared, output


def test_builder_publishes_only_a_statically_verified_plan(tmp_path: Path) -> None:
    result, prepared, output = _build(tmp_path)

    assert result["status"] == "prepared_not_executed"
    assert result["plan_path"] == str(output)
    assert output.stat().st_mode & 0o777 == 0o400
    assert prepared.token.exists(), "planning must not consume the one-time token"
    plan = load_isolated_live_plan(output, result["plan_sha256"])
    deployment = verify_static_plan(plan)
    assert deployment.release_id == result["release_id"]


def test_builder_fails_closed_before_publishing_on_non_go_offline_gate(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    prepared.plan.unlink()
    payload = json.loads(prepared.offline.read_text(encoding="utf-8"))
    payload["status"] = "NO_GO"
    _json(prepared.offline, payload)
    output = (tmp_path / "plans" / "blocked.json").resolve()

    with pytest.raises(IsolatedLiveBlocked, match="clean GO"):
        create_isolated_live_plan(
            plan_id="isolated-live-blocked",
            output=output,
            token_file=prepared.token,
            release_manifest=prepared.release_manifest,
            deploy_manifest=prepared.deploy_manifest,
            deploy_prepared_marker=prepared.marker,
            offline_gate_report=prepared.offline,
        )

    assert not output.exists()
    assert not list(output.parent.glob(".*.staging-*"))
    assert prepared.token.exists()


def test_builder_rejects_unsafe_token_and_existing_output(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    prepared.plan.unlink()
    output = (tmp_path / "plans" / "existing.json").resolve()
    output.parent.mkdir()
    output.write_text("preserve me", encoding="utf-8")

    with pytest.raises(IsolatedLiveBlocked, match="already exists"):
        create_isolated_live_plan(
            plan_id="isolated-live-existing",
            output=output,
            token_file=prepared.token,
            release_manifest=prepared.release_manifest,
            deploy_manifest=prepared.deploy_manifest,
            deploy_prepared_marker=prepared.marker,
            offline_gate_report=prepared.offline,
        )
    assert output.read_text(encoding="utf-8") == "preserve me"

    output.unlink()
    prepared.token.chmod(0o644)
    with pytest.raises(IsolatedLiveBlocked, match="0600"):
        create_isolated_live_plan(
            plan_id="isolated-live-token-mode",
            output=output,
            token_file=prepared.token,
            release_manifest=prepared.release_manifest,
            deploy_manifest=prepared.deploy_manifest,
            deploy_prepared_marker=prepared.marker,
            offline_gate_report=prepared.offline,
        )
    assert not output.exists()
