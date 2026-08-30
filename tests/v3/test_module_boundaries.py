from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_module_boundaries_are_complete_and_source_backed() -> None:
    manifest = json.loads(
        (ROOT / "config/v3_module_boundaries.json").read_text(encoding="utf-8")
    )
    assert manifest["strategy"] == "strangler_adapters_no_big_bang_rewrite"
    boundaries = manifest["boundaries"]
    assert {row["id"] for row in boundaries} == {
        "agent_kernel",
        "legal_domains",
        "external_connectors",
        "release_ops",
    }
    owned_files: list[str] = []
    for boundary in boundaries:
        assert boundary["owner"]
        assert boundary["source_roots"]
        for relative in boundary["source_roots"]:
            assert (ROOT / relative).exists(), relative
            owned_files.append(relative)
    assert len(owned_files) == len(set(owned_files))


def test_legacy_facades_remain_explicit_and_new_core_is_outside_them() -> None:
    manifest = json.loads(
        (ROOT / "config/v3_module_boundaries.json").read_text(encoding="utf-8")
    )
    facades = {row["path"] for row in manifest["legacy_facades"]}
    assert facades == {
        "api/blueprints/osc_cases.py",
        "casper_ecosystem/law_firm_orchestrators/file_review_automation.py",
        "casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py",
    }
    assert all((ROOT / relative).is_file() for relative in facades)
    rules = manifest["extraction_rules"]
    assert all(rules.values())
    new_core = {
        relative
        for boundary in manifest["boundaries"]
        for relative in boundary["source_roots"]
        if relative.endswith(".py")
    }
    assert facades.isdisjoint(new_core)
