from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.v3_release_gate import (
    BoundArtifact,
    EVIDENCE_SPECS,
    validate_evidence_semantics,
)
from scripts.v3_validation.human_approval import (
    EVIDENCE_ID,
    HumanApprovalBlocked,
    _machine_sources,
    build_approval_request,
    build_conditional_approval_request,
    capture_local_approval,
    capture_conditional_local_approval,
    compile_conditional_human_approval_evidence,
    compile_human_approval_evidence,
    derive_conditional_human_approval_metrics,
    redeem_conditional_approval,
)


NOW = datetime(2026, 7, 17, 2, 0, tzinfo=timezone.utc)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write(path: Path, value: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(value))
    path.chmod(mode)


def _fixture(tmp_path: Path) -> dict[str, object]:
    required = list(EVIDENCE_SPECS)
    assert required[-1] == EVIDENCE_ID and len(required) == 28
    config_path = (tmp_path / "gates.json").resolve()
    config = {
        "schema_version": 1,
        "required_evidence": required,
        "promotion_thresholds": {},
        "conditional_daytime_window": {
            "starts_at": (NOW + timedelta(hours=1)).isoformat(),
            "ends_at": (NOW + timedelta(hours=2)).isoformat(),
            "timezone": "Asia/Taipei",
        },
    }
    _write(config_path, config, 0o400)
    context = {
        "campaign_id": "approval-campaign",
        "release_sha": "1" * 64,
        "hardware_id": "mac-mini",
        "gate_config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
    }
    evidence_dir = (tmp_path / "evidence").resolve()
    evidence_dir.mkdir()
    for index, evidence_id in enumerate(required[:-1], start=1):
        artifact = evidence_dir / "reports" / f"{evidence_id}.json"
        _write(artifact, {"evidence_id": evidence_id, "index": index})
        artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        envelope = {
            "schema_version": 1,
            "evidence_id": evidence_id,
            "status": "passed",
            **context,
            "artifacts": [
                {
                    "role": "producer_report",
                    "media_type": "application/json",
                    "path": f"reports/{evidence_id}.json",
                    "sha256": artifact_sha,
                }
            ],
        }
        _write(evidence_dir / f"{evidence_id}.json", envelope)
    gate_path = (tmp_path / "release-gate-27.json").resolve()
    gate = {
        "schema_version": 1,
        "decision": "NO_GO",
        "fail_closed": True,
        "required_count": 28,
        "expected_context": context,
        "passed": required[:-1],
        "missing": [EVIDENCE_ID],
        "failed": [],
        "invalid": {},
    }
    _write(gate_path, gate, 0o400)
    request_path = (tmp_path / "approval-request.json").resolve()
    result = build_approval_request(
        evidence_dir=evidence_dir,
        release_gate_report=gate_path,
        gate_config=config_path,
        expected_context=context,
        output=request_path,
        now=NOW,
    )
    return {
        "required": required,
        "config": config,
        "config_path": config_path,
        "context": context,
        "evidence_dir": evidence_dir,
        "gate_path": gate_path,
        "request_path": request_path,
        "request_result": result,
    }


def _approve(data: dict[str, object], tmp_path: Path) -> Path:
    request = json.loads(Path(data["request_path"]).read_text(encoding="utf-8"))
    receipt_path = (tmp_path / "approval-receipt.json").resolve()
    capture_local_approval(
        request_path=Path(data["request_path"]),
        output=receipt_path,
        input_reader=lambda _prompt: request["approval_phrase"],
        isatty=lambda: True,
        uid=501,
        user="ai",
        tty_name="/dev/ttys-test",
        now=NOW + timedelta(minutes=1),
    )
    return receipt_path


def _bound_artifacts(output: Path, envelope: dict) -> list[BoundArtifact]:
    result = []
    for row in envelope["artifacts"]:
        path = output / row["path"]
        data = path.read_bytes()
        result.append(
            BoundArtifact(
                role=row["role"],
                media_type=row["media_type"],
                path=row["path"],
                sha256=hashlib.sha256(data).hexdigest(),
                data=data,
            )
        )
    return result


