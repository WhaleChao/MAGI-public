from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from magi_v3 import controlled_evolution as ce
from scripts.ops import magi_self_repair_guardian as guardian
from skills.engine import tool_registry


def _signal(**overrides):
    signal = {
        "id": "function_health:failed:cron:job_example",
        "source": "function_health_index",
        "category": "routing_quality",
        "severity": "error",
        "status": "needs_human",
        "summary": "當事人王小明的 /private/secret/path 對話路由失敗",
        "recommendation": "inspect raw exception abc-123",
        "evidence": {"path": "/private/secret/path", "message": "王小明"},
    }
    signal.update(overrides)
    return signal


def _proposal(root: Path, store: ce.EvolutionStore) -> dict:
    proposal = ce.build_proposal(_signal(), release_id="v3-test", root=root)
    proposal["structure_scope"] = {
        "source_prefixes": ["api/routing"],
        "acceptance_tests": ["tests/test_candidate.py"],
    }
    return store.upsert(proposal)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    (root / "api" / "routing").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "api" / "routing" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_candidate.py").write_text(
        "from api.routing.sample import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    assert _git(root, "init").returncode == 0
    assert _git(root, "config", "user.email", "magi@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "MAGI Test").returncode == 0
    assert _git(root, "add", ".").returncode == 0
    assert _git(root, "commit", "-m", "fixture").returncode == 0
    target = root / "api" / "routing" / "sample.py"
    target.write_text("VALUE = 2\n", encoding="utf-8")
    patch = _git(root, "diff", "--binary", "--no-ext-diff").stdout
    assert _git(root, "checkout", "--", "api/routing/sample.py").returncode == 0
    return root, patch


class _FakeListener:
    def getsockname(self):
        return ("127.0.0.1", 43123)

    def close(self):
        return None


def _allow_network_probe_fixture(monkeypatch):
    monkeypatch.setattr(ce, "_open_network_probe_listener", lambda: _FakeListener())


def test_proposal_is_deidentified_durable_and_never_auto_deploys(tmp_path: Path):
    store = ce.EvolutionStore(tmp_path / "state" / "evolution.sqlite3")
    proposal = ce.build_proposal(_signal(), release_id="v3-test", root=tmp_path)
    stored = store.upsert(proposal)
    serialized = json.dumps(stored, ensure_ascii=False)
    assert stored["auto_deploy"] is False
    assert stored["requires_human_before_deploy"] is True
    assert stored["candidate_only"] is True
    assert "王小明" not in serialized
    assert "/private/secret/path" not in serialized
    assert "abc-123" not in serialized
    restarted = ce.EvolutionStore(store.path)
    assert restarted.get(stored["proposal_id"])["proposal_id"] == stored["proposal_id"]


def test_signal_ingest_deduplicates_and_skips_known_safe_repair(tmp_path: Path):
    store = ce.EvolutionStore(tmp_path / "evolution.sqlite3")
    signal = _signal()
    proposals = ce.ingest_signals([signal, signal], root=tmp_path, release_id="v3-test", store=store)
    assert len(proposals) == 1
    assert len(store.list()) == 1
    assert ce.ingest_signals(
        [_signal(auto_repair="delete_owned_temp")], root=tmp_path, release_id="v3-test", store=store
    ) == []


@pytest.mark.parametrize(
    ("summary", "component", "risk"),
    [
        ("Telegram 對話意圖與工具路由錯誤", "conversation_routing", "high"),
        ("PII 個資在登入 auth 邊界外洩", "security_privacy", "critical"),
        ("法扶附件派案流程失敗", "legal_aid", "high"),
        ("筆錄同步 metadata 失敗", "transcript", "high"),
    ],
)
def test_component_and_risk_mapping(tmp_path: Path, summary: str, component: str, risk: str):
    proposal = ce.build_proposal(_signal(summary=summary), release_id="v3-test", root=tmp_path)
    assert proposal["component"] == component
    assert proposal["risk"] == risk


def test_structure_inventory_contains_relationships_without_absolute_paths(tmp_path: Path):
    (tmp_path / "api" / "routing").mkdir(parents=True)
    (tmp_path / "api" / "routing" / "one.py").write_text("pass\n", encoding="utf-8")
    inventory = ce.build_structure_inventory(tmp_path)
    assert inventory["component_count"] == len(ce.COMPONENT_RULES) + 1
    routing = next(item for item in inventory["components"] if item["component"] == "conversation_routing")
    assert routing["file_count"] == 1
    assert str(tmp_path) not in json.dumps(inventory)


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ("diff --git a/.env b/.env\n+++ b/.env\n+API_KEY='secret'\n", "possible_secret_literal"),
        ("diff --git a/../../x b/../../x\n+++ b/../../x\n+x\n", "unsafe_path:../../x"),
        ("diff --git a/scripts/x.py b/scripts/x.py\n+++ b/scripts/x.py\n+x\n", "outside_component_scope:scripts/x.py"),
        ("diff --git a/scripts/x.py b/api/routing/x.py\n--- a/scripts/x.py\n+++ b/api/routing/x.py\n+x\n", "outside_component_scope:scripts/x.py"),
        ("diff --git a/api/routing/x.py b/api/routing/x.py\n+++ b/api/routing/x.py\n+subprocess.run('sudo x', shell=True)\n", "dangerous_process_invocation"),
    ],
)
def test_patch_validator_fails_closed(tmp_path: Path, patch: str, reason: str):
    store = ce.EvolutionStore(tmp_path / "evolution.sqlite3")
    proposal = _proposal(tmp_path, store)
    result = ce.validate_patch(patch, proposal)
    assert result["ok"] is False
    assert reason in result["errors"]


