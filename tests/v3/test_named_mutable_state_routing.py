from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

import pytest

from api.runtime_paths import (
    get_cortex_sync_state_path,
    get_judgments_json_path,
    get_laf_processed_emails_path,
    get_payment_proof_registry_path,
    get_payment_proof_upload_queue_path,
    get_payment_proof_upload_store_dir,
    get_payment_registry_path,
    get_pdf_namer_case_index_path,
)


def test_payment_proof_queue_uses_existing_file_review_state_binding(tmp_path, monkeypatch):
    state = tmp_path / "sealed-file-review-state"
    monkeypatch.setenv("MAGI_FILE_REVIEW_STATE_DIR", str(state))
    monkeypatch.delenv("MAGI_PAYMENT_PROOF_UPLOAD_QUEUE_PATH", raising=False)
    monkeypatch.delenv("MAGI_PAYMENT_PROOF_UPLOAD_STORE_DIR", raising=False)

    assert get_payment_proof_upload_queue_path() == state / "payment-proof-upload-queue.json"
    assert get_payment_proof_upload_store_dir() == state / "payment-proof-pending-files"
from magi_v3.external_inputs import ExternalInputError


NAMED_BINDINGS = {
    "MAGI_LAF_PROCESSED_EMAILS_PATH": "agent/laf-orchestrator/processed_laf_emails.json",
    "MAGI_PAYMENT_REGISTRY_PATH": "file-review/downloads/payment_registry.json",
    "MAGI_PAYMENT_PROOF_REGISTRY_PATH": "file-review/downloads/payment_proof_registry.json",
    "MAGI_JUDGMENTS_JSON_PATH": "agent/judgment-collector/judgments.json",
    "MAGI_PDF_NAMER_CASE_INDEX": "pdf-namer/_case_index.json",
    "MAGI_CORTEX_SYNC_STATE_PATH": "runtime/cortex_sync_state.json",
}

RESOLVERS = {
    "MAGI_LAF_PROCESSED_EMAILS_PATH": get_laf_processed_emails_path,
    "MAGI_PAYMENT_REGISTRY_PATH": get_payment_registry_path,
    "MAGI_PAYMENT_PROOF_REGISTRY_PATH": get_payment_proof_registry_path,
    "MAGI_JUDGMENTS_JSON_PATH": get_judgments_json_path,
    "MAGI_PDF_NAMER_CASE_INDEX": get_pdf_namer_case_index_path,
    "MAGI_CORTEX_SYNC_STATE_PATH": get_cortex_sync_state_path,
}


def _sealed_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    release = tmp_path / "candidate"
    shared = tmp_path / "shared"
    release.mkdir()
    shared.mkdir()
    monkeypatch.setenv("MAGI_ROOT_DIR", str(release))
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-routing-test")
    monkeypatch.setenv("MAGI_V3_SHARED_STATE_DIR", str(shared))
    for env_name, relative in NAMED_BINDINGS.items():
        monkeypatch.setenv(env_name, str(shared / relative))
    return release, shared


def test_named_mutable_files_preserve_v2_fallbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAGI_ROOT_DIR", str(tmp_path))
    for env_name in (
        *NAMED_BINDINGS,
        "MAGI_V3_RELEASE_ID",
        "MAGI_V3_SHARED_STATE_DIR",
        "MAGI_SHARED_STATE_DIR",
        "MAGI_ORCH_DIR",
        "MAGI_CODE_DIR",
    ):
        monkeypatch.delenv(env_name, raising=False)

    assert get_laf_processed_emails_path() == (
        tmp_path / "casper_ecosystem/law_firm_orchestrators/processed_laf_emails.json"
    ).resolve()
    assert get_payment_registry_path() == tmp_path / "閱卷下載/payment_registry.json"
    assert get_payment_proof_registry_path() == tmp_path / "閱卷下載/payment_proof_registry.json"
    assert get_judgments_json_path() == tmp_path / "skills/judgment-collector/judgments.json"
    assert get_pdf_namer_case_index_path() == tmp_path / "skills/pdf-namer/_case_index.json"
    assert get_cortex_sync_state_path() == tmp_path / "cortex_sync_state.json"