def test_27_of_28_merkle_request_interactive_receipt_and_g28_normalizer(
    tmp_path: Path,
) -> None:
    data = _fixture(tmp_path)
    receipt = _approve(data, tmp_path)
    output = (tmp_path / "normalized").resolve()

    status = compile_human_approval_evidence(
        output=output,
        evidence_dir=Path(data["evidence_dir"]),
        request_path=Path(data["request_path"]),
        receipt_path=receipt,
        release_gate_report=Path(data["gate_path"]),
        gate_config=Path(data["config_path"]),
        expected_context=data["context"],
    )

    assert status == "passed"
    request = json.loads(Path(data["request_path"]).read_text(encoding="utf-8"))
    assert request["evidence_leaf_count"] == 27
    assert len(request["machine_evidence"]) == 27
    assert Path(data["request_path"]).stat().st_mode & 0o777 == 0o400
    assert receipt.stat().st_mode & 0o777 == 0o400
    envelope = json.loads((output / f"{EVIDENCE_ID}.json").read_text(encoding="utf-8"))
    errors = validate_evidence_semantics(
        envelope,
        EVIDENCE_ID,
        config=data["config"],
        bound_artifacts=_bound_artifacts(output, envelope),
        expected_context=data["context"],
    )
    assert errors == []


@pytest.mark.parametrize(
    ("isatty", "uid", "user"),
    [(False, 501, "ai"), (True, 502, "other")],
)
def test_approval_refuses_noninteractive_or_nonallowlisted_actor(
    tmp_path: Path, isatty: bool, uid: int, user: str
) -> None:
    data = _fixture(tmp_path)
    request = json.loads(Path(data["request_path"]).read_text(encoding="utf-8"))
    with pytest.raises(HumanApprovalBlocked, match="allowlisted interactive"):
        capture_local_approval(
            request_path=Path(data["request_path"]),
            output=(tmp_path / "receipt.json").resolve(),
            input_reader=lambda _prompt: request["approval_phrase"],
            isatty=lambda: isatty,
            uid=uid,
            user=user,
            tty_name="/dev/ttys-test",
            now=NOW + timedelta(minutes=1),
        )


def test_approval_compiler_rejects_post_request_machine_evidence_drift(tmp_path: Path) -> None:
    data = _fixture(tmp_path)
    receipt = _approve(data, tmp_path)
    first_id = data["required"][0]
    artifact = Path(data["evidence_dir"]) / "reports" / f"{first_id}.json"
    artifact.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(HumanApprovalBlocked, match="artifact hash"):
        compile_human_approval_evidence(
            output=(tmp_path / "normalized").resolve(),
            evidence_dir=Path(data["evidence_dir"]),
            request_path=Path(data["request_path"]),
            receipt_path=receipt,
            release_gate_report=Path(data["gate_path"]),
            gate_config=Path(data["config_path"]),
            expected_context=data["context"],
        )


def test_approval_compiler_rejects_receipt_context_forgery(tmp_path: Path) -> None:
    data = _fixture(tmp_path)
    receipt = _approve(data, tmp_path)
    value = json.loads(receipt.read_text(encoding="utf-8"))
    value["release_sha"] = "f" * 64
    receipt.chmod(0o600)
    _write(receipt, value, 0o400)

    with pytest.raises(HumanApprovalBlocked, match="context drifted"):
        compile_human_approval_evidence(
            output=(tmp_path / "normalized").resolve(),
            evidence_dir=Path(data["evidence_dir"]),
            request_path=Path(data["request_path"]),
            receipt_path=receipt,
            release_gate_report=Path(data["gate_path"]),
            gate_config=Path(data["config_path"]),
            expected_context=data["context"],
        )