def test_seatbelt_allows_trusted_python_runtime_but_denies_live_state(tmp_path: Path):
    candidate = tmp_path / "candidate"
    sandbox_dir = candidate / ".magi-evolution-sandbox"
    candidate.mkdir()
    command, isolation = ce._candidate_test_command(
        candidate=candidate,
        python=sys.executable,
        tests=("tests/test_candidate.py",),
        sandbox_dir=sandbox_dir,
        network_probe_port=12345,
    )
    assert isolation["ok"] is True
    profile = command[2]
    support = Path.home() / "Library" / "Application Support" / "MAGI"
    assert f'(deny file-read* (subpath {json.dumps(str(support))}))' not in profile
    assert f'(deny file-read* (subpath {json.dumps(str(support / "runtime"))}))' in profile
    assert f'(deny file-read* (subpath {json.dumps(str(support / "releases"))}))' in profile
    assert f'(deny file-read* (subpath {json.dumps(str(support / "runtimes"))}))' not in profile


def test_stage_and_verify_candidate_end_to_end_without_deploy(tmp_path: Path, monkeypatch):
    source, patch = _source_repo(tmp_path)
    store = ce.EvolutionStore(tmp_path / "runtime" / "evolution.sqlite3")
    proposal = _proposal(source, store)
    monkeypatch.setattr(
        ce,
        "resource_admission",
        lambda **_kwargs: {"safe": True, "reasons": [], "disk_free_gb": 99},
    )
    _allow_network_probe_fixture(monkeypatch)
    monkeypatch.setattr(
        ce,
        "_candidate_test_command",
        lambda *, candidate, python, tests, sandbox_dir, network_probe_port: (
            [
                python,
                "-c",
                "from pathlib import Path; assert Path('api/routing/sample.py').read_text() == 'VALUE = 2\\n'",
            ],
            {"ok": True, "kind": "test_isolation", "network_denied": True},
        ),
    )
    staged = ce.stage_candidate(
        proposal=proposal,
        store=store,
        source_root=source,
        workspace_root=tmp_path / "candidates",
        patch_text=patch,
    )
    assert staged["ok"] is True
    candidate = Path(staged["candidate"])
    marker = json.loads((candidate / ".magi-controlled-evolution.json").read_text())
    assert marker["live_mutation_allowed"] is False
    assert marker["deploy_operation_available"] is False
    verified = ce.verify_candidate(
        proposal=store.get(proposal["proposal_id"]),
        store=store,
        candidate=candidate,
        timeout=120,
    )
    assert verified["ok"] is True, verified
    assert verified["status"] == "ready_for_human_review"
    assert verified["certification"]["auto_deploy"] is False
    assert not hasattr(ce, "deploy_candidate")