def test_sealed_named_mutable_files_bind_exact_shared_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release, shared = _sealed_environment(monkeypatch, tmp_path)
    before = tuple(release.rglob("*"))

    for env_name, resolver in RESOLVERS.items():
        assert resolver() == shared / NAMED_BINDINGS[env_name]

    assert tuple(release.rglob("*")) == before


def test_sealed_producers_write_only_shared_targets(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    shared = tmp_path / "shared"
    legacy_targets = (
        root / "json/processed_laf_emails.json",
        root / "閱卷下載/payment_registry.json",
        root / "閱卷下載/payment_proof_registry.json",
        root / "skills/judgment-collector/judgments.json",
        root / "skills/pdf-namer/_case_index.json",
        root / "cortex_sync_state.json",
    )

    def snapshot() -> dict[str, str | None]:
        return {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            for path in legacy_targets
        }

    before = snapshot()
    environment = os.environ.copy()
    environment.update(
        {
            "MAGI_ROOT": str(root),
            "MAGI_ROOT_DIR": str(root),
            "MAGI_V3_RELEASE_ID": "v3-named-producer-test",
            "MAGI_V3_SHARED_STATE_DIR": str(shared),
            "MAGI_AGENT_DIR": str(shared / "agent"),
            "MAGI_RUNTIME_DIR": str(shared / "runtime"),
            "MAGI_PDF_NAMER_STATE_DIR": str(shared / "pdf-namer"),
            "MAGI_V3_SCHEDULE_FIXTURE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(root),
        }
    )
    for env_name, relative in NAMED_BINDINGS.items():
        environment[env_name] = str(shared / relative)

    code = r'''
import json
import os
import sys
import types
from pathlib import Path

fake_dedup = types.ModuleType("skills.ops.dedup_db")
fake_dedup.mark_done = lambda *args, **kwargs: True
sys.modules["skills.ops.dedup_db"] = fake_dedup

from skills.legal.laf import LAFGmailMonitor
monitor = LAFGmailMonitor("fixture-credentials", "fixture-token")
monitor.mark_laf_processed("fixture-message-id")

from skills.memory import cortex_sync
syncer = object.__new__(cortex_sync.CortexSync)
syncer.state = {"legal_news_last_id": 7}
syncer._save_state()

pdf_skill = Path(os.environ["MAGI_ROOT_DIR"]) / "skills/pdf-namer"
sys.path.insert(0, str(pdf_skill))
import state_paths
case_index = state_paths.prepare_write(state_paths.state_path("_case_index.json"))
case_index.write_text("[]\n", encoding="utf-8")

from api.runtime_paths import (
    get_judgments_json_path,
    get_payment_proof_registry_path,
    get_payment_registry_path,
)
for path in (get_judgments_json_path(), get_payment_registry_path(), get_payment_proof_registry_path()):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot() == before
    assert json.loads((shared / "agent/laf-orchestrator/processed_laf_emails.json").read_text()) == [
        "fixture-message-id"
    ]
    assert json.loads((shared / "runtime/cortex_sync_state.json").read_text()) == {
        "legal_news_last_id": 7
    }
    for relative in NAMED_BINDINGS.values():
        assert (shared / relative).is_file()


@pytest.mark.parametrize("missing_env", tuple(NAMED_BINDINGS))
def test_sealed_named_mutable_files_fail_closed_when_binding_missing(
    missing_env: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sealed_environment(monkeypatch, tmp_path)
    monkeypatch.delenv(missing_env)

    with pytest.raises(ExternalInputError, match=missing_env):
        RESOLVERS[missing_env]()


@pytest.mark.parametrize("env_name", tuple(NAMED_BINDINGS))
def test_sealed_named_mutable_files_reject_release_or_wrong_shared_target(
    env_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, _shared = _sealed_environment(monkeypatch, tmp_path)
    monkeypatch.setenv(env_name, str(release / Path(NAMED_BINDINGS[env_name]).name))

    with pytest.raises(ExternalInputError, match="canonical shared-state binding"):
        RESOLVERS[env_name]()