def test_conditional_daytime_preauthorization_redeems_once_with_fresh_merkle_root(
    tmp_path: Path,
) -> None:
    data = _fixture(tmp_path)
    request_path = (tmp_path / "conditional-request.json").resolve()
    pre = build_conditional_approval_request(
        expected_context=data["context"],
        cutover_window=data["config"]["conditional_daytime_window"],
        output=request_path,
        now=NOW,
    )
    receipt_path = (tmp_path / "conditional-receipt.json").resolve()
    capture_conditional_local_approval(
        request_path=request_path,
        output=receipt_path,
        input_reader=lambda _prompt: pre["approval_phrase"],
        isatty=lambda: True,
        uid=501,
        user="ai",
        tty_name="/dev/ttys-test",
        now=NOW + timedelta(minutes=1),
    )
    redeemed = redeem_conditional_approval(
        evidence_dir=Path(data["evidence_dir"]),
        release_gate_report=Path(data["gate_path"]),
        gate_config=Path(data["config_path"]),
        request_path=request_path,
        receipt_path=receipt_path,
        expected_context=data["context"],
        now=NOW + timedelta(hours=1, minutes=5),
    )
    marker = Path(redeemed["consumption"])
    consumption = json.loads(marker.read_text(encoding="utf-8"))
    machine_ids = data["required"][:-1]
    _rows, sources = _machine_sources(Path(data["evidence_dir"]), machine_ids)
    metrics = derive_conditional_human_approval_metrics(
        request=json.loads(request_path.read_text(encoding="utf-8")),
        request_sha256=hashlib.sha256(request_path.read_bytes()).hexdigest(),
        receipt=json.loads(receipt_path.read_text(encoding="utf-8")),
        receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        consumption=consumption,
        consumption_sha256=hashlib.sha256(marker.read_bytes()).hexdigest(),
        gate_report=json.loads(Path(data["gate_path"]).read_text(encoding="utf-8")),
        gate_report_sha256=hashlib.sha256(Path(data["gate_path"]).read_bytes()).hexdigest(),
        gate_config=data["config"],
        gate_config_sha256=hashlib.sha256(Path(data["config_path"]).read_bytes()).hexdigest(),
        machine_sources={source.role: source for source in sources},
        expected_context=data["context"],
    )
    assert metrics["authorization_mode"] == "conditional_daytime_window"
    assert metrics["conditional_consumption_sha256"] == hashlib.sha256(marker.read_bytes()).hexdigest()
    with pytest.raises(HumanApprovalBlocked, match="already been consumed"):
        redeem_conditional_approval(
            evidence_dir=Path(data["evidence_dir"]),
            release_gate_report=Path(data["gate_path"]),
            gate_config=Path(data["config_path"]),
            request_path=request_path,
            receipt_path=receipt_path,
            expected_context=data["context"],
            now=NOW + timedelta(hours=1, minutes=6),
        )


def test_conditional_approval_refuses_redemption_outside_approved_window(tmp_path: Path) -> None:
    data = _fixture(tmp_path)
    request_path = (tmp_path / "conditional-request.json").resolve()
    pre = build_conditional_approval_request(
        expected_context=data["context"],
        cutover_window=data["config"]["conditional_daytime_window"],
        output=request_path,
        now=NOW,
    )
    receipt_path = (tmp_path / "conditional-receipt.json").resolve()
    capture_conditional_local_approval(
        request_path=request_path,
        output=receipt_path,
        input_reader=lambda _prompt: pre["approval_phrase"],
        isatty=lambda: True,
        uid=501,
        user="ai",
        tty_name="/dev/ttys-test",
        now=NOW + timedelta(minutes=1),
    )
    with pytest.raises(HumanApprovalBlocked, match="outside its approved window"):
        redeem_conditional_approval(
            evidence_dir=Path(data["evidence_dir"]),
            release_gate_report=Path(data["gate_path"]),
            gate_config=Path(data["config_path"]),
            request_path=request_path,
            receipt_path=receipt_path,
            expected_context=data["context"],
            now=NOW + timedelta(minutes=5),
        )


def test_conditional_approval_refuses_noninteractive_actor(tmp_path: Path) -> None:
    data = _fixture(tmp_path)
    request_path = (tmp_path / "conditional-request.json").resolve()
    pre = build_conditional_approval_request(
        expected_context=data["context"],
        cutover_window=data["config"]["conditional_daytime_window"],
        output=request_path,
        now=NOW,
    )
    with pytest.raises(HumanApprovalBlocked, match="allowlisted interactive"):
        capture_conditional_local_approval(
            request_path=request_path,
            output=(tmp_path / "conditional-receipt.json").resolve(),
            input_reader=lambda _prompt: pre["approval_phrase"],
            isatty=lambda: False,
            uid=501,
            user="ai",
            tty_name="/dev/ttys-test",
            now=NOW + timedelta(minutes=1),
        )


