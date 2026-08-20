from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.v3_cutover.cli import _evidence_report, run
from scripts.v3_cutover.core import CutoverError, load_gate_config

GATES = Path(__file__).resolve().parents[2] / "config" / "v3_cutover_gates.json"


def test_plan_is_explicitly_non_mutating() -> None:
    code, report = run(["--gates", str(GATES), "plan", "live-validation"])
    assert code == 0
    assert report["mutation_performed"] is False
    assert [step["action"] for step in report["steps"]].count("start") == 2
    assert [step["action"] for step in report["steps"]].count("stop") == 2


def test_simulate_clean_and_fault_exit_codes() -> None:
    clean_code, clean = run(["--gates", str(GATES), "simulate", "cutover"])
    fault_code, fault = run(
        ["--gates", str(GATES), "simulate", "cutover", "--residual", "v2:scheduler"]
    )
    assert clean_code == 0 and clean["ok"] is True
    assert fault_code == 2 and fault["ok"] is False
    assert clean["mutation_performed"] is False
    assert fault["mutation_performed"] is False


def test_cli_reports_are_json_serializable() -> None:
    _, report = run(["--gates", str(GATES), "plan", "rollback"])
    assert json.loads(json.dumps(report))["workflow"] == "rollback"


def test_preflight_uses_release_bound_conditional_daytime_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.v3_cutover import cli

    observed: dict[str, object] = {}
    monkeypatch.setattr(cli, "discover_release_spec", lambda *args, **kwargs: args[0])
    snapshot = SimpleNamespace(to_dict=lambda: {"owners": []})
    monkeypatch.setattr(cli, "collect_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(
        cli,
        "assess_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(
            go=True,
            to_dict=lambda: {"go": True},
        ),
    )

    def absolute(window):
        observed["window"] = window
        return {"within_window": True, "kind": "conditional_daytime"}

    monkeypatch.setattr(cli, "assess_absolute_window", absolute)
    monkeypatch.setattr(
        cli,
        "assess_cutover_window",
        lambda *_args, **_kwargs: pytest.fail("legacy window must not be used"),
    )
    monkeypatch.setattr(cli, "evaluate_evidence", lambda *_args, **_kwargs: {"decision": "GO"})
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    gate_sha = hashlib.sha256(GATES.read_bytes()).hexdigest()

    code, report = run(
        [
            "--gates",
            str(GATES),
            "preflight",
            "--expect",
            "v2",
            "--evidence-dir",
            str(evidence_dir),
            "--campaign-id",
            "campaign",
            "--release-sha",
            "a" * 64,
            "--hardware-id",
            "hardware",
            "--gate-config-sha256",
            gate_sha,
        ]
    )

    assert code == 0
    assert report["cutover_window_mode"] == "conditional_daytime"
    assert observed["window"] == load_gate_config(GATES)["conditional_daytime_window"]


def test_boolean_evidence_report_shortcut_is_disabled(tmp_path: Path) -> None:
    gates = load_gate_config(GATES)
    evidence_path = tmp_path / "evidence.json"
    first = gates["required_evidence"][0]
    evidence_path.write_text(json.dumps({first: True}), encoding="utf-8")
    with pytest.raises(CutoverError, match="boolean evidence JSON is disabled"):
        _evidence_report(gates, evidence_path)


def test_preflight_requires_explicit_expected_state_and_release_context() -> None:
    with pytest.raises(SystemExit):
        run(["--gates", str(GATES), "preflight"])


def test_execute_is_explicit_and_requires_plan_hash_and_secure_token_file() -> None:
    with pytest.raises(SystemExit):
        run(["--gates", str(GATES), "execute"])


