from __future__ import annotations

import hashlib
import json
import plistlib
from pathlib import Path

import pytest

from scripts.v3_cutover.core import CutoverError
from scripts.v3_cutover.mutation import load_prepared_plan
from scripts.v3_cutover.mutation import REQUIRED_V2_APPLICATION_LABELS
from scripts.v3_cutover.planning import create_prepared_plan
from scripts.v3_pdf_namer_handoff import precopy


EXCLUDED_EVIDENCE = [
    "atomic_release_switch_and_cold_rollback_drill_passed",
    "human_go_approval_recorded",
]
REQUIRED_EVIDENCE = [
    *(f"legacy-evidence-{index}" for index in range(12)),
    *EXCLUDED_EVIDENCE,
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _legacy_gate(path: Path) -> None:
    _json(path, {"schema_version": 1, "required_evidence": REQUIRED_EVIDENCE})


def _v2_agent_bindings(directory: Path) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    bindings: list[str] = []
    for label in REQUIRED_V2_APPLICATION_LABELS:
        plist = directory / f"{label}.plist"
        plist.write_bytes(
            plistlib.dumps({"Label": label, "ProgramArguments": ["/usr/bin/false"]})
        )
        bindings.append(f"{label}={plist}")
    return bindings


def _loaded_launchd_probe(label: str) -> dict[str, object]:
    return {
        "argv": ["/bin/launchctl", "print", f"gui/501/{label}"],
        "returncode": 0,
        "stdout": "state = running\npid = 100\n",
        "stderr": "",
        "timed_out": False,
    }


@pytest.mark.parametrize(
    ("kind", "operation"),
    (("cutover", "v2_to_v3_cutover"), ("rollback", "v3_to_v2_rollback")),
)
def test_prepared_plan_generator_hash_binds_every_input(
    tmp_path: Path, kind: str, operation: str
) -> None:
    gates = tmp_path / "gates.json"
    _legacy_gate(gates)
    release_gate = tmp_path / "release-gate.json"
    _json(release_gate, {"schema_version": 1})
    report = tmp_path / "pre-cutover.json"
    _json(
        report,
        {
            "gate_config_sha256": _sha(gates),
            "execution_purpose": "atomic_drill",
            "gate_stage": "cutover_drill_12_of_14",
            "decision": "GO_FOR_CUTOVER_DRILL_ONLY",
            "required_evidence_count": 14,
            "passed_evidence_count": 12,
            "excluded_evidence": EXCLUDED_EVIDENCE,
            "release_gate_report": {
                "path": str(release_gate.resolve()),
                "sha256": _sha(release_gate),
            },
        },
    )
    release = tmp_path / "release-manifest.json"
    _json(release, {"release_id": "rc-test"})
    marker = tmp_path / "DEPLOY_PREPARED.json"
    _json(
        marker,
        {"release_id": "rc-test", "release_manifest_sha256": _sha(release)},
    )
    install = tmp_path / "LaunchAgents"
    v2_agents = _v2_agent_bindings(install)
    token = tmp_path / f"{kind}.token"
    token.write_text(f"{kind}-secret\n", encoding="utf-8")
    token.chmod(0o600)
    output = tmp_path / f"{kind}-plan.json"
    laf_source = tmp_path / "laf-processed.json"
    _json(laf_source, ["gmail-message-test"])
    laf_manifest = tmp_path / "laf-compat-manifest.json"
    db_env_file = tmp_path / "db.env"
    db_env_file.write_text("OSC_DB_PASSWORD=test-only\n", encoding="utf-8")
    db_env_file.chmod(0o600)
    pdf_source = tmp_path / "pdf-v2"
    pdf_source.mkdir()
    _json(pdf_source / "training_data.json", [{"synthetic": "value"}])
    pdf_destination = tmp_path / "pdf-v3"
    pdf_manifest = tmp_path / "pdf-handoff.json"
    precopy(pdf_source, pdf_destination, pdf_manifest, apply=True)
    laf_options = (
        {
            "laf_dedup_sources": [laf_source],
            "laf_dedup_manifest_output": laf_manifest,
            "laf_dedup_db_env_file": db_env_file,
            "pdf_namer_source": pdf_source,
            "pdf_namer_destination": pdf_destination,
            "pdf_namer_manifest": pdf_manifest,
        }
        if kind == "cutover"
        else {}
    )

    result = create_prepared_plan(
        operation=kind,
        execution_purpose="atomic_drill",
        output=output,
        token_file=token,
        gate_config=gates,
        pre_cutover_report=report,
        deploy_prepared_marker=marker,
        release_manifest=release,
        v2_launchagents=v2_agents,
        v3_install_directory=install,
        launchd_probe=_loaded_launchd_probe,
        **laf_options,
    )

    assert result["plan_sha256"] == _sha(output)
    assert result["mutation_performed"] is False
    assert token.exists()
    plan = load_prepared_plan(
        output,
        result["plan_sha256"],
        canonical_launchagents_directory=install,
    )
    assert plan.operation == operation
    assert (plan.laf_dedup_handoff is not None) is (kind == "cutover")
    assert (plan.pdf_namer_handoff is not None) is (kind == "cutover")
    document = json.loads(output.read_text(encoding="utf-8"))
    assert {row["label"] for row in document["v2_launchagents"]} == set(
        REQUIRED_V2_APPLICATION_LABELS
    )
    assert document["v2_application_set_sha256"] == plan.v2_application_set_sha256
    for name in (
        "gate_config",
        "pre_cutover_report",
        "deploy_prepared_marker",
        "release_manifest",
    ):
        assert document[name]["sha256"] == _sha(Path(document[name]["path"]))

    report.write_text("drift\n", encoding="utf-8")
    with pytest.raises(CutoverError, match="SHA-256 mismatch"):
        load_prepared_plan(
            output,
            result["plan_sha256"],
            canonical_launchagents_directory=install,
        )


def test_plan_generator_rejects_insecure_token_and_mismatched_release(tmp_path: Path) -> None:
    gates = tmp_path / "gates.json"
    _legacy_gate(gates)
    release_gate = tmp_path / "release-gate.json"
    _json(release_gate, {"schema_version": 1})
    report = tmp_path / "report.json"
    _json(
        report,
        {
            "gate_config_sha256": _sha(gates),
            "execution_purpose": "atomic_drill",
            "gate_stage": "cutover_drill_12_of_14",
            "decision": "GO_FOR_CUTOVER_DRILL_ONLY",
            "required_evidence_count": 14,
            "passed_evidence_count": 12,
            "excluded_evidence": EXCLUDED_EVIDENCE,
            "release_gate_report": {
                "path": str(release_gate.resolve()),
                "sha256": _sha(release_gate),
            },
        },
    )
    release = tmp_path / "release.json"
    _json(release, {"release_id": "one"})
    marker = tmp_path / "marker.json"
    _json(marker, {"release_id": "two", "release_manifest_sha256": _sha(release)})
    install = tmp_path / "LaunchAgents"
    v2_agents = _v2_agent_bindings(install)
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    token.chmod(0o644)
    laf_source = tmp_path / "laf-processed.json"
    _json(laf_source, [])
    db_env_file = tmp_path / "db.env"
    db_env_file.write_text("OSC_DB_PASSWORD=test-only\n", encoding="utf-8")
    db_env_file.chmod(0o600)
    pdf_source = tmp_path / "pdf-v2"
    pdf_source.mkdir()
    pdf_destination = tmp_path / "pdf-v3"
    pdf_manifest = tmp_path / "pdf-handoff.json"
    precopy(pdf_source, pdf_destination, pdf_manifest, apply=True)

    with pytest.raises(CutoverError, match="deploy marker"):
        create_prepared_plan(
            operation="cutover",
            execution_purpose="atomic_drill",
            output=tmp_path / "plan.json",
            token_file=token,
            gate_config=gates,
            pre_cutover_report=report,
            deploy_prepared_marker=marker,
            release_manifest=release,
            v2_launchagents=v2_agents,
            v3_install_directory=install,
            laf_dedup_sources=[laf_source],
            laf_dedup_manifest_output=tmp_path / "laf-manifest.json",
            laf_dedup_db_env_file=db_env_file,
            pdf_namer_source=pdf_source,
            pdf_namer_destination=pdf_destination,
            pdf_namer_manifest=pdf_manifest,
            launchd_probe=_loaded_launchd_probe,
        )

    _json(marker, {"release_id": "one", "release_manifest_sha256": _sha(release)})
    with pytest.raises(CutoverError, match="0600"):
        create_prepared_plan(
            operation="rollback",
            execution_purpose="atomic_drill",
            output=tmp_path / "rollback.json",
            token_file=token,
            gate_config=gates,
            pre_cutover_report=report,
            deploy_prepared_marker=marker,
            release_manifest=release,
            v2_launchagents=v2_agents,
            v3_install_directory=install,
            launchd_probe=_loaded_launchd_probe,
        )