def test_conditional_approval_end_is_exclusive_and_evidence_drift_does_not_consume(
    tmp_path: Path,
) -> None:
    data = _fixture(tmp_path)
    request_path = (tmp_path / "conditional-request.json").resolve()
    pre = build_conditional_approval_request(
        expected_context=data["context"],
        cutover_window=data["config"]["conditional_daytime_window"],
        output=request_path,
        now=NOW,
    )
    receipt_path = (tmp_path / "conditional-receipt.json").resolve()
    capture_conditional_local_approval(
        request_path=request_path,
        output=receipt_path,
        input_reader=lambda _prompt: pre["approval_phrase"],
        isatty=lambda: True,
        uid=501,
        user="ai",
        tty_name="/dev/ttys-test",
        now=NOW + timedelta(minutes=1),
    )
    with pytest.raises(HumanApprovalBlocked, match="outside its approved window"):
        redeem_conditional_approval(
            evidence_dir=Path(data["evidence_dir"]),
            release_gate_report=Path(data["gate_path"]),
            gate_config=Path(data["config_path"]),
            request_path=request_path,
            receipt_path=receipt_path,
            expected_context=data["context"],
            now=NOW + timedelta(hours=2),
        )
    assert not Path(pre["consumption_marker"]).exists()
    first_id = data["required"][0]
    artifact = Path(data["evidence_dir"]) / "reports" / f"{first_id}.json"
    artifact.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(HumanApprovalBlocked, match="artifact hash"):
        redeem_conditional_approval(
            evidence_dir=Path(data["evidence_dir"]),
            release_gate_report=Path(data["gate_path"]),
            gate_config=Path(data["config_path"]),
            request_path=request_path,
            receipt_path=receipt_path,
            expected_context=data["context"],
            now=NOW + timedelta(hours=1, minutes=5),
        )
    assert not Path(pre["consumption_marker"]).exists()


def test_conditional_compile_emits_semantic_g28_envelope(tmp_path: Path) -> None:
    data = _fixture(tmp_path)
    request_path = (tmp_path / "conditional-request.json").resolve()
    pre = build_conditional_approval_request(
        expected_context=data["context"],
        cutover_window=data["config"]["conditional_daytime_window"],
        output=request_path,
        now=NOW,
    )
    receipt_path = (tmp_path / "conditional-receipt.json").resolve()
    capture_conditional_local_approval(
        request_path=request_path,
        output=receipt_path,
        input_reader=lambda _prompt: pre["approval_phrase"],
        isatty=lambda: True,
        uid=501,
        user="ai",
        tty_name="/dev/ttys-test",
        now=NOW + timedelta(minutes=1),
    )
    output = (tmp_path / "conditional-normalized").resolve()
    assert compile_conditional_human_approval_evidence(
        output=output,
        evidence_dir=Path(data["evidence_dir"]),
        request_path=request_path,
        receipt_path=receipt_path,
        release_gate_report=Path(data["gate_path"]),
        gate_config=Path(data["config_path"]),
        expected_context=data["context"],
        now=NOW + timedelta(hours=1, minutes=5),
    ) == "passed"
    envelope = json.loads((output / f"{EVIDENCE_ID}.json").read_text(encoding="utf-8"))
    assert validate_evidence_semantics(
        envelope,
        EVIDENCE_ID,
        config=data["config"],
        bound_artifacts=_bound_artifacts(output, envelope),
        expected_context=data["context"],
    ) == []


def test_conditional_compile_resumes_exact_marker_created_before_g28_emit(
    tmp_path: Path,
) -> None:
    data = _fixture(tmp_path)
    request_path = (tmp_path / "conditional-request.json").resolve()
    pre = build_conditional_approval_request(
        expected_context=data["context"],
        cutover_window=data["config"]["conditional_daytime_window"],
        output=request_path,
        now=NOW,
    )
    receipt_path = (tmp_path / "conditional-receipt.json").resolve()
    capture_conditional_local_approval(
        request_path=request_path,
        output=receipt_path,
        input_reader=lambda _prompt: pre["approval_phrase"],
        isatty=lambda: True,
        uid=501,
        user="ai",
        tty_name="/dev/ttys-test",
        now=NOW + timedelta(minutes=1),
    )
    redeem_at = NOW + timedelta(hours=1, minutes=5)
    redeem_conditional_approval(
        evidence_dir=Path(data["evidence_dir"]),
        release_gate_report=Path(data["gate_path"]),
        gate_config=Path(data["config_path"]),
        request_path=request_path,
        receipt_path=receipt_path,
        expected_context=data["context"],
        now=redeem_at,
    )

    output = (tmp_path / "resumed-conditional-normalized").resolve()
    assert compile_conditional_human_approval_evidence(
        output=output,
        evidence_dir=Path(data["evidence_dir"]),
        request_path=request_path,
        receipt_path=receipt_path,
        release_gate_report=Path(data["gate_path"]),
        gate_config=Path(data["config_path"]),
        expected_context=data["context"],
        now=redeem_at + timedelta(minutes=1),
    ) == "passed"