def test_execute_allows_preinstall_v3_probe_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prepared-but-not-installed V3 has neither PID files nor launchd labels yet."""

    from scripts.v3_cutover import cli

    release_manifest = tmp_path / "release" / "release-manifest.json"
    release_manifest.parent.mkdir()
    release_manifest.write_text("{}\n", encoding="utf-8")
    plan = SimpleNamespace(
        operation="v2_to_v3_cutover",
        gate_config=SimpleNamespace(path=GATES.resolve()),
        release_manifest=SimpleNamespace(path=release_manifest.resolve()),
    )
    observed: list[tuple[str, dict[str, object]]] = []

    def fake_discover(name, root, namespace, **kwargs):
        del root, namespace
        observed.append((name, kwargs))
        return name

    class FakeExecutor:
        def __init__(self, _plan, *, snapshot_collector, **_kwargs):
            self.snapshot_collector = snapshot_collector

        def execute(self):
            self.snapshot_collector()
            return {"ok": True, "mutation_performed": False}

    monkeypatch.setattr(cli, "load_prepared_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(cli, "discover_release_spec", fake_discover)
    monkeypatch.setattr(cli, "collect_snapshot", lambda specs, ports: (specs, ports))
    monkeypatch.setattr(cli, "PreparedCutoverExecutor", FakeExecutor)

    code, report = cli.run(
        [
            "--gates",
            str(GATES),
            "execute",
            "--plan",
            str(tmp_path / "plan.json"),
            "--plan-sha256",
            "0" * 64,
            "--token-file",
            str(tmp_path / "token"),
            "--report-output",
            str((tmp_path / "execution-report.json").resolve()),
        ]
    )

    assert code == 0 and report["ok"] is True
    receipt = tmp_path / "execution-report.json"
    assert json.loads(receipt.read_text(encoding="utf-8"))["ok"] is True
    assert receipt.stat().st_mode & 0o777 == 0o400
    v3_kwargs = dict(observed)["v3"]
    assert v3_kwargs["pidfiles_required"] is False
    assert v3_kwargs["launchd_labels_required"] is False


def test_prepare_plan_cli_passes_all_laf_handoff_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.v3_cutover import cli

    observed = {}

    def fake_prepare(**kwargs):
        observed.update(kwargs)
        return {
            "schema_version": 1,
            "operation": "v2_to_v3_cutover",
            "plan": str(kwargs["output"]),
            "plan_sha256": "a" * 64,
            "mutation_performed": False,
        }

    monkeypatch.setattr(cli, "create_prepared_plan", fake_prepare)
    source = tmp_path / "processed.json"
    output = tmp_path / "compat.json"
    env_file = tmp_path / "db.env"
    pdf_source = tmp_path / "pdf-v2"
    pdf_destination = tmp_path / "pdf-v3"
    pdf_manifest = tmp_path / "pdf-handoff.json"
    mutable_source = tmp_path / "mutable-v2"
    mutable_target = tmp_path / "mutable-v3"
    mutable_dry = tmp_path / "receipts" / "mutable-dry.json"
    mutable_prepare = tmp_path / "receipts" / "mutable-prepare.json"
    mutable_stage = tmp_path / "mutable-stage"
    code, _report = cli.run(
        [
            "--gates",
            str(GATES),
            "prepare-plan",
            "cutover",
            "--execution-purpose",
            "atomic_drill",
            "--output",
            str(tmp_path / "plan.json"),
            "--token-file",
            str(tmp_path / "token"),
            "--pre-cutover-report",
            str(tmp_path / "pre.json"),
            "--deploy-prepared-marker",
            str(tmp_path / "deploy.json"),
            "--release-manifest",
            str(tmp_path / "release.json"),
            "--laf-dedup-source",
            str(source),
            "--laf-dedup-manifest-output",
            str(output),
            "--laf-dedup-db-env-file",
            str(env_file),
            "--pdf-namer-source",
            str(pdf_source),
            "--pdf-namer-destination",
            str(pdf_destination),
            "--pdf-namer-manifest",
            str(pdf_manifest),
            "--mutable-state-source-root",
            str(mutable_source),
            "--mutable-state-target-shared-root",
            str(mutable_target),
            "--mutable-state-dry-run-receipt",
            str(mutable_dry),
            "--mutable-state-prepare-receipt",
            str(mutable_prepare),
            "--mutable-state-staging-root",
            str(mutable_stage),
        ]
    )

    assert code == 0
    assert observed["laf_dedup_sources"] == [source]
    assert observed["execution_purpose"] == "atomic_drill"
    assert observed["laf_dedup_manifest_output"] == output
    assert observed["laf_dedup_db_env_file"] == env_file
    assert observed["pdf_namer_source"] == pdf_source
    assert observed["pdf_namer_destination"] == pdf_destination
    assert observed["pdf_namer_manifest"] == pdf_manifest
    assert observed["mutable_state_source_root"] == mutable_source
    assert observed["mutable_state_target_shared_root"] == mutable_target