def test_candidate_test_failure_is_not_promoted(tmp_path: Path, monkeypatch):
    source, patch = _source_repo(tmp_path)
    (source / "tests" / "test_candidate.py").write_text("def test_value():\n    assert False\n")
    assert _git(source, "add", "tests/test_candidate.py").returncode == 0
    assert _git(source, "commit", "-m", "failing acceptance").returncode == 0
    store = ce.EvolutionStore(tmp_path / "runtime" / "evolution.sqlite3")
    proposal = _proposal(source, store)
    monkeypatch.setattr(ce, "resource_admission", lambda **_kwargs: {"safe": True, "reasons": []})
    monkeypatch.setattr(
        ce,
        "_candidate_test_command",
        lambda *, candidate, python, tests, sandbox_dir, network_probe_port: (
            [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
            {"ok": True, "kind": "test_isolation", "network_denied": True},
        ),
    )
    _allow_network_probe_fixture(monkeypatch)
    staged = ce.stage_candidate(
        proposal=proposal,
        store=store,
        source_root=source,
        workspace_root=tmp_path / "candidates",
        patch_text=patch,
    )
    verified = ce.verify_candidate(
        proposal=store.get(proposal["proposal_id"]),
        store=store,
        candidate=Path(staged["candidate"]),
        timeout=120,
    )
    assert verified["ok"] is False
    assert verified["status"] == "verification_failed"


def test_candidate_mutated_by_tests_is_not_promoted(tmp_path: Path, monkeypatch):
    source, patch = _source_repo(tmp_path)
    store = ce.EvolutionStore(tmp_path / "runtime" / "evolution.sqlite3")
    proposal = _proposal(source, store)
    monkeypatch.setattr(ce, "resource_admission", lambda **_kwargs: {"safe": True, "reasons": []})
    monkeypatch.setattr(
        ce,
        "_candidate_test_command",
        lambda *, candidate, python, tests, sandbox_dir, network_probe_port: (
            [python, "-c", "from pathlib import Path; Path('api/routing/sample.py').write_text('VALUE = 99\\n')"],
            {"ok": True, "kind": "test_isolation", "network_denied": True},
        ),
    )
    _allow_network_probe_fixture(monkeypatch)
    staged = ce.stage_candidate(
        proposal=proposal,
        store=store,
        source_root=source,
        workspace_root=tmp_path / "candidates",
        patch_text=patch,
    )
    verified = ce.verify_candidate(
        proposal=store.get(proposal["proposal_id"]),
        store=store,
        candidate=Path(staged["candidate"]),
        timeout=120,
    )
    assert verified["ok"] is False
    assert verified["certification"]["candidate_integrity_after_tests"] is False


def test_resource_pressure_defers_before_worktree_creation(tmp_path: Path, monkeypatch):
    source, patch = _source_repo(tmp_path)
    store = ce.EvolutionStore(tmp_path / "runtime" / "evolution.sqlite3")
    proposal = _proposal(source, store)
    monkeypatch.setattr(
        ce, "resource_admission", lambda **_kwargs: {"safe": False, "reasons": ["cpu_pressure_high"]}
    )
    result = ce.stage_candidate(
        proposal=proposal,
        store=store,
        source_root=source,
        workspace_root=tmp_path / "candidates",
        patch_text=patch,
    )
    assert result["status"] == "deferred"
    assert not (tmp_path / "candidates" / proposal["proposal_id"]).exists()


def test_guardian_persists_candidate_only_proposal(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    result = guardian._persist_evolution_proposals(
        root=root,
        runtime_dir=tmp_path / "runtime",
        issues=[_signal()],
    )
    assert result["ok"] is True
    assert result["proposal_count"] == 1
    assert result["auto_deploy"] is False
    store = ce.EvolutionStore(tmp_path / "runtime" / "controlled-evolution" / "evolution.sqlite3")
    assert store.list()[0]["status"] == "planned"


def test_conversational_evolution_status_is_aggregate_only(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    store = ce.EvolutionStore(runtime / "controlled-evolution" / "evolution.sqlite3")
    store.upsert(ce.build_proposal(_signal(), release_id="v3-test", root=tmp_path))
    import api.runtime_paths

    monkeypatch.setattr(api.runtime_paths, "get_runtime_dir", lambda: runtime)
    output = tool_registry._evolution_status()
    payload = json.loads(output)
    assert payload["total"] == 1
    assert payload["candidate_only"] is True
    assert payload["auto_deploy"] is False
    assert "王小明" not in output
    assert "/private/secret/path" not in output
    assert "evolution_status" in tool_registry.get_compact_tools("MAGI 哪裡需要改進")


def test_conversational_evolution_status_does_not_create_missing_ledger(tmp_path: Path, monkeypatch):
    import api.runtime_paths

    monkeypatch.setattr(api.runtime_paths, "get_runtime_dir", lambda: tmp_path)
    output = tool_registry._evolution_status()
    assert "尚無正式缺口" in output
    assert not (tmp_path / "controlled-evolution").exists()
